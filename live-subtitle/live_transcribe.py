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

from asr_engines import audio_stream_start, has_audio_stream
from ollama_service import ensure_ollama
from translate_service import Translator


def split_words_to_lines(words) -> list[tuple[float, float, str]]:
    """Split one whisper segment at sentence boundaries and speech gaps.

    A segment can contain two utterances separated by many seconds of silence;
    using only its first/last word timestamps then produces one caption that
    spans the gap and shows both sentences together.
    """
    rows: list[tuple[float, float, str]] = []
    buf: list[str] = []
    buf_start: float | None = None
    buf_end = 0.0

    def flush() -> None:
        nonlocal buf, buf_start, buf_end
        if buf and buf_start is not None:
            rows.append((buf_start, buf_end, " ".join(buf).strip()))
        buf = []
        buf_start = None
        buf_end = 0.0

    for word in words or []:
        text = str(getattr(word, "word", "")).strip()
        if not text:
            continue
        start = float(getattr(word, "start", 0.0))
        end = float(getattr(word, "end", start))
        if buf and buf_start is not None:
            # A real silence inside the segment is a stronger boundary than
            # punctuation: it keeps two utterances from sharing one caption.
            if start - buf_end > 1.0:
                flush()
            elif end - buf_start > 6.0 or sum(len(part) for part in buf) + len(text) > 80:
                flush()
        if buf_start is None:
            buf_start = start
        buf.append(text)
        buf_end = max(buf_end, end)
        if text[-1:] in ".?!。！？":
            flush()
    flush()
    return rows

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

# 自包含环境：HF 缓存强制指到引擎目录（覆盖调用方环境里指向空间不足盘的变量）。
# 注意：cu12 nvidia DLL 路径只对 whisper(ctranslate2) 注入（见 main）——
# torch(cu13) 引擎注入会被旧 cuDNN 污染报 SUBLIBRARY_VERSION_MISMATCH。
_BASE = Path(__file__).resolve().parent
_cache = _BASE / "models" / "hf" / "hub"
if _cache.is_dir():
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(_cache)
# Runtime must use the installed model snapshot. A hub reachability check can
# otherwise hang for minutes even when the complete model is already on disk.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

TORCH_ENGINES = ("qwen", "sensevoice")  # 走 asr_engines.py（torch 后端）


class DecodeCancelled(Exception):
    """新任务到达时中止解码（旧实现要等整段解完才看得到 cancel）。"""


