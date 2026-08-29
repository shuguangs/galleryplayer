# CODE_REVIEW_LOG.md — 交叉审查记录（已修复 Bug 与已确认无误项）

> 记录 v1.5.0–v1.5.4 期间对 v1.4.0..HEAD 大批量改动的对抗性交叉审查结果：
> 两轮「自查 + 多路独立子代理复查」，共确认并修复 21 个问题，另有一批
> 高风险区域经核查确认无误。写给未来的会话/开发者：**再次排查时先读本文件
> 的"已排除"清单，不要重复劳动**；改完相关代码后按"复查要点"回归。
>
> 最后更新：2026-08-29（v1.5.4 发布时）。

## 排查方法（可复用）

1. 自查：逐文件重读 v1.4.0..HEAD 全量 diff，按已知漏出 bug 的模式检索。
2. 子代理交叉复查：按模块拆 2–3 路（UI/生命周期、主窗口/控制面、引擎/协议），
   每路附上一轮"已排除清单"避免重复，要求只报可触发路径明确的 bug。
3. 每个发现先核对代码上下文确认可触发，再修复；修完跑单测 + 离屏冒烟。

**漏出 bug 的共性规律**：几乎全部集中在「多组件状态机的复位路径」——互斥
标志（srt_busy）、队列对象（_batch_srt_jobs）、脏标记（last_playlist.json）、
进程身份（pid 文件）在取消/关窗/完成/切换等异常时序下没有归位。正常路径怎么
点都正常，离屏冒烟测不出，只有特定时序触发。

---

## 第一轮（v1.5.3，自查 + 单路审查，7 项修复）

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 1 | 第二次生成 SRT 被上个任务的 SRT_READY 残留误判为已完成（尾部缓冲跨任务不重置），srt_busy 提前解除 | app/main_window.py `_gen_srt_for` | 新任务重置 `_srt_job_log_tail = ""` |
| 2 | 实时字幕自动存盘脏检查按行数比较——译文后补是原地更新（行数不变），导出的 .live.srt 永远停在纯原文 | app/viewer.py `_save_live_srt` | 控制器引入 `data_version`（增/改行即递增），按版本号判脏 |
| 3 | GIF 采样线程与 mpv shutdown 竞态：关窗时线程可能卡在一次 4K screenshot | app/viewer.py | stop 事件 + shutdown 前 join(≤2s)；`_finish_gif` 重入保护 |
| 4 | 环路（系统声音）模式切回音轨会"复用"环路进程——state 是上次音轨的陈旧数据，环路不读 control | app/viewer.py 复用分支 | 复用前校验 pid 进程 cmdline 含 `live_transcribe` |
| 5 | 引擎崩溃自动恢复只写 control 文件（死进程无人读），从不真正重建 | app/viewer.py `_try_auto_restart_live` | 改调 `_start_live_caption()` 真重建 |
| 6 | 翻译降级把空译文当失败：纯符号台词连发 5 句误停整个任务翻译 | live-subtitle/live_transcribe.py | `_translate_with_retry` 返回 (译文, 是否失败)，空译文不算失败 |
| 7 | 崩溃恢复弹窗点"否"不清脏标记（每次启动重弹）；损坏 JSON 会抛进槽 | app/main_window.py `_maybe_restore_playlist` | 拒绝时写回 clean=true；解析整体 try 包裹 |
| 8 | 只标 A-B 循环的 A 点时进度条什么都不画 | app/seekbar.py | `b >= a` 即画刻度 |
| 9 | 大图后台解码失败提示不带文件名 | app/image_view.py | `_decoded` 信号携带文件名 |
| 10 | live_capture 状态行/启动行硬编码 "→ zh" | live-subtitle/live_capture.py | 用 `args.target_lang` |

## 第二轮（v1.5.4，三路子代理并行复查，14 项修复）

