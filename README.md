# 🎬 GalleryPlayer

> **A local media player built for browsing.** Open a folder and every picture and video sits in one scrollable list — flip through the whole collection like pages of a book. Foreign-language videos get **real-time bilingual captions**, all computed on your own machine.

![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Portable](https://img.shields.io/badge/Portable-No%20Install-green)
![Live%20Captions](https://img.shields.io/badge/Live_Captions-AI_Bilingual-8A2BE2)
![Version](https://img.shields.io/badge/Version-2.3-orange)
![License](https://img.shields.io/badge/License-All%20Rights%20Reserved-lightgrey)

**中文版：[README.zh-CN.md](README.zh-CN.md)**

---

## ✨ Core features

### 🖼️ Browse everything in one place

Open a folder and the whole collection becomes one scrollable list — images and videos side by side. Flip through it with the mouse wheel like pages of a book, in any of three view modes: a uniform **grid**, an aspect-true **waterfall**, or a **detail list** with every file's info.

Archives count too: double-click a `zip` / `rar` / `7z` and it opens like a folder — browse, preview and play what's inside without extracting anything first.

### 🎙️ Real-time bilingual captions

Watching a foreign-language video? Captions roll out as people speak — the original line first, the translation right under it. Everything runs **locally and offline**; no audio ever leaves your machine.

- Speech recognition plus LLM translation, both local
- The seekbar shows how far captions have reached, in teal
- The engine samples the whole file to detect the dominant language before committing, and re-transcribes mis-detected segments in place when it changes its mind
- Works while you seek and jump around; the transcriber follows you

### 📝 One-click background SRT

Right-click any video — or a whole selection — and a complete bilingual `.srt` is generated in the background while you keep using the app. Progress is visible in a floating window, a system notification pops when it's done, and you can cancel midway.

### 🧠 Your choice of engines

Three speech-recognition engines, switchable at any time, each labelled with its disk footprint and VRAM needs — plus an optional heavyweight translation model for the highest quality. When VRAM runs short the app falls back automatically.

| Engine | Disk / VRAM | Notes |
| --- | --- | --- |
| **Qwen3-ASR-1.7B** (default) | 4.7GB / ~6GB | 52 languages with auto language ID; best quality |
| SenseVoice-small | 0.9GB / ~2GB | Fast for zh/ja/ko/yue, runs on CPU |
| faster-whisper (tiny–large-v3) | 0.5–5.8GB / 1–8GB | Widest coverage, low-end fallback |

Translation defaults to local **qwen3:8b** (Ollama); SRT jobs can optionally use **HY-MT2-30B** (llama.cpp backend, started on demand and shut down afterwards).

Everything installs with **one click** in Settings: the app detects your GPU, picks the right CUDA-enabled PyTorch build, and pulls models from a fast mirror.

### 🎒 Portable by design

Copy the folder to a USB stick and it runs on any PC — no install, no registry, nothing left behind. Videos longer than a minute **remember where you stopped**. While the player is running, double-clicking a file in Explorer plays it **in the same window** instead of spawning a second one.

> [!TIP]
> There is more to discover — screenshots, GIF recording, albums, playlists, and a dark UI that gets out of your way. [Download](#-getting-started) and take it for a spin.

---

## 🚀 Getting started

1. Download and unzip — keep the **whole folder** (the files beside the .exe are part of the app).
2. On a fresh PC, run `安装运行环境.bat` once (installs the required system component).
3. Double-click `媒体播放器.exe`.

**First launch** asks for the interface language (中文 / English); switch anytime in Settings.

> [!NOTE]
> The default caption engine wants an **NVIDIA GPU (8GB+ VRAM recommended)**. Less VRAM? Switch to SenseVoice or a small whisper tier in Settings — or run captions on CPU.

---

## 🔨 Build from source

Requirements: **Windows 10+**, **Python 3.12+**, and `git`.

```bash
# 1. Clone
git clone https://github.com/shuguangs/galleryplayer.git
cd galleryplayer

# 2. Install runtime dependencies
pip install -r requirements.txt

# 3. Get libmpv (the playback core, ~112 MB — too big for git)
#    Download mpv's libmpv dev build, then place libmpv-2.dll at:
#      vendor/libmpv-2.dll

# 4. Run from source
python main.py
```

To produce the portable build:

```bash
pip install pyinstaller
python build.py          # outputs dist/媒体播放器/
```

The caption engine lives in `live-subtitle/` with its own Python environment — set it up from **Settings → Live captions → one-click install** (auto-detects your GPU and downloads models); see the section above.

---

## 💡 FAQ

**A video won't play / stutters?**
Settings (`Ctrl+,`) → try another "Decode mode" — switching auto → software fixes most old-GPU cases.

**How do I turn on live captions / generate an SRT?**
Live captions: right-click the playing video → subtitle menu. SRT: right-click videos in the browser. Run the one-click install in Settings first.

**Not enough VRAM?**
Pick a smaller engine (SenseVoice needs ~2GB) and a lighter translation model; the app also falls back automatically.

**Where was I in that video?**
Videos longer than a minute remember their position automatically.

---

## 🗂️ Where your data lives

Everything personal lives in `userdata` next to the program: settings, playback positions, thumbnail cache. Delete that folder for a full reset. Models live in `live-subtitle/models/` and are never bundled into the app.

## 🤖 About

This project was co-written by humans and AI.

**Models**

[![Claude](https://img.shields.io/badge/Claude-Anthropic-191919?logo=anthropic&logoColor=white)](https://github.com/anthropics)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-DeepSeek-blue)](https://github.com/deepseek-ai)
[![GLM](https://img.shields.io/badge/GLM-Z.ai-386BF0)](https://github.com/zai-org)

**Agent platforms** — Claude Code · Qoder · ZCode · DSH

They contributed different parts — player UI, the live-subtitle engine, architecture reviews and optimizations. Their commits carry `Co-authored-by` attribution.

## 📄 License

This project is **not open source licensed** — all rights reserved. © 2026 shuguangs
