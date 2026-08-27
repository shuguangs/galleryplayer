"""实时听音字幕 + 翻译管道（环路录音 + 滑动窗）。

模式：
  1) 实时（默认）：WASAPI 环路录音抓系统输出 → 滑动窗转写 → 实时字幕
     python live_capture.py [--model medium] [--lang en] [--translate] [--srt out.srt]
  2) 测试（wav 模拟实时流，验证滑动窗逻辑）：
     python live_capture.py --input sample.wav

按键：Ctrl+C 停止。
"""
from __future__ import annotations

import argparse
import array
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

# 子进程（播放器 QProcess）以 GBK 控制台运行时，print 中文/✓✗ 会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _SafeOut:
    """print 到已断开的管道（父进程销毁）时不崩溃，进程继续保活。"""

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

# 自包含环境：venv 自带 nvidia CUDA 库 + 引擎目录模型缓存，无需调用方注入
_BASE = Path(__file__).resolve().parent
_NV = _BASE / ".venv" / "Lib" / "site-packages" / "nvidia"
if _NV.is_dir():
    _add = [os.pathsep.join(str(_NV / d / "bin") for d in ("cublas", "cudnn", "cuda_nvrtc"))]
    os.environ["PATH"] = _add[0] + os.pathsep + os.environ.get("PATH", "")
_cache = _BASE / "models" / "hf" / "hub"
if _cache.is_dir():
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_cache))

WINDOW_SECS = 5.0      # 滑动窗长度（每窗转写一次）
OVERLAP_SECS = 1.0     # 窗口重叠（保证语句不切断）
SR = 16000


# ------------------------------------------------------------------ translate

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


# ------------------------------------------------------------------ workers

