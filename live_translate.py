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

# 自包含环境：venv 自带 nvidia CUDA 库 + 引擎目录模型缓存，无需调用方注入
_BASE = Path(__file__).resolve().parent
_NV = _BASE / ".venv" / "Lib" / "site-packages" / "nvidia"
if _NV.is_dir():
    _add = [os.pathsep.join(str(_NV / d / "bin") for d in ("cublas", "cudnn", "cuda_nvrtc"))]
    # 防止极旧 FFI 库路径污染用 setdefault 而非覆盖
    os.environ["PATH"] = _add[0] + os.pathsep + os.environ.get("PATH", "")
_cache = _BASE / "models" / "hf" / "hub"
if _cache.is_dir():
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_cache))

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
    """调用 Ollama 翻译一段文本（本地，免费，离线）。"""
    body = json.dumps({
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system",
             "content": (f"You are a professional subtitle translator. "
                         f"Translate the following text into {target}. "
                         f"Output ONLY the translation, keep names/numbers unchanged, "
                         f"no explanations.")},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = data.get("message", {}).get("content", "").strip()
    # Some models (e.g. aya-expanse) leak special tokens / turn markers.
    out = out.replace("<|END_OF_TURN_TOKEN|>", "").replace("<|end_of_turn|>", "")
    out = out.replace("<|im_end|>", "").replace("<|endoftext|>", "").strip()
    return out


def make_translator(cfg: dict):
    tr = cfg["translate"]
    if not tr["enabled"]:
        return None

    def fn(text: str) -> str:
        return translate_text(text, tr["endpoint"], tr["model"], tr["target_lang"])

    return fn


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

    print(f"[1/2] 加载 whisper {asr['model']} ({asr['device']}/{asr['compute']}) ...", flush=True)
    t0 = time.perf_counter()
    model = WhisperModel(asr["model"], device=asr["device"], compute_type=asr["compute"])
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
    segments, info = model.transcribe(
        str(media), language=asr["language"] or None,
        beam_size=int(asr["beam_size"]),
        vad_filter=True,  # 跳过静音段
    )
    segs = list(segments)
    print(f"      转写完成 {time.perf_counter() - t1:.0f}s，"
          f"语言 {info.language} (p={info.language_probability:.2f})，{len(segs)} 句\n")

    # 合并成翻译块
    tr = cfg["translate"]
    chunk_n = int(tr["chunk_sentences"]) if translator else 1
    blocks = [segs[i: i + chunk_n] for i in range(0, len(segs), chunk_n)]

    out_lines: list[str] = []
    zhtext = ""
    for bi, block in enumerate(blocks, 1):
        orig = " ".join(s.text.strip() for s in block)
        zh = ""
        if translator:
            try:
                print(f"  翻译块 {bi}/{len(blocks)} ...", end=" ", flush=True)
                zh = translator(orig)
                print("✓")
            except Exception as exc:  # noqa: BLE001
                print(f"✗ {exc}")
                zh = ""
        zhtext += zh + ("\n" if zh else "")
        for s in block:
            line = f"[{s.start:7.1f} → {s.end:7.1f}] {s.text.strip()}"
            if cfg["output"]["show_original"]:
                line += f"\n           ↳ {zh}" if zh else ""
            print(line)
            if cfg["output"]["srt"]:
                out_lines.append((s.start, s.end, s.text.strip(), zh))

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
    def ts(t: float) -> str:
        h, r = divmod(int(t * 1000), 3600000)
        m, r = divmod(r, 60000)
        s, ms = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    parts = []
    for i, (start, end, orig, zh) in enumerate(rows, 1):
        parts.append(f"{i}\n{ts(start)} --> {ts(end)}\n{orig}")
        if zh:
            parts.append(zh)
        parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    main()