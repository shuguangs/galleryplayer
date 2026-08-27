"""转写测试：加载 faster-whisper 模型，转写音频并计时。

用法：python transcribe.py <音频> [模型名] [device]
默认模型 large-v3，device 自动检测 CUDA。
"""
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    audio = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/jfk.wav")
    model_name = sys.argv[2] if len(sys.argv) > 2 else "large-v3"
    device = sys.argv[3] if len(sys.argv) > 3 else "cuda"

    print(f"加载模型 {model_name} ({device}) ...", flush=True)
    t0 = time.perf_counter()
    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_name, device=device, compute_type=compute)
    print(f"模型加载完成: {time.perf_counter() - t0:.1f}s")

    audio_len = _probe_duration(audio)
    print(f"音频: {audio}  ({audio_len:.1f}s)", flush=True)

    t1 = time.perf_counter()
    segments, info = model.transcribe(str(audio), language=None, beam_size=5)
    det_lang = info.language
    lines = []
    for seg in segments:
        lines.append(f"[{seg.start:6.1f} -> {seg.end:6.1f}] {seg.text.strip()}")
    cost = time.perf_counter() - t1
    text = "\n".join(lines)
    print(f"检测语言: {det_lang} (p={info.language_probability:.2f})")
    print(text)
    print(f"\n转写耗时: {cost:.1f}s；实时率 x{audio_len / max(cost, 0.001):.1f}")


def _probe_duration(audio: Path) -> float:
    try:
        import av

        with av.open(str(audio)) as container:
            return float(container.duration / av.time_base)
    except Exception:
        import wave

        with wave.open(str(audio), "rb") as w:
            return w.getnframes() / w.getframerate()


if __name__ == "__main__":
    main()