### 高危

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 1 | 批量 SRT 队列完成后/取消后不清空 → 第二次批量永久被"已有任务"拒绝 | app/main_window.py | done==total 与取消路径置 `_batch_srt_jobs = []` |
| 2 | 单文件与批量 SRT 不互查对方状态 → control 槽互相覆盖、先者轮询永久挂起 | 两处入口守卫 | 各自查 `_srt_active`/`_batch_srt_jobs`，命中提示 busy |
| 3 | 关闭 SRT 进度窗（X/Esc）泄漏 srt_busy → 实时字幕永久"已暂停" | `_srt_dialog_closed` | 轮询者已停，就地 `srt_busy = False` |
| 4 | 视频无音轨/损坏时引擎任务异常只写 live log（播放器只认 job log）→ SRT 对话框永久挂起 | live_transcribe.py 主循环 except | srt 任务追加写 `# SRT_ERROR` 到 job log |
| 5 | 常驻模式停止后菜单"单向门"：再点仍是关闭，永远无法恢复 | `_toggle_live_caption` | `elif _live_paused: 清暂停并 _start_live_caption()` |
| 6 | 全新拉起引擎漏 begin_media → task_spans 无条目：首个任务青色覆盖区全程缺失、span_covered 恒 False、自动恢复无 media | spawn 分支 | 补 `begin_media(current_media, start, 0, False)` |
| 7 | `_maybe_restore_playlist` 缺 `import json`，NameError 被 except 吞掉 → 崩溃恢复功能整体静默失效 | app/main_window.py | 顶部补 import |
| 8 | 打包版"一键安装"用 sys.executable 启动的是播放器自己，安装永不执行 | app/settings_dialog.py | frozen 下从 PATH 找 python/py -3，找不到给出明确指引（i18n） |

### 中危

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 9 | torch 路径 TASK_DONE 前不等翻译队列（whisper 路径有 join）→ 换代次后尾部译文永久空白 | live_transcribe.py 两处 | 收尾前 `_translate_q.join()` |
| 10 | `_transcribe` 早退（模型重载失败/媒体缺失）不发 TASK_DONE → 补洞/重启调度停摆 | live_transcribe.py | 早退前补 `TASK_DONE {generation}` |
| 11 | 从播放列表删除正在播放的项 → 旧片进度写进下一片的断点（index 先指向新项，mpv position 还是旧片） | `_remember_position` | 校验 `item.path == self._loaded_media_path`（show_index 时记录） |
| 12 | 图片模式宽窗口下 `_update_flex` 无条件 re-show，复活全套视频控件 | app/controls.py | 折叠池剔除非当前媒体类型的控件 |
| 13 | 拖动进度条中滚轮切图 → seekbar 被隐藏收不到 release，`_scrubbing` 永久卡死（进度条与字幕 seek 检测全失效） | app/seekbar.py | `hideEvent` 里复位 scrubbing 并发 scrub_finished |
| 14 | 打开压缩包不作废进行中的文件夹扫描 → done 批次覆盖压缩包视图 | `_enter_archive` | 进入时 `_scan_token += 1` 并清流式状态 |

### 低 / 健壮性

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 15 | 拖放/最近文件固定 400ms 等待，冷文件夹（网络盘）下静默丢弃 | `_open_path_when_listed` | 改轮询等待列表就绪（≤8s） |
| 16 | 引擎 `submit` 无管线时返回 False 混入代次状态机（viewer 用 `is None` 判断） | live_engine.py | 统一返回 None |
| 17 | 实时字幕落后播放时每 ~3.2s cancel 在途任务重提，永远追不上且 toast 常驻 | `_poll_live_log` | 前沿停滞 8s 才重提一次 |
| 18 | live_capture 录音线程死亡（拔设备）被心跳掩盖，字幕无声消失 | live_capture.py | 录音线程异常即退出进程，触发播放器自动重启 |
| 19 | 悬空 junction（盘符变化）`exists()` 为 False 删不掉 → SenseVoice 永久加载失败 | asr_engines.py `_ascii_junction` | `os.path.islink` 判断重建 |
| 20 | install_engine 半截 zip（够大但损坏）解压崩溃且幂等跳过下载反复失败 | install_engine.py | `zipfile.is_zipfile` 校验，坏则删除重下 |
| 21 | closeEvent/matches 热路径每次现算 effective_model（nvidia-smi + rglob） | live_engine.py | 按（设置项, monkeypatch）键缓存，key 变即失效（单测兼容） |

## 误报澄清（教训）

