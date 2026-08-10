# 🎬 GalleryPlayer

> A local image / video browser and player for Windows. Interaction is modeled after Telegram's inline player: images and videos from one folder share a single list, and the mouse wheel flips through them one by one.

![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/PySide6-6.9-41CD52?logo=qt&logoColor=white)
![libmpv](https://img.shields.io/badge/libmpv-FFmpeg%20Builtin-8B0000)
![Portable](https://img.shields.io/badge/Portable-No%20Install-green)
![License](https://img.shields.io/badge/License-All%20Rights%20Reserved-lightgrey)

**中文版：[README.md](README.md)**

---

## ✨ Features

- **Playback core**: libmpv (FFmpeg built in) — H.264 / HEVC / AV1 / VP9 and mp4, mkv, avi, wmv, flv, ts, rmvb containers play out of the box, with automatic hardware decoding on NVIDIA / Intel / AMD GPUs.
- **Image decoding**: Qt native plugins + Pillow, including HEIC / AVIF / JXL / animated WebP / APNG.
- **Fully portable**: all settings, thumbnail caches and playback progress live under `userdata/` next to the program — no registry, no AppData. Copy the whole folder to another machine and it just works.
- **Network locations**: UNC paths (`\\NAS\Videos`) and mapped network drives are browsable and playable; network shares are detected automatically, raising the buffer from 32 MB to 192 MB, readahead from 5 s to 30 s, and serializing background frame extraction so parallel tasks never fight over one link.
- **Three browse views**: grid / waterfall / detail list, with video covers auto-extracted and cached.
- **Immersive viewer**: dark UI, auto-fading controls, hover thumbnail preview on the seek bar, resume playback, subtitle / audio track switching.
- **Playlist panel**: a dockable panel combining playlist + album collections + folder browser.

The package is about 247 MB (libmpv's bundled FFmpeg accounts for 112 MB of it). Measured memory usage:

| Scenario | Usage |
| --- | --- |
| Browsing images only (thumbnails cached, libmpv never loads) | ~90 MB |
| Browsing a folder with videos (background frame extraction) | ~340 MB |
| Playing a 1080p video | ~430–560 MB, ~170 MB of which is the GPU's OpenGL driver itself |
| Browsing a 400-image folder | ~290 MB peak, flat regardless of file count |

Two deliberate constraints: the in-memory thumbnail cache is hard-capped at 200 entries (LRU eviction, then re-read from small on-disk JPEGs, which is fast); each background libmpv instance used for frame extraction costs ~55 MB and is released after 15–25 s of idling, then rebuilt on demand.

---

## 🚀 Getting Started

### Pre-built portable version

Unzip and double-click `媒体播放器.exe`. `安装运行环境.bat` checks / installs the Microsoft Visual C++ runtime for you (video decoders are already inside `vendor\libmpv-2.dll`, no codec pack needed).

### Run from source

```bash
pip install -r requirements.txt
```

`vendor/libmpv-2.dll` must exist (~112 MB). If missing, grab `mpv-dev-x86_64-*.7z` from <https://github.com/shinchiro/mpv-winbuild-cmake/releases>, extract it and put `libmpv-2.dll` into `vendor/`. Then:

```bash
python main.py
```

You can also open something directly: `python main.py "D:\some folder"` or `python main.py "D:\some video.mkv"`.

### Build your own package

```bash
python build.py
```

Output goes to `dist/媒体播放器/`. An optional argument (`python build.py complete`) overrides the output directory.

---

## 🖥️ Interface

### Browsing

On startup you get a blank welcome page — it never auto-opens the last folder; the page lists recently opened folders and one click takes you in (stale entries are pruned automatically).

Folder tree on the left, three views in the middle, counts in the status bar at the bottom.

| View | Description |
| --- | --- |
| **Grid** | Uniform 4:3 tiles, covers cropped to fill, duration badge at the video's bottom-right corner |
| **Waterfall** | Items keep their true aspect ratio; portrait and landscape mix without distortion |
| **Detail list** | Name / type / size / resolution / duration / modified time, sortable by clicking column headers |

Video covers are extracted automatically from the 18% mark, cached together with duration and resolution — reopening the same folder is instantaneous.

**Directory scanning**: layer by layer (breadth-first). The folder you opened is read first, then each whole layer of subfolders concurrently — 6 threads locally, 16 on network locations. Measured on a local SSD, 739 folders / 4272 files: cold scan **160 ms** (concurrent), cached listing 50 ms (zero I/O), background incremental re-check **113 ms**. Opening a 21k-file tree went from ~7 s of stutter to **~0.3 s** (no stutter at all on cache hits).

**Directory cache**: each directory, its mtime, file table and subfolder table are cached in `userdata/dircache.json`. On reopening, the list renders with zero I/O first (status bar shows "checking…"), then an incremental background pass re-reads only directories whose mtime actually changed. New and deleted files are both detected; `F5` forces a full rescan.

**Sorting**: name (natural sort, `第2集` sorts before `第10集`), modified time, file size, duration, random, custom (manual drag-sort in the panel switches the folder to custom and persists it).
**Filtering**: all / images only / videos only, plus filename search.
**Include subfolders**: flattens all subdirectory media into one list. You can also drag a folder or individual files into the window.

**Context menu**: open, open with default program, reveal in Explorer, copy full path / filename, rename, delete to recycle bin (`SHFileOperationW` + `FOF_ALLOWUNDO`, fully restorable); right-clicking empty space offers "open containing folder" and "rescan". Deletion always asks first, and if the recycle bin is unavailable it refuses rather than hard-deleting.

#### Browsing shortcuts

| Key | Action |
| --- | --- |
| `Ctrl+O` | Open folder |
| `Backspace` | Parent directory |
| `F5` | Rescan |
| `Ctrl+1` / `Ctrl+2` / `Ctrl+3` | Grid / Waterfall / List |
| `Ctrl+B` | Toggle folder tree |
| `Ctrl+F` | Focus search box |
| `Ctrl+Wheel` | Adjust items per row |
| `Arrows` / `Home` / `End` | Move selection |
| `Enter` / double-click | Open viewer |

### Viewer

A dark, immersive window; the top bar shows filename, resolution, codec, hardware-decoding status, file size and index. The control bar and top bar fade out after ~2.6 s of mouse idle and return on any movement.

**The wheel always switches previous / next** (like Telegram). Zoom and volume are on `Ctrl+Wheel`. The three round on-screen buttons (play / pause, previous / next) fade in and out with the bars, sized against the window's short edge and hard-capped at a quarter of it.

**Seek bar**: hovering pops a thumbnail of that time point, extracted live (~150 ms), bucketed in 5-second slots so scrubbing back and forth never re-decodes. The buffered range is shown brighter.

**Subtitles**: same-name `.srt` / `.ass` files load automatically (fuzzy match — `影片.chs.ass` works too); embedded tracks can be switched, with font-size adjustment and ±0.1 s delay fine-tuning, and external subtitle files can be loaded manually. ASS styled subtitles are rendered in full by libass.

**Resume playback**: videos longer than 1 minute, watched past 15 s and at least 15 s from the end remember their position; next open continues from the breakpoint with a one-time notice; finishing the video clears the record. Volume, speed, view mode, sort order and window position are remembered too.

#### Right-side playlist panel

Toggled with `Tab` (PotPlayer-style docked panel). The video area yields automatically when expanded; drag the left edge to resize (210–640 px); width, expanded state, tab and row style are all remembered.

| Tab | Contents |
| --- | --- |
| **Playlist** | The full list of the current folder, synced live with the main window. The playing item gets a blue background + left blue bar and auto-scrolls into view |
| **Albums** | Your own cross-folder collections, persisted in `userdata/albums.json`; rename / purge stale / delete supported |
| **Browser** | Folder tree sharing the main window's model. Pick a folder to switch in place; playback in progress is never interrupted |

Two row styles, toggled from the panel's top-right: **thumbnail rows** (54 px: small cover + name + duration·resolution·size) and **compact rows** (26 px: hundreds of episodes fit on one screen). The list supports drag-sort, multi-select move / remove, search filtering, and a context menu with "add to album", "open with default program", "reveal in Explorer" and more.

**Loop mode** cycles with `L`: off → list loop → single loop → shuffle. "Auto-next" independently controls whether the next item starts when one ends.

#### General shortcuts

| Key | Action |
| --- | --- |
| `Tab` | Toggle right playlist panel |
| `Delete` | Remove selected items from the list (files on disk untouched) |
| `Wheel` / `PageUp` `PageDown` | Previous / next media |
| `Ctrl+Wheel` | Zoom images; video volume |
| `F` / `Enter` / double-click video / middle-click | Toggle fullscreen |
| `Esc` | Exit fullscreen, or close the viewer |
| Mouse side buttons (forward / back) | Next / previous |

#### Video shortcuts

| Key | Action |
| --- | --- |
| `Space` / `K` / click picture | Play / pause |
| `←` `→` | Seek back / forward 5 s |
| `Shift+←` `→` | 1 s fine seek |
| `Ctrl+←` `→` | 60 s |
| `↑` `↓` | Volume ±5 |
| `Home` / `End` | Jump to start / end |
| `[` `]` | Slow down / speed up (0.25× to 4×, 11 steps) |
| `\` | Reset speed to 1× |
| `M` | Mute |
| `L` | Cycle loop mode (off / list / single / shuffle) |
| `V` | Toggle subtitles |
| `J` | Next subtitle track |
| `A` | Next audio track (cycles real tracks only, never hits the mute one) |

#### Image shortcuts

| Key | Action |
| --- | --- |
| `Ctrl+Wheel` | Zoom around the cursor |
| `+` `-` | Zoom in / out |
| `0` | Fit window |
| `1` | Actual size 100% |
| Double-click | Toggle between "fit window" and "100%" |
| Hold left button + drag | Pan when zoomed in |
| `R` / `Shift+R` | Rotate clockwise / counter-clockwise 90° |
| `←` `→` `↑` `↓` `Space` | Previous / next image |

Fit-window never upscales small images (they stay at 100%), and GIF / animated WebP / APNG loop automatically.

---

## 🗂️ Project Layout

```
媒体播放器/
├─ main.py              Entry point: loads libmpv before importing Qt
├─ build.py             PyInstaller build script
├─ vendor/
│  └─ libmpv-2.dll      Playback core (FFmpeg built in) — download it yourself
├─ userdata/            Generated at runtime; safe to delete entirely
│  ├─ config.json       UI & playback preferences
│  ├─ resume.json       Per-file playback positions
│  ├─ metadata.json     Duration / resolution cache
│  ├─ dircache.json     Directory listing cache (for incremental re-checks)
│  ├─ albums.json       Custom albums
│  ├─ orders.json       Per-folder custom sort orders
│  └─ thumbs/           Thumbnail cache (JPEG)
└─ app/
   ├─ runtime.py        DLL search path, OpenGL surface format, portable paths
   ├─ config.py         Settings & resume storage
   ├─ media.py          Media detection, scanning, sorting, filtering
   ├─ dircache.py       Directory listing cache
   ├─ netpath.py        UNC / mapped-drive detection
   ├─ albums.py         Album & custom sort storage
   ├─ thumbs.py         Thumbnail engine + seek-bar frame preview
   ├─ theme.py          Dark palette & stylesheets
   ├─ icons.py          Segoe Fluent Icons glyphs
   ├─ welcome.py        Blank welcome page & recent folders
   ├─ browser.py        Grid / waterfall / detail list
   ├─ image_view.py     Image pan / zoom / rotate
   ├─ mpv_widget.py     libmpv OpenGL render surface
   ├─ seekbar.py        Seek bar & hover thumbnail bubble
   ├─ controls.py       Top bar & control bar
   ├─ playlist_panel.py Right playlist / albums / browser panel
   ├─ viewer.py         Immersive viewer
   └─ main_window.py    Main window
```

## 🤖 About

This project was co-written by AI: **Claude 5 Opus**, **DeepSeek 4 Pro Preview** and **Qoder**.

## 📄 License

This project is **not open source licensed** — all rights reserved. © 2026 shuguangs
