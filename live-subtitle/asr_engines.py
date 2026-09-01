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
GTCRN_PATH = BASE / "models" / "models" / "gtcrn_simple.onnx"
GTCRN_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speech-enhancement-models/gtcrn_simple.onnx")
SR = 16000

# 播放器语言码 → 引擎语言参数
QWEN_LANG = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "yue": "Cantonese", "fr": "French", "de": "German", "es": "Spanish",
    "auto": None,
}
SV_LANG = {"zh": "zh", "yue": "yue", "en": "en", "ja": "ja", "ko": "ko",
           "auto": "auto"}
QWEN_LANGUAGE_TO_CODE = {name: code for code, name in QWEN_LANG.items()
                         if code != "auto"}

# auto 模式下的语言锁：以"最近 N 个长段(≥LOCK_MIN_SECS)的多数语言"锁定。
# 纯英文片里短促的 "Yeah" 曾被重新识别成粤语/日文；锁住后 Qwen 会按 English 解码。
# 但"连续 N 段同语言就永久锁死"是灾难：日语片里"嗯/诶"类中日同形语气词短段
# 被误判成 Chinese 连成 3 个，第 8 段起全片按中文解码，输出汉字噪音
# （实测 测试样本视频：#2 Japanese 正确 → #3-8 短句误判 → 锁 zh → 全片废）。
# 修正后的策略：
#   - 长段（≥LOCK_MIN_SECS）始终 auto 转写并参与滑动多数投票——长段自识别
#     可靠，是锁的唯一依据；早期误锁会被后续长段自然纠正（锁可变）。
#   - 短段用当前多数语言强制解码——短段是误判主源，靠上下文兜底（锁的意义）。
LANGUAGE_LOCK_SEGMENTS = 3
LOCK_MIN_SECS = 2.0       # 参与语言投票的最短段长（秒）
LANGUAGE_VOTE_WINDOW = 5  # 多数投票的滑动窗口（个长段）
# 实时字幕的延迟探测触发：攒够 LANG_PROBE_ROWS 个"有意义内容行"
# （去标点后 ≥LANG_PROBE_MIN_CHARS 实义字符）才做一次全量探测——嘈杂/
# 短句区没有语言判定价值，不触发；内容行出现说明进入对白密集区。
# 与顺序锁不一致则回溯重写（重转代价约 1/3 实时）。12 行：快节奏
# 对白 1-2 分钟攒够；纯音乐/嘈杂片不触发（本来也没有可判定的语言）。
LANG_PROBE_MIN_CHARS = 8
LANG_PROBE_ROWS = 12

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


def detected_language_code(detected: str) -> str | None:
    """把 Qwen 返回的语言名映射回播放器语言码；混合/未知语言不参与锁定。"""
    value = str(detected or "").strip()
    if not value or "," in value:
        return None
    canonical = value[:1].upper() + value[1:].lower()
    return QWEN_LANGUAGE_TO_CODE.get(canonical)


