"""实时字幕 + 翻译管道：音频 → whisper 转写 → Ollama 翻译 → 双语字幕。

用法：
    python live_translate.py <音频或视频> [--cfg config.yaml]
    python live_translate.py demo.mp4              # 默认读 config.yaml
    python live_translate.py demo.mp4 --lang en --no-translate   # 只识别不翻译

输出：终端双语台词 + 可选 .srt 文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# 子进程（播放器 QProcess）以 GBK 控制台运行时，print 中文/✓✗ 会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 自包含环境：引擎目录模型缓存，无需调用方注入。
# cu12 nvidia DLL 只对 whisper(ctranslate2) 需要（main 里按引擎判断）——
# torch(cu13) 引擎注入会被旧 cuDNN 污染报 SUBLIBRARY_VERSION_MISMATCH。
_BASE = Path(__file__).resolve().parent
_cache = _BASE / "models" / "hf" / "hub"
if _cache.is_dir():
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_cache))

TORCH_ENGINES = ("qwen", "sensevoice")  # 走 asr_engines.py（torch 后端）

import yaml

from faster_whisper import WhisperModel


def load_cfg(path: Path) -> dict:
    defaults = {
        "asr": {"model": "large-v3", "device": "cuda", "compute": "float16",
                "language": "en", "beam_size": 5},
        "translate": {"enabled": True, "endpoint": "http://127.0.0.1:11434",
                      "model": "qwen2.5:7b", "target_lang": "zh",
                      "chunk_sentences": 3},
        "output": {"show_original": True, "srt": True},
    }
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for section, values in loaded.items():
            if isinstance(values, dict):
                defaults[section].update(values)
    return defaults


# ------------------------------------------------------------------ translate

def translate_text(text: str, endpoint: str, model: str, target: str) -> str:
    """单次翻译（无上下文）。批处理走 make_translator 的 Translator（带前文）。"""
    from translate_service import Translator

    return Translator(endpoint, model, target, context_lines=0, timeout=600)(text)


def make_translator(cfg: dict):
    tr = cfg["translate"]
    if not tr["enabled"]:
        return None
    from translate_service import Translator

    return Translator(tr["endpoint"], tr["model"], tr["target_lang"], timeout=600)


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="外语视频听音字幕 + 中译")
    ap.add_argument("media", help="音频或视频文件")
    ap.add_argument("--cfg", default="config.yaml")
    ap.add_argument("--lang", default=None, help="原声语言，覆盖配置")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-translate", action="store_true", dest="no_translate")
    ap.add_argument("--out-dir", default=None,
                    help="srt 保存目录（默认与媒体同目录）")
    args = ap.parse_args()

    cfg = load_cfg(Path(args.cfg))
    asr = cfg["asr"]
    if args.lang:
        asr["language"] = args.lang
    if args.model:
        asr["model"] = args.model
    if args.no_translate:
        cfg["translate"]["enabled"] = False

    media = Path(args.media)
    if not media.is_file():
        print(f"文件不存在: {media}")
        sys.exit(1)

    engine = str(asr["model"])
    if engine not in TORCH_ENGINES:
        # cu12 nvidia DLL 仅 whisper(ctranslate2) 需要
        _nv = _BASE / ".venv" / "Lib" / "site-packages" / "nvidia"
        if _nv.is_dir():
            os.environ["PATH"] = (
                os.pathsep.join(str(_nv / d / "bin")
                                for d in ("cublas", "cudnn", "cuda_nvrtc"))
                + os.pathsep + os.environ.get("PATH", ""))
    print(f"[1/2] 加载 {engine} ({asr['device']}) ...", flush=True)
    t0 = time.perf_counter()
    if engine in ("qwen", "sensevoice"):
        import asr_engines

        if engine == "qwen":
            model = asr_engines.load_qwen(asr["device"])
        else:
            model = asr_engines.load_sensevoice(asr["device"])
        vad = asr_engines.load_vad()
    else:
        vad = None
        model = WhisperModel(engine, device=asr["device"], compute_type=asr["compute"])
    print(f"      模型就绪 {time.perf_counter() - t0:.0f}s", flush=True)

    translator = make_translator(cfg)
    if translator:
        print(f"[2/2] 翻译已启用: {cfg['translate']['model']} → {cfg['translate']['target_lang']}")
        ping = _ollama_ping(cfg["translate"]["endpoint"])
        print(f"      Ollama {'在线 ✓' if ping else '离线 ✗（先启动 ollama serve）'}", flush=True)
    else:
        print("[2/2] 翻译已关闭（--no-translate）")

    print("转写中 ...", flush=True)
    t1 = time.perf_counter()
    if engine in ("qwen", "sensevoice"):
        # qwen / sensevoice：VAD 段时间戳真实（asr_engines 内已含标点分句）
        from faster_whisper import decode_audio

        audio = decode_audio(str(media), sampling_rate=16000)
        rows = list(asr_engines.stream_transcribe(
            model, vad, engine, audio, asr["language"] or "auto"))
        lang_note = asr["language"] or "auto"
        lines: list[tuple[float, float, str]] = list(rows)
        print(f"      转写完成 {time.perf_counter() - t1:.0f}s，"
              f"语言 {lang_note}，{len(lines)} 句\n")
    else:
        segments, info = model.transcribe(
            str(media), language=asr["language"] or None,
            beam_size=int(asr["beam_size"]),
            vad_filter=True,  # 跳过静音段
            word_timestamps=True,  # 词级时间戳：断句时用真实语音时间，不按字数估算
        )
        segs = list(segments)
        print(f"      转写完成 {time.perf_counter() - t1:.0f}s，"
              f"语言 {info.language} (p={info.language_probability:.2f})，{len(segs)} 句\n")

    # 忠实模式断句：过滤碎片、合并相邻短句、按词级时间戳+标点切长句。
    # 时间轴一律取自 whisper 真实时间（词级优先），绝不压缩/按字数比例估算——
    # 否则长段被塞进固定窗口，越往后字幕偏得越早。
    def clean_rows(rows) -> list[tuple[float, float, str]]:
        import re as _re

        def has_words(t: str) -> bool:
            return bool(_re.search(r"[A-Za-z\u4e00-\u9fff0-9]", t))

        # 1) 过滤纯标点碎片 + 合并相邻短句（span 用真实首尾时间）
        merged: list[tuple[float, float, str, list]] = []
        for start, end, text, words in rows:
            text = text.strip()
            if not has_words(text):
                continue
            words = list(words or [])
            if merged and start - merged[-1][1] <= 1.5 \
                    and len(merged[-1][2]) + len(text) <= 120:
                s0, _e0, t0, w0 = merged[-1]
                merged[-1] = (s0, max(_e0, end), (t0 + " " + text).strip(), w0 + words)
            else:
                merged.append((start, end, text, words))

        out: list[tuple[float, float, str]] = []
        for start, end, text, words in merged:
            # 2) 长句按词级时间戳+标点切：句末标点所在词的真实 end 即断点
            if words and len(words) > 1 and (end - start > 6.0 or len(text) > 60):
                pieces: list[tuple[float, float, str]] = []
                buf_text = ""
                buf_start = None
                buf_end = None
                for w in words:
                    if buf_start is None:
                        buf_start = w.start
                    buf_text += w.word
                    buf_end = w.end
                    if w.word.rstrip()[-1:] in "。！？.!?,，、；;" and has_words(buf_text):
                        pieces.append((buf_start, buf_end, buf_text.strip()))
                        buf_text = ""
                        buf_start = None
                if buf_text and has_words(buf_text):
                    pieces.append((buf_start, buf_end, buf_text.strip()))
                if pieces:
                    out.extend(pieces)
                    continue
            # 3) 短句 / 无词级时间戳：直接用 whisper 原始时间（不压缩）
            out.append((start, end, text))
        return out

    if engine not in ("qwen", "sensevoice"):
        lines = []
        for s in segs:
            lines.extend(clean_rows(
                [(s.start, s.end, s.text or "", getattr(s, "words", None))]))
    print(f"      断句后 {len(lines)} 条\n")

    out_lines: list[tuple[float, float, str, str]] = []
    zhtext = ""
    for i, (start, end, orig) in enumerate(lines, 1):
        zh = ""
        if translator:
            try:
                print(f"  翻译 {i}/{len(lines)} ...", end=" ", flush=True)
                zh = translator(orig)
                print("✓")
            except Exception as exc:  # noqa: BLE001
                print(f"✗ {exc}")
                zh = ""
        zhtext += zh + ("\n" if zh else "")
        line = f"[{start:7.1f} → {end:7.1f}] {orig}"
        if cfg["output"]["show_original"]:
            line += f"\n           ↳ {zh}" if zh else ""
        print(line)
        if cfg["output"]["srt"]:
            out_lines.append((start, end, orig, zh))

    if cfg["output"]["srt"] and out_lines:
        if args.out_dir:
            srt_path = Path(args.out_dir) / (media.stem + ".zh.srt")
            srt_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            srt_path = media.with_suffix(".zh.srt")
        _write_srt(srt_path, out_lines)
        print(f"\n已导出：{srt_path}")


def _ollama_ping(endpoint: str) -> bool:
    try:
        urllib.request.urlopen(f"{endpoint}/api/tags", timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def _write_srt(path: Path, rows) -> None:
    from translate_service import write_srt_file

    write_srt_file(path, list(rows))


if __name__ == "__main__":
    main()