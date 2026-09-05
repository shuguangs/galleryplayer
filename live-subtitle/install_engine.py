"""一键安装字幕引擎：venv + 依赖 + ASR 模型 + Ollama 翻译模型。

用法（由播放器设置界面调用，也可命令行手动跑）：
    python install_engine.py --dir <引擎目录> [--model qwen] [--mirror hf-mirror]
                             [--translate qwen2.5:3b|qwen2.5:7b|none] [--skip-ollama]

引擎：
    qwen        Qwen3-ASR-1.7B（4.7GB，建议 6GB 显存）——52 语言，实测最准
    sensevoice  SenseVoice-small（0.9GB，2GB 显存）——中日韩粤快，英语弱
    tiny/base/small/medium/large-v3  faster-whisper 各档位

幂等：已存在的步骤自动跳过（venv/依赖/模型/配置）。每步进度打印到 stdout
（播放器设置界面捕获显示）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 播放器经 QProcess 管道捕获本脚本的输出：管道下 Python 默认用系统码页
# （中文 Windows = GBK）编码 stdout/stderr，而失败分支都打印 "✗" 等
# GBK 编不出的字符——直接 UnicodeEncodeError 崩溃，把真正的失败原因
# 盖住（用户实测：一键安装/SRT 安装结尾全是 illegal multibyte sequence）。
# 统一改用 UTF-8（播放器按 utf-8 解码，逐字对得上），错误字符 replace 兜底。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 输出流不可重配时静默——安装照跑，只是日志可能乱码
# 子进程（venv/pip/modelscope/ollama）继承同一约定：它们的输出汇进同
# 一条管道，也必须以 UTF-8 编码
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")

MODEL_CACHE_HINT = "（首次下载；已缓存则秒过）"

# run() 在"可执行文件根本不存在"时的返回码（区别于命令自身失败）
MISSING_EXE = 127

TORCH_ENGINES = ("qwen", "sensevoice")
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")

# funasr（qwen/sensevoice 的运行依赖）钉着 numpy<2，而 numpy 1.x 最后一版
# 1.26.4 只出到 cp312 轮子——在 Python 3.13 上 pip 只能现编译 numpy，必然
# 以 meson 报错收场。所以 torch 系引擎的 venv 必须用 3.10-3.12 建。
# whisper 档位不依赖 funasr，3.13 上完全正常。
FUNASR_PY_MIN = (3, 10)
FUNASR_PY_MAX = (3, 12)


def _probe_python(cmd: list[str]) -> tuple[int, int] | None:
    """跑一下候选解释器问版本；不可用返回 None（顺带滤掉商店 stub）。"""
    try:
        done = subprocess.run(
            [*cmd, "-c", "import sys;print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True, text=True, timeout=20, errors="replace")
    except Exception:
        return None
    if done.returncode != 0:
        return None
    try:
        major, minor = (int(x) for x in done.stdout.split()[:2])
    except Exception:
        return None
    return (major, minor)


def pick_venv_python(need_funasr: bool) -> tuple[list[str], tuple[int, int]] | None:
    """挑一个用来建 venv 的解释器。

    need_funasr（qwen/sensevoice）时必须落在 3.10-3.12：当前解释器合规就
    用它，否则按 3.12→3.11→3.10 找。找不到返回 None，由调用方给出人话
    提示——这一步必须在下载 3GB torch **之前**判掉。
    """
    cur = _probe_python([sys.executable])
    if cur and (not need_funasr or FUNASR_PY_MIN <= cur <= FUNASR_PY_MAX):
        return [sys.executable], cur
    if not need_funasr:
        return ([sys.executable], cur) if cur else None
    for minor in range(FUNASR_PY_MAX[1], FUNASR_PY_MIN[1] - 1, -1):
        for cmd in ([f"py", f"-3.{minor}"], [f"python3.{minor}"]):
            if cmd[0] == "python3." + str(minor) and not shutil.which(cmd[0]):
                continue
            if cmd[0] == "py" and not shutil.which("py"):
                continue
            ver = _probe_python(cmd)
            if ver and FUNASR_PY_MIN <= ver <= FUNASR_PY_MAX:
                return cmd, ver
    return None


def venv_python_version(venv_py: Path) -> tuple[int, int] | None:
    return _probe_python([str(venv_py)])


# ModelScope 仓库 ID（qwen/sensevoice/VAD 都走 modelscope，国内直连快）
MS_REPOS = {
    "qwen": ("Qwen/Qwen3-ASR-1.7B", "Qwen3-ASR-1.7B"),
    "sensevoice": ("iic/SenseVoiceSmall", "iic--SenseVoiceSmall"),
}
MS_VAD = ("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "fsmn-vad")
# llama.cpp（SRT 大模型翻译后端）：Windows CUDA 13.3 版 + HY-MT2-30B 模型
LLAMACPP_RELEASE = ("https://github.com/ggml-org/llama.cpp/releases/download",
                    "b10675/llama-b10675-bin-win-cuda-13.3-x64.zip",
                    "b10675/cudart-llama-bin-win-cuda-13.3-x64.zip")
HYMT2_GGUF = ("alphaZimuth/Hy-MT2-30B-A3B-APEX-GGUF",
              "Hy-MT2-30B-A3B-APEX-Imatrix-I-Nano.gguf", 11.59)
GGH = "https://gh-proxy.com/"  # GitHub 下载加速；失败时自动回落直连

# 翻译模型下载体积（仅用于提示；实际由 ollama pull 决定）
TRANSLATE_SIZES = {
    "qwen3:8b": "约 5.2GB", "translategemma:4b": "约 3.3GB",
    "qwen2.5:7b": "约 3.8GB", "qwen2.5:3b": "约 2.1GB",
    "aya-expanse:8b": "约 5.1GB",
}

# 各引擎磁盘/显存需求（打印给用户，与设置界面一致）
SPECS = {
    "qwen": (4.7, 6.0, "Qwen3-ASR-1.7B"),
    "sensevoice": (0.9, 2.0, "SenseVoice-small"),
    "large-v3": (5.8, 8.0, "Whisper large-v3"),
    "medium": (1.5, 4.0, "Whisper medium"),
    "small": (0.5, 2.0, "Whisper small"),
    "base": (0.15, 1.0, "Whisper base"),
    "tiny": (0.08, 1.0, "Whisper tiny"),
}


def log(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def _normalize_proxy(enable, server) -> str:
    """(ProxyEnable, ProxyServer) → "http://host:port"；未启用/空返回 ""。"""
    if not enable or not server:
        return ""
    if "=" in server:
        # "http=127.0.0.1:7890;https=127.0.0.1:7890" 按协议分开写的形式
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = parts.get("https") or parts.get("http") or ""
    if server and not server.startswith("http"):
        server = "http://" + server
    return server


def system_proxy() -> str:
    """Windows 系统代理（IE/WinINET 设置，注册表读取）；未启用返回 ""。

    用户网络环境各异：GitHub / HuggingFace 直连大多不通，但系统里通常
    已经挂着代理软件（Clash/v2rayN 等都会写 IE 代理设置）。curl/pip
    不会自己读 Windows 的这套设置，下载失败时由这里显式抓出来当兜底。
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            key.Close()
    except Exception:
        return ""
    return _normalize_proxy(enable, server)


