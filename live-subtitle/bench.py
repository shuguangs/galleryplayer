"""速度对比：warmup 后同进程二次转写 + beam_size 1/5 对比。"""
import time
from pathlib import Path

from faster_whisper import WhisperModel

MODEL = "large-v3"
AUDIO = Path("samples/jfk.wav")


def run(model, beam, label) -> None:
    t0 = time.perf_counter()
    segments, _ = model.transcribe(str(AUDIO), language="en", beam_size=beam)
    dur = []
    for seg in segments:
        dur.append(f"{seg.start:.1f}-{seg.end:.1f}")
    cost = time.perf_counter() - t0
    print(f"[{label}] beam={beam} 耗时 {cost:.1f}s 实时率 x{11.0 / cost:.1f} 段={len(dur)}")


def main() -> None:
    print("加载模型（首次会 CUDA 初始化）...", flush=True)
    t0 = time.perf_counter()
    model = WhisperModel(MODEL, device="cuda", compute_type="float16")
    print(f"加载: {time.perf_counter() - t0:.1f}s", flush=True)

    run(model, 5, "第一次(含首次推理)")
    run(model, 5, "第二次")
    run(model, 1, "beam=1")
    run(model, 1, "beam=1 再来")


if __name__ == "__main__":
    main()