# CODE_REVIEW_LOG.md — 交叉审查记录（已修复 Bug 与已确认无误项）

> 记录 v1.5.0–v1.5.5 期间对 v1.4.0..HEAD 大批量改动的对抗性交叉审查结果：
> 三轮「自查 + 多路独立子代理复查」，共确认并修复 51 个问题，另有一批
> 高风险区域经核查确认无误。写给未来的会话/开发者：**再次排查时先读本文件
> 的"已排除"清单，不要重复劳动**；改完相关代码后按"复查要点"回归。
>
> 最后更新：2026-08-29（第三轮，30 项修复）。

## 排查方法（可复用）

1. 自查：逐文件重读 v1.4.0..HEAD 全量 diff，按已知漏出 bug 的模式检索。
2. 子代理交叉复查：按模块拆 2–4 路（UI/生命周期、主窗口/控制面、引擎/协议，
   第三轮另加一路专门**复查前两轮的修复本身**），每路附上历轮"已排除清单"
   避免重复，要求只报可触发路径明确的 bug。
3. 每个发现先核对代码上下文确认可触发，再修复；修完跑单测 + 离屏冒烟。

**漏出 bug 的共性规律**：几乎全部集中在「多组件状态机的复位路径」——互斥
标志（srt_busy）、队列对象（_batch_srt_jobs）、脏标记（last_playlist.json）、
进程身份（pid 文件）在取消/关窗/完成/切换等异常时序下没有归位。正常路径怎么
点都正常，离屏冒烟测不出，只有特定时序触发。

**第三轮补充的两条规律**（新发现，下次优先按这两条检索）：

- **"修了一半"比没修更隐蔽**：第三轮 8 项高危里有 4 项就长在前两轮的修复旁边
  （取消路径清了队列没清代次、复位标志只在复用分支做了、加了校验但没修根因的
  下标失同步）。改状态机时把该状态的**每一个**写入点列出来逐条对账。
- **两侧协议各改一半**：播放器与引擎、面板与播放器之间的数据契约（JSON 字段、
  列表顺序、pid 身份）只要有一侧演进，另一侧就会静默丢数据——环路字幕整功能
  失效、面板双击播错文件都是这个形状。

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

## 第三轮（v1.5.5，四路子代理并行复查 + 逐条独立复核，30 项修复）

四路分工：A=复查前两轮的 21 个修复本身；B=playlist_panel/browser/thumbs/media/
archive（前两轮基本没碰的模块）；C=viewer/controls/seekbar/live_engine 状态机；
D=引擎侧全部。共报 30+ 条候选，逐条读真实代码复核后确认 30 项。

### 高危

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 1 | **「系统声音」模式实时字幕一行都不出**（整功能失效）：环路引擎写的 JSON 没有 `g`/`end`，`accept_line` 两道守卫把每行都丢掉 | live_capture.py 主循环 + viewer `_poll_live_log` | 引擎补 `g:0` 与 `end`（区间取滑动窗 [t0-5, t0]，与 SRT 行一致）；播放器对非 audio 来源就地补齐，兼容旧引擎 |
| 2 | 批量 SRT 取消后 `_batch_active_gen` 不归零 → 之后每次批量永久停在 0/N（一个任务都不提交），`srt_busy` 同时泄漏卡死实时字幕 | main_window `_batch_cancel_srt` | 置 0 并停表；正常完成路径也补上 |
| 3 | `_start_live_caption` 只有"复用"分支复位 `_live_paused` → SRT 结束后的自动恢复走 spawn 时引擎在跑却永远停在"启动中…" | app/viewer.py | 在 `srt_busy` 检查后统一复位一次 |
| 4 | `extend_playlist` 只夹紧局部 index、从不赋给 `self.index` → 命令行/双击打开的那部片**断点续播永不保存**、上/下一个跳错片、面板高亮错行 | app/viewer.py | 按 path 重算（压缩包过滤后下标会前移）后落到 `self.index` |
| 5 | 面板 `set_playlist` 拿到播放器的列表后**又排一次序**（seed 默认 0、sort_key 可能没同步）→ `_all_items` 与 `viewer.items` 顺序脱钩，双击第 N 行播的是另一个文件 | playlist_panel | 直接沿用播放器给的顺序；命令行启动那条路径改由主窗口先排好再交给播放器 |
| 6 | 引擎 `pending_job` 单槽被后到任务顶掉时，未启动的 SRT 任务不写任何终止标记 → 进度窗永久"生成中"、`srt_busy` 卡死（点了取消也没反馈） | live_transcribe `_watch_control` | 被丢弃的 srt job 就地补写 `# SRT_CANCELLED` |
| 7 | 预转写缓存命中时整片行在几十毫秒内灌进有界队列 → 除最后 64 句外**译文全被 drop-oldest 丢光**（"下一集秒出字幕"实际只有片尾有翻译） | live_transcribe `_emit` | 缓存回放走阻塞 put（按翻译速度回压），代次前进即放弃 |