def transcribe_worker(model: WhisperModel, jobs: queue.Queue, out: queue.Queue,
                      language: str | None, translator) -> None:
    """消费音频块，转写+翻译，结果放 out（音频块为 np.float32 16kHz）。"""
    while True:
        item = jobs.get()
        if item is None:
            out.put(None)  # EOF：通知主循环结束（wav 模拟模式）
            return
        t0, samples = item
        segments, info = model.transcribe(
            samples, language=language, beam_size=1, vad_filter=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        zh = ""
        if text and translator is not None:
            try:
                zh = translator(text)
            except Exception as exc:  # noqa: BLE001
                zh = ""
                print(f"✗ 翻译失败（Ollama）: {exc}", flush=True)
        out.put((t0, text, zh))


def record_loopback(loopback_keyword: str, jobs: queue.Queue) -> None:
    """系统输出环路录音采集：优先当前默认输出设备的环路口（匹配真实声音来源）。"""
    import soundcard as sc

    default = None
    try:
        default = sc.default_speaker()
    except Exception:
        pass
    if default is not None:
        print(f"默认输出设备: {default.name}", flush=True)

    mics = list(sc.all_microphones(include_loopback=True))
    mic = None
    # 1) 用户显式关键词（--loopback）
    if loopback_keyword:
        for m in mics:
            if loopback_keyword.lower() in m.name.lower():
                mic = m
                break
    # 2) 默认输出设备前缀（soundcard 环路 mic 与 speaker 同名/同前缀）
    if mic is None and default is not None:
        base = default.name.split(" (")[0]
        for m in mics:
            if m.name == default.name or m.name.startswith(base):
                mic = m
                break
    # 3) 兜底：按默认设备 id 取环路
    if mic is None and default is not None:
        try:
            mic = sc.get_microphone(id=str(default.name), include_loopback=True)
        except Exception:
            mic = None
    if mic is None:
        raise RuntimeError("未找到可用的环路录音设备——请检查系统声音输出设备")
    print(f"录音设备（环路）: {mic.name}", flush=True)

    window = int(WINDOW_SECS * SR)
    overlap = int(OVERLAP_SECS * SR)
    buf: list[float] = []
    block = int(0.5 * SR)
    t_start = time.perf_counter()
    with mic.recorder(samplerate=SR, channels=1, blocksize=block) as rec:
        while True:
            data = rec.record(numframes=block)
            buf.extend(data.flatten().tolist())
            while len(buf) >= window:
                chunk = buf[:window]
                t0 = time.perf_counter() - t_start
                jobs.put((t0, np.asarray(chunk, dtype=np.float32)))
                buf = buf[window - overlap:]  # keep overlap for continuity


def record_from_wav(path: str, jobs: queue.Queue) -> None:
    """wav 模拟实时流：按 0.5s 块喂入，验证滑动窗逻辑。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        assert sr == SR, f"需要 16kHz wav（当前 {sr}）"
        frames = w.readframes(w.getnframes())
    samples = array.array("h", frames).tolist()

    window = int(WINDOW_SECS * SR)
    overlap = int(OVERLAP_SECS * SR)
    buf: list[int] = []
    block = int(0.5 * SR)
    t_start = time.perf_counter()
    for i in range(0, len(samples), block):
        buf.extend(samples[i: i + block])
        while len(buf) >= window:
            chunk = buf[:window]
            t0 = time.perf_counter() - t_start
            jobs.put((t0, np.asarray(chunk, dtype=np.float32) / 32768.0))
            buf = buf[window - overlap:]
        time.sleep(0.25)  # 模拟实时节奏（0.5s 音频块 0.25s 处理）
    if buf:
        jobs.put((time.perf_counter() - t_start,
                  np.asarray(buf, dtype=np.float32) / 32768.0))
    jobs.put(None)  # EOF：wav 模拟模式输入结束


# ---------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="实时环路录音 + whisper 滑动窗字幕")
    ap.add_argument("--input", default=None, help="wav 测试模式（模拟实时流）")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--loopback", default="扬声器", help="环路设备名关键词")
    ap.add_argument("--translate", action="store_true", help="启用 Ollama 中译")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--ollama-model", default="qwen2.5:7b")
    ap.add_argument("--srt", default=None, help="同时写入 srt 文件")
    ap.add_argument("--out-dir", default=None,
                    help="srt 保存目录（默认与 --srt 路径相同）")
    ap.add_argument("--json", action="store_true",
                    help="每行输出 JSON（供播放器实时解析）：{\"t\":秒, \"text\":\"原语\", \"zh\":\"译文\"}")
    ap.add_argument("--log", default=None,
                    help="JSON 行同时追加写入该文件（播放器解耦监视用）；进程可脱离父进程存活")
    args = ap.parse_args()

    # 播放器解耦监视：pid 锁 + log 文件（最先初始化，进程一启动即可被检测/复用）
    log_fp = None
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(args.log, "a", encoding="utf-8")
        Path(args.log + ".pid").write_text(str(os.getpid()), encoding="utf-8")
        print(f"log 输出: {args.log}", flush=True)

    print(f"加载 whisper {args.model} ({args.device}) ...", flush=True)
    t0 = time.perf_counter()
    compute = "float16" if args.device == "cuda" else "int8"
    model = WhisperModel(args.model, device=args.device, compute_type=compute)
    print(f"模型就绪 {time.perf_counter() - t0:.0f}s", flush=True)

    translator = Translator(args.ollama, args.ollama_model) if args.translate else None
    if translator:
        print(f"翻译启用: {args.ollama_model} → zh", flush=True)
        try:
            urllib.request.urlopen(f"{args.ollama}/api/tags", timeout=5)
        except Exception:
            print("✗ Ollama 服务不可用（11434 未启动）——仅出原文字幕，翻译失败会提示",
                  flush=True)

    jobs: queue.Queue = queue.Queue()
    out: queue.Queue = queue.Queue()
    threading.Thread(
        target=transcribe_worker,
        args=(model, jobs, out, args.lang or None, translator),
        daemon=True,
    ).start()

    if args.input:
        rec_target = lambda: record_from_wav(args.input, jobs)  # noqa: E731
    else:
        rec_target = lambda: record_loopback(args.loopback, jobs)  # noqa: E731
    threading.Thread(target=rec_target, daemon=True).start()

    srt_rows: list[tuple[float, float, str, str]] = []
    print("开始监听（Ctrl+C 停止）...\n", flush=True)
    try:
        while True:
            item = out.get()
            if item is None:
                break
            t0, text, zh = item
            if not text:
                continue
            if args.json or args.log:
                line = json.dumps({"t": round(t0, 1), "text": text, "zh": zh},
                                  ensure_ascii=False)
                if args.log:
                    log_fp.write(line + "\n")
                    log_fp.flush()
                if args.json:
                    print(line, flush=True)
            else:
                line = f"[{t0:6.1f}s] {text}"
                if zh:
                    line += f"\n          ↳ {zh}"
                print(line, flush=True)
            if args.srt:
                srt_rows.append((max(0.0, t0 - WINDOW_SECS), t0, text, zh))
    except KeyboardInterrupt:
        pass
    finally:
        if args.srt and srt_rows:
            srt_path = Path(args.srt)
            if args.out_dir:
                srt_path = Path(args.out_dir) / srt_path.name
                srt_path.parent.mkdir(parents=True, exist_ok=True)
            _write_srt(srt_path, srt_rows)
            print(f"已写入 {srt_path}")


def _write_srt(path: str, rows) -> None:
    def ts(x: float) -> str:
        h, r = divmod(int(x * 1000), 3600000)
        m, r = divmod(r, 60000)
        s, ms = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    parts = []
    for i, (st, en, orig, zh) in enumerate(rows, 1):
        parts.append(f"{i}\n{ts(st)} --> {ts(en)}\n{orig}")
        if zh:
            parts.append(zh)
        parts.append("")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    main()