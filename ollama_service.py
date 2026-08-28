"""Start and verify the local Ollama service used for live translation."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


def _ping(endpoint: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def _model_exists(endpoint: str, model: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = {entry.get("name", "") for entry in data.get("models", [])}
        if model in names:
            return True, ""
        return False, f"模型不存在: {model}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _ollama_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Ollama" / "ollama.exe",
    ]
    for exe in candidates:
        if exe.is_file():
            return exe
    return None


def ensure_ollama(endpoint: str, model: str,
                  log: Callable[[str], None]) -> tuple[bool, str]:
    """Make sure Ollama is running and the configured model is installed."""
    if _ping(endpoint):
        exists, error = _model_exists(endpoint, model)
        return (True, "") if exists else (False, error)

    exe = _ollama_executable()
    if exe is None:
        return False, "未找到 Ollama，请先安装 Ollama"

    log(f"启动 Ollama 服务: {exe}")
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        subprocess.Popen(
            [str(exe), "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Ollama 启动失败: {exc}"

    deadline = time.time() + 20
    while time.time() < deadline:
        if _ping(endpoint, timeout=0.5):
            exists, error = _model_exists(endpoint, model)
            return (True, "") if exists else (False, error)
        time.sleep(0.25)
    return False, "Ollama 服务启动超时"