### 中危

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 8 | 面板自己的排序下拉：用排序**前**的行号回填"正在播放"高亮，且不通知播放器 → 高亮落到不相干的文件、之后双击全错 | playlist_panel `_apply_panel_sort` | 按 path 重定位高亮 + 发 `playlist_reordered` 同步 viewer |
| 9 | 进度条青色"已覆盖区"每段都从轨道左端画到段尾 → 跳转转写后中间的空洞被涂满，用户以为整片已转写（与控制器刻意保留的空洞语义、单测断言直接冲突） | app/seekbar.py | 段起点用 `x0`，并夹在轨道内 |
| 10 | `start_preload()` 只看 pid 活着 + `.state` 匹配；环路进程写同一个 pid 文件但不写 `.state`、不读 control，且 `kill()` 从不清 `.state` → 环路进程冒充"引擎就绪"，SRT 任务永远没人处理 | app/live_engine.py | 新增 `pid_is_transcribe()` 参与复用判定；`kill()` 一并清 `.state` |
| 11 | `effective_model()` 缓存键不含文件系统状态且全进程无失效点 → 一键装完模型不重启播放器，仍按安装前的回退档位起引擎（直接 MODEL_ERROR） | live_engine + settings_dialog | 新增 `invalidate_model_cache()`，安装成功回调里调用 |
| 12 | SRT 结束的自动恢复不检查当前是不是视频 → 期间切到图片会把图片路径当媒体提交给引擎，图片界面冒出"启动中…" | viewer `_pause_live_for_srt._check` | 非视频就清暂停标志并停表 |
| 13 | `_enter_archive` 在**所有校验之前**就作废进行中的文件夹扫描 → 密码取消/坏包/包内无媒体时提前 return，文件列表永久停在半截结果、状态栏停在"扫描中" | app/main_window.py | 作废挪到校验全部通过之后 |
| 14 | 压缩包浏览中再打开另一个压缩包（含拖入）：`if self._archive_mode: return` 静默无反应 | app/main_window.py | 校验通过后先 `_leave_archive_state()` 再换包，"返回"仍回最初的文件夹 |
| 15 | `_exit_archive` 调 `set_folder(back)`，而 `back` 常等于 `self.folder` → 被"同一目录直接 return"挡掉，网格仍显示包内文件；`back is None` 时更是标题/网格全残留 | app/main_window.py | `force=True`；无来处则清网格回欢迎页 |
| 16 | 播放列表右键「移到回收站」后不 `takeItem`、不更新 `_all_items` → 被删的行留在面板里，之后所有行号→列表项的映射整体错位 | playlist_panel `_recycle` | 与 `_remove_selected` 对齐：先重建列表再发信号，且"正在播放"高亮按 path 在删除后的列表里重算（删掉的项在它前面时下标会前移） |
| 17 | 加密 zip：中央目录不需要密码就能列出，于是**每个成员**弹一次密码框，点取消也只跳过当前成员（N 个文件点 N 次） | archive `_list_zip` + `_show_archive_dir` | 按 `flag_bits & 0x1` 在列表阶段返回 `password`（与 7z 分支一致）；解压循环只问一次 |
| 18 | live_capture 转写线程无异常守卫（CUDA OOM/驱动 TDR）→ 主循环永久卡在 `out.get()`，心跳仍在 touch log，播放器判"存活"永不重启，字幕无声消失 | live_capture `transcribe_worker` | 与录音线程同一纪律：异常即 `os._exit(1)` 触发重建 |
| 19 | `_load_model()` 的 except 顺手吞掉"刚到达的下一个任务"（本意只针对启动期）→ 空闲卸载后的重载失败会把新任务静默丢弃，调度停摆 | live_transcribe | pending 的消费挪到启动期首次加载的调用点 |
| 20 | 翻译降级是**终身**的：Ollama 抖动一次，此后所有实时字幕纯原文，且 SRT 也跟着静默交付纯原文还报 `SRT_READY` 成功 | live_transcribe `_translate_worker` / `_generate_srt` | 保留原始 translator 引用，新任务恢复重试（累计 3 次降级才彻底放弃）；降级状态行改成播放器能识别的 `TRANSLATE_ERROR` |
| 21 | 收尾的 `_translate_q.join()` 无上限：Ollama "能连上但不回"时单句 120s×2，队列 64 条 → 引擎主循环挂死，换片/seek 全不响应而心跳照旧 | live_transcribe | 换成带 90s 上限的 `_drain_translations()` |
| 22 | `kill()`/taskkill 不收子进程 → SRT 用 HY-MT2 翻译时拉起的 `llama-server.exe` 成孤儿，常驻 ~5GB 显存 + 占着 8020 端口 | live_engine `kill()`、viewer 两处 taskkill | psutil `children(recursive=True)` 一并终止；taskkill 加 `/T` |
| 23 | `install_engine._curl` 把"体积≥期望的 95%"当已下载 → 11.6GB 模型下到 96% 中断后被永久当成"已下载"，重装也修不好；日志声称续传但命令里没有 `-C -` | install_engine | 下到 `.part` + 真续传，curl 正常退出且体积达标才改名 |
| 24 | `install_llamacpp` 先解压 bins 再校验 cudart，且完成判据只看 `llama-server.exe` → cudart 包损坏一次，CUDA 运行库永久缺失且重装被跳过 | install_engine | 两个包都校验完再解压；判据加 `cudart*.dll` |

