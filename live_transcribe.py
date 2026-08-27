"""音轨模式：直接读媒体文件音轨流式转写 + 翻译。

与 live_capture.py（环路录音）互补：本模式读播放器正在播的文件本身，
不出录音设备、不被系统其他声音干扰；输出带绝对时间戳的 JSON 行，
播放器按"当前播放位置"选取字幕。

用法（由播放器调用）：
    pythonw live_transcribe.py <媒体> --log <log文件> [--model medium]
                       [--lang en] [--translate] [--ollama-model qwen2.5:7b]
    JSON: {"t": 秒(绝对), "text": 原语, "zh": 译文}
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import time
import urllib.request
from pathlib import Path

# GBK 控制台安全输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _SafeOut:
    def __init__(self, real):
        self._real = real

    def write(self, s):
        try:
            self._real.write(s)
        except (BrokenPipeError, OSError):
            pass

    def flush(self):
        try:
            self._real.flush()
        except (BrokenPipeError, OSError):
            pass


sys.stdout = _SafeOut(sys.stdout)

# 自包含环境：venv nvidia 库 + 引擎目录模型缓存（强制覆盖，防止调用方
# 环境里的 HF 缓存变量指向空间不足的盘导致模型下载失败）
_BASE = Path(__file__).resolve().parent
_NV = _BASE / ".venv" / "Lib" / "site-packages" / "nvidia"
if _NV.is_dir():
    os.environ["PATH"] = (os.pathsep.join(str(_NV / d / "bin")
                                          for d in ("cublas", "cudnn", "cuda_nvrtc"))
                          + os.pathsep + os.environ.get("PATH", ""))
_cache = _BASE / "models" / "hf" / "hub"
if _cache.is_dir():
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(_cache)


class Translator:
    def __init__(self, endpoint: str, model: str, target: str = "zh"):
        self.endpoint, self.model, self.target = endpoint, model, target

    def __call__(self, text: str) -> str:
        body = json.dumps({
            "model": self.model, "stream": False,
            "messages": [
                {"role": "system",
                 "content": (f"You are a professional subtitle translator. Translate "
                             f"into {self.target}. Output ONLY the translation, keep "
                             f"names/numbers, no explanations.")},
                {"role": "user", "content": text},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(f"{self.endpoint}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            out = json.loads(resp.read().decode("utf-8"))["message"]["content"]
        for mark in ("<|END_OF_TURN_TOKEN|>", "<|end_of_turn|>", "<|im_end|>", "<|endoftext|>"):
            out = out.replace(mark, "")
        return out.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="音轨模式：读文件流式转写 + 翻译")
    ap.add_argument("media", help="媒体文件路径")
    ap.add_argument("--log", default=None, help="JSON 行写此文件（播放器监视）")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--translate", action="store_true")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--ollama-model", default="qwen2.5:7b")
    ap.add_argument("--model-dir", default=None, help="本地模型目录（WhisperModel 直接加载）")
    ap.add_argument("--seek", type=float, default=0.0,
                    help="从第 N 秒开始转写（音轨模式追播放进度）")
    args = ap.parse_args()

    # pid 锁 + log（最先初始化，进程一启动即可被检测）
    log_fp = None
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(args.log, "a", encoding="utf-8")
        Path(args.log + ".pid").write_text(str(os.getpid()), encoding="utf-8")

    # 心跳：加载/转写期间每 10s touch log，播放器健康检查凭 mtime 判定存活，
    # 避免首次 cuDNN 自动调优（1-2 分钟无输出）被误判"卡死"杀掉导致死循环
    import threading

    _stop_hb = threading.Event()

    def _heartbeat() -> None:
        while not _stop_hb.is_set():
            try:
                if args.log:
                    Path(args.log).touch()
            except Exception:
                pass
            _stop_hb.wait(10)

    if args.log:
        threading.Thread(target=_heartbeat, daemon=True).start()

    media = Path(args.media)
    if not media.is_file():
        print("✗ 媒体文件不存在: %s" % media)
        sys.exit(1)

    translator = Translator(args.ollama, args.ollama_model) if args.translate else None
    if translator:
        print(f"翻译启用: {args.ollama_model} → zh", flush=True)
        try:
            urllib.request.urlopen(f"{args.ollama}/api/tags", timeout=5)
        except Exception:
            print("✗ Ollama 服务不可用——仅出原文字幕", flush=True)

    print(f"音轨模式：转写 {media.name} ...", flush=True)
    t0 = time.perf_counter()
    from faster_whisper import WhisperModel

    compute = "int8"  # GPU/CPU 通用、占用低（缓解转写期掉帧）；精度足够字幕用途
    model_name_or_dir = args.model_dir or args.model
    model = WhisperModel(model_name_or_dir, device=args.device, compute_type=compute)
    print(f"模型就绪 {time.perf_counter() - t0:.0f}s", flush=True)

    # 音频：seek 偏移则读全量后切片（decode_audio 快），并给时间戳加偏移
    from faster_whisper import decode_audio

    seek = max(0.0, args.seek)
    if seek > 0:
        audio = decode_audio(str(media), sampling_rate=16000)
        audio = audio[int(seek * 16000):]
        seg_iter, info = model.transcribe(
            audio, language=args.lang or None, beam_size=1, vad_filter=False,
        )
        offset = seek
    else:
        seg_iter, info = model.transcribe(
            str(media), language=args.lang or None, beam_size=1, vad_filter=False,
        )
        offset = 0.0
    print(f"语言 {info.language} (p={info.language_probability:.2f})", flush=True)

    for seg in seg_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        zh = ""
        if translator:
            try:
                zh = translator(text)
            except Exception as exc:  # noqa: BLE001
                zh = ""
                print(f"✗ 翻译失败（Ollama）: {exc}", flush=True)
        line = json.dumps({"t": round(offset + seg.start, 2), "text": text, "zh": zh},
                          ensure_ascii=False)
        if log_fp is not None:
            log_fp.write(line + "\n")
            log_fp.flush()
        print(line, flush=True)

    print("=== 转写完成 ===", flush=True)
    # 转写完成后进程保持存活（心跳继续）：播放器据此认为字幕引擎仍在，
    # 不会"回退到开启"；seek 跳转由播放器 kill 后用 --seek 重启
    while True:
        _stop_hb.wait(3600)


if __name__ == "__main__":
    main()