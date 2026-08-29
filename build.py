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

    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"\n完成：{target}")
    print(f"入口：{target / (NAME + '.exe')}")
    print(f"体积：{total / 1024 / 1024:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
