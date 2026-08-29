"""Shared control plane for the resident live-subtitle engine."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import PRESET_MODELS, find_subtitle_pipeline_dir, settings

ENGINE_VERSION = 4

# SRT 生成进行中（含批量）：viewer 暂停提交实时字幕任务（UI 层互斥，
# 引擎串行队列本身没问题，防的是 seek/换片误 cancel 掉 SRT 任务）
srt_busy = False


def paths() -> tuple[Path, Path, Path, Path] | None:
    pipe = find_subtitle_pipeline_dir()
    if pipe is None:
        return None
    log = pipe / "live-caption.log"
    return log, Path(str(log) + ".pid"), Path(str(log) + ".control"), Path(str(log) + ".state")


def state() -> dict:
    found = paths()
    if found is None:
        return {}
    _log, _pid, _control, state_path = found
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def hardware_snapshot() -> dict:
    """Best-effort GPU snapshot for diagnostics and model recommendations."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
    except Exception:
        return {"gpu": None, "vram_mb": 0, "used_mb": 0}
    if not out:
        return {"gpu": None, "vram_mb": 0, "used_mb": 0}
    first = out.splitlines()[0].split(",")
    if len(first) != 3:
        return {"gpu": None, "vram_mb": 0, "used_mb": 0}
    try:
        return {
            "gpu": first[0].strip(),
            "vram_mb": int(float(first[1])),
            "used_mb": int(float(first[2])),
        }
    except ValueError:
        return {"gpu": first[0].strip(), "vram_mb": 0, "used_mb": 0}


def model_installed(model: str) -> bool:
    """qwen/sensevoice 需要引擎目录里的模型文件；whisper 走 HF 缓存自行下载。"""
    if model not in ("qwen", "sensevoice"):
        return True
    pipe = find_subtitle_pipeline_dir()
    if pipe is None:
        return False
    folder = "Qwen3-ASR-1.7B" if model == "qwen" else "iic--SenseVoiceSmall"
    base = pipe / "models" / "models" / folder
    if not base.is_dir():
        return False
    return any(base.rglob("*.pt")) or any(base.rglob("*.safetensors"))


def whisper_fallback() -> str:
    """按显存挑一个 whisper 档位（qwen/sensevoice 未安装时的兜底）。"""
    vram = hardware_snapshot().get("vram_mb", 0)
    if vram >= 8000:
        return "large-v3"
    if vram >= 4000:
        return "medium"
    if vram >= 2000:
        return "small"
    return "tiny"


def recommended_model() -> str:
    """按显存推荐引擎。Qwen3-ASR 实测最准但需 ~6GB；不足则退 SenseVoice/whisper。"""
    vram = hardware_snapshot().get("vram_mb", 0)
    if vram >= 7000:
        return "qwen"
    if vram >= 4000:
        return "medium"
    if vram >= 2000:
        return "sensevoice"
    return "tiny"


def effective_model() -> str:
    """实际启动引擎用的模型。未安装的新引擎自动回退，避免升级后直接崩。"""
    if bool(settings["hardware_aware_model"]):
        model = recommended_model()
    else:
        preset = str(settings["live_model_preset"])
        model = PRESET_MODELS.get(preset, str(settings["live_asr_model"]))
    if model_installed(model):
        return model
    if model == "qwen" and model_installed("sensevoice"):
        return "sensevoice"
    return whisper_fallback()


def control_job() -> dict:
    found = paths()
    if found is None:
        return {}
    _log, _pid, control, _state = found
    try:
        value = json.loads(control.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def alive() -> bool:
    found = paths()
    if found is None:
        return False
    _log, pid_path, _control, _state = found
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    try:
        import psutil

        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    except Exception:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=5, errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return str(pid) in out and "python" in out.lower()
    except Exception:
        return False


def model_dir_arg() -> str:
    """本地模型目录只对 whisper 有意义；qwen/sensevoice 用引擎目录固定路径。"""
    if effective_model() in ("qwen", "sensevoice"):
        return ""
    return str(settings["live_asr_dir"] or "").strip()


def matches() -> bool:
    current = state()
    return (
        current.get("engine") == ENGINE_VERSION
        and current.get("source") == "audio"
        and current.get("model") == effective_model()
        and current.get("model_dir") == model_dir_arg()
        and current.get("translate") == str(settings["live_ollama_model"])
    )


def kill() -> None:
    found = paths()
    if found is None:
        return
    _log, pid_path, control, _state = found
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        try:
            import psutil

            process = psutil.Process(pid)
            process.terminate()
            try:
                process.wait(timeout=3)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            return
        except ImportError:
            pass
        except Exception:
            pass
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass
    pid_path.unlink(missing_ok=True)
    control.unlink(missing_ok=True)


def start_preload() -> bool:
    """Start one background engine; the UI never waits for the model."""
    pipe = find_subtitle_pipeline_dir()
    if pipe is None:
        return False
    if alive() and matches():
        return True
    if alive():
        # A just-launched engine may not have written its state file yet.
        if not state():
            return True
        kill()

    found = paths()
    if found is None:
        return False
    log, _pid, control, _state = found
    control.unlink(missing_ok=True)
    exe = pipe / ".venv" / "Scripts" / "pythonw.exe"
    script = pipe / "live_transcribe.py"
    if not exe.is_file() or not script.is_file():
        return False

    args = [
        str(script), "--preload", "--log", str(log),
        "--lang", str(settings["live_caption_lang"]),
        "--model", effective_model(),
        "--ollama-model", str(settings["live_ollama_model"]),
    ]
    if model_dir_arg():
        args += ["--model-dir", model_dir_arg()]
    if str(settings["live_ollama_model"]) != "none":
        args.append("--translate")
    err_path = log.parent / "live-caption.err"
    try:
        err_fp = open(err_path, "w", encoding="utf-8", errors="replace")
    except Exception:
        err_fp = subprocess.DEVNULL
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        proc = subprocess.Popen(
            [str(exe), *args], cwd=str(pipe), env=None,
            stdout=subprocess.DEVNULL, stderr=err_fp,
            creationflags=flags, startupinfo=startupinfo,
        )
        time.sleep(0.2)
        if proc.poll() is not None:
            # Another engine may have won the single-instance lock race.
            return alive() and matches()
        return True
    except Exception:
        return False
    finally:
        if err_fp is not subprocess.DEVNULL:
            err_fp.close()


def next_generation(control: Path) -> int:
    try:
        current = json.loads(control.read_text(encoding="utf-8"))
        return int(current.get("generation", 0)) + 1
    except Exception:
        return 1


def submit(job: dict) -> int | None:
    found = paths()
    if found is None:
        return False
    _log, _pid, control, _state = found
    job["generation"] = next_generation(control)
    tmp = control.with_suffix(control.suffix + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp.replace(control)
    return job["generation"]
