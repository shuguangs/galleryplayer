# Live Subtitle Prototype

外语视频实时听音字幕 + 翻译原型（独立实验仓库，不影响主项目）。

## 方案

- 识别（三引擎可选，设置界面切换）：
  - **Qwen3-ASR-1.7B**（默认）：transformers + qwen-asr，52 语言 + 22 中文方言，自带语种识别
  - **SenseVoice-small**：funasr，中日韩粤最快
  - **faster-whisper**（tiny~large-v3）：CTranslate2，低显存兜底 / 语言覆盖最广
- 分句：fsmn-vad 按语音停顿切段（时间戳真实），段内按标点分行
- 翻译：Ollama 本地模型（qwen2.5:3b/7b、aya-expanse:8b）

## 引擎对比实测（2026-08-28，RTX 5060 Ti 16GB）

样本：TTS 合成中/日/英各一段（原文已知，可算错误率）+ The Pitt S02E01 60s 真实影视音频。
中日看 CER、英文看 WER，数字书写差异（2026 vs 二零二六）按数值等价处理。

| 引擎 | 加载 | 中文 CER | 日语 CER | 英文 WER | 真实英文 60s |
|---|---|---|---|---|---|
| **Qwen3-ASR-1.7B** | 15s | **0%** | **2.8%** | **0%** | 9.8s，质量最好 |
| Whisper medium | 10s | 0.9%（数字听错） | 4.3% | 0% | 8.9s |
| SenseVoice-small | 15s | 0.9% | 3.6% | 24.4% | 输出破碎 |

结论：Qwen3-ASR 全面领先且自带语种识别，故设为默认；SenseVoice 保留给低显存/纯中文场景，
whisper 保留给显存不足与冷门语言。

## 环境要点（踩过的坑）

- **PyTorch 必须装 CUDA 版**：`install_engine.py` 按 `nvidia-smi` 报告的 CUDA 版本自动选
  cu130/cu128/cu126 源；CPU 版 torch 会在运行时才报 "Torch not compiled with CUDA enabled"。
- **cu12 与 cu13 运行库不能同时进 PATH**：whisper(ctranslate2) 需要 venv 里的
  `nvidia-cudnn-cu12`，torch 自带 cu13 cuDNN；混在一起报
  `CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH`。三个入口脚本都按所选引擎条件注入。
- **中文路径**：funasr/sentencepiece 读不了含中文的模型路径 → 自动建 NTFS junction 到
  ASCII 路径；qwen-asr 的日语对齐依赖 nagisa 同样失败 → 垫桩跳过（不影响识别）。
- 依赖：Python 3.13 兼容 ✓（faster-whisper 1.2.1 / ctranslate2 4.8.1 / funasr 1.4.x /
  torch 2.13+cu130 / qwen-asr）

## 硬件实测（2026-08-27）

- RTX 5060 Ti 16GB + i5-12490F + 32GB 内存：Qwen3-ASR / large-v3 均带得动
- 显存参考：Qwen3-ASR ~6GB、large-v3 ~8GB、medium ~4GB、SenseVoice ~2GB

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

## 方案选择（用户自选，改 config.yaml 或设置界面即可）

### 识别（whisper 档位，GPU 实测 60s 片段）

| 模型 | 体积 | 转写速度 | 准确度 | 适合 |
|---|---|---|---|---|
| small | ~460MB | x4.2 实时率 | 个别词有错 | 极速/低配机器 |
| medium | ~1.5GB | x8.4 实时率 | 准确 | whisper 里性价比最高 |
| large-v3 | ~3GB | x6.3 实时率 | whisper 最准 | 冷门语言/追求覆盖 |

### 识别（其他公司方案，同片段实测）

| 方案 | 公司/组织 | 体积 | 速度 | 质量（实测 The Pitt 60s） |
|---|---|---|---|---|
| streaming-zipformer-en | k2-fsa (sherpa) | 296MB | x10.7 | ❌ 严重幻觉（"VERY MEMORY OF THE LAW" 循环） |
| MiMo-V2.5-ASR | 小米 | 32GB (F32) | — | ❌ 未支持日语，体积/显存要求过高，未采用 |
| **SenseVoice-small** | 阿里 (FunASR) | 914MB | **x16 CPU** | ⚠️ 中日韩粤好，英语弱（整段直推会破碎，需 VAD 切段） |
| **Qwen3-ASR-1.7B** | 阿里 (Qwen) | 4.7GB | x6 GPU | ✅ 实测最准，52 语言 + 自带语种识别 |
| faster-whisper | OpenAI（CTranslate2） | 1.5GB+ | x8.4 GPU | ✅ 准确，医学术语全对 |

