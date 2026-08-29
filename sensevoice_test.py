"""阿里 SenseVoice-small 多语识别测试（funasr）。

路径全部相对本文件定位；中文路径问题由 asr_engines 的 junction 处理。
"""
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
AUDIO = BASE / "samples" / "pitt_s02e01_60s.wav"


def main() -> None:
    import asr_engines

    print("加载 SenseVoiceSmall ...", flush=True)
    t0 = time.perf_counter()
    model = asr_engines.load_sensevoice("cpu", print)
    print(f"加载完成 {time.perf_counter() - t0:.0f}s", flush=True)

    t1 = time.perf_counter()
    res = model.generate(
        input=str(AUDIO),
        language="auto",
        use_itn=True,   # 数字/标点规整
        batch_size_s=60,
    )
    cost = time.perf_counter() - t1
    print(f"转写耗时: {cost:.1f}s（60s 音频） 实时率 x{60.0 / max(cost, 0.001):.1f}")
    if res and isinstance(res, list):
        import re
        text = re.sub(r"<\|[^|]*\|>", "", res[0].get("text", ""))
        print("--- 结果 ---")
        print(text[:2000])


if __name__ == "__main__":
    main()
