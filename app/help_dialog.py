"""In-app help: a scrollable cheat-sheet of every feature and keyboard shortcut.

The app grew a rich key map and a pile of toggles that were previously only
discoverable by reading the source. This dialog surfaces all of it in one place,
reachable from the toolbar (?) and from the viewer (F1 / ?).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout

from . import theme

# (key, description) rows, grouped by section title.
_BROWSER = [
    ("Ctrl+O", "打开文件夹"),
    ("Backspace", "返回上一级目录"),
    ("F5", "重新扫描当前文件夹"),
    ("Ctrl+1 / 2 / 3", "网格 / 瀑布流 / 详情列表视图"),
    ("Ctrl+滚轮", "调整每行数量（列数）"),
    ("Ctrl+F", "搜索文件名"),
    ("Ctrl+B", "显示 / 隐藏左侧目录树"),
    ("Ctrl+,", "打开设置窗口"),
    ("双击", "打开图片或视频"),
    ("右键", "打开 / 按原尺寸打开 / 资源管理器中显示 / 复制路径 / 重命名 / 删除到回收站"),
]

_PLAYER = [
    ("空格 / K", "播放 / 暂停"),
    ("F / 回车 / 双击画面", "全屏切换"),
    ("Esc", "退出全屏，或关闭查看器"),
    ("Tab", "显示 / 隐藏播放列表面板"),
    ("滚轮 / PageUp / PageDown", "上一个 / 下一个媒体"),
    ("← / →", "快退 / 快进 5 秒（Shift 微调 1 秒，Ctrl 大跳 60 秒）"),
    ("↑ / ↓", "音量加 / 减"),
    ("Home / End", "跳到开头 / 结尾附近"),
    ("M", "静音"),
    ("L", "循环模式（顺序 / 列表循环 / 单个循环 / 随机）"),
    ("V", "字幕显隐"),
    ("J", "切换字幕轨"),
    ("A", "切换音轨"),
    ("[ / ]", "减速 / 加速，\\ 复位到 1.0×"),
    ("S", "截图当前画面（PNG）"),
    ("G", "开始 / 停止录制 GIF"),
    ("I", "设 A-B 循环点（依次设 A、B，再按取消）"),
    ("O", "取消 A-B 循环"),
    (". / ,", "逐帧前进 / 逐帧后退"),
    ("Delete", "从播放列表移除当前项（面板打开时）"),
]

_IMAGE = [
    ("+ / - / Ctrl+滚轮", "放大 / 缩小"),
    ("0", "适应窗口"),
    ("1", "原始大小"),
    ("R / Shift+R", "顺时针 / 逆时针旋转 90°"),
    ("← ↑ / → ↓", "上一张 / 下一张"),
]

_FEATURES = [
    ("图片视频混合浏览", "同一列表里图片和视频无缝混排，滚轮即可切换。"),
    ("断点续播", "视频看到一半关掉，下次自动从上次位置附近继续（可在设置中关闭）。"),
    ("硬解 / 软解切换", "播放栏“解码”按钮：自动硬解（默认最稳）、强制硬解、纯软解。"),
    ("按原尺寸打开", "打开视频时窗口自动匹配视频原始分辨率，而不是最大化；右键菜单可开关。"),
    ("截图与 GIF", "S 存 PNG，G 录 GIF；文件默认存到视频所在文件夹，只读时改存程序目录下的“截图”文件夹。GIF 的帧率 / 时长 / 宽度可在设置中调整。"),
    ("A-B 循环", "播放时按 I 依次设起点 / 终点，在两点之间反复播放；O 取消。"),
    ("逐帧步进", "按 . / , 一帧一帧前进或后退，方便精确定位。"),
    ("画面调节", "播放栏“画面”按钮：亮度 / 对比 / 饱和 / 伽马 / 色相，一键复位。"),
    ("拖拽打开", "把文件或文件夹拖到主窗口或查看器里即可打开。"),
    ("最近播放", "欢迎页会记住最近打开过的文件夹和单个文件，点一下直接回到上次看的地方。"),
    ("播放列表导入导出", "面板底部可将当前列表导出为 m3u8，或导入已有的播放列表。"),
    ("文件关联", "设置窗口可把本程序注册到系统“打开方式”列表（仅当前用户，无需管理员）。"),
    ("设置窗口", "Ctrl+, 打开，集中管理续播 / 连放 / 解码 / 音量 / 字幕 / GIF 等选项。"),
    ("专辑收藏", "把喜欢的媒体归到自建专辑，独立于文件夹结构。"),
    ("便携化", "缩略图、进度、配置全部存在程序目录的 userdata 里，拷走即用。"),
]


def _rows_html(rows: list[tuple[str, str]]) -> str:
    out = []
    for key, desc in rows:
        out.append(
            f"<tr><td class='k'>{key}</td><td class='d'>{desc}</td></tr>"
        )
    return "".join(out)


def _build_html() -> str:
    def section(title: str, rows: list[tuple[str, str]]) -> str:
        return (
            f"<h2>{title}</h2>"
            f"<table cellspacing='0' cellpadding='0' width='100%'>{_rows_html(rows)}</table>"
        )

    return f"""
    <html><head><style>
      body {{ color:{theme.TEXT}; font-size:14px; }}
      h1 {{ color:{theme.ACCENT}; font-size:19px; margin:0 0 4px 0; }}
      h2 {{ color:{theme.ACCENT}; font-size:15px; margin:18px 0 6px 0;
            border-bottom:1px solid {theme.BORDER}; padding-bottom:4px; }}
      p.sub {{ color:{theme.TEXT_DIM}; margin:0 0 6px 0; }}
      td.k {{ color:{theme.TEXT}; font-family:'Consolas','Segoe UI Mono',monospace;
              white-space:nowrap; padding:3px 14px 3px 0; vertical-align:top; width:34%; }}
      td.d {{ color:{theme.TEXT_DIM}; padding:3px 0; vertical-align:top; }}
    </style></head><body>
      <h1>使用说明 · 快捷键</h1>
      <p class='sub'>这是本播放器的全部功能与快捷键速查。按 Esc 关闭。</p>
      {section("浏览页（主界面）", _BROWSER)}
      {section("视频播放", _PLAYER)}
      {section("图片查看", _IMAGE)}
      {section("功能特性", _FEATURES)}
    </body></html>
    """


class HelpDialog(QDialog):
    """Modeless cheat-sheet. One instance is reused via `HelpDialog.show_for`."""

    _instance: "HelpDialog | None" = None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用说明 · 快捷键")
        self.setMinimumSize(560, 640)
        self.setStyleSheet(
            f"QDialog {{ background:{theme.BG_BASE}; }}"
            f"QTextBrowser {{ background:{theme.BG_PANEL}; border:1px solid {theme.BORDER};"
            f" border-radius:8px; padding:10px 16px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        view = QTextBrowser(self)
        view.setOpenExternalLinks(False)
        view.setHtml(_build_html())
        lay.addWidget(view)

    def keyPressEvent(self, e):  # noqa: ANN001
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    @classmethod
    def show_for(cls, parent=None) -> "HelpDialog":
        """Open (or re-focus) the shared help window."""
        dlg = cls._instance
        if dlg is None:
            dlg = cls(parent)
            cls._instance = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg
