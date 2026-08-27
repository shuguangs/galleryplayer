"""一键安装字幕引擎：venv + 依赖 + whisper 模型（HF/镜像）+ Ollama 翻译模型。

用法（由播放器设置界面调用，也可命令行手动跑）：
    python install_engine.py --dir <引擎目录> [--model medium] [--mirror hf-mirror]
                             [--translate qwen2.5:3b|qwen2.5:7b|none] [--skip-ollama]

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


def log(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def run(cmd: list[str], env: dict | None = None, **kw) -> int:
    log(f"> {' '.join(str(c) for c in cmd)}")
    env = {**os.environ, **(env or {})}
    return subprocess.call(cmd, env=env, **kw)


def main() -> None:
    ap = argparse.ArgumentParser(description="一键安装字幕引擎")
    ap.add_argument("--dir", default=str(Path.cwd()))
    ap.add_argument("--model", default="medium", choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--mirror", default="huggingface",
                    choices=["huggingface", "hf-mirror"])
    ap.add_argument("--translate", default="qwen2.5:7b",
                    help="翻译模型（Ollama）：qwen2.5:3b / qwen2.5:7b / none")
    ap.add_argument("--skip-ollama", action="store_true", dest="skip_ollama",
                    help="跳过 Ollama 安装/模型下载")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    venv_py = root / ".venv" / "Scripts" / "python.exe"
    hf_endpoint = "https://hf-mirror.com" if args.mirror == "hf-mirror" else "https://huggingface.co"
    log(f"引擎目录: {root}")
    log(f"模型源: {hf_endpoint}")

    # ---------------------------------------------------------- 1. venv
    if not venv_py.is_file():
        log("创建虚拟环境 .venv ...")
        if run([sys.executable, "-m", "venv", str(root / ".venv")]) != 0:
            log("✗ venv 创建失败")
            sys.exit(1)
    else:
        log("venv 已存在，跳过")

    # --------------------------------------------------- 2. pip 依赖
    gpu = False
    if shutil.which("nvidia-smi"):
        gpu = True
        log("检测到 NVIDIA GPU → CUDA 模式")
    else:
        log("未检测到 NVIDIA GPU → CPU 模式（建议 --model small）")
    opt = ""
    run([str(venv_py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         "faster-whisper", "pyyaml"] + (["nvidia-cublas-cu12", "nvidia-cudnn-cu12"] if gpu else []))
    log("依赖安装完成")

    # ------------------------------------------------- 3. whisper 模型
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
    log(f"whisper 模型 {args.model} 就绪")

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

        # -------------------------------------- 4b. 翻译模型 GGUF → import
        from ollama_modelfile import GGUF_SIZES  # 见下方内嵌

        gguf_spec = GGUF_SIZES.get(args.translate)
        if gguf_spec is None:
            log(f"未知翻译模型 {args.translate}，跳过")
        else:
            repo, fname, size = gguf_spec
            g = root / "models" / "gguf" / fname
            if not g.is_file():
                base = f"https://{('hf-mirror.com' if args.mirror == 'hf-mirror' else 'huggingface.co')}"
                url = f"{base}/{repo}/resolve/main/{fname}"
                log(f"下载翻译模型 {fname}（{size}） {MODEL_CACHE_HINT}")
                if run(["curl.exe", "-L", "-o", str(g), url]) != 0 \
                        or not g.is_file() or g.stat().st_size < 1_000_000:
                    log("✗ 翻译模型下载失败——可换镜像源或稍后重试；--translate none 可跳过")
                    sys.exit(1)
            else:
                log("翻译模型已缓存，跳过下载")
            mf = root / f"Modelfile{args.translate.replace(':', '-')}"
            mf.write_text(f"FROM {g}\n", encoding="utf-8")
            log(f"导入 Ollama 模型 {args.translate} ...")
            if run([str(ollama_exe), "create", args.translate, "-f", str(mf)]) != 0:
                log("✗ 模型导入失败")
                sys.exit(1)

    # ------------------------------------------------------- 5. 写配置
    cfg = root / "config.yaml"
    cfg.write_text(
        f"asr:\n  model: {args.model!r}\n"
        f"  device: {'cuda' if gpu else 'cpu'}\n"
        f"  compute: {'float16' if gpu else 'int8'}\n"
        "  language: en\n  beam_size: 5\n"
        f"translate:\n  enabled: {'false' if args.translate == 'none' else 'true'}\n"
        f"  endpoint: http://127.0.0.1:11434\n  model: {args.translate!r}\n"
        "  target_lang: zh\n  chunk_sentences: 3\n"
        "output:\n  show_original: true\n  srt: true\n",
        encoding="utf-8",
    )
    log("config.yaml 已生成")

    log(f"\n==== 安装完成 ====\n引擎目录: {root}\n"
        f"识别: {args.model}（{'GPU' if gpu else 'CPU'}）\n"
        f"翻译: {args.translate}\n"
        "现在可以在播放器里右键视频 →「生成 SRT 字幕」使用。")


if __name__ == "__main__":
    main()