"""Shared control plane for the resident live-subtitle engine."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import (
    ASR_MODEL_SPECS,
    PRESET_MODELS,
    TRANSLATE_MODEL_SPECS,
    find_subtitle_pipeline_dir,
    settings,
)

ENGINE_VERSION = 5

# 引擎拉起冷却：30s 内已 spawn 过就不再 kill+respawn（设置刚改过也等下一次
# 调用再重建），防止多个启动入口把"加载中的引擎"反复杀掉造成重启循环
_last_spawn_at = 0.0

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


_em_cache: dict = {"key": None, "value": None}


def effective_model() -> str:
    """实际启动引擎用的模型。未安装的新引擎自动回退，避免升级后直接崩。

    按输入（档位/模型/硬件感知开关/monkeypatch）缓存：model_installed 的
    rglob 与 hardware_snapshot 的 nvidia-smi 都不便宜，而本函数在 matches()
    等热路径被频繁调用；设置项一变 key 即失效，行为与不缓存完全一致。
    """
    key = (bool(settings["hardware_aware_model"]),
           str(settings["live_model_preset"]),
           str(settings["live_asr_model"]),
           str(model_installed))
    if _em_cache["key"] == key:
        return _em_cache["value"]
    if key[0]:
        model = recommended_model()
    else:
        model = PRESET_MODELS.get(key[1], key[2])
    if not model_installed(model):
        if model == "qwen" and model_installed("sensevoice"):
            model = "sensevoice"
        else:
            model = whisper_fallback()
    _em_cache["key"] = key
    _em_cache["value"] = model
    return model


def vram_footprint_gb(include_translate: bool = True) -> float:
    """当前引擎组合的显存占用估计（GB）。

    include_translate=True（设置提示）：识别模型 + 翻译模型合计，与设置界面
    的合计占用行口径一致。
    include_translate=False（退出确认）：只算识别模型——翻译走的是独立的
    Ollama 服务进程，杀掉引擎进程不会释放它。
    """
    model = effective_model()
    vram = ASR_MODEL_SPECS.get(model, {}).get("vram_gb", 0.0)
    if include_translate:
        tr = str(settings["live_ollama_model"])
        vram += TRANSLATE_MODEL_SPECS.get(tr, {}).get("vram_gb", 0.0)
    return vram


def model_label() -> str:
    """当前实际引擎的人类可读名称（Qwen3-ASR-1.7B / SenseVoice-small / …）。"""
    return ASR_MODEL_SPECS.get(effective_model(), {}).get("label",
                                                          effective_model())


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
        and current.get("target") == str(settings["live_translate_target"])
        and current.get("idle") == int(settings["live_caption_idle_unload"])
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
    global _last_spawn_at
    pipe = find_subtitle_pipeline_dir()
    if pipe is None:
        return False
    if alive() and matches():
        return True
    if alive():
        # A just-launched engine may not have written its state file yet.
        if not state():
            return True
        # 冷却期内不动刚拉起的引擎（即使配置不匹配——那多半是设置刚变更，
        # 让下一次调用再重建，也别在它加载模型的 20-60s 里反复杀）
        if time.time() - _last_spawn_at < 30:
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
        "--target-lang", str(settings["live_translate_target"]),
        "--idle-unload", str(int(settings["live_caption_idle_unload"])),
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
        _last_spawn_at = time.time()
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
        return None  # 调用方按 `if not generation`/`is None` 判断，勿返回 False
    _log, _pid, control, _state = found
    job["generation"] = next_generation(control)
    tmp = control.with_suffix(control.suffix + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp.replace(control)
    return job["generation"]
