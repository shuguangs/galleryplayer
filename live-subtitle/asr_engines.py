"""Qwen3-ASR / SenseVoice 引擎加载与转写。

供 live_transcribe.py（音轨实时+SRT）、live_capture.py（环路录音）、
live_translate.py（批处理）共用。whisper 走 faster-whisper 原路径，不经过本模块。

设计要点：
- Qwen3-ASR-1.7B：52 语言（含中英日+22 中文方言），自带语种识别，bf16 约 5GB 显存。
- SenseVoice-small：中/粤/英/日/韩，最快，CPU 也可实时。
- 两者都不输出词级时间戳 → 用 fsmn-vad 按语音活动切段，段落级时间戳足够字幕用。
- 中文路径的坑（已实测）：sentencepiece/funasr 在含中文的路径下加载失败 →
  SenseVoice 用 NTFS junction 映射到纯 ASCII 路径；qwen-asr 的对齐依赖 nagisa
  同样读不了中文路径 → 垫桩跳过（ASR 本身不需要它）。
- DLL 隔离（已实测）：whisper(ctranslate2) 需要 venv 里 cu12 的 nvidia DLL 注入
  PATH；torch(cu13) 引擎注入会被旧 cuDNN 污染报 SUBLIBRARY_VERSION_MISMATCH。
  调用方务必只对 whisper 引擎注入 cu12 路径。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator

def _resolve_model_dir(base: Path) -> Path:
    """兼容两种模型目录布局：文件直接在顶层 / modelscope 缓存的 snapshots/<ref>/。"""
    if (base / "config.yaml").is_file() or (base / "model.pt").is_file() \
            or (base / "configuration.json").is_file():
        return base
    snap = base / "snapshots"
    if snap.is_dir():
        for child in sorted(snap.iterdir()):
            if (child / "config.yaml").is_file() or (child / "model.pt").is_file():
                return child
    return base


BASE = Path(__file__).resolve().parent
QWEN_DIR = _resolve_model_dir(BASE / "models" / "models" / "Qwen3-ASR-1.7B")
SV_DIR = _resolve_model_dir(BASE / "models" / "models" / "iic--SenseVoiceSmall")
VAD_DIR = _resolve_model_dir(BASE / "models" / "models" / "fsmn-vad")
VAD_MODELSCOPE_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
SR = 16000

# 播放器语言码 → 引擎语言参数
QWEN_LANG = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "yue": "Cantonese", "fr": "French", "de": "German", "es": "Spanish",
    "auto": None,
}
SV_LANG = {"zh": "zh", "yue": "yue", "en": "en", "ja": "ja", "ko": "ko",
           "auto": "auto"}

# 字幕段落约束：VAD 单段上限（自然停顿优先；连续无停顿语音才强切——
# 切点落在词中间会伤识别准确率，故上限放宽到 30s，真实内容几乎不触发）
VAD_MAX_SINGLE_SEG_MS = 30000
MAX_SEG_SECS = 30.0
MIN_SEG_SECS = 0.4
SUB_SPLIT_SECS = 14.0   # 段内超过此时长 → 按句末标点分句（时间按占比近似，总跨度不变）
SUB_SPLIT_CHARS = 60
MIN_TEXT_CHARS = 2  # 剔除"た。"类幻觉碎片（去标点后不足 2 字）


def _usable_text(text: str) -> bool:
    """过滤幻觉碎片：去标点/空白后不足 2 个字符视为无效。"""
    import re

    return len(re.sub(r"[\s\W]+", "", text, flags=re.UNICODE)) >= MIN_TEXT_CHARS


def stub_nagisa() -> None:
    """qwen-asr 顶层 import 强制对齐器 → 对齐器 import nagisa → 中文路径下崩溃。

    ASR 不需要 nagisa（仅日语词对齐用），垫桩跳过。
    """
    try:
        import nagisa  # noqa: F401
        return
    except Exception:
        pass
    import types

    stub = types.ModuleType("nagisa")

    def _unavailable(*_a, **_k):
        raise RuntimeError("nagisa unavailable（中文路径 venv）；强制对齐不可用，ASR 不受影响")

    stub.tagging = _unavailable
    stub.Tagger = _unavailable
    sys.modules["nagisa"] = stub


def _ascii_junction(path: Path) -> Path:
    """中文路径下 funasr/sentencepiece 加载失败 → 建 NTFS junction 到 ASCII 路径。"""
    try:
        str(path).encode("ascii")
        return path  # 本来就是 ASCII 路径，无需处理
    except UnicodeEncodeError:
        pass
    link = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MediaPlayerASR" / path.name
    probe = next(path.iterdir(), None) if path.is_dir() else None
    link_probe = next(link.iterdir(), None) if link.is_dir() else None
    if probe is not None and link_probe is None:
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            # exists() 对悬空 junction（盘符变化后指向不存在目标）返回 False，
            # 必须按链接本身判断存在（os.path.islink 认 junction），否则旧的
            # 删不掉、mklink 又报已存在，回退中文路径后 SenseVoice 永久加载失败
            if link.exists() or os.path.islink(link):
                link.rmdir()
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(path)],
                           capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    return link if (link_probe is not None or link.is_dir()) else path


# ------------------------------------------------------------------ qwen
def load_qwen(device: str = "cuda", status: Callable[[str], None] = print):
    """返回 Qwen3ASRModel；模型目录缺失时抛 FileNotFoundError（安装界面兜底）。"""
    if not QWEN_DIR.is_dir():
        raise FileNotFoundError(f"Qwen3-ASR 模型未安装: {QWEN_DIR}")
    stub_nagisa()
    import torch
    from qwen_asr import Qwen3ASRModel

    use_cuda = device == "cuda" and torch.cuda.is_available()
    return Qwen3ASRModel.from_pretrained(
        str(QWEN_DIR),
        dtype=torch.bfloat16 if use_cuda else torch.float32,
        device_map="cuda:0" if use_cuda else "cpu",
        max_new_tokens=512,
    )


def qwen_transcribe(model, audio16k, lang: str | None) -> tuple[str, str]:
    """整段推理：返回 (文本, 检测语言)。audio16k 为 16kHz float32 mono。

    qwen_asr 只认 (ndarray, sr) 元组（不接受裸 ndarray）。
    """
    results = model.transcribe(audio=(audio16k, SR),
                               language=QWEN_LANG.get(lang, None))
    if not results:
        return "", ""
    return results[0].text.strip(), str(getattr(results[0], "language", "") or "")


# ------------------------------------------------------------- sensevoice
def load_sensevoice(device: str = "cuda", status: Callable[[str], None] = print):
    """返回 funasr AutoModel(SenseVoiceSmall)。"""
    if not SV_DIR.is_dir():
        raise FileNotFoundError(f"SenseVoice 模型未安装: {SV_DIR}")
    from funasr import AutoModel

    use_cuda = device == "cuda"
    try:
        return AutoModel(model=str(SV_DIR), trust_remote_code=True,
                         device="cuda" if use_cuda else "cpu", disable_update=True)
    except Exception:
        # 中文路径 sentencepiece 失败 → junction 重试（显卡无碍，仅路径问题）
        link = _ascii_junction(SV_DIR)
        if link == SV_DIR:
            raise
        status("SenseVoice 中文路径加载失败，改用 ASCII junction ...")
        return AutoModel(model=str(link), trust_remote_code=True,
                         device="cuda" if use_cuda else "cpu", disable_update=True)


def sv_transcribe(model, audio16k, lang: str | None) -> str:
    import re

    res = model.generate(input=audio16k, language=SV_LANG.get(lang, "auto"),
                         use_itn=True)
    text = re.sub(r"<\|[^|]*\|>", "", res[0].get("text", ""))  # 去 <|zh|> 等标记
    return text.strip()


# ------------------------------------------------------------------- VAD
def load_vad(status: Callable[[str], None] = print):
    """fsmn-vad（CPU，几 MB）。优先本地目录；缺失时 funasr 自动从 modelscope 下载。

    max_single_segment_time=10s：连续无停顿语音在 VAD 层强切，
    保证字幕行时长可控（generate 传参不生效，必须构造时覆盖）。
    """
    from funasr import AutoModel

    kw = {"max_single_segment_time": int(VAD_MAX_SINGLE_SEG_MS)}
    if VAD_DIR.is_dir():
        return AutoModel(model=str(VAD_DIR), device="cpu",
                         disable_update=True, **kw)
    status("fsmn-vad 本地缺失，自动下载（首次）...")
    return AutoModel(model=VAD_MODELSCOPE_ID, device="cpu",
                     disable_update=True, **kw)


def vad_segments(vad, audio16k) -> list[tuple[float, float]]:
    """VAD 语音段（秒）；单段 ≤10s 由模型保证，MAX_SEG_SECS 兜底。"""
    total = len(audio16k) / SR
    if total <= 0:
        return []
    res = vad.generate(input=audio16k)
    raw = []
    for item in res or []:
        for start_ms, end_ms in item.get("value") or []:
            s, e = start_ms / 1000.0, end_ms / 1000.0
            if e - s >= MIN_SEG_SECS:
                raw.append((max(0.0, s), min(total, e)))
    out: list[tuple[float, float]] = []
    for s, e in raw:
        while e - s > MAX_SEG_SECS:
            out.append((s, s + MAX_SEG_SECS))
            s += MAX_SEG_SECS
        out.append((s, e))
    return out


# ------------------------------------------------------------- 流式转写
def split_long_row(s: float, e: float, text: str) -> list[tuple[float, float, str]]:
    """超长字幕行按句末标点分句。

    时间在真实 [s, e] 窗口内按字符占比分配（与旧版"压缩时间轴"不同：
    总跨度保持真实值，只是段内边界近似，绝不提前收尾）。
    """
    if e - s <= SUB_SPLIT_SECS and len(text) <= SUB_SPLIT_CHARS:
        return [(s, e, text)]
    import re

    pieces = [p.strip() for p in re.split(r"(?<=[。！？.!?；;])\s*", text) if p.strip()]
    if len(pieces) <= 1:
        pieces = [p.strip() for p in re.split(r"(?<=[，,、])\s*", text) if p.strip()]
    if len(pieces) <= 1:
        return [(s, e, text)]
    total = max(1, sum(len(p) for p in pieces))
    span = e - s
    out, t = [], s
    for p in pieces:
        d = span * len(p) / total
        out.append((round(t, 2), round(min(e, t + d), 2), p))
        t += d
    return out


def stream_transcribe(model, vad, kind: str, audio16k, lang: str | None,
                      block_secs: float = 120.0) -> Iterator[tuple[float, float, str]]:
    """按 block_secs 块推进（保持实时产出），块内 VAD 切段逐段转写。

    yield (start秒, end秒, 文本)，时间相对传入音频起点。
    kind: "qwen" | "sensevoice"
    """
    total = len(audio16k) / SR
    pos = 0.0
    while pos < total:
        block_end = min(pos + block_secs, total)
        chunk = audio16k[int(pos * SR):int(block_end * SR)]
        for s, e in vad_segments(vad, chunk):
            seg = chunk[int(s * SR):int(e * SR)]
            if len(seg) < int(MIN_SEG_SECS * SR):
                continue
            if kind == "qwen":
                text, _detected = qwen_transcribe(model, seg, lang)
            else:
                text = sv_transcribe(model, seg, lang)
            text = (text or "").strip()
            if not (text and _usable_text(text)):
                continue
            for ps, pe, pt in split_long_row(pos + s, pos + e, text):
                yield ps, pe, pt
        pos = block_end
