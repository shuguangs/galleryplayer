# ARCHITECTURE.md — 字幕系统交接文档

> 写给未来的 AI 会话/开发者：本文件记录实时字幕与 SRT 生成系统的完整架构、
> 各功能的确切位置、实测数据和已知的坑。改功能前先读这份，别再重新探索一遍。
> 最后更新：2026-08-29（Qwen3-ASR 接入 + 翻译异步化 + 批量 SRT 修复之后）。

## 0. 项目迁移状态（重要）

工程已从 `G:\播放器`（USB 机械盘，顺序读仅 19.5MB/s，所有"卡顿"的根源）整体迁移到
`J:\播放器`（SSD）。**源码中已无任何写死的盘符路径**——引擎定位是
`app/config.py:find_subtitle_pipeline_dir()`（用户设置 → 工程同级 live-subtitle/ →
环境变量 LIVE_SUBTITLE_DIR）。

- 两个 git 仓库：主仓库 `J:\播放器` + 引擎仓库 `J:\播放器\live-subtitle`（独立 .git）。
  改引擎代码要分别在两个仓库提交。
- 待办：git 提交当时被 Mimosa 插件的项目路径失配拦截（详见 GIT_COMMIT_INFO.md），
  改动全部在暂存区，提交信息已写好在 `J:\播放器\GIT_COMMIT_INFO.md`。
- 旧 `G:\播放器` 目录（48GB）确认无误后可删。
- dist 最新包：`J:\播放器\dist\媒体播放器\媒体播放器.exe`（254MB，
  `python build.py` 重新生成；打包前必须关掉 dist 里正在运行的播放器，否则锁
  libmpv-2.dll）。

## 1. 总体数据流

```
播放器 app/viewer.py（UI 层）
   │  提交任务：写 live-caption.log.control（JSON，generation 递增）
   │  收结果：  600ms 轮询 live-caption.log（JSON 行 + # 状态行）
   ▼
常驻引擎 live-subtitle/live_transcribe.py（pythonw 分离子进程，保活不退出）
   │  ASR: asr_engines.py（qwen/sensevoice）或 faster-whisper
   │  分句: fsmn-vad（funasr）按语音停顿切段 → asr_engines 段内标点分行
   │  翻译: translate_service.py（Ollama / llama.cpp）——异步 worker
   ▼
live-caption.log 的 JSON 行：{"g": 代次, "t": 起, "end": 止, "text": 原文, "zh": 译文}
```

**关键文件速查**（行号为大致位置，改动后会漂移，按函数名搜）：

