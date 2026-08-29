"""翻译模型对比：同一批字幕行 × 多个 Ollama 模型 × 两种 prompt。

用法：
    python bench_translate.py                      # 跑所有已装候选
    python bench_translate.py --models qwen2.5:7b kaelri/hy-mt2:7b-q4_K_M

对比维度：
    1) 模型：qwen2.5:7b（现用）vs HY-MT2（翻译专精）vs translategemma 等
    2) prompt：原英文 prompt（要求直译式"only the translation"）
       vs 中文口语化 prompt（明确要求影视字幕腔、避免直译）
    3) 上下文：逐句独立 vs 带前文（字幕连贯性）
    4) 延迟：每句耗时（实时字幕能否跟上）

测试句刻意选了直译会很别扭的口语/习语/省略句。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

ENDPOINT = "http://127.0.0.1:11434"

# (语言, 原文, 直译陷阱说明)
CASES = [
    ("en", "She's probably going to grow to hate her.", "will grow to 的意译"),
    ("en", "Off the chest, Ogilvy. Get ready to step up here, Joy.", "急救口令，直译会莫名"),
    ("en", "Algorithm's not working. Faster and deeper, please.", "省略主语的命令句"),
    ("en", "You need a break, Joy? No, I need an attending.", "反讽/职场语气"),
    ("en", "We're calling it. Sorry, the patient's dead.", "call it = 宣布死亡（习语）"),
    ("en", "When she started talking about AI, I started thinking about robots "
           "and kind of stopped listening.", "kind of 的语气词"),
    ("en", "Cut me some slack, would you? I've been on my feet for 12 hours.", "两个习语连用"),
    ("ja", "皆さん、こんにちは。今日は音声認識技術についてお話しします。", "礼貌体开场"),
    ("ja", "しょうがないな、今回だけだぞ。", "口语让步语气"),
    ("ja", "そんなことより、早く帰ろうよ。", "话题转换的口语"),
    ("ja", "彼のことが気になって、夜も眠れないんだ。", "気になる 的多义"),
]

PROMPT_OLD = ("You are a professional subtitle translator. Translate into zh. "
              "Output ONLY the translation, keep names/numbers, no explanations.")

PROMPT_NEW = (
    "你是资深影视字幕译者，把台词翻成简体中文。要求：\n"
    "1. 说人话——用中文影视字幕的口语腔，别留直译痕迹和英式/日式语序；\n"
    "2. 习语按中文习惯意译（如 call it 在急救语境是「宣布死亡」，不是「叫它」）；\n"
    "3. 保留人名、数字、专业术语；语气词、粗话、反讽都要译出来；\n"
    "4. 长度接近原文，一行台词一行译文；\n"
    "5. 只输出译文，不要解释、不要注音、不要原文。"
)

# 控制 token 泄漏清理（HY-MT2 用 <｜hy_User｜> 这类非常规 token）
JUNK = ("<|END_OF_TURN_TOKEN|>", "<|end_of_turn|>", "<|im_end|>", "<|endoftext|>",
        "<suggested_response>", "</suggested_response>", "<｜hy_Assistant｜>",
        "<｜hy_User｜>", "<source>", "</source>")


def chat(model: str, system: str, user: str, timeout: int = 180) -> tuple[str, float]:
    payload = {
        "model": model, "stream": False,
        "options": {"temperature": 0.7, "top_p": 0.6, "top_k": 20,
                    "repeat_penalty": 1.05},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    # qwen3 等带思考模式的模型：字幕翻译不需要思维链（否则每句多等几秒）
    if "qwen3" in model or "hy-mt2" in model.lower():
        payload["think"] = False
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{ENDPOINT}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))["message"]["content"]
    cost = time.perf_counter() - t0
    for mark in JUNK:
        out = out.replace(mark, "")
    return out.strip(), cost


def installed_models() -> list[str]:
    with urllib.request.urlopen(f"{ENDPOINT}/api/tags", timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [m["name"] for m in data.get("models", [])]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--prompt", choices=("old", "new", "both"), default="both")
    args = ap.parse_args()

    available = installed_models()
    models = args.models or available
    missing = [m for m in models if m not in available]
    if missing:
        print(f"!! 未安装（先 ollama pull）: {missing}")
        models = [m for m in models if m in available]

    prompts = {"old": PROMPT_OLD, "new": PROMPT_NEW}
    keys = ("old", "new") if args.prompt == "both" else (args.prompt,)

    stats: dict[tuple[str, str], list[float]] = {}
    for lang, text, note in CASES:
        print(f"\n{'=' * 78}\n[{lang}] {text}\n  （难点：{note}）")
        for model in models:
            for key in keys:
                try:
                    out, cost = chat(model, prompts[key], text)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {model:32s} {key:3s} ✗ {str(exc)[:60]}")
                    continue
                stats.setdefault((model, key), []).append(cost)
                one_line = " ⏎ ".join(out.splitlines())
                print(f"  {model:32s} {key:3s} {cost:5.1f}s  {one_line}")

    print(f"\n{'=' * 78}\n平均延迟（每句）：")
    for (model, key), costs in sorted(stats.items()):
        print(f"  {model:32s} {key:3s} {sum(costs) / len(costs):5.2f}s"
              f"（{len(costs)} 句）")


if __name__ == "__main__":
    main()
