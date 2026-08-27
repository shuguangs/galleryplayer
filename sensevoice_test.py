"""阿里 SenseVoice-small 多语识别测试（funasr）。"""
import time
from pathlib import Path

AUDIO = r"G:\播放器\live-subtitle\samples\pitt_s02e01_60s.wav"
MODEL_DIR = r"G:\svmodel"  # 中文路径会使 sentencepiece 失败，用 NTFS junction 指向模型


def main() -> None:
    from funasr import AutoModel

    print("加载 SenseVoiceSmall ...", flush=True)
    t0 = time.perf_counter()
    model = AutoModel(
        model=MODEL_DIR,
        trust_remote_code=True,
        device="cpu",
        disable_update=True,
    )
    print(f"加载完成 {time.perf_counter() - t0:.0f}s", flush=True)

    t1 = time.perf_counter()
    res = model.generate(
        input=AUDIO,
        language="auto",
        use_itn=True,   # 数字/标点规整
        batch_size_s=60,
    )
    cost = time.perf_counter() - t1
    print(f"转写耗时: {cost:.1f}s（60s 音频） 实时率 x{60.0 / max(cost, 0.001):.1f}")
    if res and isinstance(res, list):
        text = res[0].get("text", "")
        print("--- 结果 ---")
        print(text[:2000])


if __name__ == "__main__":
    main()