def proxy_env(proxy: str) -> dict:
    """给子进程（pip/modelscope/HF）注入代理约定的大小写全套变量。"""
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
            "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy}


def run_proxy(cmd: list[str], env: dict | None = None, **kw) -> int:
    """直连失败且系统配置了代理时，自动带系统代理重试一次。

    用于长耗时下载步骤（pip / 模型）：直接失败后不立刻判死，抓系统
    代理再试一轮——用户报错的场景里"两个直连都下不动"多半就差这一步。
    """
    if run(cmd, env=env, **kw) == 0:
        return 0
    proxy = system_proxy()
    if not proxy:
        return 1
    log(f"直连失败，检测到系统代理 {proxy}，改走代理重试 ...")
    penv = proxy_env(proxy)
    if env:
        penv.update(env)   # HF_ENDPOINT 等业务变量优先于代理变量
    return run(cmd, env=penv, **kw)


def run(cmd: list[str], env: dict | None = None, **kw) -> int:
    log(f"> {' '.join(str(c) for c in cmd)}")
    env = {**os.environ, **(env or {})}
    try:
        return subprocess.call(cmd, env=env, **kw)
    except FileNotFoundError:
        # 全新电脑上很常见：没有 winget（LTSC/精简版/App Installer 缺失）、
        # 老 Win10 没有 curl.exe、winget 把 Ollama 装到了别的位置。
        # subprocess 直接抛 FileNotFoundError，不接住的话整场安装以一段
        # Python traceback 收尾，用户只看到"安装失败"却不知道缺什么。
        log(f"✗ 找不到可执行文件: {cmd[0]}（未安装，或不在 PATH 上）")
        return MISSING_EXE
    except OSError as exc:
        log(f"✗ 无法启动 {cmd[0]}: {exc}")
        return MISSING_EXE