### 低 / 健壮性

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| 25 | 图片模式窄窗口下「更多」菜单仍列出全套视频项（第二轮只修了 `_update_flex`，同胞函数漏了） | controls `_rebuild_more_menu` | 抽出 `_flex_pool()` 两处共用 |
| 26 | 小图（同步分支）解码失败提示显示完整绝对路径（第一轮只改了异步分支） | image_view | 传 `path.name` |
| 27 | 「打开方式」指到非图片非视频文件 → `item_for_path` 返回 None 未校验，AttributeError 被吞，界面静默空白 | main_window `_startup_play` | 落回欢迎页 + 状态栏提示（新增 i18n 键 `unsupported_file`） |
| 28 | 一句都没识别出来时仍写 0 字节字幕并报 `SRT_READY` → 播放器弹"已生成" | live_transcribe `_generate_srt` | 空结果写 `SRT_ERROR 未识别到语音` |
| 29 | 目标语言选 English 时系统提示词仍要求"用中文影视字幕的口语腔、别留英式语序"（自相矛盾的指令） | translate_service | 按目标语言组装第 1/2 条要求，新增 `system_prompt()` + 3 个单测 |
| 30 | spawn 分支 `begin_media` 起点取 `pos-5`，而给引擎的是 `--seek int(pos)` → 青色覆盖区多画 5 秒、`span_covered` 误报 | app/viewer.py | 两处对齐 |

## 误报澄清（教训）

- **搜索框防抖从未失效**：`textChanged.connect(QTimer.start)` 实际有效。第一轮
  "实测"与审查代理都犯了同一个错误——在 300ms 单发定时器**触发完之后**才检查
  `isActive()`（此时必然 False）。正确的验证是在 setText 后立即检查。lambda 写法
  保留（等效且直观），但勿再据此断言 direct-connect 无效。
