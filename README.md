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

## 真实剧集测试（The Pitt S02E01 60s 片段）

- 识别：医学术语全部正确（tamponade / tension pneumo / thrombosis / TNK）
- 翻译：qwen2.5:7b（q3_k_m 量化）→ 中文质量良好，专业术语准确
- 输出：双语字幕 + `*.zh.srt` 导出（`samples/pitt_s02e01_60s.zh.srt`）

## 离线导入翻译模型（ollama pull 慢时用）

```powershell
# 1) 从 HuggingFace（走代理）下载单文件 GGUF（不要用带 00001-of-00002 的分片）
curl.exe -L -o models\gguf\qwen2.5-7b-instruct-q3_k_m.gguf `
  "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q3_k_m.gguf" `
  --proxy http://127.0.0.1:12589

# 2) 导入 ollama（Modelfile 已在仓库里）
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" create qwen2.5:7b -f Modelfile
```

## 一键配置 & 设置界面

- `setup.ps1`：venv + 依赖 + GPU 检测 + whisper 模型 + Ollama + 翻译模型，一键完成
- `settings_gui.py`：图形界面改配置（模型/设备/语言/翻译开关等）
- `config.yaml`：手动配置入口（模板 `config.example.yaml`）

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