"""Load translation scenario metadata from the subtitle engine's JSON files."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import settings
from .runtime import APP_DIR


PROJECT_ENGINE_RELATIVE_DIR = Path("live-subtitle")
SCENARIO_RELATIVE_DIR = Path("scenarios")


def _scenario_dirs() -> list[Path]:
    candidates: list[Path] = []
    custom = str(settings["subtitle_pipeline_dir"] or "").strip()
    if custom:
        candidates.append(Path(custom).expanduser() / SCENARIO_RELATIVE_DIR)
    candidates.append(APP_DIR / PROJECT_ENGINE_RELATIVE_DIR / SCENARIO_RELATIVE_DIR)
    env_dir = os.environ.get("LIVE_SUBTITLE_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser() / SCENARIO_RELATIVE_DIR)

    result: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def load_scenarios() -> list[dict[str, Any]]:
    """Return scenario metadata in JSON order, with custom dirs taking priority."""
    scenarios: dict[str, dict[str, Any]] = {}
    for directory in _scenario_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            key = str(payload.get("key") or path.stem).strip()
            label = payload.get("label")
            if not key or not isinstance(label, dict):
                continue
            scenarios.setdefault(key, {
                "key": key,
                "order": int(payload.get("order", 1000)),
                "label": {str(k): str(v) for k, v in label.items()},
            })
    return sorted(scenarios.values(), key=lambda item: (item["order"], item["key"]))
