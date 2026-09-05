"""字幕引擎脚本清单——打包与"安装到自定义目录"共用同一份事实。

便携包要带齐这些脚本，否则用户把模型下载安装完也起不了字幕；用户在设置里
把引擎目录指到别处（比如 C 盘空间不够改装到 D 盘）时，也要把同一份脚本
准备到那个目录。两处都从这里取，避免哪天加了新模块只改一边。

- install_engine.py / ollama_modelfile.py：装引擎用
- live_transcribe.py / live_capture.py：播放器实际启动的入口
- asr_engines.py / translate_service.py / ollama_service.py：上面两个的
  本地 import 闭包（install_engine 的"验证模型可加载"也要 asr_engines）
"""
from __future__ import annotations

ENGINE_SCRIPTS: tuple[str, ...] = (
    "install_engine.py",
    "ollama_modelfile.py",
    "live_transcribe.py",
    "live_capture.py",
    "asr_engines.py",
    "translate_service.py",
    "ollama_service.py",
)
