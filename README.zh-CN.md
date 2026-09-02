# 🎬 GalleryPlayer（媒体播放器）

> 一个专门用来**看图、看视频**的本地播放器：把整个文件夹打开，图片和视频混在一个列表里，滚轮一滚就能挨个翻过去——还能给外语视频配上**实时双语字幕**，全程在你自己的电脑上完成。

![平台](https://img.shields.io/badge/平台-Windows-0078D6?logo=windows&logoColor=white)
![便携](https://img.shields.io/badge/便携-免安装-green)
![实时字幕](https://img.shields.io/badge/实时字幕-AI双语-8A2BE2)
![版本](https://img.shields.io/badge/版本-2.2-orange)
![License](https://img.shields.io/badge/License-保留所有权利-lightgrey)

**English version: [README.md](README.md)**

---

## ✨ 核心功能

### 🖼️ 一个列表看遍所有

打开文件夹，整个合集变成一条可滚动的列表——图片和视频并肩排列，滚轮翻页一样挨个看过去。三种视图任选：整齐的**方块格子墙**、按原始比例的**瀑布流**、或者带全部文件信息的**明细列表**。

压缩包也一样：双击 `zip` / `rar` / `7z` 直接当文件夹打开——浏览、预览、播放里面的内容，无需先解压。

### 🎙️ 实时双语字幕

看外语视频时，字幕随语音即时滚出——原文一行，中文翻译一行紧跟其后。全程**本地离线**运行，不上传任何音频。

- 本地语音识别 + 本地大模型翻译
- 已转写的区间在进度条上以青色标出，一眼看到字幕跟到哪了
- 引擎先对整片采样判断主导语言再落笔，中途改判会自动重写误判的段落
- 拖动进度条、来回跳转都不怕，转写自动跟着你走

### 📝 一键后台生成 SRT

右键任意视频——或者一次框选一批——完整的双语 `.srt` 字幕就在后台生成，期间你可以继续正常使用软件。浮动窗口实时显示进度，完成弹系统通知，中途随时可取消。

### 🧠 引擎由你挑

三种语音识别引擎随时切换，每个都标好磁盘占用和显存需求，另有可选的重量级翻译模型追求最高质量。显存不足时自动回退到更小的引擎。

| 引擎 | 磁盘 / 显存 | 说明 |
| --- | --- | --- |
| **Qwen3-ASR-1.7B**（默认） | 4.7GB / ~6GB | 52 语言自动识别，质量最佳 |
| SenseVoice-small | 0.9GB / ~2GB | 中日韩粤快而准，CPU 也能跑 |
| faster-whisper（tiny–large-v3） | 0.5–5.8GB / 1–8GB | 覆盖最广，低配兜底 |

翻译默认走本地 **qwen3:8b**（Ollama）；生成 SRT 时可选 **HY-MT2-30B**（llama.cpp 后端，按需启动、用完即关）获得更高翻译质量。

所有引擎在设置里**一键安装**：自动探测显卡、选对 CUDA 版 PyTorch、从国内高速源拉取模型。

### 🎒 便携至上

整个文件夹拷进 U 盘，到任何电脑都能直接跑——不装、不写注册表、不留痕迹。超过一分钟的视频自动**记住上次看到哪**。播放器开着时，在资源管理器双击文件**直接在当前窗口播放**，绝不多开一个。

> [!TIP]
> 还有更多等你亲手探索——截图、GIF 录制、专辑、播放列表，以及一个懂得安静退场的深色界面。[下载](#-快速上手)试试吧。

---

## 🚀 快速上手

1. 下载并解压——**保留整个文件夹**（.exe 旁边的文件是程序的一部分，别只抠出 exe）。
2. 全新电脑上先运行一次 `安装运行环境.bat`（安装必需的系统组件）。
3. 双击 `媒体播放器.exe`。

**首次启动**会让你选界面语言（中文 / English），之后随时可在设置里改。

> [!NOTE]
> 默认字幕引擎建议 **NVIDIA 显卡（显存 8GB+）**。显存不够？在设置里换成 SenseVoice 或小号 whisper 档位即可，CPU 也能跑实时字幕。

---

## 🔨 自行编译

环境要求：**Windows 10 及以上**、**Python 3.12+**、`git`。

```bash
# 1. 克隆仓库
git clone https://github.com/shuguangs/galleryplayer.git
cd galleryplayer

# 2. 安装运行依赖
pip install -r requirements.txt

# 3. 获取 libmpv（播放核心，约 112 MB——超出 git 限制需手动下载）
#    下载 mpv 的 libmpv dev 版本，然后把 libmpv-2.dll 放到：
#      vendor/libmpv-2.dll

# 4. 源码运行
python main.py
```

打包成便携版：

```bash
pip install pyinstaller
python build.py          # 产出 dist/媒体播放器/
```

字幕引擎位于 `live-subtitle/`，有自己独立的 Python 环境——在**设置 → 实时字幕 → 一键安装**里自动配好（自动探测显卡、下载模型），见上文引擎章节。

---

## 💡 常见问题

**视频播不了 / 卡顿？**
设置（`Ctrl+,`）→ 换一个「解码模式」——从自动切到软件解码能解决大多数老显卡问题。

**实时字幕 / SRT 在哪开？**
实时字幕：播放中右键视频 → 字幕菜单。SRT：在浏览列表里右键视频。两者都需先在设置里一键安装引擎。

**显存不够？**
换小一点的引擎（SenseVoice 只需 ~2GB），翻译模型换轻量版；软件也会在显存紧张时自动回退。

**上次看到哪了？**
超过一分钟的视频自动记住播放位置，无需操心。

---

## 🗂️ 数据都在哪

你的所有个人数据都在程序旁边的 `userdata` 文件夹里：设置、播放进度、缩略图缓存。删掉它即完全重置。模型文件在 `live-subtitle/models/`，从不随程序打包。

## 🤖 关于

本项目由人类与 AI 协作完成。

**模型**

[![Claude](https://img.shields.io/badge/Claude-Anthropic-191919?logo=anthropic&logoColor=white)](https://github.com/anthropics)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-DeepSeek-blue)](https://github.com/deepseek-ai)
[![GLM](https://img.shields.io/badge/GLM-Z.ai-386BF0)](https://github.com/zai-org)

**Agent 平台** —— Claude Code · Qoder · ZCode · DSH

它们各自负责了不同的部分——播放器界面、实时字幕引擎、架构审查与优化。提交历史中带有 `Co-authored-by` 署名。

## 📄 许可

本项目**未开放源码授权**——保留所有权利。© 2026 shuguangs