- **第三轮的两条"看着像 bug 其实不是"**：`seekbar.hideEvent` 发 `scrub_finished`
  在最小化/正常关窗时是安全的（接收端只清标志，不触发 seek 或字幕重算）；
  `_gif_capture_loop` 收尾判 `self._gif_recording` 的竞态窗口只有一两条字节码
  （`_finish_gif` 在 `stop.set()` 下一行就置 False），触发不了。

---

## 第三轮复核过、结论无误的项（勿重复排查）

- **前两轮 21 个修复中的 17 项经 A 路逐条回读确认正确完整**：`_srt_job_log_tail`
  重置时机、`data_version` 只单调递增（四处 +1，无归零路径）、GIF stop 事件 +
  join + `_finish_gif` 重入保护、环路复用的 pid cmdline 校验、`_try_auto_restart_live`
  的 600s 冷却与 90s 宽限、`_translate_with_retry` 两处调用点都已解包、
  `_maybe_restore_playlist` 的 try 覆盖面与 clean 回写、A-B 只标 A 点的绘制、
  `_srt_dialog_closed` 连 `rejected` 而 `_finish_srt_job` 走 `accept`（不重复触发）、
  引擎 except 写 `# SRT_ERROR` 与播放器 `startswith` 拼写一致、`begin_media` 实参
  顺序/类型、`import json` 在模块顶、`_transcribe` 早退补 `TASK_DONE` 的四处位置、
  `_remember_position` 的 `_loaded_media_path` 赋值点只有 video/image 两处、
  `_flex` 不含图片控件、`submit` 统一返回 None（无 False 混入代次状态机）。
- **thumbs 代次与线程纪律**：`invalidate_queue` 同时推进 generation + 清 pending/failed；
  `_work`/`_work_inner` 两处比对 gen；所有失败出口都 `_pending.discard`；
  `_video_inflight` 在 finally 配平；worker 只造 PIL/QImage，`QPixmap.fromImage`
  只在 paint（GUI 线程）；`release_idle_grabbers` 非阻塞且只挑 `tag=="thumb"`。
- **TileView / MediaModel**：`_key_to_row` 只在 modelReset 重建，与 `set_items` 的
  begin/endResetModel 配套，不会脱同步。
- **持久化三件套**：DirCache / AlbumStore / OrderStore 全部 Lock + tmp→replace，
  读取端 isinstance 校验 + 全 except 回落。
- **media 类型判定**：`classify_name` 的 rfind/lower、无扩展名、双扩展名、
  `.tar.gz` 复合后缀、UNC/映射盘（`\\` 与 `//`）、超长路径（缓存键走 sha1）、
  `item_for_path` 的 OSError 占位项。
- **`extract_member` 的路径穿越防护**：`rel.is_absolute() or ".." in rel.parts` 直接拒绝。
- **信号连接无重复**：`_ensure_tiles`/`_ensure_details`/`_materialize_tree`/
  `_materialize_archive_tree`/`PlaylistPanel._ensure_tree` 都有 `is None` 守卫；
  `_wire_panel` 只在 Viewer 构造时调一次。
- **引擎代次收发配对**：`_transcribe` 的全部早退分支都发 `TASK_DONE {generation}`
  且代次号取自 job；取消分支不发是对的（后续代次接管、旧代次被 `ignored`）。
- **`_generate_srt` 的 llama.cpp 生命周期**：`llama_used` 只在 ensure 成功后置位，
  finally 覆盖取消/异常/正常三条路径（外部 kill 的孤儿问题已由第三轮 #22 修掉）。
- **`asr_engines`**：`vad_segments` 的 MIN/MAX 兜底与 `stream_transcribe` 的短段
  过滤配套；`split_long_row` 保证 `end >= start` 且不超真实窗口。
- 顺带记录一条非 bug 事实：`app/archive_browser.py` 已是死代码（全仓无 import，
  压缩包浏览早已改走 `main_window._enter_archive`）。

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
- 打包版一键安装依赖目标机器装有 Python（已给明确报错指引 + 第三轮加了 WindowsApps 别名 stub 规避与解释器实探）；长期可考虑随包内嵌 embeddable python。
- **手动拖动排序的 `orders.json` 只写不读**：`media.SORT_LABELS` 里没有 `custom` 项，
  两个排序下拉框都生成不出它，`_apply_view` 的 custom 分支永远走不到（i18n 键
  `media.sort_custom` 与 `OrderStore` 都已就位）。补齐要连面板侧的 `manual_order`
  一起改，属功能缺口而非回归，留待产品决定。