def _decode_audio_from(media: str, seek: float, max_seconds: float = 900.0,
                       should_cancel=None):
    """从 seek 秒附近解码音频为 16kHz float32 mono（与 decode_audio 输出一致）。

    av 容器 seek 直接到目标时间附近再解码，不再全量解码：37 分钟视频跳转到
    30 分钟处，旧路径要把前 30 分钟全部解码（几十秒、受媒体盘速度拖累）。
    实现仿 faster_whisper.audio.decode_audio（s16 → f32/32768），仅加容器 seek。

    max_seconds 封顶解码窗口：旧实现会一路解到文件尾——2 小时片子跳到
    30 分钟处 ≈ 350MB 音频常驻内存。转写用不到尾部，15 分钟窗口足够覆盖
    一次连续播放（用完后由下一次 seek 触发重新就近解码）。

    should_cancel 每解出一块调用一次：换片/seek 时立刻抛 DecodeCancelled。
    解码本身不可中断曾是换片延迟的主因——大文件解完要几十秒，期间
    cancel_generation 已经前进但没人看得到。
    """
    import gc
    import io

    import av
    import numpy as np
    from faster_whisper.audio import (
        _group_frames,
        _ignore_invalid_frames,
        _resample_frames,
    )

    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=16000,
    )
    raw = io.BytesIO()
    dtype = None
    container = av.open(media, mode="r", metadata_errors="ignore")
    try:
        audio_stream = container.streams.audio[0]
        start_seconds = max(0.0, seek - 2.0)  # 早 2s 起解，给 VAD 留上下文
        # 音频流首帧在媒体时间轴上的时刻（MP4 edit list / TS 起始 PTS 等会使
        # 其非 0）：早于它的媒体区间没有音频可解，seek 只能落到首帧。
        first_media = (
            0.0 if audio_stream.start_time is None
            else float(audio_stream.start_time * audio_stream.time_base)
        )
        tb = float(audio_stream.time_base or av.time_base)
        container.seek(int(start_seconds / tb), stream=audio_stream)
        frames = container.decode(audio_stream)
        frames = _ignore_invalid_frames(frames)
        frames = _group_frames(frames, 500000)
        frames = _resample_frames(frames, resampler)
        decoded_seconds = 0.0
        for frame in frames:
            if should_cancel is not None and should_cancel():
                raise DecodeCancelled
            array = frame.to_ndarray()
            dtype = array.dtype
            raw.write(array)
            decoded_seconds += array.shape[-1] / 16000.0
            if decoded_seconds >= max_seconds:
                break
    finally:
        container.close()
        del resampler
        gc.collect()

    audio = np.frombuffer(raw.getbuffer(), dtype=dtype or np.int16)
    audio = audio.astype(np.float32) / 32768.0
    # 裁剪前导：解码实际从 max(start_seconds, first_media) 起（见上 first_media），
    # 而非臆想的 start_seconds——否则首帧晚于 start_seconds 时会多裁掉缓冲区内
    # 的真实音频，且把返回结果错标成"从 seek 起"。
    actual_start = max(start_seconds, first_media)
    keep_from = int((seek - actual_start) * 16000)
    if keep_from > 0:
        audio = audio[keep_from:]
    return audio


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
    ap.add_argument("--scenario", default="general",
                    help="内容场景提示词（translate_service.SCENARIO_HINTS 的键）")
    ap.add_argument("--target-lang", default="zh",
                    help="翻译目标语言: zh / zh-Hant / en（translate_service.TARGET_NAMES）")
    ap.add_argument("--idle-unload", type=float, default=0.0,
                    help="空闲 N 秒后自动卸载模型释放显存（0=不卸载）")
    ap.add_argument("--denoise", action="store_true",
                    help="实时字幕人声降噪（整段先降噪再 VAD；SRT 路径恒开不受此开关影响）")
    args = ap.parse_args()
    if not args.media and not args.preload:
        ap.error("必须提供媒体文件，或使用 --preload")

    # cu12 nvidia DLL 仅 whisper(ctranslate2) 需要；qwen/sensevoice 用 torch 自带 cu13
    if args.model not in TORCH_ENGINES:
        _nv = _BASE / ".venv" / "Lib" / "site-packages" / "nvidia"
        if _nv.is_dir():
            os.environ["PATH"] = (os.pathsep.join(str(_nv / d / "bin")
                                                  for d in ("cublas", "cudnn", "cuda_nvrtc"))
                                  + os.pathsep + os.environ.get("PATH", ""))

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
        # 截断重建：log 不再跨会话无限累积（viewer 有 size<pos 保护，安全）
        open(args.log, "w", encoding="utf-8").close()
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
            "target": args.target_lang,
            "scenario": args.scenario,
            "idle": int(args.idle_unload),
            "denoise": bool(getattr(args, "denoise", False)),
            "engine": 7,
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
                        # 透传整个 job：translate_model 等字段由下游按需读取，
                        # 白名单裁剪曾把 SRT 的翻译模型选择静默丢弃
                        entry = dict(job)
                        entry["seek"] = max(0.0, float(entry.get("seek", 0.0)))
                        entry["mode"] = str(entry.get("mode", "live"))
                        for key in ("media", "output", "log"):
                            if key in entry:
                                entry[key] = Path(str(entry[key]))
                        with pending_lock:
                            dropped = pending_job
                            pending_job = entry
                        # 单槽被顶掉的 SRT 任务从未进主循环，它的 job log 还
                        # 不存在——播放器的进度窗只认 job log 里的终止标记，
                        # 漏写会让对话框与 srt_busy 永久挂起（"生成中"不动）。
                        # 这里直接写文件而不用 _job_status：模型加载期间它还
                        # 没定义（本线程比它先跑起来）
                        if (dropped is not None and dropped.get("mode") == "srt"
                                and dropped.get("log")):
                            try:
                                dropped["log"].parent.mkdir(parents=True,
                                                            exist_ok=True)
                                with open(dropped["log"], "a",
                                          encoding="utf-8") as fp:
                                    fp.write("# SRT_CANCELLED\n")
                            except Exception:
                                pass
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

    model = None
    vad = None

    def _load_model() -> None:
        """加载 ASR 模型（启动与空闲自动卸载后的重载共用一条路径）。"""
        nonlocal model, vad
        t0 = time.perf_counter()
        try:
            if args.model in TORCH_ENGINES:
                import asr_engines

                if args.model == "qwen":
                    model = asr_engines.load_qwen(args.device, status)
                else:
                    model = asr_engines.load_sensevoice(args.device, status)
                vad = asr_engines.load_vad(status)
            else:
                from faster_whisper import WhisperModel

                # int8：GPU/CPU 通用、占用低（缓解转写期掉帧）；精度足够字幕用途
                model = WhisperModel(args.model_dir or args.model,
                                     device=args.device, compute_type="int8")
        except Exception as exc:  # noqa: BLE001
            status(f"MODEL_ERROR {exc}")
            raise
        status(f"模型就绪 {time.perf_counter() - t0:.0f}s")

    try:
        _load_model()
    except Exception as exc:  # noqa: BLE001
        # 启动期首次加载失败，进程随即退出：此刻 pending 里可能已排着一个 SRT
        # 任务（播放器 preload 后立刻提交），替它写终止标记否则进度窗永久挂起。
        # 运行期的重载失败绝不能碰 pending——那是刚到达的下一个任务，吞掉它
        # 会让补洞/重启调度整体停摆
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

    def _ensure_model() -> None:
        if model is None:
            status("模型已卸载，重新加载 ...")
            _load_model()

    def _unload_model() -> None:
        """空闲自动卸载：释放显存；下次任务 _ensure_model 自动重载。"""
        nonlocal model, vad
        import gc as _gc

        model = None
        vad = None
        _gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        status("MODEL_UNLOADED 空闲超时，已释放显存")

    if args.preload:
        status("MODEL_READY")

    translator = (Translator(args.ollama, args.ollama_model, target=args.target_lang,
                             scenario=args.scenario)
                  if args.translate else None)
    # 降级只在"本任务"内生效：translator 置 None 后靠这个原始引用在下一个
    # 任务（新代次/新 SRT）恢复。原实现是终身降级——Ollama 抖动一次，此后
    # 几小时内所有实时字幕与 SRT 都静默变纯原文，且 SRT 仍报"生成成功"
    translator_base = translator
    # 有界翻译队列：Ollama 卡顿时旧实现无限积压（内存膨胀 + 拖住 join）；
    # 满了丢最旧一条的译文（字幕跟手优先，丢的只是后补的翻译）
    _translate_q: queue.Queue = queue.Queue(maxsize=64)
    # 预转写缓存：prefetch 任务把下一集的完整转写结果留在内存，切到该媒体
    # （seek≈0 且文件未变）时直接复用，省掉整片重复转写。缓存只存一行一句
    # 的 (start, end, text)，体量可忽略；文件被修改/换片即失效
    _prefetch: dict = {"key": None, "rows": []}

    def _drain_translations(timeout: float = 90.0,
                            generation: int | None = None) -> None:
        """收尾前等译文写完，但有上限。

        裸 join() 会把引擎主循环永久挂住：Ollama "能连上但不回"时单句就要等
        120s×2 次重试，队列里最多 64 条——期间换片/seek/新任务全不响应，而心跳
        仍在 touch log，播放器判"存活"永不重启。超时后未译完的行留在队列里，
        由 worker 继续写（同代次的更新行播放器仍会原地补上）。

        有新任务在等（换片/seek/关窗）时立刻让路：90s 上限原本是"最坏情况
        兜底"，实测却成了换片延迟的主因——用户已经打开下一部片，引擎还在
        为上一部片等译文。剩余译文由 worker 继续写，不丢。
        """
        deadline = time.monotonic() + timeout
        while getattr(_translate_q, "unfinished_tasks", 0) > 0:
            if pending_job is not None:
                status("有新任务在等，译文收尾让路（剩余译文稍后补写）")
                return
            if generation is not None and cancel_generation > generation:
                return
            if time.monotonic() >= deadline:
                status("翻译收尾超时，先结束任务（剩余译文稍后补写）")
                return
            time.sleep(0.05)

    def _translate_with_retry(trans, text: str, note) -> tuple[str, bool]:
        """翻译一句，异常重试一次。返回 (译文, 是否最终失败)。

        空译文但无异常是合法结果（纯符号/不可译），不计入连续失败——
        否则纯符号台词连发 5 句会把整个任务的翻译误降级停用。
        """
        for attempt in (1, 2):
            try:
                return trans(text), False
            except Exception as exc:  # noqa: BLE001
                note(f"翻译失败(第{attempt}次): {str(exc)[:120]}")
        return "", True

    def _translate_worker() -> None:
        nonlocal translator
        cur_gen = None
        fail_streak = 0
        degrade_count = 0
        while True:
            item = _translate_q.get()
            if item is None:
                return
            gen, t0, t1, text = item
            try:
                if cancel_generation > gen:
                    continue
                if gen != cur_gen:
                    # 新任务：上一任务的降级不带过来（Ollama 恢复后自动重启
                    # 翻译）。连续降级 3 次后才彻底放弃，避免服务长期不可用时
                    # 每个任务都白等重试
                    if (translator is None and translator_base is not None
                            and degrade_count < 3):
                        translator = translator_base
                        fail_streak = 0
                        status("翻译恢复重试（新任务）")
                    if translator is not None:
                        translator.reset()  # 换片/seek：上一段剧情不带过来
                    cur_gen = gen
                zh = ""
                if translator:
                    zh, failed = _translate_with_retry(translator, text, status)
                    if failed:
                        fail_streak += 1
                        if fail_streak >= 5:
                            status("TRANSLATE_ERROR 连续失败，本任务降级为仅原文")
                            translator = None
                            degrade_count += 1
                    else:
                        fail_streak = 0
                line = json.dumps({
                    "g": gen,
                    "t": round(t0, 2),
                    "end": round(t1, 2),
                    "text": text,
                    "zh": zh,
                }, ensure_ascii=False)
                if log_fp is not None:
                    log_fp.write(line + "\n")
                    log_fp.flush()
                print(line, flush=True)
            except Exception as exc:  # noqa: BLE001
                # 线程死了没人再 task_done，_translate_q.join() 会永久卡住收尾
                # （心跳仍在 touch log，播放器判"存活"，字幕彻底静默）
                try:
                    status(f"✗ 翻译线程异常（本行跳过）: {str(exc)[:120]}")
                except Exception:
                    pass
            finally:
                _translate_q.task_done()

    threading.Thread(target=_translate_worker, daemon=True).start()
    if translator:
        status(f"翻译启用: {args.ollama_model} → {args.target_lang}")
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

    def _write_srt(path: Path, rows: list[tuple[float, float, str, str]],
                   fmt: str = "srt") -> None:
        from translate_service import write_srt_file

        write_srt_file(path, rows, fmt)

    def _media_key(media: Path):
        """媒体身份键：路径+大小+修改时间。文件被替换/重编码即失效。"""
        try:
            stat = media.stat()
        except OSError:
            return None
        return (str(media), stat.st_size, stat.st_mtime)

    # 语言探测缓存：同一文件的主导语言不会因为 seek 而改变。实时字幕每
    # 跳到未覆盖区就开一个新任务，而语言判定状态全是任务内局部变量——
    # 不缓存的话每个任务都要重新攒 3 张长段票、重新攒 12 个内容行、再抽
    # 12 段做一次全量探测（每次几秒 GPU，且这 1-2 分钟里短段仍可能误判
    # 出汉字噪音，最后靠 LANG_REWRITE 回溯重写）。缓存后 seek 任务从第一
    # 段起就有锁，探测整个跳过。
    # 缓存只作初始值：长段仍恒 auto 自识别并投票，3 个一致的长段即可推翻
    # 它（不会把一次误探测永久固化），被推翻时按旧语言解码过的短段会重跑。
    # full=True 表示结论来自整片音频；seek 窗口/滑动锁的结论 full=False，
    # SRT/预转写这类一次成型的离线任务只复用 full=True（探测才几秒，
    # 赌错要毁掉整份字幕）。
    # 按媒体键存多条：连播里上下集来回切不会互相挤掉。
    _lang_cache: dict = {}
    _LANG_CACHE_MAX = 8

    def _lang_cache_get(media: Path, need_full: bool = False) -> str | None:
        key = _media_key(media)
        entry = _lang_cache.get(key) if key is not None else None
        if entry is None:
            return None
        if need_full and not entry[1]:
            return None
        return entry[0]

    def _lang_cache_strong(media: Path) -> bool:
        """缓存结论是否来自整片抽样探测（强证据）。"""
        key = _media_key(media)
        entry = _lang_cache.get(key) if key is not None else None
        return bool(entry and entry[1])

    def _lang_cache_put(media: Path, lang: str | None, full: bool) -> None:
        if not lang:
            return
        key = _media_key(media)
        if key is None:
            return
        old = _lang_cache.get(key)
        # 整片结论不被 seek 窗口/滑动锁的弱结论降级覆盖（证据强度不同）
        if old is not None and old[1] and not full:
            return
        _lang_cache.pop(key, None)          # 重新插入 = 置为最近使用
        _lang_cache[key] = (str(lang), bool(full))
        while len(_lang_cache) > _LANG_CACHE_MAX:
            _lang_cache.pop(next(iter(_lang_cache)))

    def _prefetch_job(job: dict) -> None:
        """转写下一集并缓存结果（不写 log、不排队翻译）。"""
        media = job["media"]
        if not media.is_file():
            return
        if not has_audio_stream(str(media)):
            # 无音轨的下一集：预转写直接跳过（否则解码抛 IndexError，
            # 被主循环记成"任务异常"，日志里满是与用户无关的报错）
            status(f"PREFETCH_SKIP（无音轨）{media.name}")
            return
        key = _media_key(media)
        if key is None:
            return
        if _prefetch["key"] == key:
            status(f"PREFETCH_CACHED {media.name}")
            return
        if args.model not in TORCH_ENGINES:
            status("PREFETCH_SKIP（预转写仅支持 qwen/sensevoice）")
            return
        try:
            _ensure_model()
        except Exception:  # noqa: BLE001
            status("PREFETCH_SKIP（模型重载失败）")
            return
        generation = job["generation"]
        status(f"PREFETCH_START {media.name}")
        started = time.perf_counter()
        import asr_engines

        audio = decode_audio(str(media), sampling_rate=16000)
        audio_off = audio_stream_start(media)  # 缓存行存媒体时间，命中即用
        # 预转写与 SRT 同构：同样人声降噪（默认开），缓存命中行为才一致
        denoiser = asr_engines.load_denoiser(status=status)
        if denoiser is not None:
            audio = asr_engines.denoise_audio(denoiser, audio)
        prefetch_lang = args.lang or "auto"
        if prefetch_lang == "auto" and args.model == "qwen":
            # 预转写也是全片离线任务：先探测主导语言（同 _generate_srt）。
            # 同一文件已有整片结论（此前 SRT/预转写做过）时直接复用。
            cached = _lang_cache_get(media, need_full=True)
            if cached:
                status(f"语言探测: 复用整片结论 {cached}")
                prefetch_lang = cached
            else:
                dom = asr_engines.detect_dominant_language(model, vad, audio,
                                                           status=status)
                if dom:
                    prefetch_lang = dom
                    _lang_cache_put(media, dom, full=True)
        rows: list[tuple[float, float, str]] = []
        for seg_start, seg_end, text in asr_engines.stream_transcribe(
                model, vad, args.model, audio, prefetch_lang):
            # 新任务到来（cancel_generation 前进）即放弃，不缓存半成品
            if cancel_generation > generation:
                status("PREFETCH_CANCELLED")
                return
            if text:
                rows.append((seg_start + audio_off, seg_end + audio_off, text))
        _prefetch["key"] = key
        _prefetch["rows"] = rows
        status(f"PREFETCH_DONE {media.name} {len(rows)} 句 "
               f"{time.perf_counter() - started:.0f}s")

    def _generate_srt(job: dict) -> None:
        media = job["media"]
        output = job["output"]
        generation = job["generation"]
        # 文件/音轨检查先于模型加载：无音轨片源不必白等模型（30s + 6GB 显存）
        if not media.is_file():
            _job_status(job, f"SRT_ERROR 文件不存在: {media}")
            return
        # 无音轨视频：解码会抛 IndexError，SRT 进度窗只认终止标记，
        # 不先判定就会显示成一句意义不明的"任务异常: tuple index out of range"
        if not has_audio_stream(str(media)):
            _job_status(job, f"SRT_ERROR 此视频没有音轨: {media.name}")
            return
        try:
            _ensure_model()
        except Exception as exc:  # noqa: BLE001
            _job_status(job, f"SRT_ERROR 模型重载失败: {str(exc)[:120]}")
            return
        if cancel_generation > generation:
            _job_status(job, "SRT_CANCELLED")
            return

        _job_status(job, f"SRT_STARTED {media.name}")
        started = time.perf_counter()
        raw_rows: list[tuple[float, float, str]] = []
        if args.model in TORCH_ENGINES:
            # qwen / sensevoice：VAD 段时间戳天然真实（无压缩/比例估算）
            import asr_engines

            audio = decode_audio(str(media), sampling_rate=16000)
            audio_off = audio_stream_start(media)  # 缓冲区首样本≠媒体 0 时补差
            duration = max(1.0, len(audio) / 16000.0)
            # 人声降噪（默认开）：VAD 前整段过 gtcrn，削环境音——嘈杂片源
            # 实测语音段 22→71（弱语音不再被噪音淹没）。0.5MB CPU，全片
            # RTF 0.05，1 小时片约 3 分钟。不可用时静默跳过。
            if str(job.get("denoise", "on")) != "off":
                denoiser = asr_engines.load_denoiser(
                    status=lambda m: _job_status(job, m))
                if denoiser is not None:
                    total_secs = len(audio) / 16000.0
                    _job_status(job, f"人声降噪中（约 {total_secs/60:.0f} 分钟音频）...")
                    cleaned = asr_engines.denoise_audio(
                        denoiser, audio,
                        progress=lambda d, t: _job_status(
                            job, f"人声降噪 {d/60:.0f}/{t/60:.0f} 分钟 ..."),
                        should_cancel=lambda: cancel_generation > generation)
                    if cleaned is None:
                        # 降噪中取消：立即终止（旧实现要等整段降噪完才检查，
                        # 30 分钟片取消要干等 2-4 分钟）
                        _job_status(job, "SRT_CANCELLED")
                        return
                    audio = cleaned
            lang_note = args.lang or "auto"
            if lang_note == "auto" and args.model == "qwen":
                # SRT 是离线任务全片在手：先抽样探测主导语言再全文转写，
                # 不赌逐段顺序锁（开场杂乱/快节奏对白的短段误判会把锁带偏，
                # 实测日语片第 8 段锁 zh 后全片汉字噪音）。
                # 同一文件已有整片结论（此前 SRT/预转写做过）时直接复用。
                cached = _lang_cache_get(media, need_full=True)
                if cached:
                    _job_status(job, f"语言探测: 复用整片结论 {cached}")
                    lang_note = cached
                else:
                    dom = asr_engines.detect_dominant_language(
                        model, vad, audio,
                        status=lambda m: _job_status(job, m))
                    if dom:
                        lang_note = dom
                        _lang_cache_put(media, dom, full=True)
            last_pct = -10
            for seg_start, seg_end, text in asr_engines.stream_transcribe(
                    model, vad, args.model, audio, lang_note):
                if cancel_generation > generation:
                    _job_status(job, "SRT_CANCELLED")
                    return
                if text:
                    raw_rows.append((seg_start + audio_off, seg_end + audio_off, text))
                    # 识别进度按语音位置推进（每 5% 报一次，避免刷屏）
                    pct = int(seg_end / duration * 100) // 5 * 5
                    if pct > last_pct:
                        last_pct = pct
                        _job_status(job, f"SRT_PROGRESS 识别 {min(pct, 100)}%")
        else:
            audio_off = audio_stream_start(media)  # whisper 同样基于解码缓冲区起点
            segments, info = model.transcribe(
                str(media), language=args.lang or None, beam_size=5,
                vad_filter=True, word_timestamps=True,
            )
            total = max(1.0, float(getattr(info, "duration", 0.0)) or 1.0)
            last_pct = -10
            for segment in segments:
                if cancel_generation > generation:
                    _job_status(job, "SRT_CANCELLED")
                    return
                text = (segment.text or "").strip()
                if text:
                    raw_rows.append((segment.start + audio_off,
                                     segment.end + audio_off, text))
                pct = int(segment.end / total * 100) // 5 * 5
                if pct > last_pct:
                    last_pct = pct
                    _job_status(job, f"SRT_PROGRESS 识别 {min(pct, 100)}%")
            lang_note = f"{info.language} (p={info.language_probability:.2f})"
        _job_status(
            job,
            f"识别完成 {time.perf_counter() - started:.0f}s，"
            f"语言 {lang_note}，{len(raw_rows)} 句",
        )

        # whisper 会把一句话切成碎片 → 合并成可读字幕行。
        # qwen/sensevoice 已在 asr_engines 里按标点分好句，再合并会重新变成长行。
        rows: list[tuple[float, float, str]] = []
        if args.model in TORCH_ENGINES:
            rows = list(raw_rows)
        else:
            for start, end, text in raw_rows:
                if rows and start - rows[-1][1] <= 1.5 \
                        and len(rows[-1][2]) + len(text) <= 120:
                    old_start, old_end, old_text = rows[-1]
                    rows[-1] = (old_start, max(old_end, end),
                                (old_text + " " + text).strip())
                else:
                    rows.append((start, end, text))

        # SRT 翻译模型按任务指定（设置里与实时字幕分开）：hy-mt2-30b 走
        # llama.cpp（按需启动、用完即关）；live=跟随实时字幕的 Ollama 模型
        job_model = str(job.get("translate_model") or "live")
        # translator 可能被上一个任务的连续失败降级成 None——SRT 不能跟着
        # 静默交付纯原文（还报 SRT_READY"成功"），用原始引用重试
        job_translator = translator if translator is not None else translator_base
        # 本任务的场景（主循环已把它拨到共享 translator 上）：按任务新建的翻译器
        # 也要用同一个值，别退回引擎启动时的旧场景
        job_scenario = str(job.get("scenario") or args.scenario)
        llama_used = False
        try:
            if job_model == "hy-mt2-30b":
                from translate_service import (
                    LlamaServerTranslator,
                    ensure_llama_server,
                    stop_llama_server,
                )

                if ensure_llama_server(lambda m: _job_status(job, m)):
                    job_translator = LlamaServerTranslator(
                        target=args.target_lang, scenario=job_scenario)
                    llama_used = True
                else:
                    _job_status(job, "llama.cpp 不可用，回退 Ollama")
                    job_translator = translator
            elif job_model not in ("", "live", args.ollama_model):
                from translate_service import Translator

                job_translator = Translator(args.ollama, job_model,
                                            target=args.target_lang,
                                            scenario=job_scenario)
        except Exception as exc:  # noqa: BLE001
            _job_status(job, f"翻译模型初始化失败: {str(exc)[:120]}")
            job_translator = translator

        translated_rows: list[tuple[float, float, str, str]] = []
        fail_streak = 0
        try:
            if job_translator is not None:
                job_translator.reset()
            for index, (start, end, original) in enumerate(rows, 1):
                if cancel_generation > generation:
                    _job_status(job, "SRT_CANCELLED")
                    return
                _job_status(job, f"翻译 {index}/{len(rows)} ...")
                zh = ""
                if job_translator is not None:
                    zh, failed = _translate_with_retry(
                        job_translator, original,
                        lambda m: _job_status(job, m))
                    if failed:
                        fail_streak += 1
                        if fail_streak >= 5:
                            _job_status(job, "翻译连续失败，本任务降级为仅原文")
                            job_translator = None
                    else:
                        fail_streak = 0
                translated_rows.append((start, end, original, zh))
        finally:
            if llama_used:
                from translate_service import stop_llama_server

                stop_llama_server()

        if not translated_rows:
            # 一句都没识别出来（纯音乐/全静音/全被 VAD 过滤）：写 0 字节文件并
            # 报 SRT_READY 会让播放器弹"已生成"，用户拿到空字幕
            _job_status(job, "SRT_ERROR 未识别到语音，未生成字幕")
            return
        _write_srt(output, translated_rows, str(job.get("format", "srt")))
        _job_status(job, f"SRT_READY {output}")

    def _transcribe(media: Path, seek: float, generation: int = 0) -> None:
        if cancel_generation > generation:
            status("模型加载期间收到切换，跳过旧转写任务 ...")
            return
        # 文件与音轨检查放在模型加载之前：无音轨的片源本来就转不了，
        # 先加载模型纯属白等 30 秒（还占 6GB 显存）。
        # 无音轨时解码入口会以 IndexError(tuple index out of range) 崩掉，
        # 播放器把它当"转写失败"继续补洞/追赶，同一文件反复重试（实测 18 轮
        # 零产出）。这里先判定并发 NO_AUDIO，播放器据此直接关掉实时字幕。
        if not media.is_file():
            status("✗ 媒体文件不存在: %s" % media)
            status(f"TASK_DONE {generation}")
            return
        if not has_audio_stream(str(media)):
            status(f"NO_AUDIO {media.name}")
            status(f"TASK_DONE {generation}")
            return
        try:
            _ensure_model()
        except Exception as exc:  # noqa: BLE001
            status(f"✗ 模型重载失败: {str(exc)[:120]}")
            status(f"TASK_DONE {generation}")  # 不发会让播放器的补洞/重启调度停摆
            return
        write_state(media)
        status(f"音轨模式：转写 {media.name} ...")

        def _decode_cancelled() -> bool:
            return cancel_generation > generation or pending_job is not None

        # 翻译异步化：转写行立即写 log（zh 为空），翻译 worker 按序译完再写
        # "更新行"（同 g/t/end/text，zh=译文）——转写不再被 Ollama 延迟拖住，
        # 播放器端按 (g,t,end,text) 匹配原地补译文

        def _emit(piece_start: float, piece_end: float, piece_text: str,
                  block: bool = False) -> None:
            line = json.dumps({
                "g": generation,
                "t": round(piece_start, 2),
                "end": round(piece_end, 2),
                "text": piece_text,
                "zh": "",
            }, ensure_ascii=False)
            if log_fp is not None:
                log_fp.write(line + "\n")
                log_fp.flush()
            print(line, flush=True)
            if translator is not None:
                item = (generation, piece_start, piece_end, piece_text)
                if block:
                    # 缓存回放（预转写命中）是紧循环：整片几百行几十毫秒内灌完，
                    # drop-oldest 会把除最后 64 条以外的译文全部丢掉。这里按翻译
                    # 速度回压，代次前进即放弃
                    while True:
                        try:
                            _translate_q.put(item, timeout=0.5)
                            return
                        except queue.Full:
                            if cancel_generation > generation:
                                return
                try:
                    _translate_q.put_nowait(item)
                except queue.Full:
                    try:
                        _translate_q.get_nowait()
                        _translate_q.task_done()
                    except queue.Empty:
                        pass
                    try:
                        _translate_q.put_nowait(item)
                    except queue.Full:
                        pass

        audio_off = audio_stream_start(media)
        if args.model in TORCH_ENGINES:
            # qwen / sensevoice：VAD 切段流式转写，时间戳为段落真实时间。
            # seek 时从目标位置就近解码（av 容器 seek），不再全量解码整个
            # 文件——跳转响应从几十秒（受媒体盘速度拖累）降到一两秒
            import asr_engines

            # 预转写缓存命中（从片头开始播且文件未变）：直接出结果
            if seek <= 0.5:
                key = _media_key(media)
                if key is not None and _prefetch["key"] == key:
                    cached = _prefetch["rows"]
                    _prefetch["key"] = None
                    _prefetch["rows"] = []
                    status(f"{args.model} 命中预转写缓存（{len(cached)} 句）")
                    for piece_start, piece_end, piece_text in cached:
                        if cancel_generation > generation:
                            status("切换媒体，中断当前转写 ...")
                            return
                        # 缓存行已存媒体时间（_prefetch_job 加过 audio_off）
                        _emit(piece_start, piece_end, piece_text, block=True)
                    if translator is not None:
                        # 尾部译文随任务收尾写完，不留给下一代次；有新任务在等就让路
                        _drain_translations(generation=generation)
                    status(f"TASK_DONE {generation}")
                    return

            if seek > 0:
                try:
                    audio = _decode_audio_from(str(media), seek,
                                               should_cancel=_decode_cancelled)
                except DecodeCancelled:
                    status("切换媒体，中断音频解码 ...")
                    status(f"TASK_DONE {generation}")
                    return
            else:
                # 片头起播也走可中断解码（max_seconds=inf 保持整片解码的旧行为）：
                # 大文件整片解码要几十秒，期间关窗换片完全没人响应
                try:
                    audio = _decode_audio_from(str(media), 0.0,
                                               max_seconds=float("inf"),
                                               should_cancel=_decode_cancelled)
                except DecodeCancelled:
                    status("切换媒体，中断音频解码 ...")
                    status(f"TASK_DONE {generation}")
                    return
            if cancel_generation > generation:
                status("切换媒体，中断当前转写 ...")
                status(f"TASK_DONE {generation}")
                return
            # 人声降噪（实时字幕，可选）：整段先降噪再 VAD——嘈杂片源 VAD
            # 能捞出被噪音淹没的弱语音段（实测 22→71 段）。代价：追赶首段
            # +全段 RTF 0.05 的延迟（1 分钟音频约 3s，用户已接受）。
            # 引擎启动参数 --denoise 由播放器设置下发；不传时默认关
            #（实时质量增益不稳定的实测结论，SRT 路径不受此开关影响恒开）。
            if getattr(args, "denoise", False):
                denoiser = asr_engines.load_denoiser(status=status)
                if denoiser is not None:
                    total_secs = len(audio) / 16000.0
                    status(f"人声降噪中（约 {total_secs/60:.0f} 分钟音频）...")
                    cleaned = asr_engines.denoise_audio(
                        denoiser, audio,
                        progress=lambda d, t: status(
                            f"人声降噪 {d/60:.0f}/{t/60:.0f} 分钟 ..."),
                        should_cancel=lambda: cancel_generation > generation
                        or pending_job is not None)
                    if cleaned is None:
                        status("降噪中取消/被新任务取代 ...")
                        status(f"TASK_DONE {generation}")
                        return
                    audio = cleaned
            status(f"{args.model} 引擎转写中 ...")

            # 延迟探测+精准重跑：逐段转写照常出字幕，同时记录每个 auto
            # 段的检测结果 (start, end, lang)。攒够 LANG_PROBE_ROWS 个
            # "有意义内容行"（≥LANG_PROBE_MIN_CHARS 实义字符——嘈杂/短句
            # 区没有语言判定价值，不触发）后做一次全片抽样探测，得到
            # 主导语言 dom。然后：
            # - dom != 顺序锁（锁错）：锁生效后被强制解码的段全被带偏，
            #   从任务起点整体重跑（用 dom）。
            # - dom == 顺序锁（锁对）：只重跑"auto 段中检测语言≠dom 的
            #   短段"——短段误判修正；长段检测可信（真实多语言，保留）。
            # LANG_REWRITE 行通知播放器清对应区间的行，引擎逐区间用 dom
            # 强制重转，新行以相同时间戳重新覆盖。
            class _LangRewrite(Exception):
                def __init__(self, ranges: list[tuple[float, float]], lang: str):
                    super().__init__()
                    self.ranges = ranges
                    self.lang = lang

            seg_records: list[tuple[float, float, str]] = []
            # 强制解码段（用缓存锁解码的短段）：缓存判错时按此定位重跑区间
            forced_records: list[tuple[float, float, str]] = []
            # 同一文件的语言缓存（见 _lang_cache）：seek 任务拿它当初始锁，
            # 跳过重新攒 3 张长段票 + 重新攒 12 个内容行 + 整片抽样探测
            initial_lock: str | None = None
            lock_is_strong = False
            if args.model == "qwen" and (args.lang or "auto") == "auto":
                initial_lock = _lang_cache_get(media)
                lock_is_strong = _lang_cache_strong(media)
                if initial_lock:
                    note = "整片探测结论" if lock_is_strong else "此前任务结论"
                    status(f"语言沿用 {initial_lock}（{note}，"
                           f"{'跳过重新探测' if lock_is_strong else '仍会做一次探测复核'}）")
            # 强证据（整片抽样）才跳过延迟探测；弱证据（seek 窗口/滑动锁）
            # 只当初始锁，仍让延迟探测跑一次拿到整片结论
            probed = [bool(initial_lock) and lock_is_strong]
            content_rows = [0]
            flipped = [False]
            # 沿用缓存被推翻时收集的重跑区间（不中断主流程，跑完再补）
            pending_rewrites: list[tuple[list[tuple[float, float]], str]] = []

            def _seg_sink(s, e, code):
                seg_records.append((s, e, code))

            def _forced_sink(s, e, used):
                forced_records.append((s, e, used))

            def _merge_ranges(spans: list[tuple[float, float]],
                              gap: float = 1.0) -> list[tuple[float, float]]:
                if not spans:
                    return []
                spans = sorted(spans)
                merged = [list(spans[0])]
                for a, b in spans[1:]:
                    if a <= merged[-1][1] + gap:
                        merged[-1][1] = max(merged[-1][1], b)
                    else:
                        merged.append([a, b])
                return [(a, b) for a, b in merged]

            def _on_lock_change(new_lock: str | None) -> None:
                """长段投票推翻了沿用的缓存锁 → 缓存判错，纠正并重跑被带偏的段。

                这是"复用缓存"的纠错出口：缓存只是初始值，长段恒 auto
                自识别，窗口内 3 个一致的长段就能翻锁（一次误探测不会被
                永久固化）。翻锁后按旧语言强制解码过的短段排进重跑队列。
                与 _maybe_probe 不同，这里**不中断本任务**：生成器内部的锁
                已经更新，后续段自然按新语言解码，主流程跑完再补重跑区间——
                没有必要丢掉已解码的音频让播放器重排一个新任务。
                """
                if new_lock:
                    # 滑动锁本身也是结论（弱证据）：存缓存，让后续 seek 任务
                    # 即使这次没触发整片探测也有初始锁可用
                    _lang_cache_put(media, new_lock, full=False)
                if not initial_lock or not new_lock or new_lock == initial_lock \
                        or flipped[0]:
                    return
                flipped[0] = True
                mis = [(s, e) for s, e, used in forced_records if used != new_lock]
                mis += [(s, e) for s, e, c in seg_records
                        if c != new_lock and (e - s) < asr_engines.LOCK_MIN_SECS]
                ranges = _merge_ranges(mis)
                if not ranges:
                    status(f"语言改判 {new_lock}（沿用的 {initial_lock} 被长段推翻）："
                           f"无需重跑")
                    return
                total_bad = sum(b - a for a, b in ranges)
                status(f"LANG_REWRITE {new_lock};"
                       + ";".join(f"{a:.1f}-{b:.1f}" for a, b in ranges))
                status(f"语言改判 {new_lock}（沿用的 {initial_lock} 被长段推翻）："
                       f"重跑 {len(ranges)} 段区间共 {total_bad:.0f}s")
                pending_rewrites.append((ranges, new_lock))

            def _maybe_probe(row_text: str, row_start: float,
                             cur_pos: float) -> None:
                if probed[0] or args.model != "qwen" \
                        or (args.lang or "auto") != "auto":
                    return
                import re as _re

                meaningful = len(_re.sub(r"[\s\W]+", "", row_text))
                if meaningful < asr_engines.LANG_PROBE_MIN_CHARS:
                    return
                content_rows[0] += 1
                if content_rows[0] < asr_engines.LANG_PROBE_ROWS:
                    return
                probed[0] = True
                dom = asr_engines.detect_dominant_language(
                    model, vad, audio, status=status)
                if not dom:
                    return
                # 结论存缓存：同一文件后续的 seek 任务直接沿用。seek 任务的
                # audio 只是解码窗口（不含片头），结论标 full=False——
                # SRT/预转写这类一次成型的离线任务只认整片证据
                _lang_cache_put(media, dom, full=(seek <= 0.5))
                # 探测/重跑只在【本任务音频】内做（见上方注释：seek 任务
                # 音频不含片头，整体重跑会清掉别的任务的正确行且无法回填）
                mis = [(s, e) for s, e, c in seg_records
                       if c != dom
                       and (e - s) < asr_engines.LOCK_MIN_SECS]
                # 用缓存锁强制解码过、而锁与探测结论不符的段：这些段整段
                # 都是按错语言解出来的，必须重跑（沿用缓存的代价出口）
                mis += [(s, e) for s, e, used in forced_records if used != dom]
                # 之前"缓存锁被长段推翻"排下的重跑区间并进来：探测结论证据
                # 更强，统一用 dom 重转；否则抛异常离开主流程会把它们丢掉
                for _ranges, _lang in pending_rewrites:
                    mis += _ranges
                pending_rewrites.clear()
                ranges = _merge_ranges(mis)
                if not ranges:
                    return
                total_bad = sum(b - a for a, b in ranges)
                status(f"LANG_REWRITE {dom};"
                       + ";".join(f"{a:.1f}-{b:.1f}" for a, b in ranges))
                status(f"语言改判 {dom}：重跑 {len(ranges)} 段区间 "
                       f"共 {total_bad:.0f}s")
                raise _LangRewrite(ranges, dom)

            def _rerun_ranges(ranges: list[tuple[float, float]], lang: str) -> bool:
                """逐区间用给定语言强制重转；播放器已按 LANG_REWRITE 清掉
                这些区间的行，新行时间戳相同位置自然覆盖。返回是否跑完。"""
                base = max(seek, audio_off)
                for a, b in ranges:
                    for piece_start, piece_end, piece_text in asr_engines.stream_transcribe(
                            model, vad, args.model,
                            audio[int(a * asr_engines.SR):int(b * asr_engines.SR)],
                            lang):
                        if cancel_generation > generation:
                            status("切换媒体，中断当前转写 ...")
                            return False
                        if piece_text:
                            _emit(base + a + piece_start,
                                  base + a + piece_end, piece_text)
                return True

            try:
                for piece_start, piece_end, piece_text in asr_engines.stream_transcribe(
                        model, vad, args.model, audio, args.lang or "auto",
                        seg_lang_sink=_seg_sink,
                        forced_seg_sink=_forced_sink,
                        lang_observer=_on_lock_change,
                        initial_lock=initial_lock):
                    if cancel_generation > generation:
                        status("切换媒体，中断当前转写 ...")
                        return
                    if piece_text:
                        _emit(max(seek, audio_off) + piece_start,
                              max(seek, audio_off) + piece_end, piece_text)
                    _maybe_probe(piece_text or "", piece_start, piece_end)
            except _LangRewrite as rw:
                if not _rerun_ranges(rw.ranges, rw.lang):
                    return
            else:
                # 沿用的缓存锁被长段推翻：主流程已按新语言跑完剩余段，
                # 这里只补跑早期被旧语言带偏的区间
                for ranges, lang in pending_rewrites:
                    if not _rerun_ranges(ranges, lang):
                        return
            if translator is not None:
                # 同 whisper 路径：收尾前等译文写出，但有新任务在等就让路
                _drain_translations(generation=generation)
            status(f"TASK_DONE {generation}")
            return

        # seek 就近解码（同 torch 引擎路径，带解码窗口封顶），时间戳加偏移
        if seek > 0:
            try:
                audio = _decode_audio_from(str(media), seek,
                                           should_cancel=_decode_cancelled)
            except DecodeCancelled:
                status("切换媒体，中断音频解码 ...")
                status(f"TASK_DONE {generation}")
                return
            seg_iter, info = model.transcribe(
                audio, language=args.lang or None, beam_size=1, vad_filter=True,
                word_timestamps=True,
            )
            offset = max(seek, audio_off)
        else:
            seg_iter, info = model.transcribe(
                str(media), language=args.lang or None, beam_size=1, vad_filter=True,
                word_timestamps=True,
            )
            # 片头全量解码：whisper 内部 decode_audio 从音频流首帧起，起点即 audio_off
            offset = audio_off
        status(f"语言 {info.language} (p={info.language_probability:.2f})，转写中 ...")

        for seg in seg_iter:
            if cancel_generation > generation:
                status("切换媒体，中断当前转写 ...")
                return
            text = (seg.text or "").strip()
            if not text:
                continue
            words = list(getattr(seg, "words", None) or [])
            pieces = split_words_to_lines(words)
            if not pieces:
                pieces = [(float(seg.start), float(seg.end), text)]
            for piece_start, piece_end, piece_text in pieces:
                _emit(offset + piece_start, offset + piece_end, piece_text)
        if translator is not None:
            _drain_translations(generation=generation)  # 等译文写出，有新任务即让路
        status(f"TASK_DONE {generation}")

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

    last_task_end = time.monotonic()

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
            # 空闲自动卸载：默认 0=不卸载；重载耗时由 _ensure_model 兜底
            if (args.idle_unload > 0 and model is not None
                    and time.monotonic() - last_task_end >= args.idle_unload):
                _unload_model()
                last_task_end = time.monotonic()
            _stop_hb.wait(0.25)
            continue
        try:
            # 场景随任务下发：播放器改了「翻译场景」后，下一个任务立刻按新场景
            # 翻译，不必重建引擎（translator 与 translator_base 正常是同一对象，
            # 降级后 translator 为 None，所以两个都要拨）
            job_scenario = str(job.get("scenario") or args.scenario)
            for _tr in (translator, translator_base):
                if _tr is not None and getattr(_tr, "scenario", None) != job_scenario:
                    _tr.scenario = job_scenario
            if job["mode"] == "cancel":
                # generation 已被 _watch_control 置为最新 → 进行中的任务会在
                # 下一个检查点退出并写 SRT_CANCELLED/TASK_DONE；此处无事可做
                status(f"CANCELLED {job['generation']}")
                continue
            if job["mode"] == "prefetch":
                _prefetch_job(job)
                continue
            if job["mode"] == "srt":
                _generate_srt(job)
            else:
                _transcribe(job["media"], job["seek"], job["generation"])
        except Exception as exc:  # noqa: BLE001
            # 单个任务失败不让进程死掉（否则播放器反复判定追赶→重启→死循环）
            status(f"✗ 任务异常: {exc}")
            import traceback

            traceback.print_exc()
            if job.get("mode") == "srt" and job.get("log"):
                # 播放器的 SRT 进度窗只认 job log 里的终止标记（live log 的
                # TASK_DONE 它不读）——漏写会让对话框永久挂起、srt_busy 卡死
                try:
                    job["log"].parent.mkdir(parents=True, exist_ok=True)
                    with open(job["log"], "a", encoding="utf-8") as fp:
                        fp.write(f"# SRT_ERROR 任务异常: {str(exc)[:150]}\n")
                except Exception:
                    pass
            status(f"TASK_DONE {job.get('generation', 0)}")
        finally:
            last_task_end = time.monotonic()


if __name__ == "__main__":
    main()
