"""字幕上下文方案对比：逐句独立 vs 带前文 vs 整段批量。

字幕"死板"的一半原因不在模型，而在逐句独立翻译时模型看不见上下文，
只能字面直译。这里用一段连续台词实测三种喂法的差别。

用法：
    python bench_context.py --model qwen3:8b
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

ENDPOINT = "http://127.0.0.1:11434"

SYSTEM = (
    "你是资深影视字幕译者，把台词翻成简体中文。要求：\n"
    "1. 说人话——用中文影视字幕的口语腔，别留直译痕迹和英式/日式语序；\n"
    "2. 习语按中文习惯意译（如 call it 在急救语境是「宣布死亡」）；\n"
    "3. 保留人名、数字、专业术语；语气词、反讽都要译出来；\n"
    "4. 一行台词一行译文，长度接近原文；\n"
    "5. 只输出译文，不要解释、不要原文。"
)

# The Pitt S02E01 连续台词（代词指代、话轮衔接密集，最考验上下文）
LINES = [
    "She's probably going to grow to hate her.",
    "What's her name again?",
    "Dr. Baran Alhashimi.",
    "She's some sort of clinical informatics expert.",
    "When she started talking about AI, I started thinking about robots "
    "and kind of stopped listening.",
    "Clear. Off the chest, Ogilvy.",
    "Still VTAC, but it looks weird.",
    "Agreed. No pulse.",
    "We haven't considered all the T's.",
    "What if it's thrombosis?",
    "We are sticking with the algorithm for now.",
    "Algorithm's not working. Faster and deeper, please.",
    "You need a break, Joy?",
    "No, I need an attending.",
    "Stop compressions. We're calling it.",
]


def chat(model: str, messages: list[dict], timeout: int = 300) -> tuple[str, float]:
    payload = {"model": model, "stream": False, "messages": messages,
               "options": {"temperature": 0.7, "top_p": 0.6, "top_k": 20,
                           "repeat_penalty": 1.05}}
    if "qwen3" in model or "hy-mt2" in model.lower():
        payload["think"] = False
    req = urllib.request.Request(f"{ENDPOINT}/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))["message"]["content"]
    return out.strip(), time.perf_counter() - t0


def run_isolated(model: str) -> tuple[list[str], float]:
    rows, total = [], 0.0
    for line in LINES:
        out, cost = chat(model, [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": line}])
        rows.append(out.replace("\n", " "))
        total += cost
    return rows, total


def run_with_context(model: str, window: int = 3) -> tuple[list[str], float]:
    """前 window 句原文作为上下文，只翻当前句（延迟与逐句相当）。"""
    rows, total = [], 0.0
    for i, line in enumerate(LINES):
        prev = LINES[max(0, i - window):i]
        user = (("【前文（仅供理解，不要翻译）】\n" + "\n".join(prev) + "\n\n") if prev else "") \
            + "【要翻译的这一句】\n" + line
        out, cost = chat(model, [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": user}])
        rows.append(out.replace("\n", " "))
        total += cost
    return rows, total


def run_batch(model: str, size: int = 8) -> tuple[list[str], float]:
    """整段编号批量翻译（后台 SRT 可用；实时字幕不适合——要等一整批）。"""
    rows, total = [], 0.0
    for start in range(0, len(LINES), size):
        chunk = LINES[start:start + size]
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk))
        user = ("按编号逐行翻译下面的连续台词，保持编号和行数完全一致：\n" + numbered)
        out, cost = chat(model, [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": user}])
        total += cost
        got = [ln.split(".", 1)[-1].strip() for ln in out.splitlines() if ln.strip()]
        if len(got) != len(chunk):  # 行数不符 → 记下来（这是批量方案的主要风险）
            rows.extend(got + ["!! 行数不符"] * max(0, len(chunk) - len(got)))
        else:
            rows.extend(got)
    return rows[:len(LINES)], total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:8b")
    args = ap.parse_args()

    print(f"模型：{args.model}，{len(LINES)} 句连续台词\n")
    results = {}
    for name, fn in (("逐句独立", run_isolated),
                     ("带前文3句", run_with_context),
                     ("批量8句", run_batch)):
        rows, total = fn(args.model)
        results[name] = rows
        print(f"[{name}] 总耗时 {total:.1f}s（每句 {total / len(LINES):.2f}s）")

    print("\n" + "=" * 100)
    for i, src in enumerate(LINES):
        print(f"\n原文: {src}")
        for name, rows in results.items():
            text = rows[i] if i < len(rows) else "（缺）"
            print(f"  {name:8s} {text}")


if __name__ == "__main__":
    main()
