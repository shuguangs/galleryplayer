# 🎬 GalleryPlayer

> A local player built for **browsing images and watching videos**: open a folder and every picture and video sits in one list — scroll and flip through them one by one. It also adds **real-time bilingual subtitles** to foreign-language videos.

![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Portable](https://img.shields.io/badge/Portable-No%20Install-green)
![Live%20Captions](https://img.shields.io/badge/Live_Captions-AI_Bilingual-8A2BE2)
![License](https://img.shields.io/badge/License-All%20Rights%20Reserved-lightgrey)

**中文版：[README.zh-CN.md](README.zh-CN.md)**

---

## ✨ What it does

- **Images and videos in one list**: no switching between a photo viewer and a video player — whatever is in the folder, you just keep scrolling.
- **Three ways to browse**: a uniform grid, a waterfall that keeps each image's real aspect ratio, and a detailed list with all the info.
- **Auto covers for videos**: every video gets a thumbnail and its duration shown, so you can tell at a glance which episode is which.
- **Resume playback**: close a video halfway and it continues from where you left off next time.
- **Subtitles just work**: drop a same-name subtitle file next to the video; adjust the font size and timing on the fly.
- **Phone photos included**: HEIC and other modern image formats open directly.
- **Network folders work too**: browse and play from shared LAN folders and mapped network drives.
- **Sort it your way**: sort the file browser and the left folder tree by name / modified time / size, ascending or descending; the playlist inherits your chosen order when the player opens.
- **Open from outside, play in the current window**: while the player is running, double-click a video or image in Explorer (or pick it via "Open with") and it plays right in the current window — no second window. Folders and archives work the same way.
- **Truly portable**: copy the whole folder to a USB stick and it runs on any PC — no install, no registry, nothing left behind.
- **Clean, immersive UI**: dark theme; the controls fade out when the mouse is idle.

---

## 🎙️ Live captions & AI translation

Everything runs **locally and offline** — no audio ever leaves your machine. While watching a foreign-language video, captions roll out as people speak: one line in the original language, one line translated to Chinese.

- **Real-time bilingual captions**: local speech recognition plus a local LLM for translation — the original line appears first, the translation follows right after.
- **Visible progress**: transcribed ranges are drawn in teal on the seekbar; hover to see "transcribed up to …".
- **One-click background SRT**: right-click a video to generate a full SRT subtitle (recognition + translation) in the background. A system notification pops up when it finishes or fails, and it can be cancelled midway; select multiple videos to batch-generate.
- **Three ASR engines**: switch engines in Settings — each one is labelled with its disk footprint and VRAM needs, plus a combined-usage readout. If VRAM runs short, the app falls back to a smaller engine automatically.
- **One-click setup**: the Settings dialog installs everything for you — detects your GPU, picks the right CUDA-enabled PyTorch build, and pulls models from a fast mirror.
- **Resilient**: the recognition engine auto-recovers after a crash; SRT generation and live captions coordinate so they never fight over the engine.

### Which engine to pick (measured on this machine)

| Engine | Disk / VRAM | Notes |
| --- | --- | --- |
| **Qwen3-ASR-1.7B** (default) | 4.7GB / ~6GB | 52 languages with auto language ID; best quality |
| SenseVoice-small | 0.9GB / ~2GB | Fast and accurate for zh/ja/ko/yue, runs on CPU; weaker on English |
| faster-whisper (tiny–large-v3) | 0.5–5.8GB / 1–8GB | Widest language coverage, low-end fallback |

Translation defaults to local **qwen3:8b** (via Ollama, natural colloquial output); with VRAM to spare, SRT generation can also use **HY-MT2-30B** (llama.cpp backend, better quality, started on demand and shut down afterwards).

> [!NOTE]
> The default live-caption engine needs an **NVIDIA GPU (8GB+ VRAM recommended)**. No discrete GPU or little VRAM? Just switch to SenseVoice or a small whisper tier in Settings.

> [!TIP]
> First time: `Ctrl+,` to open Settings → "Live captions" section → click one-click install. Pick an engine and language and you're set. Press `F1` for details.

---

## 🚀 Getting started

1. Download and unzip — keep the **whole folder** (don't drag out just the .exe; the files beside it are part of the app).
2. On a fresh PC, run `安装运行环境.bat` once (installs the required system component).
3. Double-click `媒体播放器.exe`.

**On first launch** you'll be asked to pick the interface language (中文 / English). You can switch anytime in **Settings → Language**.

### The essentials

| Action | How |
| --- | --- |
| Open a folder | `Ctrl+O`, or just drag a folder into the window |
| Previous / next | Mouse wheel (images and videos alike) |
| Play / pause | Click the picture, or press Space |
| Fullscreen | Press `F`, or double-click |
| Seek | `←` `→` (5 s), `Ctrl+←→` (60 s) |
| Volume | `↑` `↓`, or `Ctrl+Wheel` |
| Prev / next episode | `PageUp` / `PageDown` in the viewer |
| Subtitles | `V` to toggle, `J` to switch track |
| Screenshot / GIF | `S` saves a still, `G` records a short clip |
| Settings | `Ctrl+,` |

Press **`F1`** inside the app for the full shortcut reference.

---

## 💡 FAQ

**A video won't play / stutters?**
Open Settings (`Ctrl+,`) and try a different "Decode mode" — e.g. switch from auto to software. Old GPUs sometimes choke on certain formats; another mode usually fixes it.

**Where do I turn on live captions / generate an SRT?**
Live captions: right-click the video while playing → subtitle menu. SRT generation: right-click one or more videos in the file list. Both share the same recognition engine — run the one-click install in Settings first.

**It says there's not enough VRAM?**
Pick a smaller engine in Settings (SenseVoice needs only ~2GB), and lower the translation model to `qwen2.5:3b`. The app also falls back automatically when VRAM runs short.

**The interface switched to another language — how do I change it back?**
`Ctrl+,` → Language → pick your language, then restart as prompted.

**Lost track of where you were in a video?**
No need to remember — videos longer than a minute keep their position automatically.

**Files disappeared?**
Press `F5` to rescan the current folder; new, deleted and moved files are all picked up.

---

## 🗂️ Where your data lives

- Everything personal lives in the `userdata` folder next to the program: settings, playback positions, thumbnail cache. Delete that folder to fully reset — the app itself is untouched.
- When running from source, `live-subtitle/` holds the full caption-engine source (a separate Python environment set up automatically by the install script); models are downloaded into `live-subtitle/models/` and are not bundled with the app.

---

## 🤖 About

This project was co-written by AI:

[![Claude](https://img.shields.io/badge/Claude-Anthropic-191919?logo=anthropic&logoColor=white)](https://github.com/anthropics)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-DeepSeek-blue)](https://github.com/deepseek-ai)
[![GLM](https://img.shields.io/badge/GLM-Zhipu_AI-386BF0)](https://github.com/zhipuai)
**Qoder**

They contributed different parts — player UI, the live-subtitle engine, architecture reviews and optimizations. Their commits carry `Co-authored-by` attribution.

## 📄 License

This project is **not open source licensed** — all rights reserved. © 2026 shuguangs
