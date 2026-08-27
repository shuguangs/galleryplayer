"""sherpa-onnx 流式英文识别测试（k2-fsa 方案）。"""
import time
import wave
from pathlib import Path

import sherpa_onnx

MODEL_DIR = Path(r"G:\播放器\live-subtitle\models\sherpa\sherpa-onnx-streaming-zipformer-en-2023-06-26")
AUDIO = Path(r"G:\播放器\live-subtitle\samples\pitt_s02e01_60s.wav")


def main() -> None:
    rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(MODEL_DIR / "tokens.txt"),
        encoder=str(MODEL_DIR / "encoder-epoch-99-avg-1-chunk-16-left-128.onnx"),
        decoder=str(MODEL_DIR / "decoder-epoch-99-avg-1-chunk-16-left-128.onnx"),
        joiner=str(MODEL_DIR / "joiner-epoch-99-avg-1-chunk-16-left-128.onnx"),
        num_threads=4,
        sample_rate=16000,
        feature_dim=80,
        enable_endpoint_detection=False,
    )

    with wave.open(str(AUDIO), "rb") as w:
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    samples = __import__("array").array("h", data).tolist()

    chunk = int(0.5 * sr)  # 0.5s 一块
    t0 = time.perf_counter()
    stream = rec.create_stream()
    for i in range(0, len(samples), chunk):
        block = samples[i: i + chunk]
        if not block:
            break
        stream.accept_waveform(16000, block)
        while rec.is_ready(stream):
            rec.decode_stream(stream)
    stream.input_finished()
    while rec.is_ready(stream):
        rec.decode_stream(stream)
    cost = time.perf_counter() - t0

    print(f"转写耗时: {cost:.1f}s（60s 音频，含实时流式块处理）")
    print(f"实时率参考 x{60.0 / max(cost, 0.001):.1f}")
    print("--- 结果（最终确认文本）---")
    print(rec.get_result(stream))


if __name__ == "__main__":
    main()