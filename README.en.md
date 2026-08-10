# 🎬 GalleryPlayer

> A local player built for **browsing images and watching videos**: open a folder and every picture and video sits in one list — scroll and flip through them one by one.

![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Portable](https://img.shields.io/badge/Portable-No%20Install-green)
![License](https://img.shields.io/badge/License-All%20Rights%20Reserved-lightgrey)

**中文版：[README.md](README.md)**

---

## ✨ What it does

- **Images and videos in one list**: no switching between a photo viewer and a video player — whatever is in the folder, you just keep scrolling.
- **Three ways to browse**: a uniform grid, a waterfall that keeps each image's real aspect ratio, and a detailed list with all the info.
- **Auto covers for videos**: every video gets a thumbnail and its duration shown, so you can tell at a glance which episode is which.
- **Resume playback**: close a video halfway and it continues from where you left off next time.
- **Subtitles just work**: drop a same-name subtitle file next to the video; adjust the font size and timing on the fly.
- **Phone photos included**: HEIC and other modern image formats open directly.
- **Network folders work too**: browse and play from shared LAN folders and mapped network drives.
- **Truly portable**: copy the whole folder to a USB stick and it runs on any PC — no install, no registry, nothing left behind.
- **Clean, immersive UI**: dark theme; the controls fade out when the mouse is idle.

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

**The interface switched to another language — how do I change it back?**
`Ctrl+,` → Language → pick your language, then restart as prompted.

**Lost track of where you were in a video?**
No need to remember — videos longer than a minute keep their position automatically.

**Files disappeared?**
Press `F5` to rescan the current folder; new, deleted and moved files are all picked up.

---

## 🗂️ Where your data lives

Everything personal lives in the `userdata` folder next to the program: settings, playback positions, thumbnail cache. Delete that folder to fully reset — the app itself is untouched.

---

## 🤖 About

This project was co-written by AI: **Claude 5 Opus**, **DeepSeek 4 Pro Preview** and **Qoder**.

## 📄 License

This project is **not open source licensed** — all rights reserved. © 2026 shuguangs