- **搜索框防抖从未失效**：`textChanged.connect(QTimer.start)` 实际有效。第一轮
  "实测"与审查代理都犯了同一个错误——在 300ms 单发定时器**触发完之后**才检查
  `isActive()`（此时必然 False）。正确的验证是在 setText 后立即检查。lambda 写法
  保留（等效且直观），但勿再据此断言 direct-connect 无效。

---

## 已确认无误清单（复查过，勿重复排查）

- **设置三处一致性**：`live_caption_idle_unload`（int，config 默认/设置页/引擎 state/matches 四处一致）、`live_translate_target`（zh/zh-Hant/en 枚举一致）、`srt_export_format`（viewer 命名与引擎 job["format"] 一致）、`ENGINE_VERSION=5` 两侧一致。
- **control 文件协议**：tmp+replace 原子写；所有 submit 调用点全在 GUI 线程（无代次竞争）；`_watch_control` 透传完整 job；last_generation 防重放。
- **prefetch 缓存**：key=(path,size,mtime) 失效正确；cancel 代次前进即弃；TASK_DONE 后才提交（无并发取消冲突）；空闲卸载后 `_ensure_model` 覆盖 live/srt/prefetch 三路径。
- **控制器**：`_row_index` 只增不改下标、三条 reset 路径同步清空、译文回填 discard/add 对称（乱序回填有单测）；`span_covered`（VAD 间隙语义）与前沿 max(end) 有单测。
- **image_view 线程纪律**：worker 只建 QImage/QImageReader；QPixmap 仅 GUI 线程；`_load_seq` 失配丢弃；`_decoded`/`loaded` 信号链。
- **批量/单文件日志尾部拼接的结束标记检测**正确（64 字符 tail + 跨读拼接）。
- **翻译**：有界队列 drop-oldest 记账平衡（join 不死锁）；worker 与 ensure_ollama 启动竞态无害；降级后残留项的空译文重复行被 accept_line 精确去重；llama.cpp 按需启停。
- **主窗口**：set_folder 的 token 失效、`_stream_items` 清理、缓存命中静默校验分支；导航历史 push/trim/back/forward；缩略图队列按 generation 失效；`_JsonStore`/DirCache/MetadataCache 保存均有锁；single_instance 转发与 `handle_external_paths` 组合正常。
- **引擎杂项**：log_fp 双线程整行写无交错损坏；msvcrt 锁失败分支正确退出；心跳 `_stop_hb` 语义；`_decode_audio_from` keep_from 恒非负、seek 超尾安全；`stream_transcribe` 时间戳偏移与截断。
- **设置滚轮过滤器**：直接拨滚动条无重入投递（v1.5.2 修复后复核）、showEvent 幂等重装、23 控件全覆盖。
- **i18n**：本轮全部新增键（restore_playlist/scan_errors/live_resident_hint/live_caption_quit_text/install_no_python 等）占位符与 `.format` 一一对齐。

## 已知未处理（有意保留 / 待议）

- 视频→图片→视频后实时字幕不自动恢复（切图片时有意关闭；是否要"回到视频自动续上"属产品决策）。
- SRT 结束检测的 64 字符 tail 理论上可被 >64 字符的跨读截断击穿（引擎小 buffer 单次 append，实际概率极低；已记入观察）。
- `image_view.loaded` 信号只带宽高不带身份，当前路径安全（切图必过 load/clear），新增"切走不动 image_view"的路径时需补校验。
- 打包版一键安装依赖目标机器装有 Python（已给明确报错指引）；长期可考虑随包内嵌 embeddable python。

## 回归要点（改这些区域时必测）

1. **引擎生命周期**：开→停止（常驻/非常驻）→重开；换片/seek/换源（loopback↔audio）；SRT 生成中撞实时字幕（两个方向）；引擎进程手动杀掉后的自动恢复。
2. **SRT 任务族**：单文件/批量各自完成、取消、关窗（X/Esc）；第二次批量；生成中关播放器。
3. **滚轮三处语义**：设置界面（滚动不误改）、播放器（切媒体）、seekbar（忽略）。
4. **断点续播**：正常退出重开；删除正在播放的项；外部打开/拖放/最近文件（冷文件夹）。
