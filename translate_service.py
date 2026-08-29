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
    "1. 说人话——用中文影视字幕的口语腔，别留直译痕迹和英式/日式语序；\n"
    "2. 习语按中文习惯意译（如 call it 在急救语境是「宣布死亡」，不是「叫它」）；\n"
    "3. 保留人名、数字、专业术语；语气词、反讽、粗话都要译出来，不要净化；\n"
    "4. 一行台词一行译文，长度接近原文；\n"
    "5. 只输出译文，不要解释、不要注音、不要重复原文。"
)

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
                 context_lines: int = 3, timeout: int = 120):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.target = target
        self.context_lines = max(0, context_lines)
        self.timeout = timeout
        self._recent: list[str] = []

    def reset(self) -> None:
        """换片/seek 跳转后清空上下文，避免拿上一段的剧情理解当前台词。"""
        self._recent.clear()

    def _payload(self, text: str) -> bytes:
        target = TARGET_NAMES.get(self.target, self.target)
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
                {"role": "system", "content": SYSTEM_PROMPT.format(target=target)},
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

    def __init__(self, target: str = "zh", context_lines: int = 3, timeout: int = 300):
        self.target = target
        self.context_lines = max(0, context_lines)
        self.timeout = timeout
        self._recent: list[str] = []

    def reset(self) -> None:
        self._recent.clear()

    def __call__(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        target = TARGET_NAMES.get(self.target, self.target)
        user = text
        if self.context_lines and self._recent:
            prev = chr(10).join(self._recent[-self.context_lines:])
            header = chr(10).join(["【前文（仅供理解剧情，不要翻译）】", prev, "",
                           "【要翻译的这一句】", text])
            user = header
        body = json.dumps({
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(target=target)},
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


def write_srt_file(path, rows: list[tuple[float, float, str, str]]) -> None:
    """双语 SRT 写出（zh 空则只写原文行）。"""
    def ts(value: float) -> str:
        h, rest = divmod(int(value * 1000), 3600000)
        m, rest = divmod(rest, 60000)
        sec, ms = divmod(rest, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    parts: list[str] = []
    for index, (start, end, original, translated) in enumerate(rows, 1):
        parts.append(f"{index}\n{ts(start)} --> {ts(end)}\n{original}")
        if translated:
            parts.append(translated)
        parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")
