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

MODEL_CACHE_HINT = "（首次下载；已缓存则秒过）"

TORCH_ENGINES = ("qwen", "sensevoice")
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")

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


def run(cmd: list[str], env: dict | None = None, **kw) -> int:
    log(f"> {' '.join(str(c) for c in cmd)}")
    env = {**os.environ, **(env or {})}
    return subprocess.call(cmd, env=env, **kw)


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
    return run([str(venv_py), "-c", code]) == 0


def _curl(url: str, out: Path, min_bytes: int = 1_000_000) -> bool:
    """curl 下载（gh-proxy 加速失败自动回落直连），幂等：文件够大则跳过。"""
    if out.is_file() and out.stat().st_size >= min_bytes:
        log(f"已存在，跳过下载: {out.name}")
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    for base in (GGH, ""):
        url2 = base + url if base else url
        log(f"下载 {out.name}（{url2[:80]}...）")
        if run(["curl.exe", "-L", "--fail", "-o", str(out), url2]) == 0                 and out.is_file() and out.stat().st_size >= min_bytes:
            return True
    return False


def install_llamacpp(root: Path, mirror: str) -> None:
    """llama.cpp 二进制 + HY-MT2-30B GGUF（引擎目录内，幂等）。"""
    hf = "https://hf-mirror.com" if mirror == "hf-mirror" else "https://huggingface.co"
    llamacpp = root / "llamacpp"
    server = llamacpp / "llama-server.exe"
    if server.is_file():
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

        for z in zips:
            # 下载中断可能留下够大但损坏的半截 zip：不校验会在解压时崩溃，
            # 且下次运行因"文件够大"跳过下载，反复失败
            if not zipfile.is_zipfile(z):
                z.unlink(missing_ok=True)
                log("✗ 下载的压缩包损坏（已删除），请重试安装")
                sys.exit(1)
            log(f"解压 {z.name} ...")
            with zipfile.ZipFile(z) as zf:
                zf.extractall(llamacpp)
            z.unlink()
        tmp.rmdir()
        if not server.is_file():
            log("✗ 解压后未找到 llama-server.exe")
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

    # ---------------------------------------------------------- 1. venv
    if not venv_py.is_file():
        log("创建虚拟环境 .venv ...")
        if run([sys.executable, "-m", "venv", str(root / ".venv")]) != 0:
            log("✗ venv 创建失败")
            sys.exit(1)
    else:
        log("venv 已存在，跳过")
    pip = [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]

    # --------------------------------------------------- 2. pip 依赖
    # faster-whisper 始终装：decode_audio 用于所有引擎的音频解码
    base_pkgs = ["faster-whisper", "pyyaml", "soundcard", "soundfile"]
    if gpu_name:
        # ctranslate2(whisper) 需要 cu12 版 nvidia 运行库（与 torch 的 CUDA 隔离使用）
        base_pkgs += ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
    log("安装基础依赖 ...")
    if run(pip + base_pkgs) != 0:
        log("✗ 基础依赖安装失败（检查网络）")
        sys.exit(1)

    if args.model in TORCH_ENGINES:
        index = torch_index(cuda_ver)
        log(f"安装 PyTorch（{'CUDA ' + index.rsplit('/', 1)[-1] if index else 'CPU'} 版，约 3GB）...")
        torch_cmd = pip + ["torch", "torchaudio"]
        if index:
            torch_cmd += ["--index-url", index]
        if run(torch_cmd) != 0:
            log("✗ PyTorch 安装失败（换网络或稍后重试）")
            sys.exit(1)
        # 校验 CUDA 可用（CPU 版 torch 装进来会在运行时才报错，这里提前发现）
        probe = "import torch;print('CUDA', torch.cuda.is_available(), torch.__version__)"
        run([str(venv_py), "-c", probe])
        log("安装 ASR 引擎依赖（funasr / modelscope / qwen-asr）...")
        engine_pkgs = ["funasr", "modelscope"]
        if args.model == "qwen":
            engine_pkgs.append("qwen-asr")
        if run(pip + engine_pkgs) != 0:
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
        if run([str(venv_py), "-c", probe], env=env) != 0:
            log("✗ 模型下载/加载失败（网络或磁盘？可重试或换镜像）")
            sys.exit(1)
    log(f"识别模型 {label} 就绪")

    # ------------------------------------------------------ 4. Ollama
    ollama_exe = None
    if not args.skip_ollama and args.translate != "none":
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if cand.is_file():
            ollama_exe = cand
        else:
            found = shutil.which("ollama")
            ollama_exe = Path(found) if found else None
        if ollama_exe is None:
            log("未找到 Ollama，尝试用 winget 安装（约 1.5GB，需要几分钟）...")
            rc = run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--accept-source-agreements",
                      "--accept-package-agreements", "--silent"])
            if rc != 0:
                log("✗ Ollama 自动安装失败——请手动安装 https://ollama.com/download 后重试")
                sys.exit(1)
            ollama_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        log(f"Ollama: {ollama_exe}")

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
                    if run(["curl.exe", "-L", "-o", str(g), url]) != 0 \
                            or not g.is_file() or g.stat().st_size < 1_000_000:
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