def find_ollama() -> Path | None:
    """定位 ollama.exe：用户级安装 → PATH → 机器级安装。

    winget 装到哪取决于包的 scope（用户级 LOCALAPPDATA / 机器级
    Program Files），装完还不会刷新当前进程的 PATH——所以固定只看一个
    位置会在"其实已经装好"的机器上判成没装。
    """
    cands: list[Path] = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    found = shutil.which("ollama")
    if found:
        cands.append(Path(found))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var, "")
        if base:
            cands.append(Path(base) / "Ollama" / "ollama.exe")
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def ollama_ready(exe: Path, timeout: float = 20.0) -> bool:
    """Ollama 服务是否可用（list 能连上就算）。"""
    try:
        done = subprocess.run([str(exe), "list"], capture_output=True,
                              text=True, timeout=timeout, errors="replace")
        return done.returncode == 0
    except Exception:
        return False


def ensure_ollama_daemon(exe: Path, wait: float = 40.0) -> bool:
    """确保 Ollama 服务在跑：没跑就后台拉起 `ollama serve` 并等就绪。

    winget 静默装完的 Ollama 在当前会话里不会自动起服务（托盘 App 要
    重新登录才自启），而 `ollama pull` 必须连服务——不拉起的话新机器上
    翻译模型永远拉不下来，还会被误判成"拉取失败"去走 GGUF 镜像回退
    （默认的 qwen3:8b 没有镜像映射，直接判死）。
    """
    if ollama_ready(exe):
        return True
    log("Ollama 服务未运行，正在后台启动 ollama serve ...")
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    try:
        subprocess.Popen([str(exe), "serve"], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log(f"✗ 无法启动 Ollama 服务: {exc}")
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if ollama_ready(exe, timeout=5.0):
            log("Ollama 服务已就绪")
            return True
        time.sleep(1.0)
    return False


def gpu_info() -> tuple[str, float, float]:
    """(显卡名, 显存GB, 驱动支持的 CUDA 版本)；无 NVIDIA 卡返回 ("", 0, 0)。"""
    if not shutil.which("nvidia-smi"):
        return "", 0.0, 0.0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, errors="replace").stdout.strip()
        name, vram = out.splitlines()[0].split(",")
        head = subprocess.run(["nvidia-smi"], capture_output=True, text=True,
                              timeout=10, errors="replace").stdout
        cuda = 0.0
        marker = "CUDA Version"
        if marker in head:
            tail = head.split(marker, 1)[1].lstrip(": ")
            cuda = float(tail.split()[0])
        return name.strip(), float(vram) / 1024.0, cuda
    except Exception:
        return "", 0.0, 0.0


def torch_index(cuda_version: float) -> str | None:
    """按驱动支持的 CUDA 版本挑 PyTorch wheel 源；无 GPU 返回 None（CPU 版）。

    新卡（Blackwell/sm_120）必须 cu128 以上，否则算子跑不起来。
    """
    if cuda_version <= 0:
        return None
    if cuda_version >= 13.0:
        return "https://download.pytorch.org/whl/cu130"
    if cuda_version >= 12.8:
        return "https://download.pytorch.org/whl/cu128"
    return "https://download.pytorch.org/whl/cu126"


def ms_download(venv_py: Path, repo: str, target: Path, label: str) -> bool:
    """ModelScope 下载到固定目录（幂等：目录已有内容则跳过）。"""
    if target.is_dir() and any(target.rglob("*.pt")) or \
            (target.is_dir() and any(target.rglob("*.safetensors"))):
        log(f"{label} 已存在，跳过下载")
        return True
    log(f"下载 {label} {MODEL_CACHE_HINT}")
    code = (
        "from modelscope import snapshot_download as d\n"
        f"d({repo!r}, local_dir={str(target)!r})\n"
    )
    return run_proxy([str(venv_py), "-c", code]) == 0


def _curl(url: str, out: Path, min_bytes: int = 1_000_000) -> bool:
    """curl 下载：gh-proxy 加速 → 直连 → 系统代理重试（自动回落直连）。

    幂等只认"下完的最终文件"：先下到 .part，curl 正常退出且体积达标才改名。
    原实现按"文件够大"（阈值是期望大小的 95%）跳过下载——11.6GB 的模型下到
    96% 中断就会被永久当成"已下载"，llama-server 加载半截 GGUF 启动即退，
    而重装因幂等跳过下载永远修不好。顺带补上真续传（-C -）。
    """
    if out.is_file() and out.stat().st_size >= min_bytes:
        log(f"已存在，跳过下载: {out.name}")
        return True
    if not (shutil.which("curl.exe") or shutil.which("curl")):
        # Win10 1803+ 自带 curl.exe；缺它的多是精简/极老系统。提前给出
        # 明确结论，免得四轮尝试各抛一次"找不到可执行文件"
        log("✗ 系统缺少 curl.exe（Windows 10 1803 起自带）——"
            "请升级系统，或手动下载所需文件后放进引擎目录")
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    # gh-proxy 只代理 GitHub：给 HuggingFace 之类的 URL 套上去只会白试
    # 一轮 404（翻译模型的 GGUF 镜像回退就走 HF）
    bases = [GGH, ""] if "github.com" in url else [""]
    attempts = [(b, None) for b in bases]
    proxy = system_proxy()
    if proxy:
        attempts += [(b, proxy) for b in bases]   # 直连下不动 → 走系统代理
    for base, px in attempts:
        url2 = base + url if base else url
        via = f"（代理 {px}）" if px else ""
        log(f"下载 {out.name}（{url2[:80]}...）{via}")
        cmd = ["curl.exe", "-L", "--fail", "-o", str(part), url2]
        if part.is_file() and part.stat().st_size > 0:
            cmd = ["curl.exe", "-L", "--fail", "-C", "-", "-o", str(part), url2]
        if px:
            cmd += ["-x", px]
        code = run(cmd)
        if code in (22, 33, 36) and part.is_file():
            # 续传被拒（服务器不支持 Range，或 .part 已完整触发 416）：整份重下
            part.unlink(missing_ok=True)
            cmd = ["curl.exe", "-L", "--fail", "-o", str(part), url2]
            if px:
                cmd += ["-x", px]
            code = run(cmd)
        if code == 0 and part.is_file() and part.stat().st_size >= min_bytes:
            part.replace(out)
            return True
    return False


