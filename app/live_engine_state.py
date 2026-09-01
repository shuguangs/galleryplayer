"""Structured events for the resident subtitle engine."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class EngineEvent(Enum):
    ENGINE_STARTING = auto()
    MODEL_LOADING = auto()
    MODEL_READY = auto()
    ENGINE_READY = auto()
    TASK_STARTED = auto()
    TASK_PROGRESS = auto()
    TASK_DONE = auto()
    TASK_CANCELLED = auto()
    TRANSLATE_READY = auto()
    TRANSLATE_ERROR = auto()
    LANG_REWRITE = auto()
    NO_AUDIO = auto()
    MODEL_ERROR = auto()
    ERROR = auto()


@dataclass(frozen=True)
class EngineEventData:
    event: EngineEvent
    detail: str = ""
    generation: int | None = None


def parse_engine_line(line: str) -> EngineEventData | None:
    """Translate engine status lines into a small explicit state machine."""
    text = line.strip()
    if text.startswith("# "):
        text = text[2:]
    elif not text.startswith("{"):
        return None
    else:
        return None

    if text.startswith("MODEL_PRELOADING"):
        return EngineEventData(EngineEvent.MODEL_LOADING, "preload")
    if text.startswith("MODEL_READY"):
        return EngineEventData(EngineEvent.MODEL_READY)
    if text.startswith("MODEL_PRELOADED"):
        return EngineEventData(EngineEvent.ENGINE_READY)
    if text.startswith("MODEL_ERROR "):
        return EngineEventData(EngineEvent.MODEL_ERROR, text[12:].strip())
    if text.startswith("TRANSLATE_READY "):
        return EngineEventData(EngineEvent.TRANSLATE_READY, text[16:].strip())
    if text.startswith("TRANSLATE_ERROR "):
        return EngineEventData(EngineEvent.TRANSLATE_ERROR, text[16:].strip())
    if text.startswith("LANG_REWRITE "):
        # 延迟探测改判：detail = "<lang>;<a1>-<b1>;<a2>-<b2>..."——
        # 丢弃这些区间内的行，引擎正按探测语言逐区间重转
        return EngineEventData(EngineEvent.LANG_REWRITE, text[13:].strip())
    if text.startswith("NO_AUDIO"):
        # 此视频没有音轨：转写无从下手。必须在 TASK_DONE 之前被识别，
        # 否则播放器会把它当普通"任务未覆盖全片"继续补洞、无限重试。
        return EngineEventData(EngineEvent.NO_AUDIO, text[8:].strip())
    if text.startswith("TASK_DONE "):
        try:
            generation = int(text.split()[1])
        except (IndexError, ValueError):
            generation = None
        return EngineEventData(EngineEvent.TASK_DONE, generation=generation)
    if text.startswith("切换媒体"):
        return EngineEventData(EngineEvent.TASK_CANCELLED, text)
    if text.startswith("音轨模式："):
        return EngineEventData(EngineEvent.TASK_STARTED, text)
    if text.startswith("语言 "):
        return EngineEventData(EngineEvent.TASK_PROGRESS, text)
    if any(token in text for token in ("Traceback", "Error", "RuntimeError", "✗")):
        return EngineEventData(EngineEvent.ERROR, text[:160])
    return None