| 功能 | 位置 |
|---|---|
| 引擎目录探测/回退 | `app/config.py` → `find_subtitle_pipeline_dir()`；同文件 `PRESET_MODELS`（档位→引擎映射）、`ASR_MODEL_SPECS`（磁盘/显存数据，UI 提示共用）、`TRANSLATE_MODEL_SPECS` |
| 实际生效引擎计算 | `app/live_engine.py` → `effective_model()`（含未安装自动回退 `model_installed`/`whisper_fallback`）；`ENGINE_VERSION = 4`（改引擎协议时 bump，旧进程会自动重启） |
| SRT 生成中互斥标志 | `app/live_engine.py` 模块级 `srt_busy`（viewer 写/读，防 seek 误杀 SRT 任务） |
| 实时字幕调度状态机 | `app/live_caption_controller.py`（纯逻辑、有单测）：rows/generation/任务区间 `task_spans`/`display_ranges()`（seekbar 青色显示用）/补洞决策 `next_full_pass_start()` |
| 实时字幕 UI/引擎进程管理 | `app/viewer.py`：`_start_live_caption`（启动，含 srt_busy 拦截）、`_switch_live_media`（seek/换片重启，起点垫头 5s，srt_busy 拦截）、`_poll_live_log`（解析+600ms 轮询+崩溃自动恢复 `_try_auto_restart_live`）、`_on_live_rows_changed`（译文后补即时刷新）、`_sync_caption_ranges` |
| seekbar 已转写显示 | `app/seekbar.py`：`set_caption_ranges`（青色段画在主轨道内）、`set_caption_front_text`（hover 气泡"转写至 XX"） |
| 设置界面 | `app/settings_dialog.py`：识别引擎下拉（带磁盘/显存标注 `_asr_model_label`）、合计占用行 `_update_combo_resources`、SRT 翻译模型独立下拉 `cb_srt_translate`、一键安装 `_start_install`/`_start_install_llama`、滚轮禁用 `eventFilter`（下拉/滑动条转发滚轮给父级） |
| 后台 SRT（单文件+批量） | `app/main_window.py`：`_gen_srt_for`（进度窗+取消按钮+系统通知 `_notify`）、`_batch_generate_srt`（**懒提交**：完成一个再提交下一个，见 §4 bug 记录）、`_pick_videos_for_srt`（多选对话框）、`_batch_cancel_srt` |
| 引擎主入口 | `live-subtitle/live_transcribe.py`：三引擎加载分支（`TORCH_ENGINES = ("qwen","sensevoice")`）、`_transcribe`（实时转写，seek 就近解码 `_decode_audio_from`，翻译异步队列 `_translate_worker`）、`_generate_srt`（SRT 任务，按 `job["translate_model"]` 选翻译后端）、`_watch_control`（control 文件轮询，mode: live/srt/prefetch/cancel）、保活主循环（任务异常防崩） |
| ASR 引擎封装 | `live-subtitle/asr_engines.py`：`load_qwen/load_sensevoice/load_vad`、`stream_transcribe`（VAD 分段逐段推理，时间戳真实）、中文路径 junction `_ascii_junction`、nagisa 垫桩 `stub_nagisa` |
| 翻译封装 | `live-subtitle/translate_service.py`：`Translator`（Ollama，SYSTEM_PROMPT 中文口语化 + 前 3 句上下文 + `clean_output`）、`LlamaServerTranslator` + `ensure_llama_server/stop_llama_server`（llama.cpp，仅 SRT）、`write_srt_file`（双侧共享 SRT 写出）、`merge_fragments`（whisper 碎片合并） |
| 环路录音模式 | `live-subtitle/live_capture.py`（系统声音，5s 滑窗 + 1s 重叠） |
| 一键安装 | `live-subtitle/install_engine.py`：GPU/CUDA 探测 → PyTorch 源选择（cu130/cu128/cu126）→ 模型下载（ModelScope）→ 加载验证；`--llamacpp-only`（llama.cpp + HY-MT2-30B） |

## 2. 引擎选型结论（实测 2026-08-28，RTX 5060 Ti 16GB）

| 引擎 | 体积/显存 | 实测 | 定位 |
|---|---|---|---|
| **Qwen3-ASR-1.7B**（默认） | 4.7GB / ~6GB | 中英 CER 0%、日 2.8%、52 语言自带语种识别 | 实时+SRT 首选 |
| SenseVoice-small | 0.9GB / ~2GB | 中日韩粤快而准，**英文严重不行** | 低配/纯中文 |
| faster-whisper 各档 | 0.5-5.8GB | 通用，中文数字偶错 | 兼容/冷门语言 |
| HY-MT2-30B（翻译，llama.cpp） | 11.6GB / moe30 约 4-5GB | 翻译质量最佳，16-34 t/s | **仅 SRT**，按需启停 |
| HY-MT2-7B | 4.6GB | ❌ 会编造内容（把台词当对话回答），已排除 | 不可用 |

翻译质量两大杠杆（与换模型同等重要，见 `translate_service.py` 模块注释）：
中文口语化 SYSTEM_PROMPT（旧英文 prompt 导致直译腔）+ 前 3 句上下文（急救口令类
台词只有带上下文才译对，实测不增加稳态延迟）。

基准脚本：`live-subtitle/bench_asr.py`（识别）、`bench_translate.py`/`bench_context.py`（翻译）、
`bench_offload.py`（GPU/CPU 分层）。样本 `samples/asr_{zh,ja,en}.16k.wav`（TTS 合成，
原文已知可算错误率；.16k 后缀是重采样版，live_capture 只吃 16k）。

## 3. 环境坑清单（已修复，别再踩）

1. **PyTorch 必须 CUDA 版**：venv 里 CPU 版 torch 运行时才报错（"Torch not compiled
   with CUDA enabled"）。`install_engine.py` 按 `nvidia-smi` 的 CUDA 版本自动选
   cu130/cu128/cu126 源；RTX 5060 Ti（Blackwell）最低 cu128。
2. **cu12 与 cu13 运行库不能同时进 PATH**：whisper(ctranslate2) 要 venv 的
   `nvidia-cudnn-cu12`，torch 自带 cu13 cuDNN；混入报
   `CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH`。三个入口脚本都按引擎条件注入
   （`live_transcribe.py` main 开头 / `live_capture.py` / `app/viewer.py` 启动子进程的 env 构造处）——**加新引擎时记得这个条件**。