- **压缩包解压缓存永不清理**：`%TEMP%\GalleryPlayer\archive-cache\<包名>` 全仓没有
  任何清理入口，浏览几个大包就是几 GB 常驻。需要决定清理时机（退出时 / 进新包时 /
  按容量上限），暂记。
- **MODEL_ERROR 后 600ms 轮询与 5s 存盘定时器继续空转**：只置 `_live_on=False`
  不停表，属资源泄漏而非功能错误（关窗即收），未动。
- 以下三条第三轮判定为"待确认、无法在只读环境复现"，未改动，下轮如要碰相关代码请先复现：
  ① `FramePreviewer.stop()` 的 `join(timeout=4)` 超时后线程仍在跑，紧接着
  `shutdown_grabbers()` 抽掉同一个 mpv 实例（预览线程全程不持 `_busy`）——需要慢速
  网络共享上悬停进度条再立刻关窗；② `assoc._del_tree` 的 `while True: EnumKey(0)`
  在 DeleteKey 失败时理论死循环（HKCU+ALL_ACCESS 下难构造失败前提）；
  ③ `install_engine.ms_download` 用"存在任一 .pt/.safetensors"当幂等判据，若
  ModelScope 中断会把不完整快照留在最终文件名上，则同 #23/#24 属"永久失败"形状。

## 回归要点（改这些区域时必测）

1. **引擎生命周期**：开→停止（常驻/非常驻）→重开；换片/seek/换源（loopback↔audio）；SRT 生成中撞实时字幕（两个方向）；引擎进程手动杀掉后的自动恢复。
2. **SRT 任务族**：单文件/批量各自完成、取消、关窗（X/Esc）；第二次批量；生成中关播放器；**提交后立刻点取消**（引擎还没取走任务，第三轮 #6）。
3. **滚轮三处语义**：设置界面（滚动不误改）、播放器（切媒体）、seekbar（忽略）。
4. **断点续播**：正常退出重开；删除正在播放的项；外部打开/拖放/最近文件（冷文件夹）；**双击文件夹里第 N 个视频→等后台扫描完→关窗**（第三轮 #4）。
5. **「系统声音」来源**（第三轮 #1 整功能修复，此前一行字幕都不出）：开→出字幕→
   看进度条覆盖区→关窗后 `.live.srt` 有内容；换到音轨来源再换回来。
6. **播放列表顺序一致性**（第三轮 #5/#8）：主窗口选「随机」排序→双击打开→侧栏
   双击任意行必须播那一行；面板底部改排序后高亮不跳、双击仍正确。
7. **压缩包**（第三轮 #13/#14/#15/#17）：大文件夹扫描中开包→密码取消（列表要恢复
   完整）；包内再开另一个包；返回；启动后直接开包再返回；加密 zip 只问一次密码。
8. **翻译降级恢复**（第三轮 #20）：翻译中重启 Ollama → 当前任务降级为纯原文，
   换片/新任务后自动恢复翻译；SRT 任务不会静默交付纯原文。

## 本轮验证方式

- 主仓单测 20 例 + 引擎单测 16 例（新增 3 例 `system_prompt`）全绿。
- 离屏冒烟三批共 38 项断言全通过：进度条覆盖区逐像素采样（空洞不得涂青）、
  控制器接收环路行（新/旧格式）、图片模式折叠池与「更多」菜单、面板顺序与
  高亮重定位、批量取消后的代次/互斥标志、非媒体文件启动、`extend_playlist`
  下标、压缩包进/换/退与密码取消后的扫描 token。
- 引擎侧 `pending_job` 顶掉终止标记、预转写回放回压这两处只做了代码复核
  （需真实模型与 Ollama 才能端到端跑），下次跑真机时按回归要点 2、8 复测。
