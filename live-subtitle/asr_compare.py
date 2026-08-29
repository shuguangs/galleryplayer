"""ASR 模型对比：small / medium / large-v3 转写同一音频（GPU），计时 + 输出。

用法：python asr_compare.py <音频>
"""
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

AUDIO = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/pitt_s02e01_60s.wav")
MODELS = [("small", "cuda", "float16"), ("medium", "cuda", "float16"),
          ("large-v3", "cuda", "float16")]


def main() -> None:
    for name, device, compute in MODELS:
        print(f"\n===== {name} ({device}/{compute}) =====", flush=True)
        t0 = time.perf_counter()
        model = WhisperModel(name, device=device, compute_type=compute)
        load_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        segments, info = model.transcribe(
            str(AUDIO), language="en", beam_size=5, vad_filter=True,
            condition_on_previous_text=False,
        )
        segs = list(segments)
        cost = time.perf_counter() - t1
        print(f"加载 {load_s:.0f}s | 转写 {cost:.1f}s"
              f" | 实时率 x{60.0 / max(cost, 0.001):.1f} | 段数 {len(segs)}")
        for s in segs[:6]:
            print(f"  [{s.start:5.1f}→{s.end:5.1f}] {s.text.strip()[:80]}")


if __name__ == "__main__":
    main()