def detect_dominant_language(model, vad, audio16k, sample_n: int = 12,
                             status: Callable[[str], None] | None = None
                             ) -> str | None:
    """全片抽样探测主导语言（SRT 生成/预转写等离线场景）。

    顺序语言锁对快节奏对白、开场杂乱音频无解：短段（中日同形语气词）
    误判连片会把锁带偏（实测 多语言测试样本 前 6 分钟逐段检测 ja/zh/en/yue
    混杂，旧逻辑第 8 段锁死 zh 全片报废）。离线任务全片在手：均匀抽
    sample_n 个 ≥LOCK_MIN_SECS 的长段 auto 转写，检测结果严格多数投票。
    中后段密集对白检测可靠（同片 12 抽 9 票 Japanese）。返回语言码；
    长段不足/无严格多数时 None（调用方回退顺序投票逻辑）。
    """
    from collections import Counter

    segs = [(s, e) for s, e in vad_segments(vad, audio16k)
            if e - s >= LOCK_MIN_SECS]
    if not segs:
        return None
    step = max(1, len(segs) // sample_n)
    picked = segs[::step][:sample_n]
    votes = []
    for s, e in picked:
        seg = audio16k[int(s * SR):int(e * SR)]
        _text, detected = qwen_transcribe(model, seg, None)
        code = detected_language_code(detected)
        if code:
            votes.append(code)
    if not votes:
        return None
    top, n = Counter(votes).most_common(1)[0]
    if status is not None and votes:
        detail = ",".join(f"{c}:{v}" for c, v in Counter(votes).most_common())
        status(f"语言探测: {detail} → {top if n * 2 > len(votes) else '无多数，回退逐段'}")
    return top if n * 2 > len(votes) else None


# ------------------------------------------------------------- 人声降噪
def load_denoiser(status: Callable[[str], None] = print):
    """gtcrn 人声降噪（常驻，0.5MB CPU 流式，实测段级 52ms/整段 RTF 0.05）。

    模型缺失时自动从 sherpa-onnx 官方 release 下载（0.5MB，秒级）。
    返回 None 表示不可用（调用方跳过降噪）。
    """
    import sherpa_onnx

    if not GTCRN_PATH.is_file():
        status("降噪模型缺失，自动下载 gtcrn（0.5MB）...")
        import urllib.request

        GTCRN_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(GTCRN_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(GTCRN_PATH, "wb") as fp:
                fp.write(resp.read())
        except Exception as exc:  # noqa: BLE001
            status(f"✗ 降噪模型下载失败: {exc}")
            return None
    try:
        cfg = sherpa_onnx.OfflineSpeechDenoiserConfig(
            model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(
                    model=str(GTCRN_PATH))))
        if not cfg.validate():
            return None
        return sherpa_onnx.OfflineSpeechDenoiser(cfg)
    except Exception:  # noqa: BLE001
        return None


def denoise_audio(denoiser, audio16k, progress=None,
                  should_cancel=None) -> "np.ndarray":
    """整段人声降噪（削环境音；VAD 前置用——降噪后 VAD 能捞出被噪音
    淹没的弱语音段，实测嘈杂片源语音段 22→71）。失败原样返回。

    progress(done_secs, total_secs)：分块进度回调（SRT 进度窗显示，
    避免长片 2-4 分钟静默被当成卡死）。分块与整段结果一致（gtcrn
    流式模型按块独立，实测零差异），单块失败跳过该块。
    should_cancel()：块间检查——取消 SRT/实时任务时立即中止降噪，
    返回 None 表示已取消（调用方按取消处理，不再进入转写）。
    """
    if denoiser is None:
        return audio16k
    import numpy as _np

    total = len(audio16k)
    chunk = int(300 * SR)  # 5 分钟/块：32 分钟片约 7 块，每块 ~20s

    def _run_block(block):
        out = denoiser.run(block, SR)
        cleaned = _np.asarray(out.samples, dtype=_np.float32)
        return cleaned if cleaned.size == block.size else block

    try:
        if total <= chunk:
            if should_cancel is not None and should_cancel():
                return None
            return _run_block(audio16k)
        pieces = []
        for pos in range(0, total, chunk):
            if should_cancel is not None and should_cancel():
                return None
            pieces.append(_run_block(audio16k[pos:pos + chunk]))
            if progress is not None:
                progress(min(total, pos + chunk) / SR, total / SR)
        return _np.concatenate(pieces)
    except Exception:  # noqa: BLE001
        return audio16k


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


# ------------------------------------------------------- 容器音频时间轴
def has_audio_stream(media: str) -> bool:
    """媒体是否含可解码的音频流。

    无音轨的视频（纯画面录屏、抽掉音轨的片源）在解码入口会以
    `container.streams.audio[0]` → IndexError: tuple index out of range
    崩掉任务；调用方先问一句，就能给出"此视频没有音轨"的明确结论，
    而不是把底层索引错误抛给用户，也不会让播放器把它当成"转写失败、
    继续补洞"而无限重试（实测同一文件连刷 18 轮）。

    读取失败（文件损坏/无权限）时返回 False——同样不该进入转写。
    """
    try:
        import av

        with av.open(str(media), mode="r", metadata_errors="ignore") as container:
            return len(container.streams.audio) > 0
    except Exception:  # noqa: BLE001
        return False


def audio_stream_start(media: str) -> float:
    """音频流在媒体时间轴上的起始偏移（秒），用于把 ASR 时间戳修正到媒体时间。

    faster-whisper 的 decode_audio 把音频流从首帧起按样本序解码并丢弃时间戳，
    因此 VAD/whisper 报的时间是相对"音频流首帧"的；而播放器按容器时间轴呈现
    （MP4 edit list、TS 起始 PTS、MKV codec delay 等都会让音频流首帧落在非 0
    的媒体时刻）。把该偏移加回所有时间戳，SRT/实时字幕才能与画面同步。

    该值可为负（容器裁掉开头音频时）；无音频流/读取失败时返回 0.0。
    """
    try:
        import av

        with av.open(str(media), mode="r", metadata_errors="ignore") as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None or stream.start_time is None:
                return 0.0
            return float(stream.start_time * stream.time_base)
    except Exception:
        return 0.0


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


# fsmn-vad 单次调用的输入上限（秒）：超长整段偶发 funasr streaming-cache bug
#（lfr_splice_cache 空列表崩溃，SRT 长片实测）——分块调用彻底绕开，
# 且分块与整段结果完全一致（300s 实测 22 段 / 25.1s 语音零差异）。
VAD_CHUNK_SECS = 120.0


def vad_segments(vad, audio16k) -> list[tuple[float, float]]:
    """VAD 语音段（秒）；单段 ≤10s 由模型保证，MAX_SEG_SECS 兜底。

    超过 VAD_CHUNK_SECS 的输入分块调用（块间边界段如实保留——实测
    与整段调用结果一致），规避 funasr 长输入的偶发崩溃。
    """
    total = len(audio16k) / SR
    if total <= 0:
        return []
    raw: list[tuple[float, float]] = []
    chunk = int(VAD_CHUNK_SECS * SR)
    for pos in range(0, len(audio16k), chunk):
        block = audio16k[pos:pos + chunk]
        offset = pos / SR
        try:
            res = vad.generate(input=block)
        except Exception:  # noqa: BLE001 单块失败：跳过该块（不炸整任务）
            continue
        for item in res or []:
            for start_ms, end_ms in item.get("value") or []:
                s, e = offset + start_ms / 1000.0, offset + end_ms / 1000.0
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
                      block_secs: float = 120.0,
                      language_lock_after: int | None = LANGUAGE_LOCK_SEGMENTS,
                      lang_observer=None,
                      seg_lang_sink=None,
                      forced_seg_sink=None,
                      initial_lock: str | None = None,
                      ) -> Iterator[tuple[float, float, str]]:
    """按 block_secs 块推进（保持实时产出），块内 VAD 切段逐段转写。

    yield (start秒, end秒, 文本)，时间相对传入音频起点。
    kind: "qwen" | "sensevoice"

    auto 模式的语言策略（qwen）：
    - 长段（≥LOCK_MIN_SECS）始终 auto 转写——长段自带足够语境，自识别
      可靠（实测日语片长段稳定返回 Japanese，即使之前被短句误锁）；
      其检测结果进入滑动投票窗口，多数语言即当前锁定语言。
    - 短段用锁定语言强制解码——短段（中日同形语气词"嗯/诶"、单音节
      "Yeah/자"）是误判主源，靠多数上下文兜底（这正是锁存在的意义）。
    - 锁随投票窗口滑动更新：早期误锁会被后续长段自然纠正；视频真实
      换语言（少见）时锁也会跟着走。

    lang_observer(lock_or_none)：锁定语言变化时回调。
    seg_lang_sink(start, end, code)：每个 **auto 转写段**的检测结果回调
    （强制解码段的 detected 不可信，不回调）。实时字幕用它记录"哪一段
    曾被判成什么语言"，探测确立主导语言后只重跑判错的段。
    forced_seg_sink(start, end, lang_used)：每个 **强制解码段**回调，记录
    当时用的语言。initial_lock 来自缓存时，这些段是"赌缓存正确"的产物；
    一旦后来探测出别的主导语言，调用方要按此记录把它们重跑。
    initial_lock：预置锁定语言（同一文件此前已探明主导语言时用）。它让
    短段从第一段起就有上下文兜底，省掉重新攒票/重新抽样探测的开销；
    只作为初始值，长段仍恒 auto 投票，窗口滑动后可被推翻（缓存万一是
    错的不会被永久固化）。
    """
    total = len(audio16k) / SR
    pos = 0.0
    requested_lang = str(lang or "auto")
    forced_lang = None if requested_lang == "auto" else requested_lang
    lock_after = LANGUAGE_LOCK_SEGMENTS if language_lock_after is None \
        else max(0, int(language_lock_after))
    locked_lang: str | None = None
    votes: list[str] = []
    if forced_lang is None and lock_after and initial_lock:
        # 预置锁：塞满 lock_after 张票让它立刻生效，但只占投票窗口的一部分——
        # 后续长段（恒 auto，自识别可靠）会把这些票挤出窗口，缓存判错时
        # 3 个一致的长段就能翻锁
        locked_lang = str(initial_lock)
        votes = ([locked_lang] * lock_after)[-LANGUAGE_VOTE_WINDOW:]

    def _tally() -> str | None:
        """投票窗口的多数语言（票数 ≥ lock_after 才算锁定）。"""
        if not votes:
            return None
        best: str | None = None
        best_n = 0
        for code in set(votes):
            n = votes.count(code)
            if n > best_n or (n == best_n and best is None):
                best, best_n = code, n
        return best if best_n >= lock_after else None

    while pos < total:
        block_end = min(pos + block_secs, total)
        chunk = audio16k[int(pos * SR):int(block_end * SR)]
        for s, e in vad_segments(vad, chunk):
            seg = chunk[int(s * SR):int(e * SR)]
            if len(seg) < int(MIN_SEG_SECS * SR):
                continue
            if kind == "qwen":
                long_seg = (e - s) >= LOCK_MIN_SECS
                # 长段：auto（可靠自识别）；短段：锁定的多数语言兜底
                ask = forced_lang if forced_lang is not None \
                    else (None if long_seg else locked_lang)
                text, detected = qwen_transcribe(model, seg, ask)
                if ask is None and seg_lang_sink is not None:
                    # auto 段的 detected 才可信（强制解码后检测字段无意义）
                    code = detected_language_code(detected)
                    if code is not None:
                        seg_lang_sink(pos + s, pos + e, code)
                elif ask is not None and forced_seg_sink is not None:
                    # 强制解码段：记录当时用的语言，供"缓存锁判错"时定位重跑区间
                    forced_seg_sink(pos + s, pos + e, str(ask))
                if forced_lang is None and lock_after and long_seg:
                    code = detected_language_code(detected)
                    if code is not None:
                        votes.append(code)
                        del votes[:-LANGUAGE_VOTE_WINDOW]
                        new_lock = _tally()
                        if new_lock != locked_lang:
                            locked_lang = new_lock
                            if lang_observer is not None:
                                lang_observer(new_lock)
            else:
                text = sv_transcribe(model, seg, lang)
            text = (text or "").strip()
            if not (text and _usable_text(text)):
                continue
            for ps, pe, pt in split_long_row(pos + s, pos + e, text):
                yield ps, pe, pt
        pos = block_end