def install_llamacpp(root: Path, mirror: str) -> None:
    """llama.cpp 二进制 + HY-MT2-30B GGUF（引擎目录内，幂等）。"""
    hf = "https://hf-mirror.com" if mirror == "hf-mirror" else "https://huggingface.co"
    llamacpp = root / "llamacpp"
    server = llamacpp / "llama-server.exe"
    # 完成判据必须包含 CUDA 运行库：只看 llama-server.exe 时，cudart 包损坏
    # 导致的半截安装会被下次运行整块跳过，cudart64_*.dll 永久缺失
    if server.is_file() and any(llamacpp.glob("cudart*.dll")):
        log("llama.cpp 已存在，跳过")
    else:
        release, bins, cudart = LLAMACPP_RELEASE
        tmp = root / "llamacpp-dl"
        tmp.mkdir(parents=True, exist_ok=True)
        zips = []
        for name in (bins, cudart):
            z = tmp / name.split("/")[-1]
            if not _curl(f"{release}/{name}", z, min_bytes=50_000_000):
                log("✗ llama.cpp 下载失败（网络；可重试）")
                sys.exit(1)
            zips.append(z)
        import zipfile

        # 两个包全部校验通过再解压：原实现先解压 bins 再校验 cudart，cudart
        # 损坏时 llama-server.exe 已落地，下次运行就"已存在"跳过整块安装
        for z in zips:
            if not zipfile.is_zipfile(z):
                z.unlink(missing_ok=True)
                log("✗ 下载的压缩包损坏（已删除），请重试安装")
                sys.exit(1)
        for z in zips:
            log(f"解压 {z.name} ...")
            with zipfile.ZipFile(z) as zf:
                zf.extractall(llamacpp)
            z.unlink()
        tmp.rmdir()
        if not server.is_file() or not any(llamacpp.glob("cudart*.dll")):
            log("✗ 解压后缺少 llama-server.exe 或 CUDA 运行库")
            sys.exit(1)
        log("llama.cpp 就绪")

    gguf_dir = root / "models" / "gguf"
    repo, fname, size_gb = HYMT2_GGUF
    gguf = gguf_dir / fname
    if not _curl(f"{hf}/{repo}/resolve/main/{fname}", gguf,
                 min_bytes=int(size_gb * 0.95 * 1024 ** 3)):
        log("✗ HY-MT2-30B 模型下载失败（网络；可重试，已下载部分会续传）")
        sys.exit(1)
    log(f"HY-MT2-30B 模型就绪（{size_gb:g}GB）")