### 翻译（Ollama 模型，bench_translate.py 实测 2026-08-28）

| 模型 | 体积 | 实测结论 |
|---|---|---|
| **qwen3:8b** | 5.2GB | **默认，口语化最自然**（"Cut me some slack"→"行行行，给我点面子吧"） |
| translategemma:4b | 3.3GB | 谷歌翻译专精，日译最稳、体积最小；英语习语略直译 |
| qwen2.5:7b | 3.8GB | 旧默认；换中文口语化 prompt 后明显改善 |
| qwen2.5:3b | 2.1GB | 最省资源 |
| aya-expanse:8b | 5.1GB | 偏直译（"I need an attending"→"我需要关注"） |
| HY-MT2-7B | 4.6GB | ❌ 会编造内容（把台词当对话回答），已排除 |

翻译质量两大杠杆（与换模型同等重要，见 translate_service.py）：
1. **中文口语化 prompt**：旧英文 prompt 只说 "Output ONLY the translation"，同一模型输出
   "算法不起作用。更快更深，请。"；换中文 prompt 后变成"算法不行，再快点，再深入点"。
2. **前 3 句上下文**：逐句独立翻译看不到剧情，"Off the chest"（急救口令）只有带上下文才
   译对；实测带上下文不增加稳态延迟（KV 缓存复用）。

## HY-MT2-30B-A3B（可选大模型，未接入，验证记录）

alphaZimuth/Hy-MT2-30B-A3B-APEX-GGUF 的 Imatrix-I-Nano（11.59GB，MoE 30B 总参/3B 激活）。
llama.cpp b10675（hy_v3 架构需 ≥b9993）+ RTX 5060 Ti 16GB + SSD 实测：

| 配置 | 生成速度 | 显存峰值 | 备注 |
|---|---|---|---|
| `-ngl 99` 全 GPU | 76-107 t/s | ~14.3GB | 与 Qwen3-ASR（6GB）不能共存 |
| `--n-cpu-moe 13` | ~34 t/s | 待精确复测 | 作者 12GB 卡的推荐档 |
| `-ngl 0` 全 CPU | 7.3 t/s | ~0 | 慢，仅兜底 |

质量：✅ "We're calling it"（急救=宣布死亡）→"我们宣布吧。很遗憾，病人去世了。"，
习语正确、无 7B 版的编造问题。

**未接入原因**：Ollama 0.33.1 不支持 hy_v3 架构（导入卡死在 parsing GGUF），接入需为
播放器引入 llama.cpp 作为第二后端。定位"可选非必装"：默认翻译仍是 qwen3:8b，30B 仅作
显存充裕用户的高级选项。

**硬件教训（重要）**：本项目曾在 USB 机械盘（顺序读 19.5MB/s）上运行，模型加载 40-90 秒；
迁移 SSD 后 Qwen3-ASR 冷启动 0.2s。MoE 模型随机读专家权重，机械盘上完全不可用。

### 翻译（旧档位实测，供参考）

| 模型 | 公司 | 体积 | 短句延迟 | 长句/术语 | 实测发现 |
|---|---|---|---|---|---|
| qwen2.5:3b | 阿里 | 2.1GB | <0.2s | 一般 | 日常对话够用 |
| **qwen2.5:7b** | 阿里 | 3.8GB | <0.2s | 最准（死肺滑移） | **推荐**（医疗/专业内容） |
| aya-expanse:8b | Cohere | 5.1GB | 0.2s | 可用（死滑动肺） | 多语功底好但长句慢（27s）、EOS 标记泄漏（已自动清理） |

> 多语言验证：日/韩/西/法/德/英 → 中文 qwen/aya 均翻译自然准确（qwen 原生多语支持）

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