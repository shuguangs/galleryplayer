"""音轨模式：直接读媒体文件音轨流式转写 + 翻译。

与 live_capture.py（环路录音）互补：本模式读播放器正在播的文件本身，
不出录音设备、不被系统其他声音干扰；输出带绝对时间戳的 JSON 行，
播放器按"当前播放位置"选取字幕。

用法（由播放器调用）：
    pythonw live_transcribe.py <媒体> --log <log文件> [--model medium]
                       [--lang en] [--translate] [--ollama-model qwen2.5:7b]
    pythonw live_transcribe.py --preload --log <log文件>
    JSON: {"t": 秒(绝对), "end": 秒(绝对), "text": 原语, "zh": 译文}
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

from ollama_service import ensure_ollama

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
# Runtime must use the installed model snapshot. A hub reachability check can
# otherwise hang for minutes even when the complete model is already on disk.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


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
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8"))["message"]["content"]
        for mark in ("<|END_OF_TURN_TOKEN|>", "<|end_of_turn|>", "<|im_end|>", "<|endoftext|>"):
            out = out.replace(mark, "")
        return out.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="音轨模式：读文件流式转写 + 翻译")
    ap.add_argument("media", nargs="?", default=None, help="媒体文件路径")
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
    ap.add_argument("--preload", action="store_true",
                    help="启动后仅加载模型并等待任务，不立即转写")
    args = ap.parse_args()
    if not args.media and not args.preload:
        ap.error("必须提供媒体文件，或使用 --preload")

    # 单实例文件锁：加载/启动竞态曾经产生两个 large-v3 进程，互相抢 GPU。
    lock_fp = None
    lock_path = Path(str(args.log) + ".lock") if args.log else None
    if lock_path is not None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fp = open(lock_path, "a+", encoding="utf-8")
        try:
            import msvcrt

            lock_fp.seek(0)
            msvcrt.locking(lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
        except (ImportError, OSError):
            # 已有引擎持锁：安静退出，避免双模型同载。
            lock_fp.close()
            return

    # pid + log（持锁成功后才写，进程一启动即可被检测）
    log_fp = None
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(args.log, "a", encoding="utf-8")
        Path(args.log + ".pid").write_text(str(os.getpid()), encoding="utf-8")

    state_path = Path(str(args.log) + ".state") if args.log else None

    def write_state(media: Path | None) -> None:
        if state_path is None:
            return
        state = {
            "source": "audio",
            "media": str(media or ""),
            "translate": args.ollama_model if args.translate else "none",
            "model": args.model,
            "model_dir": args.model_dir or "",
            "engine": 3,
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # 心跳：加载/转写期间每 10s touch log，播放器健康检查凭 mtime 判定存活，
    # 避免首次 cuDNN 自动调优（1-2 分钟无输出）被误判"卡死"杀掉导致死循环
    import threading

    _stop_hb = threading.Event()
    control_path = Path(str(args.log) + ".control") if args.log else None
    pending_job: dict | None = None
    pending_lock = threading.Lock()
    cancel_generation = 0

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

    def _watch_control() -> None:
        """Receive live-caption and SRT jobs without unloading Whisper."""
        nonlocal pending_job, cancel_generation
        last_generation = 0
        while not _stop_hb.is_set():
            if control_path is not None:
                try:
                    job = json.loads(control_path.read_text(encoding="utf-8"))
                    generation = int(job.get("generation", 0))
                    if generation > last_generation:
                        last_generation = generation
                        with pending_lock:
                            pending_job = {
                                "media": Path(str(job.get("media", ""))),
                                "seek": max(0.0, float(job.get("seek", 0.0))),
                                "generation": generation,
                                "mode": str(job.get("mode", "live")),
                                "output": Path(str(job.get("output", ""))),
                                "log": Path(str(job.get("log", ""))),
                            }
                        cancel_generation = generation
                except Exception:
                    pass
            _stop_hb.wait(0.25)

    threading.Thread(target=_watch_control, daemon=True).start()

    def status(msg: str) -> None:
        """状态行写入 log（# 前缀）+ 终端，供诊断实时字幕卡点。"""
        if log_fp is not None:
            log_fp.write("# " + msg + "\n")
            log_fp.flush()
        print(msg, flush=True)

    write_state(Path(args.media) if args.media else None)
    if args.preload:
        status("MODEL_PRELOADING")

    t0 = time.perf_counter()
    from faster_whisper import WhisperModel

    compute = "int8"  # GPU/CPU 通用、占用低（缓解转写期掉帧）；精度足够字幕用途
    model_name_or_dir = args.model_dir or args.model
    try:
        model = WhisperModel(model_name_or_dir, device=args.device, compute_type=compute)
    except Exception as exc:  # noqa: BLE001
        status(f"MODEL_ERROR {exc}")
        with pending_lock:
            job = pending_job
            pending_job = None
        if job and job.get("mode") == "srt" and job.get("log"):
            try:
                job["log"].parent.mkdir(parents=True, exist_ok=True)
                with open(job["log"], "a", encoding="utf-8") as fp:
                    fp.write(f"# SRT_ERROR 模型加载失败: {exc}\n")
            except Exception:
                pass
        sys.exit(1)
    status(f"模型就绪 {time.perf_counter() - t0:.0f}s")

    if args.preload:
        status("MODEL_READY")

    translator = Translator(args.ollama, args.ollama_model) if args.translate else None
    if translator:
        status(f"翻译启用: {args.ollama_model} → zh")
        ready, error = ensure_ollama(args.ollama, args.ollama_model, status)
        if ready:
            status(f"TRANSLATE_READY {args.ollama_model}")
        else:
            status(f"TRANSLATE_ERROR {error}")
            translator = None

    if args.preload:
        write_state(None)
        status("MODEL_PRELOADED")

    from faster_whisper import decode_audio

    def _job_status(job: dict, msg: str) -> None:
        """Write job progress to the job's own log, leaving live captions intact."""
        log_path = job.get("log")
        if not log_path:
            status(msg)
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fp:
                fp.write("# " + msg + "\n")
                fp.flush()
        except Exception:
            pass

    def _translate(text: str, job: dict) -> str:
        if translator is None:
            return ""
        try:
            return translator(text)
        except Exception as exc:  # noqa: BLE001
            _job_status(job, f"翻译失败: {exc}")
            return ""

    def _write_srt(path: Path, rows: list[tuple[float, float, str, str]]) -> None:
        def ts(value: float) -> str:
            h, rest = divmod(int(value * 1000), 3600000)
            m, rest = divmod(rest, 60000)
            s, ms = divmod(rest, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        parts: list[str] = []
        for index, (start, end, original, translated) in enumerate(rows, 1):
            parts.append(f"{index}\n{ts(start)} --> {ts(end)}\n{original}")
            if translated:
                parts.append(translated)
            parts.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts), encoding="utf-8")

    def _generate_srt(job: dict) -> None:
        media = job["media"]
        output = job["output"]
        generation = job["generation"]
        if cancel_generation > generation:
            _job_status(job, "SRT_CANCELLED")
            return
        if not media.is_file():
            _job_status(job, f"SRT_ERROR 文件不存在: {media}")
            return

        _job_status(job, f"SRT_STARTED {media.name}")
        started = time.perf_counter()
        segments, info = model.transcribe(
            str(media), language=args.lang or None, beam_size=5,
            vad_filter=True, word_timestamps=True,
        )
        raw_rows: list[tuple[float, float, str]] = []
        for segment in segments:
            if cancel_generation > generation:
                _job_status(job, "SRT_CANCELLED")
                return
            text = (segment.text or "").strip()
            if text:
                raw_rows.append((segment.start, segment.end, text))
        _job_status(
            job,
            f"识别完成 {time.perf_counter() - started:.0f}s，"
            f"语言 {info.language}，{len(raw_rows)} 句",
        )

        # Merge whisper fragments into readable subtitle lines.
        rows: list[tuple[float, float, str]] = []
        for start, end, text in raw_rows:
            if rows and start - rows[-1][1] <= 1.5 and len(rows[-1][2]) + len(text) <= 120:
                old_start, old_end, old_text = rows[-1]
                rows[-1] = (old_start, max(old_end, end), (old_text + " " + text).strip())
            else:
                rows.append((start, end, text))

        translated_rows: list[tuple[float, float, str, str]] = []
        for index, (start, end, original) in enumerate(rows, 1):
            if cancel_generation > generation:
                _job_status(job, "SRT_CANCELLED")
                return
            _job_status(job, f"翻译 {index}/{len(rows)} ...")
            translated_rows.append((start, end, original, _translate(original, job)))

        _write_srt(output, translated_rows)
        _job_status(job, f"SRT_READY {output}")

    def _transcribe(media: Path, seek: float, generation: int = 0) -> None:
        if cancel_generation > generation:
            status("模型加载期间收到切换，跳过旧转写任务 ...")
            return
        write_state(media)
        if not media.is_file():
            status("✗ 媒体文件不存在: %s" % media)
            return
        status(f"音轨模式：转写 {media.name} ...")

        # seek 偏移则读全量后切片（decode_audio 快），并给时间戳加偏移
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
        status(f"语言 {info.language} (p={info.language_probability:.2f})，转写中 ...")

        for seg in seg_iter:
            if cancel_generation > generation:
                status("切换媒体，中断当前转写 ...")
                return
            text = (seg.text or "").strip()
            if not text:
                continue
            zh = ""
            if translator:
                try:
                    zh = translator(text)
                except Exception as exc:  # noqa: BLE001
                    zh = ""
                    status(f"✗ 翻译失败（Ollama）: {exc}")
            line = json.dumps({
                "g": generation,
                "t": round(offset + seg.start, 2),
                "end": round(offset + seg.end, 2),
                "text": text,
                "zh": zh,
            }, ensure_ascii=False)
            if log_fp is not None:
                log_fp.write(line + "\n")
                log_fp.flush()
            print(line, flush=True)

    initial_job: dict | None = None
    if args.media:
        initial_job = {
            "media": Path(args.media),
            "seek": max(0.0, args.seek),
            "generation": 0,
            "mode": "live",
            "output": Path(),
            "log": Path(),
        }

    while True:
        if pending_job is not None:
            with pending_lock:
                job = pending_job
                pending_job = None
        elif initial_job is not None:
            job = initial_job
            initial_job = None
        else:
            job = None
        if job is None:
            _stop_hb.wait(0.25)
            continue
        if job["mode"] == "srt":
            _generate_srt(job)
        else:
            _transcribe(job["media"], job["seek"], job["generation"])


if __name__ == "__main__":
    main()
