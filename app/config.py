"""Portable settings + per-file resume positions, stored next to the executable."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .runtime import USERDATA_DIR

DEFAULTS: dict[str, Any] = {
    "last_folder": "",
    "recent_folders": [],
    "recursive": False,
    "view_mode": "grid",          # grid | waterfall | list
    "grid_columns": 5,
    "sort_key": "name",           # name | mtime | size | duration | random
    "sort_desc": False,
    "filter_kind": "all",         # all | image | video
    "volume": 80,
    "muted": False,
    "speed": 1.0,
    "sub_font_size": 42,
    "sub_visible": True,
    "resume_enabled": True,
    "autoplay_next": False,
    "loop_mode": "off",            # off | list | one | shuffle
    # --- side panel
    "panel_visible": True,
    "panel_width": 300,
    "panel_tab": 0,                # 0 playlist, 1 albums, 2 browser
    "panel_thumb_mode": True,
    "window_geometry": "",
    "splitter_sizes": [],
    "tree_visible": True,
    "hwdec": "auto-safe",           # auto-safe | auto | auto-copy | no
    "open_native_size": True,       # 打开视频时按原始分辨率，不强制最大化
    "recent_files": [],             # 最近播放过的单个媒体文件
    # --- 截图 / GIF
    "gif_fps": 10,                  # GIF 采样帧率
    "gif_max_seconds": 15,          # 单段 GIF 最长秒数
    "gif_max_width": 480,           # GIF 缩放到的最大宽度（px）
    "capture_path": "",                # 截图/GIF 保存目录（空=自动：视频所在文件夹，不行则 exe 旁）
    "remember_scroll": True,           # 切换回之前访问过的文件夹时恢复滚动位置
    "language": "",                    # ""=未选择(首启弹窗) | zh | en
    "tree_sort_key": "name",           # 左侧目录树排序: name | mtime | size
    "tree_sort_desc": False,
    "archive_cache": "",               # 压缩包解压缓存目录（空=系统临时目录）
    "subtitle_pipeline_dir": "",       # 字幕引擎目录（空=自动探测：工程同级 live-subtitle/）
    "subtitle_save_dir": "media",      # 字幕保存位置: media=视频所在文件夹 / player=播放器所在文件夹
    "live_ollama_model": "qwen3:8b",    # 实时字幕翻译模型（Ollama）；none=不翻译
    "srt_translate_model": "live",     # 生成 SRT 的翻译模型: live=跟随实时字幕 / Ollama 名 / hy-mt2-30b（llama.cpp）
    "live_caption_font_size": 32,       # 实时字幕覆盖层字号（px）
    "live_caption_width": 84,           # 实时字幕水平覆盖范围（%）
    "live_caption_height": 16,          # 实时字幕垂直覆盖范围（%）
    "caption_bilingual_ratio": 1.0,     # 1=双语，0.5=优先译文，0=仅译文
    "caption_glossary": {},             # 人名/术语替换表
    "live_caption_resident": True,     # 实时字幕常驻：停止时保留模型进程（重开秒出）
    "live_caption_source": "audio",    # 字幕来源: audio=文件音轨（本地播放，推荐）/ loopback=系统声音
    "live_caption_lang": "auto",        # 字幕识别语言: zh/yue/en/ja/ko/fr/de/es/auto
    "live_asr_model": "qwen",          # 识别引擎: qwen/sensevoice/tiny/base/small/medium/large-v3
    "live_model_preset": "accurate",   # fast/balanced/accurate/custom
    "live_asr_dir": "",                # 字幕模型目录（空=HF 缓存下载；可指向本地 faster-whisper 模型文件夹）
    "live_model_preload": True,        # 启动播放器时后台预载字幕模型（不阻塞界面）
    "hardware_aware_model": False,     # 根据显存自动选择合适识别模型
    "archive_no_thumbs": False,        # 压缩包浏览时不加载缩略图（加快浏览）
    "filter_show_image": True,         # 筛选复选框：显示图片
    "filter_show_video": True,         # 筛选复选框：显示视频
    "filter_show_archive": True,       # 筛选复选框：显示压缩包
}

# Videos shorter than this are never resumed; nor are ones watched to the end.
RESUME_MIN_DURATION = 60.0
RESUME_MIN_POSITION = 15.0
RESUME_END_MARGIN = 15.0


class _JsonStore:
    def __init__(self, path: Path, default: Any):
        self._path = path
        self._lock = threading.Lock()
        self._dirty = False
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self._data, type(default)):
                self._data = json.loads(json.dumps(default))
        except Exception:
            self._data = json.loads(json.dumps(default))

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self._path)
            self._dirty = False


# 识别档位 → 引擎/模型。fast=SenseVoice（最快，中日韩粤好、英语弱）、
# balanced=whisper medium（通用、显存小）、accurate=Qwen3-ASR（52 语言，实测最准）
PRESET_MODELS = {
    "fast": "sensevoice",
    "balanced": "medium",
    "accurate": "qwen",
}

# 识别引擎资源需求（安装界面/设置界面提示用）
ASR_MODEL_SPECS = {
    "qwen": {"size_gb": 4.7, "vram_gb": 6.0, "label": "Qwen3-ASR-1.7B"},
    "sensevoice": {"size_gb": 0.9, "vram_gb": 2.0, "label": "SenseVoice-small"},
    "large-v3": {"size_gb": 5.8, "vram_gb": 8.0, "label": "Whisper large-v3"},
    "medium": {"size_gb": 1.5, "vram_gb": 4.0, "label": "Whisper medium"},
    "small": {"size_gb": 0.5, "vram_gb": 2.0, "label": "Whisper small"},
    "base": {"size_gb": 0.15, "vram_gb": 1.0, "label": "Whisper base"},
    "tiny": {"size_gb": 0.08, "vram_gb": 1.0, "label": "Whisper tiny"},
}


# 翻译模型（Ollama）资源需求与实测评语。体积按 Ollama 官方库的量化版计。
# 实测（2026-08-28，见 live-subtitle/bench_translate.py）：
#   qwen3:8b        口语化最自然，习语意译到位——推荐
#   translategemma:4b  谷歌翻译专精，日译稳，英语习语偏直译，体积最小
#   qwen2.5:7b      旧默认，换中文 prompt 后可用，仍有错译
#   aya-expanse:8b  偏直译（"I need an attending"→"我需要关注"）
#   HY-MT2 系列     实测会编造内容（把台词当对话回答），已排除
TRANSLATE_MODEL_SPECS = {
    "none": {"size_gb": 0.0, "vram_gb": 0.0, "label": "不翻译（仅原语）"},
    "qwen3:8b": {"size_gb": 5.2, "vram_gb": 5.5, "label": "qwen3:8b（推荐，最自然）"},
    "translategemma:4b": {"size_gb": 3.3, "vram_gb": 3.5,
                          "label": "translategemma:4b（轻量，翻译专精）"},
    "qwen2.5:7b": {"size_gb": 3.8, "vram_gb": 4.5, "label": "qwen2.5:7b（旧默认）"},
    "qwen2.5:3b": {"size_gb": 2.1, "vram_gb": 2.5, "label": "qwen2.5:3b（最省）"},
    "aya-expanse:8b": {"size_gb": 5.1, "vram_gb": 5.5, "label": "aya-expanse:8b（多语）"},
}


class Settings(_JsonStore):
    def __init__(self) -> None:
        super().__init__(USERDATA_DIR / "config.json", DEFAULTS)
        for k, v in DEFAULTS.items():
            self._data.setdefault(k, v)
        # Older configs could carry a preset plus a stale explicit model. The
        # preset is the user-facing choice, so reconcile it once at startup.
        preset = self._data.get("live_model_preset")
        if preset in PRESET_MODELS \
                and self._data.get("live_asr_model") != PRESET_MODELS[preset]:
            self._data["live_asr_model"] = PRESET_MODELS[preset]
            self._dirty = True

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        if self._data.get(key) != value:
            self._data[key] = value
            self._dirty = True

    get = __getitem__


class ResumeStore(_JsonStore):
    """Maps file path -> last playback position in seconds."""

    MAX_ENTRIES = 4000

    def __init__(self) -> None:
        super().__init__(USERDATA_DIR / "resume.json", {})

    @staticmethod
    def _key(path: str | Path) -> str:
        # normcase(abspath) rather than resolve(): resolve() touches the filesystem,
        # which is a needless round trip on a network share and can fail outright when
        # the share is momentarily unreachable.
        return os.path.normcase(os.path.abspath(str(path)))

    def remember(self, path: str | Path, position: float, duration: float | None) -> None:
        key = self._key(path)
        keep = (
            duration is not None
            and duration >= RESUME_MIN_DURATION
            and position >= RESUME_MIN_POSITION
            and position <= duration - RESUME_END_MARGIN
        )
        if keep:
            self._data[key] = round(position, 2)
        elif key in self._data:
            del self._data[key]
        else:
            return
        self._dirty = True
        if len(self._data) > self.MAX_ENTRIES:
            for k in list(self._data)[: len(self._data) - self.MAX_ENTRIES]:
                del self._data[k]

    def lookup(self, path: str | Path) -> float | None:
        v = self._data.get(self._key(path))
        return float(v) if isinstance(v, (int, float)) else None

    def forget(self, path: str | Path) -> None:
        if self._data.pop(self._key(path), None) is not None:
            self._dirty = True


settings = Settings()
resume = ResumeStore()


def flush() -> None:
    settings.save()
    resume.save()


def find_subtitle_pipeline_dir() -> Path | None:
    """Locate the live-subtitle engine dir (venv with faster-whisper).

    Search order: user-configured path → project folder (source build or
    portable install, resolved relative to this file) → %LIVE_SUBTITLE_DIR%.
    No machine-specific absolute path is ever hardcoded, so the project runs
    from any drive/folder after cloning or moving.
    Returns None when not found.
    """
    candidates: list[Path] = []
    custom = str(settings["subtitle_pipeline_dir"] or "").strip()
    if custom:
        candidates.append(Path(custom).expanduser())
    # source tree / portable layout: <project>/live-subtitle next to <project>/app/
    candidates.append(Path(__file__).resolve().parent.parent / "live-subtitle")
    env_dir = os.environ.get("LIVE_SUBTITLE_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    for c in candidates:
        if c.is_dir() and (c / ".venv" / "Scripts" / "python.exe").is_file():
            return c
    return None
