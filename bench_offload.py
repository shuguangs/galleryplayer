"""GPU/CPU 权重分配梯度测试（Ollama num_gpu = 放到显卡的层数）。

用于 MoE 大模型（如 Hy-MT2-30B-A3B Nano 11.5GB）在 16GB 显卡上的取舍：
显存要留给 Qwen3-ASR（约 6GB），翻译模型只能吃剩下的，剩余层跑内存。

用法：
    python bench_offload.py --model hy-mt2-30b-nano
    python bench_offload.py --model hy-mt2-30b-nano --layers 0 8 16 24 32 --reserve-asr

--reserve-asr 会先把 ASR 引擎拉起来占住显存，测真实共存场景。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request

ENDPOINT = "http://127.0.0.1:11434"

SYSTEM = (
    "你是资深影视字幕译者，把台词翻成简体中文。要求：\n"
    "1. 说人话——用中文影视字幕的口语腔，别留直译痕迹；\n"
    "2. 习语按中文习惯意译（call it 在急救语境是「宣布死亡」）；\n"
    "3. 保留人名、数字、专业术语；语气词、反讽都要译出来；\n"
    "4. 只输出译文，不要解释。"
)

LINES = [
    "She's probably going to grow to hate her.",
    "Cut me some slack, would you? I've been on my feet for 12 hours.",
    "Stop compressions. We're calling it.",
    "When she started talking about AI, I started thinking about robots "
    "and kind of stopped listening.",
    "しょうがないな、今回だけだぞ。",
    "彼のことが気になって、夜も眠れないんだ。",
]

JUNK = ("<|im_end|>", "<|endoftext|>", "<｜hy_Assistant｜>", "<｜hy_User｜>",
        "<suggested_response>", "</suggested_response>", "<think>", "</think>")


def vram_used_mb() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


def unload(model: str) -> None:
    """keep_alive=0 让 Ollama 立即卸载，保证下一档从干净状态加载。"""
    body = json.dumps({"model": model, "messages": [], "keep_alive": 0}).encode()
    try:
        req = urllib.request.Request(f"{ENDPOINT}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
    except Exception:
        pass
    time.sleep(3)


def translate(model: str, text: str, num_gpu: int | None,
              timeout: int = 600) -> tuple[str, float, int]:
    options = {"temperature": 0.7, "top_p": 1.0, "top_k": 0, "repeat_penalty": 1.0}
    if num_gpu is not None:
        options["num_gpu"] = num_gpu
    payload = {"model": model, "stream": False, "options": options,
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": text}],
               "keep_alive": "5m"}
    if "hy-mt2" in model.lower() or "qwen3" in model:
        payload["think"] = False
    req = urllib.request.Request(f"{ENDPOINT}/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cost = time.perf_counter() - t0
    out = data.get("message", {}).get("content", "")
    for mark in JUNK:
        out = out.replace(mark, "")
    tokens = int(data.get("eval_count", 0))
    return out.strip(), cost, tokens


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--layers", nargs="*", type=int, default=[0, 8, 16, 24, 99])
    ap.add_argument("--reserve-asr", action="store_true",
                    help="先拉起 Qwen3-ASR 占住显存，测真实共存场景")
    args = ap.parse_args()

    if args.reserve_asr:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 工程根（app/ 的上级）
        from app import live_engine as le
        from app.config import settings
        settings._data.update({"live_model_preset": "accurate"})
        le.kill(); time.sleep(1)
        le.start_preload()
        log = le.paths()[0]
        for _ in range(90):
            time.sleep(2)
            if "MODEL_PRELOADED" in log.read_text(encoding="utf-8", errors="replace"):
                break
        print(f"ASR 已常驻，显存占用 {vram_used_mb()} MB\n")

    base_vram = vram_used_mb()
    print(f"起始显存 {base_vram} MB\n{'=' * 92}")
    for layers in args.layers:
        unload(args.model)
        label = "全部层" if layers >= 99 else f"{layers} 层"
        # 首句包含加载时间，单独记；后续句子算稳态速度
        try:
            _out, load_cost, _tk = translate(args.model, LINES[0], layers)
        except Exception as exc:  # noqa: BLE001
            print(f"[GPU {label:8s}] ✗ {str(exc)[:120]}")
            continue
        peak = vram_used_mb()
        total, tokens = 0.0, 0
        outs = []
        for line in LINES[1:]:
            out, cost, tk = translate(args.model, line, layers)
            outs.append(out.replace("\n", " "))
            total += cost
            tokens += tk
        n = len(LINES) - 1
        print(f"\n[GPU {label:8s}] 首句(含加载) {load_cost:5.1f}s ｜ "
              f"稳态 {total / n:4.2f}s/句 ｜ {tokens / max(total, 0.01):5.1f} tok/s ｜ "
              f"显存 {peak} MB（净增 {peak - base_vram} MB）")
        for src, out in zip(LINES[1:], outs):
            print(f"    {src[:38]:40s} → {out[:60]}")
    unload(args.model)


if __name__ == "__main__":
    main()
