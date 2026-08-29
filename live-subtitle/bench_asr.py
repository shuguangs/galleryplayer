"""ASR 引擎基准：加载时间 / 推理时间 / 准确率（CER、WER）。

每个引擎在独立子进程跑（--engine），避免相互影响显存与加载计时。
    python bench_asr.py --engine whisper-medium
    python bench_asr.py --engine sensevoice
    python bench_asr.py --engine qwen

样本：samples/asr_{zh,ja,en}.wav（TTS 合成，文本已知 → 可算错误率），
外加 pitt_s02e01_60s.wav（真实影视音频，仅看速度与输出质量）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SAMPLES = BASE / "samples"

# TTS 合成时的原文（准确率参考答案）
REFS = {
    "zh": "大家好，今天我们来聊一聊语音识别技术的发展。从早期的统计模型，到现在的深度学习，"
          "识别准确率已经大幅提升。现在的系统不仅可以识别普通话，还能听懂粤语和四川话等方言。"
          "在实际应用中，字幕生成、会议记录、视频检索都离不开这项技术。"
          "二零二六年八月二十八日，星期五，天气晴。",
    "ja": "皆さん、こんにちは。今日は音声認識技術についてお話しします。人工知能の進歩により、"
          "音声から文字への変換精度は大きく向上しました。現在のシステムは、日本語、英語、"
          "中国語など多くの言語を認識できます。字幕の自動生成や会議の記録など、"
          "様々な場面で活用されています。今後の発展が期待されます。ありがとうございました。",
    "en": "Hello everyone. Today we are talking about the progress of automatic speech recognition. "
          "Modern systems can transcribe speech in many languages with high accuracy. "
          "Subtitle generation, meeting notes, and voice search all depend on this technology. "
          "August twenty eighth, twenty twenty six. Thank you for listening.",
}

# HF 缓存统一指到引擎目录（whisper 模型在里面）
_cache = BASE / "models" / "hf" / "hub"
if _cache.is_dir():
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(_cache)
os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _enable_ctranslate2_cuda_path() -> None:
    """ctranslate2(faster-whisper) 需要的 cu12 nvidia DLL 路径。

    仅限 whisper 引擎注入：qwen 用 torch 自带的 cu13 DLL，
    旧 cuDNN 出现在 PATH 会造成 SUBLIBRARY_VERSION_MISMATCH。
    """
    nv = BASE / ".venv" / "Lib" / "site-packages" / "nvidia"
    if nv.is_dir():
        os.environ["PATH"] = (os.pathsep.join(str(nv / d / "bin")
                                              for d in ("cublas", "cudnn", "cuda_nvrtc"))
                              + os.pathsep + os.environ.get("PATH", ""))


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num_value(s: str) -> int | None:
    """中文数字串（如 二零二六、二十八）→ 数值；解析失败返回 None。"""
    if not s or any(ch not in _CN_DIGITS and ch not in "十百千" for ch in s):
        return None
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = num * 10 + _CN_DIGITS[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        elif ch == "千":
            total += (num or 1) * 1000
            num = 0
    return total + num


_NUM_SEQ = re.compile(r"[0-9]+|[零〇一二两三四五六七八九十百千]+")
_NUM_YEAR = re.compile(r"[0-9]{4}年")


def _canon_numbers(text: str) -> str:
    """数字按数值统一（二零二六 → 2026），识别错数字才计错，书写形式差异不计。"""
    def repl(m: re.Match) -> str:
        s = m.group(0)
        if s.isdigit():
            return str(int(s))
        val = _cn_num_value(s)
        return str(val) if val is not None else s
    return _NUM_SEQ.sub(repl, text)


def _norm_cer(text: str) -> str:
    """中日文：数字按数值统一，去标点空白后按字比较。"""
    return re.sub(r"[\s\W]+", "", _canon_numbers(text), flags=re.UNICODE)


def cer(ref: str, hyp: str) -> float:
    r, h = _norm_cer(ref), _norm_cer(hyp)
    if not r:
        return 0.0
    return _levenshtein(r, h) / len(r)


_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirtieth",
    "twenty-eighth", "twenty-eighth", "twenty-six", "twentieth", "th",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "twenty", "six", "twenty", "sixth",
}


def wer(ref: str, hyp: str) -> float:
    def words(t: str) -> list[str]:
        toks = re.sub(r"[^\w\s]", " ", t.lower()).split()
        # 剔除纯数字与数词：书写格式差异不算识别错误
        return [w for w in toks if not w.isdigit() and w not in _NUMBER_WORDS
                and not re.fullmatch(r"\d+(st|nd|rd|th)", w)]
    r, h = words(ref), words(hyp)
    if not r:
        return 0.0
    return _levenshtein(" ".join(r), " ".join(h)) / len(r)


def strip_sv_tags(text: str) -> str:
    """SenseVoice 输出带 <|zh|><|NEUTRAL|><|woitn|> 等标记，去掉。"""
    return re.sub(r"<\|[^|]*\|>", "", text).strip()


def load_whisper(name: str):
    t0 = time.perf_counter()
    from faster_whisper import WhisperModel
    model = WhisperModel(name, device="cuda", compute_type="int8")
    return model, time.perf_counter() - t0


def run_whisper(model, wav: Path) -> tuple[str, str]:
    segments, info = model.transcribe(str(wav), language=None, beam_size=5)
    text = "".join(s.text for s in segments).strip()
    return info.language, text


def load_sensevoice():
    t0 = time.perf_counter()
    import asr_engines  # 同目录：load_sensevoice 自带中文路径 junction 处理

    return asr_engines.load_sensevoice("cuda", print), time.perf_counter() - t0


def run_sensevoice(model, wav: Path) -> tuple[str, str]:
    res = model.generate(input=str(wav), language="auto", use_itn=True)
    text = strip_sv_tags(res[0]["text"])
    return "auto", text


def load_qwen():
    t0 = time.perf_counter()
    import sys as _sys
    import types as _types
    try:
        import nagisa  # noqa: F401
    except Exception:
        # nagisa（仅对齐器用）在中文路径 venv 里加载失败 → 垫桩，ASR 不受影响
        stub = _types.ModuleType("nagisa")
        stub.tagging = lambda *a, **k: None
        stub.Tagger = object
        _sys.modules["nagisa"] = stub
    import torch
    from qwen_asr import Qwen3ASRModel
    model = Qwen3ASRModel.from_pretrained(
        str(BASE / "models" / "models" / "Qwen3-ASR-1.7B"),
        dtype=torch.bfloat16, device_map="cuda:0", max_new_tokens=512,
    )
    return model, time.perf_counter() - t0


def run_qwen(model, wav: Path) -> tuple[str, str]:
    results = model.transcribe(audio=str(wav), language=None)
    return results[0].language, results[0].text.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True,
                    choices=("whisper-medium", "whisper-small", "sensevoice", "qwen"))
    args = ap.parse_args()

    if args.engine.startswith("whisper"):
        _enable_ctranslate2_cuda_path()

    loader = {
        "whisper-medium": lambda: load_whisper("medium"),
        "whisper-small": lambda: load_whisper("small"),
        "sensevoice": load_sensevoice,
        "qwen": load_qwen,
    }[args.engine]
    runner = {
        "whisper-medium": run_whisper,
        "whisper-small": run_whisper,
        "sensevoice": run_sensevoice,
        "qwen": run_qwen,
    }[args.engine]

    print(f"[{args.engine}] 加载中 ...", flush=True)
    model, load_s = loader()
    print(f"[{args.engine}] 加载完成 {load_s:.1f}s", flush=True)

    for key in ("zh", "ja", "en"):
        wav = SAMPLES / f"asr_{key}.wav"
        if not wav.is_file():
            print(f"!! 缺样本 {wav}")
            continue
        t0 = time.perf_counter()
        try:
            lang, text = runner(model, wav)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"engine": args.engine, "lang": key,
                              "error": str(exc)[:200]}, ensure_ascii=False))
            continue
        dur = time.perf_counter() - t0
        err = cer(REFS[key], text) if key in ("zh", "ja") else wer(REFS[key], text)
        print(json.dumps({
            "engine": args.engine, "lang": key, "detected": lang,
            "seconds": round(dur, 2), "error_rate": round(err, 4),
            "text": text[:400],
        }, ensure_ascii=False), flush=True)

    pitt = SAMPLES / "pitt_s02e01_60s.wav"
    if pitt.is_file():
        t0 = time.perf_counter()
        try:
            lang, text = runner(model, pitt)
            print(json.dumps({"engine": args.engine, "lang": "pitt(real en)",
                              "detected": lang,
                              "seconds": round(time.perf_counter() - t0, 2),
                              "text": text[:300]}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"engine": args.engine, "lang": "pitt",
                              "error": str(exc)[:200]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
