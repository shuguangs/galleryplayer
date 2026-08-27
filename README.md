# Live Subtitle Prototype

外语视频实时听音字幕 + 翻译原型（独立实验仓库，不影响主项目）。

## 方案

- 识别：faster-whisper（CTranslate2，GPU: CUDA），模型 large-v3（float16）
- 翻译：（待定——Ollama 本地模型 / 在线 API）

## 硬件实测（2026-08-27）

- RTX 5060 Ti 16GB + i5-12490F + 32GB 内存：**带得动 large-v3 GPU 推理**
- 依赖：faster-whisper + ctranslate2（Python 3.13 兼容 ✓），
  需 `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`，DLL 目录加 PATH

## 性能实测（jfk.wav 11s 英文）

| 项目 | 耗时 | 备注 |
|---|---|---|
| 模型加载 | 127-437s | 一次性；集成时模型常驻 |
| 首次转写 | 145s | cuDNN 引擎编译一次性；warmup 后快 |
| **warmup 后转写** | **1.5-1.7s** | **实时率 x6.5-7.6**，实时字幕可行 |

## 运行

```powershell
$env:PATH = "$PWD\.venv\Lib\site-packages\nvidia\cublas\bin;$PWD\.venv\Lib\site-packages\nvidia\cudnn\bin;$env:PATH"
$env:HUGGINGFACE_HUB_CACHE = 'G:\播放器\live-subtitle\models\hf\hub'
$env:HTTPS_PROXY = 'http://127.0.0.1:12589'   # 首次拉模型需要
.\.venv\Scripts\python.exe transcribe.py samples\jfk.wav large-v3 cuda
```

## 环境

- Python 3.13.15（venv: `.venv`）
- faster-whisper 1.2.1 / ctranslate2 4.8.1