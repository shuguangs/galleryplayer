"""Shared control plane for the resident live-subtitle engine."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import find_subtitle_pipeline_dir, settings

ENGINE_VERSION = 3


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


def alive() -> bool:
    found = paths()
    if found is None:
        return None
    _log, pid_path, _control, _state = found
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
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


def matches() -> bool:
    current = state()
    return (
        current.get("engine") == ENGINE_VERSION
        and current.get("source") == "audio"
        and current.get("model") == str(settings["live_asr_model"])
        and current.get("model_dir") == str(settings["live_asr_dir"] or "")
        and current.get("translate") == str(settings["live_ollama_model"])
    )


def kill() -> None:
    found = paths()
    if found is None:
        return
    _log, pid_path, control, _state = found
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
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
        "--model", str(settings["live_asr_model"]),
        "--ollama-model", str(settings["live_ollama_model"]),
    ]
    if str(settings["live_asr_dir"] or "").strip():
        args += ["--model-dir", str(settings["live_asr_dir"]).strip()]
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