3. **中文路径**：funasr/sentencepiece 读不了含中文的模型路径（J:\播放器 本身含中文）
   → `asr_engines._ascii_junction` 自动建 NTFS junction 到 LOCALAPPDATA；
   qwen-asr 的对齐依赖 nagisa 同样失败 → `stub_nagisa` 垫桩（识别不受影响）。
4. **MoE 模型（HY-MT2-30B）在机械盘上完全不可用**（随机读专家权重）；
   Ollama 0.33.1 不支持 hy_v3 架构（导入卡死 parsing GGUF），必须 llama.cpp ≥b9993
   （已放引擎目录 `llamacpp/`，gitignore 不入库）。
5. dist 打包时 userdata 会被暂存到 `.userdata-stash`；打包失败要手动移回或删掉暂存目录再重跑。

## 4. 历史 bug 记录（防复发）

- **SRT 时间戳提前**（v1.3.x）：旧后处理把长段压到 ≤10s 再按字数比例分时间。
  现在时间轴全部来自真实语音时间（whisper 词级 / qwen+sv 的 VAD 段），
  `write_srt_file`/`merge_fragments` 已归一到 translate_service 共享——别再两边各写一份。
- **批量 SRT 只跑最后一个**：引擎 control 槽只保留最新 generation，一次性全量提交
  会覆盖前面的任务。`_batch_generate_srt` 已改懒提交（完成一个再提交下一个）。
- **实时字幕静默消失**：崩溃无提示 + 心跳停止 + 无 toast 的复位分支三连。
  现在崩溃自动恢复一次（`_try_auto_restart_live`，10 分钟限一次），stderr 落盘
  `live-caption.err`，复位必弹 toast。
- **seek 误杀 SRT**：`live_engine.srt_busy` 互斥（SRT 进行中 viewer 拦截实时任务提交）。
- **启动掉帧**：健康检查的 tasklist/wmic 曾在 UI 线程同步跑（每次数百 ms）。
  现在 `_is_live_alive` 读 2s 缓存 + 后台线程检查；启动宽限期 90s。
- **跳转转写响应慢**：`decode_audio` 全量解码已改 `_decode_audio_from`（av 容器 seek，
  输出与全量切片波形相关性 1.0000）。

## 5. 已知边界（刻意设计，不是 bug）

- 青色进度条（seekbar）显示的是**每个转写任务的真实覆盖区间**（跳转会留空洞，
  补洞后弥合）——不是"从 0 连续推进"的进度条（用户确认过这个语义）。
- HY-MT2-30B 不计入设置界面"识别+翻译合计占用"行（它仅 SRT 时启动，不与实时共存）；
  配实时字幕的话 16 t/s 太慢（一句 2 秒+），所以实时字幕翻译只有 Ollama 后端。
- qwen/sensevoice 无词级时间戳，字幕行边界是 VAD 段 + 标点分行，段内按字符占比
  近似（总跨度真实）；30s 无停顿的极端音频会在 30s 处硬切。
- 引擎单进程串行：SRT 与实时字幕共用常驻进程，靠 generation 和 srt_busy 互斥。
- `translate_service` 的 endpoint 仅允许本机服务（Ollama 11434 / llama-server 8020）。
- Mimosa 安全插件对此项目报的 11 个"高危"已逐条核实为本地应用语境误报
  （127.0.0.1 调用、用户自选文件路径），main_window 随机数已改 secrets。

## 6. 验证方式

- 单测（8 项，纯逻辑，几秒）：项目根 `python -m unittest discover -s tests`
  （controller 的 display_ranges 跳转/补洞场景、译文原地更新、事件解析等）。
- 引擎级冒烟（跑真模型，分钟级）：
  `cd live-subtitle && .venv/Scripts/python.exe live_transcribe.py samples/asr_zh.16k.wav
  --model qwen --lang auto --translate --ollama-model qwen3:8b --log <路径>`
  → 看 log：原文行先出（zh 空）、更新行带译文、TASK_DONE 结尾。
- SRT 端到端：`app/live_engine.py` 的 `start_preload()` + `submit({"mode":"srt",...})`，
  读 srt-generation.log 等 `# SRT_READY`。
- UI 冒烟（不开窗口）：`QT_QPA_PLATFORM=offscreen`，PATH 加 `vendor/`（libmpv），
  Viewer 实例化需要 `ThumbnailCache()` 参数。
