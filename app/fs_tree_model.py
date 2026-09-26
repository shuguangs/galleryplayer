"""目录树用的 QFileSystemModel 工厂（主窗口目录树与播放器面板目录树共用）。

为什么不能直接 new 一个默认的 QFileSystemModel：它在 GUI 线程上逐项
"识别文件类型"来挑图标——QAbstractFileIconProvider::type → QMimeDatabase::
mimeTypeForFile，后者会**打开文件读头部字节**做内容嗅探。本地盘上每项几毫秒
无感；在网络盘（RaiDrive/CloudDrive 这类把网盘挂成本地盘符的）上每次读都是
一次网络往返，展开或同步到一个网盘目录，GUI 线程就被一连串阻塞 ReadFile
卡死几十秒（实测 Y: 网盘 43.9s 未响应，进程 CPU 仅 1.8s——纯等 I/O）。

修法：换一个不碰文件内容的图标提供器——目录树只列文件夹，图标只需区分
"驱动器 / 文件夹"，按 QFileInfo 的元数据（已由后台 gatherer 线程取好）判断
即可，type() 返回固定文案，不再走 MIME 嗅探。
"""
from __future__ import annotations

from PySide6.QtCore import QDir, QFileInfo
from PySide6.QtWidgets import QApplication, QFileIconProvider, QFileSystemModel, QStyle


class _NoSniffIconProvider(QFileIconProvider):
    """只看元数据、绝不读文件内容的图标提供器。"""

    def __init__(self) -> None:
        super().__init__()
        # 不要 Windows 外壳按文件取的个性图标（desktop.ini 自定义文件夹图标
        # 同样要读网盘上的文件）
        self.setOptions(QFileIconProvider.DontUseCustomDirectoryIcons)
        style = QApplication.style()
        self._drive = style.standardIcon(QStyle.SP_DriveHDIcon)
        self._dir = style.standardIcon(QStyle.SP_DirIcon)
        self._file = style.standardIcon(QStyle.SP_FileIcon)

    def icon(self, arg):  # noqa: D401 - Qt 重载：IconType 或 QFileInfo
        if isinstance(arg, QFileInfo):
            if arg.isRoot():
                return self._drive
            return self._dir if arg.isDir() else self._file
        if arg == QFileIconProvider.Drive:
            return self._drive
        if arg == QFileIconProvider.File:
            return self._file
        return self._dir

    def type(self, info: QFileInfo) -> str:
        # 默认实现在这里调 QMimeDatabase::mimeTypeForFile（读文件头）
        if info.isRoot():
            return "Drive"
        return "Folder" if info.isDir() else "File"


def make_dir_tree_model(parent=None) -> QFileSystemModel:
    model = QFileSystemModel(parent)
    model.setIconProvider(_NoSniffIconProvider())
    model.setFilter(QDir.Dirs | QDir.Drives | QDir.NoDotAndDotDot)
    # 不装文件监视：目录树只列文件夹，监视大树只会白耗句柄和启动时间
    model.setOption(QFileSystemModel.DontWatchForChanges, True)
    # 图标/类型已由上面的提供器接管，再关掉模型自己去解析 .lnk 与外壳图标
    model.setResolveSymlinks(False)
    model.setRootPath("")
    return model
