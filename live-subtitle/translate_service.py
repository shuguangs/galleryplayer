"""字幕翻译（Ollama 本地模型）——共享实现。

三个入口（live_transcribe / live_capture / live_translate）共用，避免 prompt
和 token 清理逻辑各写一份漂移。

实测要点（2026-08-28，见 bench_translate.py / bench_context.py）：
- **prompt 的影响不比换模型小**：原来的英文 prompt 只说 "Output ONLY the
  translation"，没有风格要求，同一个模型就会输出"算法不起作用。更快更深，请。"
  这类直译腔；改成中文的、明确要求影视字幕口语腔后变成"算法不行，再快点"。
- **前文上下文几乎不花钱**：带前 3 句原文（只作理解、不翻译）质量明显更好，
  实测每句延迟没有增加（KV 缓存复用），"Off the chest"这类靠场景才能判断的
  台词只有带上下文才译对。
- 批量编号翻译（一次 8 句）会改行数、破坏时间戳对齐，不采用。

另有 LlamaServerTranslator：llama.cpp 后端（HY-MT2-30B 本地 GGUF），
按需启动 llama-server、SRT 任务结束即关，不参与实时字幕预载。
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
LLAMA_SERVER = BASE / "llamacpp" / "llama-server.exe"
LLAMA_MODEL = BASE / "models" / "gguf" / "Hy-MT2-30B-A3B-APEX-Imatrix-I-Nano.gguf"
LLAMA_PORT = 8020

SYSTEM_PROMPT = (
    "你是资深影视字幕译者，把台词翻成{target}。要求：\n"
    "1. 说人话——{style}；\n"
    "2. 习语按{target}的习惯意译（如 call it 在急救语境是「宣布死亡」，不是「叫它」）；\n"
    "3. 保留人名、数字、专业术语；语气词、反讽、粗话都要译出来，不要净化；\n"
    "4. 一行台词一行译文，长度接近原文；\n"
    "5. 只输出译文，不要解释、不要注音、不要重复原文。"
)
# 目标语言不是中文时，第 1 条不能再要求"中文口语腔/别留英式语序"——那是
# 与输出语言直接矛盾的指令，会诱导模型输出中文或中式英文
STYLE_HINTS = {
    "zh": "用中文影视字幕的口语腔，别留直译痕迹和英式/日式语序",
    "zh-Hant": "用中文影视字幕的口语腔，别留直译痕迹和英式/日式语序",
}
STYLE_DEFAULT = "用目标语言影视字幕的口语腔，别留逐字直译的痕迹"


def system_prompt(target: str, scenario: str = "general") -> str:
    """按目标语言 + 内容场景组装系统提示（target 为 zh/zh-Hant/en 等设置值）。

    scenario 见 SCENARIO_HINTS；general（通用影视）不追加场景段。
    中文特有的词表/例子（SCENARIO_ZH_EXTRA）只在目标语言是中文时追加——
    否则会出现"翻成 English"却要求 dick→鸡巴 这种自相矛盾的指令。
    """
    name = TARGET_NAMES.get(target, target)
    prompt = SYSTEM_PROMPT.format(
        target=name, style=STYLE_HINTS.get(str(target), STYLE_DEFAULT)
    )
    scenario = str(scenario)
    allowed_targets = SCENARIO_TARGETS.get(scenario)
    if allowed_targets is not None and str(target) not in allowed_targets:
        hint = None
    else:
        hint = SCENARIO_HINTS.get(scenario)
    if hint:
        if str(target).startswith("zh"):
            hint += SCENARIO_ZH_EXTRA.get(str(scenario), "")
        # 第 7 条是所有场景共用的护栏：实测发现场景段会诱导模型"发挥"——
        # blog 场景漏掉半句、anime 场景把 Nani 译成「喂」并自创战力译名
        prompt += ("\n6. 场景补充——" + hint
                   + "\n7. 场景补充只改变用词与语域，不得增删信息、不得改写原意。")
    return prompt

# 内容场景提示词组：按片源类型微调语气与术语策略（设置界面可选）。
# 原则一：场景补充只调整"译文的语域与术语策略"，与第 3 条"粗话要译出来，
# 不要净化"叠加而非冲突——nsfw 是把它推向粗俗对等，meeting 是推向正式。
# 原则二：正文必须与目标语言无关（说"目标语言里真的会说的词"而不是列中文词），
# 中文词表放 SCENARIO_ZH_EXTRA，只在 zh/zh-Hant 目标下追加。
# 内容场景提示词从 scenarios/*.json 加载。每个 JSON 文件对应一个设置选项；
# 私有场景文件可以留在本地而不进入公开仓库。
# 相对当前引擎目录定位，不依赖运行时工作目录或固定盘符。
SCENARIO_RELATIVE_DIR = Path("scenarios")
SCENARIO_DIR = BASE / SCENARIO_RELATIVE_DIR


def _load_scenarios() -> dict[str, dict[str, object]]:
    scenarios: dict[str, dict[str, object]] = {}
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("key") or path.stem).strip()
        hint = str(payload.get("hint") or "").strip()
        label = payload.get("label")
        if not key or not isinstance(label, dict):
            continue
        item: dict[str, object] = {
            "key": key,
            "order": int(payload.get("order", 1000)),
            "label": {str(k): str(v) for k, v in label.items()},
            "hint": hint,
        }
        targets = payload.get("targets")
        if isinstance(targets, list):
            item["targets"] = {str(target) for target in targets if str(target).strip()}
        zh_extra = str(payload.get("zh_extra") or "").strip()
        if zh_extra:
            item["zh_extra"] = zh_extra
        scenarios[key] = item
    return dict(sorted(scenarios.items(),
                       key=lambda pair: (int(pair[1]["order"]), pair[0])))


SCENARIOS = _load_scenarios()
SCENARIO_HINTS = {key: str(item["hint"]) for key, item in SCENARIOS.items()}
SCENARIO_TARGETS = {
    key: set(item["targets"])
    for key, item in SCENARIOS.items()
    if "targets" in item
}
SCENARIO_ZH_EXTRA = {
    key: str(item["zh_extra"])
    for key, item in SCENARIOS.items()
    if item.get("zh_extra")
}
SCENARIO_LABELS = {
    key: dict(item["label"])
    for key, item in SCENARIOS.items()
}
SCENARIO_DEFAULT = "general" if "general" in SCENARIOS else next(iter(SCENARIOS), "general")

# 各家模型泄漏的控制 token（aya 的 turn marker、HY-MT2 的 hy_ 系列等）
JUNK_TOKENS = (
    "<|END_OF_TURN_TOKEN|>", "<|end_of_turn|>", "<|im_end|>", "<|endoftext|>",
    "<suggested_response>", "</suggested_response>",
    "<｜hy_Assistant｜>", "<｜hy_User｜>", "<source>", "</source>",
    "<think>", "</think>",
)

TARGET_NAMES = {"zh": "简体中文", "zh-Hant": "繁体中文", "en": "English"}

# 带思考模式的模型：字幕翻译不需要思维链（每句会多等几秒）
NO_THINK_HINTS = ("qwen3", "hy-mt2", "hy_mt2", "glm", "deepseek-r1")


def clean_output(text: str) -> str:
    for mark in JUNK_TOKENS:
        text = text.replace(mark, "")
    # 模型偶尔会把整句用引号包起来（translategemma 常见）
    text = text.strip()
    if len(text) > 2 and text[0] in "“\"「" and text[-1] in "”\"」":
        text = text[1:-1].strip()
    return text


class Translator:
    """逐句翻译，内部维护前文窗口（原文），供模型理解上下文。

    context_lines=0 关闭上下文（回到逐句独立，用于对照或极端省算力场景）。
    """

    def __init__(self, endpoint: str, model: str, target: str = "zh",
                 context_lines: int = 3, timeout: int = 120,
                 scenario: str = SCENARIO_DEFAULT):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.target = target
        self.scenario = scenario
        self.context_lines = max(0, context_lines)
        self.timeout = timeout
        self._recent: list[str] = []

    def reset(self) -> None:
        """换片/seek 跳转后清空上下文，避免拿上一段的剧情理解当前台词。"""
        self._recent.clear()

    def _payload(self, text: str) -> bytes:
        user = text
        if self.context_lines and self._recent:
            prev = "\n".join(self._recent[-self.context_lines:])
            user = (f"【前文（仅供理解剧情，不要翻译）】\n{prev}\n\n"
                    f"【要翻译的这一句】\n{text}")
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0.7, "top_p": 0.6, "top_k": 20,
                        "repeat_penalty": 1.05},
            "messages": [
                {"role": "system", "content": system_prompt(self.target, self.scenario)},
                {"role": "user", "content": user},
            ],
        }
        if any(h in self.model.lower() for h in NO_THINK_HINTS):
            payload["think"] = False
        return json.dumps(payload).encode("utf-8")

    def __call__(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        req = urllib.request.Request(
            f"{self.endpoint}/api/chat", data=self._payload(text),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = clean_output(data.get("message", {}).get("content", ""))
        if self.context_lines:
            self._recent.append(text)
            del self._recent[:-self.context_lines]
        return out

# ---------------------------------------------------------------- llama.cpp
_llama_proc: subprocess.Popen | None = None


def llama_server_running() -> bool:
    """llama-server 健康探测（/health 200 即在跑，不区分是谁启动的）。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_llama_server(status=print, timeout: float = 180.0) -> bool:
    """按需启动 llama-server（HY-MT2-30B，MoE 专家留 CPU，显存 ~4GB 可与 ASR 共存）。

    已在跑则直接复用；启动后轮询 /health 就绪。
    """
    global _llama_proc
    if llama_server_running():
        return True
    if not LLAMA_SERVER.is_file() or not LLAMA_MODEL.is_file():
        status(f"✗ llama.cpp 或 HY-MT2-30B 模型未安装（{LLAMA_SERVER.parent.name}/）")
        return False
    # -ngl 99 --n-cpu-moe 30：注意力/共享层上 GPU，大块专家权重留 CPU
    # （实测 16 t/s，显存峰值 ~5GB，与 6GB 的 Qwen3-ASR 在 16GB 卡上共存）
    _llama_proc = subprocess.Popen(
        [str(LLAMA_SERVER), "-m", str(LLAMA_MODEL),
         "--port", str(LLAMA_PORT), "-c", "4096",
         "-ngl", "99", "--n-cpu-moe", "30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if llama_server_running():
            status("llama-server 就绪（HY-MT2-30B）")
            return True
        if _llama_proc.poll() is not None:
            status("✗ llama-server 启动即退出（显存不足？参数错误？）")
            return False
        time.sleep(1)
    status("✗ llama-server 启动超时")
    return False


def stop_llama_server() -> None:
    """SRT 任务结束即关（按需引入、不驻留）。"""
    global _llama_proc
    if _llama_proc is not None and _llama_proc.poll() is None:
        _llama_proc.terminate()
        try:
            _llama_proc.wait(timeout=10)
        except Exception:
            _llama_proc.kill()
    _llama_proc = None


class LlamaServerTranslator:
    """llama.cpp /v1/chat/completions 翻译（HY-MT2-30B）。

    接口与 Translator 一致（__call__/reset），SRT 任务可无缝替换。
    """

    def __init__(self, target: str = "zh", context_lines: int = 3, timeout: int = 300,
                 scenario: str = SCENARIO_DEFAULT):
        self.target = target
        self.scenario = scenario
        self.context_lines = max(0, context_lines)
        self.timeout = timeout
        self._recent: list[str] = []

    def reset(self) -> None:
        self._recent.clear()

    def __call__(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        user = text
        if self.context_lines and self._recent:
            prev = chr(10).join(self._recent[-self.context_lines:])
            header = chr(10).join(["【前文（仅供理解剧情，不要翻译）】", prev, "",
                           "【要翻译的这一句】", text])
            user = header
        body = json.dumps({
            "messages": [
                {"role": "system", "content": system_prompt(self.target, self.scenario)},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7, "top_p": 1.0, "top_k": 0,
            "repeat_penalty": 1.0, "max_tokens": 512,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{LLAMA_PORT}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        content = out.get("choices", [{}])[0].get("message", {}).get("content", "")
        if self.context_lines:
            self._recent.append(text)
            del self._recent[:-self.context_lines]
        return clean_output(content)


def merge_fragments(rows: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """whisper 碎片合并：相邻 ≤1.5s 且合计 ≤120 字符合并为一条可读字幕。

    仅 whisper 路径需要（qwen/sensevoice 已按标点分好句）。
    """
    out: list[tuple[float, float, str]] = []
    for start, end, text in rows:
        if out and start - out[-1][1] <= 1.5 and len(out[-1][2]) + len(text) <= 120:
            o0, o1, otext = out[-1]
            out[-1] = (o0, max(o1, end), (otext + " " + text).strip())
        else:
            out.append((start, end, text))
    return out


def write_srt_file(path, rows: list[tuple[float, float, str, str]],
                   fmt: str = "srt") -> None:
    """双语字幕写出（译文空则只写原文行）。fmt: srt / vtt / ass。"""
    def ts_srt(value: float) -> str:
        h, rest = divmod(int(value * 1000), 3600000)
        m, rest = divmod(rest, 60000)
        sec, ms = divmod(rest, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    def ts_vtt(value: float) -> str:
        h, rest = divmod(int(value * 1000), 3600000)
        m, rest = divmod(rest, 60000)
        sec, ms = divmod(rest, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"

    def ts_ass(value: float) -> str:
        # ASS 时间精度 0.01s：H:MM:SS.CC
        h, rest = divmod(int(value * 100), 360000)
        m, rest = divmod(rest, 6000)
        sec, cs = divmod(rest, 100)
        return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"

    fmt = (fmt or "srt").lower()
    if fmt == "vtt":
        parts = ["WEBVTT", ""]
        for start, end, original, translated in rows:
            text = original if not translated else f"{original}\n{translated}"
            parts.append(f"{ts_vtt(start)} --> {ts_vtt(end)}")
            parts.append(text)
            parts.append("")
    elif fmt == "ass":
        header = (
            "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
            "BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
            "Style: Sub,Microsoft YaHei,48,&H00FFFFFF,&H00000000,&H7F000000,0,2,1,2,60,60,36\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        )
        lines = []
        for start, end, original, translated in rows:
            text = original if not translated else f"{original}\\N{translated}"
            text = text.replace("\n", "\\N")
            lines.append(f"Dialogue: 0,{ts_ass(start)},{ts_ass(end)},Sub,{text}")
        parts = [header, *lines]
    else:  # srt（默认，保持历史行为）
        parts = []
        for index, (start, end, original, translated) in enumerate(rows, 1):
            parts.append(f"{index}\n{ts_srt(start)} --> {ts_srt(end)}\n{original}")
            if translated:
                parts.append(translated)
            parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")
