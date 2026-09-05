"""Build the portable (免安装) folder with PyInstaller.

    python build.py

Produces dist/媒体播放器/媒体播放器.exe plus a sibling vendor/ folder. The libmpv
DLL is copied next to the exe rather than bundled as PyInstaller data, because
runtime.app_dir() resolves vendor/ relative to the executable, while bundled data
lands in _internal/.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "媒体播放器"
# Optional CLI argument overrides the output folder:  python build.py complete
DIST = (ROOT / sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist"
BUILD = ROOT / "build"

# Qt ships a lot we never touch; trimming these is most of the size win.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.QtQmlModels", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtSensors", "PySide6.QtTest", "PySide6.QtHelp",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtSql", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets", "PySide6.QtHttpServer", "PySide6.QtGraphs",
    "tkinter", "unittest", "pydoc", "doctest", "numpy", "scipy", "pandas",
    "matplotlib", "IPython", "setuptools", "pip",
]


# PyInstaller's PySide6 hook collects these DLLs no matter what --exclude-module
# says, because the exclusion only applies to Python modules. Nothing in this app
# touches QML, Quick, PDF or the virtual keyboard.
DEAD_QT_LIBS = [
    "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6VirtualKeyboard.dll",
]


def _prune(target: Path) -> None:
    """Drop collected-but-unused payload. Keeps opengl32sw.dll: it is the only
    fallback on machines whose GL drivers cannot give mpv a 3.3 context."""
    freed = 0
    pyside = target / "_internal" / "PySide6"
    for name in DEAD_QT_LIBS:
        p = pyside / name
        if p.exists():
            freed += p.stat().st_size
            p.unlink()

    # keep only the Chinese Qt translations (installed by main.py), drop the rest
    tr = pyside / "translations"
    if tr.is_dir():
        for f in tr.iterdir():
            if f.is_file() and "zh_CN" not in f.name:
                freed += f.stat().st_size
                f.unlink()

    print(f"精简掉 {freed / 1024 / 1024:.0f} MB 未使用的组件")


def _copy_public_scenarios(target: Path) -> None:
    """Copy only tracked scenario JSONs; ignored local/private scenarios stay out."""
    result = subprocess.run(
        ["git", "ls-files", "--", "live-subtitle/scenarios/*.json"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    files = []
    if result.returncode == 0:
        for name in result.stdout.splitlines():
            source = ROOT / name
            if source.is_file():
                files.append(source)
    if not files:
        print("警告：没有找到已跟踪的公共场景 JSON，跳过场景文件打包", file=sys.stderr)
        return
    out = target / "live-subtitle" / "scenarios"
    out.mkdir(parents=True, exist_ok=True)
    for source in files:
        shutil.copy2(source, out / source.name)
    print(f"已复制 {len(files)} 个公共场景 JSON")


# 便携包里必须带的字幕引擎脚本。
# 前两个是"装引擎"用的；后五个是"跑引擎"用的运行时闭包——播放器启动的是
# live_transcribe.py / live_capture.py，它们又 import asr_engines /
# translate_service / ollama_service。少带任何一个，用户把模型下载安装完
# 也拉不起字幕，而且 install_engine 最后那步"验证模型可加载"本身就 import
# asr_engines：默认引擎（qwen/sensevoice）会在下完 4.7GB 模型后失败收场。
ENGINE_FILES = (
    "install_engine.py",
    "ollama_modelfile.py",
    "live_transcribe.py",
    "live_capture.py",
    "asr_engines.py",
    "translate_service.py",
    "ollama_service.py",
)


def _copy_engine_runtime(target: Path) -> None:
    """把字幕引擎的脚本带进便携包（见 ENGINE_FILES 的清单说明）。

    只带脚本：config.yaml / Modelfile* 由安装时在目标目录自生成，带开发机
    的副本会把开发者配置压到用户头上；venv / models / checkpoints 更不能带。
    """
    out = target / "live-subtitle"
    out.mkdir(parents=True, exist_ok=True)
    missing = []
    for name in ENGINE_FILES:
        source = ROOT / "live-subtitle" / name
        if source.is_file():
            shutil.copy2(source, out / name)
        else:
            missing.append(name)
    if missing:
        print(f"警告：缺少引擎脚本 {missing}，便携版的实时字幕/SRT 会跑不起来",
              file=sys.stderr)
    print(f"已复制 {len(ENGINE_FILES) - len(missing)} 个引擎脚本"
          f"（安装器 + 运行时闭包）")


def main() -> int:
    dll = ROOT / "vendor" / "libmpv-2.dll"
    if not dll.exists():
        print(f"错误：找不到 {dll}", file=sys.stderr)
        return 2

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("正在安装 PyInstaller …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pyinstaller"])

    # PyInstaller 打的是"运行本脚本的解释器"的环境：用错解释器（比如装
    # 依赖不全的另一个 Python）也能打包成功，但产物启动即
    # ModuleNotFoundError: PySide6——这里提前拦下
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("错误：当前解释器缺 PySide6，打出的包无法启动。"
              f"当前：{sys.executable}",
              file=sys.stderr)
        print("请用装有完整依赖的解释器重跑（如 Python313）。",
              file=sys.stderr)
        return 2

    # The app is portable: everything the user accumulates -- thumbnail cache, album
    # definitions, resume positions, window prefs -- lives in userdata/ next to the
    # exe. Wiping dist/ for a rebuild would throw all of it away, so it is parked
    # outside the tree first and put back once the new build is in place.
    target = DIST / NAME
    live_userdata = target / "userdata"
    stash = ROOT / ".userdata-stash"
    if live_userdata.is_dir():
        if stash.is_dir():
            raise RuntimeError(
                f"发现旧的 userdata 暂存目录 {stash}，请先恢复它再重新打包"
            )
        size = sum(f.stat().st_size for f in live_userdata.rglob("*") if f.is_file())
        print(f"暂存已有的 userdata（{size / 1024 / 1024:.0f} MB）…")
        shutil.move(str(live_userdata), str(stash))
    elif stash.is_dir():
        print("继续使用上次打包失败后暂存的 userdata …")

    for path in (DIST, BUILD):
        shutil.rmtree(path, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--windowed",                 # no console window
        "--onedir",                   # fast startup, no temp extraction
        "--contents-directory", "_internal",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        # keep the generated .spec inside build/, not at the project root,
        # so a build never rewrites the committed relative-path spec
        "--specpath", str(BUILD),
        str(ROOT / "main.py"),
    ]
    for mod in EXCLUDES:
        args += ["--exclude-module", mod]

    print("正在打包 …")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        if stash.is_dir():
            print(f"打包失败，你的 userdata 还在 {stash}", file=sys.stderr)
        return result.returncode

    _prune(target)

    if stash.is_dir():
        shutil.move(str(stash), str(target / "userdata"))
        print("已恢复 userdata")

    vendor_out = target / "vendor"
    vendor_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dll, vendor_out / dll.name)
    shutil.copy2(ROOT / "README.zh-CN.md", target / "使用说明.md")
    shutil.copy2(ROOT / "安装运行环境.bat", target / "安装运行环境.bat")
    shutil.copy2(ROOT / "清除后台字幕模型.bat", target / "清除后台字幕模型.bat")
    _copy_public_scenarios(target)
    _copy_engine_runtime(target)

    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"\n完成：{target}")
    print(f"入口：{target / (NAME + '.exe')}")
    print(f"体积：{total / 1024 / 1024:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