def main() -> None:
    ap = argparse.ArgumentParser(description="一键安装字幕引擎")
    ap.add_argument("--dir", default=str(Path.cwd()))
    ap.add_argument("--model", default="qwen",
                    choices=list(TORCH_ENGINES) + list(WHISPER_MODELS))
    ap.add_argument("--mirror", default="huggingface",
                    choices=["huggingface", "hf-mirror"])
    ap.add_argument("--translate", default="qwen3:8b",
                    help="翻译模型（Ollama）：qwen3:8b / translategemma:4b / "
                         "qwen2.5:7b / qwen2.5:3b / aya-expanse:8b / none")
    ap.add_argument("--skip-ollama", action="store_true", dest="skip_ollama",
                    help="跳过 Ollama 安装/模型下载")
    ap.add_argument("--llamacpp-only", action="store_true", dest="llamacpp_only",
                    help="只装 SRT 大模型翻译（llama.cpp + HY-MT2-30B），其余跳过")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    venv_py = root / ".venv" / "Scripts" / "python.exe"
    hf_endpoint = "https://hf-mirror.com" if args.mirror == "hf-mirror" else "https://huggingface.co"
    disk_gb, vram_need, label = SPECS.get(args.model, (0.0, 0.0, args.model))
    log(f"引擎目录: {root}")
    log(f"识别引擎: {label}（模型 {disk_gb:g}GB，建议显存 {vram_need:g}GB）")

    # ------------------------------------------------- 0. 环境体检
    gpu_name, vram_gb, cuda_ver = gpu_info()
    if gpu_name:
        log(f"GPU: {gpu_name}（{vram_gb:.1f}GB 显存，驱动支持 CUDA {cuda_ver:g}）")
        if vram_gb + 0.4 < vram_need:
            log(f"⚠ 显存低于建议值：{label} 可能加载失败或极慢——"
                f"可改用 sensevoice / small 档位")
    else:
        log("未检测到 NVIDIA GPU → CPU 模式（速度慢，建议 sensevoice 或 small）")
    try:
        free_gb = shutil.disk_usage(str(root)).free / (1024 ** 3)
        need_gb = disk_gb + (4.0 if args.model in TORCH_ENGINES else 1.5)
        log(f"磁盘剩余 {free_gb:.1f}GB，本次约需 {need_gb:.1f}GB")
        if free_gb < need_gb:
            log("✗ 磁盘空间不足——请清理后重试（模型下载中途失败会留下半个模型）")
            sys.exit(1)
    except Exception:
        pass

    # --------------------------------------- 0b. llama.cpp + HY-MT2-30B
    if args.llamacpp_only:
        install_llamacpp(root, args.mirror)
        log("==== SRT 大模型翻译安装完成 ====")
        log("设置里把「SRT 翻译模型」选为 HY-MT2-30B 即可；仅生成 SRT 时运行。")
        return

    # ------------------------------------------------ 0c. 引擎脚本自检
    # 便携包曾经只带 install_engine.py：用户把 4.7GB 模型下完，才在最后
    # 一步 `import asr_engines` 上失败（那条 ✗ 还会被 GBK 编码崩溃盖住，
    # 只剩 illegal multibyte sequence）。这里先查、先说、先退。
    runtime_scripts = ("live_transcribe.py", "live_capture.py",
                       "asr_engines.py", "translate_service.py",
                       "ollama_service.py")
    missing_scripts = [n for n in runtime_scripts if not (root / n).is_file()]
    if missing_scripts:
        log(f"⚠ 引擎目录缺少运行脚本: {', '.join(missing_scripts)}")
        if args.model in TORCH_ENGINES:
            log(f"✗ 缺少 asr_engines.py 等脚本，{label} 装完也加载不了——"
                f"请更新到新版程序包（旧包漏打了引擎脚本）；"
                f"或先改用 whisper 档位（不需要这些脚本即可识别）")
            sys.exit(1)
        log("  （whisper 档位仍可安装，但实时字幕要等脚本补齐才能启动）")

    # ---------------------------------------------------------- 1. venv
    need_funasr = args.model in TORCH_ENGINES
    if venv_py.is_file():
        have = venv_python_version(venv_py)
        if need_funasr and have and not (FUNASR_PY_MIN <= have <= FUNASR_PY_MAX):
            # 上一轮用不合规的解释器（如 3.13）建过 venv：不重建的话
            # funasr 永远装不上，而"venv 已存在就跳过"会让用户永远卡在
            # 同一个错误上。venv 里全是可重装的依赖，删了没有用户数据损失。
            log(f"已有 .venv 是 Python {have[0]}.{have[1]} 建的，"
                f"{args.model} 需要 3.{FUNASR_PY_MIN[1]}-3.{FUNASR_PY_MAX[1]}"
                f"（funasr 依赖 numpy<2，无 3.13 轮子）——重建虚拟环境")
            shutil.rmtree(root / ".venv", ignore_errors=True)
        else:
            log(f"venv 已存在，跳过"
                + (f"（Python {have[0]}.{have[1]}）" if have else ""))
    if not venv_py.is_file():
        picked = pick_venv_python(need_funasr)
        if picked is None:
            log(f"✗ {label} 需要 Python 3.{FUNASR_PY_MIN[1]}-3.{FUNASR_PY_MAX[1]}"
                f"（funasr 依赖 numpy<2，Python 3.13 没有预编译轮子，"
                f"pip 只能现编译 numpy 并失败），本机没找到可用版本。"
                f"两条出路：装一个 Python 3.12 后重跑安装；"
                f"或把识别引擎换成 whisper 档位"
                f"（large-v3/medium/small…，不依赖 funasr，3.13 也能装）")
            sys.exit(1)
        base_cmd, ver = picked
        log(f"创建虚拟环境 .venv（Python {ver[0]}.{ver[1]}）...")
        if run([*base_cmd, "-m", "venv", str(root / ".venv")]) != 0:
            log("✗ venv 创建失败")
            sys.exit(1)
    pip = [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]

    # --------------------------------------------------- 2. pip 依赖
    # faster-whisper 始终装：decode_audio 用于所有引擎的音频解码
    base_pkgs = ["faster-whisper", "pyyaml", "soundcard", "soundfile"]
    if gpu_name:
        # ctranslate2(whisper) 需要 cu12 版 nvidia 运行库（与 torch 的 CUDA 隔离使用）
        base_pkgs += ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
    log("安装基础依赖 ...")
    if run_proxy(pip + base_pkgs) != 0:
        log("✗ 基础依赖安装失败（检查网络）")
        sys.exit(1)

    if args.model in TORCH_ENGINES:
        index = torch_index(cuda_ver)
        log(f"安装 PyTorch（{'CUDA ' + index.rsplit('/', 1)[-1] if index else 'CPU'} 版，约 3GB）...")
        torch_cmd = pip + ["torch", "torchaudio"]
        if index:
            torch_cmd += ["--index-url", index]
        if run_proxy(torch_cmd) != 0:
            log("✗ PyTorch 安装失败（换网络或稍后重试）")
            sys.exit(1)
        # 校验 CUDA 可用（CPU 版 torch 装进来会在运行时才报错，这里提前发现）
        probe = "import torch;print('CUDA', torch.cuda.is_available(), torch.__version__)"
        run([str(venv_py), "-c", probe])
        log("安装 ASR 引擎依赖（funasr / modelscope / qwen-asr）...")
        engine_pkgs = ["funasr", "modelscope"]
        if args.model == "qwen":
            engine_pkgs.append("qwen-asr")
        if run_proxy(pip + engine_pkgs) != 0:
            log("✗ 引擎依赖安装失败")
            sys.exit(1)
    log("依赖安装完成")

    # ------------------------------------------------- 3. ASR 模型
    models_dir = root / "models" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    if args.model in TORCH_ENGINES:
        repo, folder = MS_REPOS[args.model]
        if not ms_download(venv_py, repo, models_dir / folder, label):
            log("✗ 模型下载失败（网络？磁盘？可重试）")
            sys.exit(1)
        vad_repo, vad_folder = MS_VAD
        if not ms_download(venv_py, vad_repo, models_dir / vad_folder, "fsmn-vad 语音端点检测"):
            log("✗ VAD 模型下载失败")
            sys.exit(1)
        log("验证模型可加载 ...")
        verify = (
            "import sys; sys.path.insert(0, %r)\n"
            "import asr_engines as ae\n"
            "m = ae.load_%s(%r)\n"
            "v = ae.load_vad()\n"
            "print('模型验证通过')\n"
        ) % (str(root), "qwen" if args.model == "qwen" else "sensevoice",
             "cuda" if gpu_name else "cpu")
        if run([str(venv_py), "-c", verify], cwd=str(root)) != 0:
            log("✗ 模型加载验证失败（看上方报错；显存不足可改用更小引擎）")
            sys.exit(1)
    else:
        log(f"准备 whisper 模型 {args.model} {MODEL_CACHE_HINT}")
        env = {
            "HF_ENDPOINT": hf_endpoint,
            "HUGGINGFACE_HUB_CACHE": str(root / "models" / "hf" / "hub"),
            "HF_HUB_DISABLE_PROGRESS_BARS": "0",
        }
        probe = (
            "from faster_whisper import WhisperModel; "
            f"WhisperModel({args.model!r}, device='cpu', compute_type='int8')"
        )
        if run_proxy([str(venv_py), "-c", probe], env=env) != 0:
            log("✗ 模型下载/加载失败（网络或磁盘？可重试或换镜像）")
            sys.exit(1)
    log(f"识别模型 {label} 就绪")

    # ------------------------------------------------------ 4. Ollama
    ollama_exe = None
    if not args.skip_ollama and args.translate != "none":
        ollama_exe = find_ollama()
        if ollama_exe is None:
            if not shutil.which("winget"):
                # LTSC / 精简系统 / App Installer 缺失：没有 winget 就没法
                # 自动装。给出可执行的出路，而不是让 subprocess 抛异常
                log("✗ 未找到 Ollama，且本机没有 winget 无法自动安装——"
                    "请手动装 https://ollama.com/download 后重跑；"
                    "或把翻译模型选「不翻译」跳过（识别照常可用）")
                sys.exit(1)
            log("未找到 Ollama，尝试用 winget 安装（约 1.5GB，需要几分钟）...")
            rc = run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--accept-source-agreements",
                      "--accept-package-agreements", "--silent"])
            if rc != 0:
                log("✗ Ollama 自动安装失败——请手动安装 https://ollama.com/download 后重试")
                sys.exit(1)
            # 装到用户级还是机器级由 winget 决定，且当前进程 PATH 不刷新
            ollama_exe = find_ollama()
            if ollama_exe is None:
                log("✗ Ollama 装好了但找不到 ollama.exe——重启电脑刷新 PATH 后重跑安装")
                sys.exit(1)
        log(f"Ollama: {ollama_exe}")
        if not ensure_ollama_daemon(ollama_exe):
            log("✗ Ollama 服务起不来——手动打开一次 Ollama 应用，再重跑安装")
            sys.exit(1)

        # ------------------------- 4b. 翻译模型：优先 ollama pull（官方库）
        # GGUF 手工下载+import 只作为拉取失败时的回退（qwen2.5 系列有镜像映射）
        installed = ""
        try:
            installed = subprocess.run([str(ollama_exe), "list"], capture_output=True,
                                       text=True, timeout=30, errors="replace").stdout
        except Exception:
            pass
        if args.translate.split(":")[0] in installed and args.translate in installed:
            log(f"翻译模型 {args.translate} 已安装，跳过")
        else:
            log(f"拉取翻译模型 {args.translate}（{TRANSLATE_SIZES.get(args.translate, '')}）"
                f" {MODEL_CACHE_HINT}")
            if run([str(ollama_exe), "pull", args.translate]) != 0:
                log("直接拉取失败，尝试镜像 GGUF 导入 ...")
                from ollama_modelfile import GGUF_SIZES

                gguf_spec = GGUF_SIZES.get(args.translate)
                if gguf_spec is None:
                    log(f"✗ 翻译模型 {args.translate} 拉取失败且无镜像回退——"
                        f"可稍后手动 `ollama pull {args.translate}`，或用 --translate none 跳过")
                    sys.exit(1)
                repo, fname, size = gguf_spec
                g = root / "models" / "gguf" / fname
                g.parent.mkdir(parents=True, exist_ok=True)
                if not g.is_file():
                    base = ("https://hf-mirror.com" if args.mirror == "hf-mirror"
                            else "https://huggingface.co")
                    url = f"{base}/{repo}/resolve/main/{fname}"
                    log(f"下载翻译模型 {fname}（{size}）...")
                    # 走 _curl：断点续传 + 系统代理兜底 + curl 缺失预检
                    # （此前是裸 curl 一次性下载，网络不好就永远装不上）
                    if not _curl(url, g, min_bytes=1_000_000):
                        log("✗ 翻译模型下载失败——可换镜像源或稍后重试；--translate none 可跳过")
                        sys.exit(1)
                mf = root / f"Modelfile{args.translate.replace(':', '-')}"
                mf.write_text(f"FROM {g}\n", encoding="utf-8")
                log(f"导入 Ollama 模型 {args.translate} ...")
                if run([str(ollama_exe), "create", args.translate, "-f", str(mf)]) != 0:
                    log("✗ 模型导入失败")
                    sys.exit(1)

    # ------------------------------------------------------- 5. 写配置
    cfg = root / "config.yaml"
    if cfg.is_file():
        # 只在首次安装时生成：重装/换引擎不应清掉用户手工配置
        #（播放器实际通过命令行参数下发配置，这份文件只服务独立入口）
        log("config.yaml 已存在，保留不覆盖")
    else:
        cfg.write_text(
            f"asr:\n  model: {args.model!r}\n"
            f"  device: {'cuda' if gpu_name else 'cpu'}\n"
            f"  compute: {'float16' if gpu_name else 'int8'}\n"
            "  language: auto\n  beam_size: 5\n"
            f"translate:\n  enabled: {'false' if args.translate == 'none' else 'true'}\n"
            f"  endpoint: http://127.0.0.1:11434\n  model: {args.translate!r}\n"
            "  target_lang: zh\n  chunk_sentences: 3\n"
            "output:\n  show_original: true\n  srt: true\n",
            encoding="utf-8",
        )
        log("config.yaml 已生成")

    log(f"\n==== 安装完成 ====\n引擎目录: {root}\n"
        f"识别: {label}（{'GPU' if gpu_name else 'CPU'}）\n"
        f"翻译: {args.translate}\n"
        "现在可以在播放器里右键视频 →「生成 SRT 字幕」，或播放时右键「开启实时字幕」。")


if __name__ == "__main__":
    main()