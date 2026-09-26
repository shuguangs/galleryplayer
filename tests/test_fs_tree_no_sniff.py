"""目录树不得在 GUI 线程上读文件内容（网络盘未响应回归）。

背景 bug（用户实测）：从网盘挂载的 Y: 打开视频，目录树同步到该目录时，
默认 QFileSystemModel 的图标提供器对每一项调 QMimeDatabase::mimeTypeForFile
——它会打开文件读头部做内容嗅探。网盘上每次读都是一次网络往返，GUI 线程
被一串阻塞 ReadFile 卡死 43.9 秒（进程 CPU 仅 1.8s）。原生栈：
ZwReadFile ← QIODevice::peek ← QMimeDatabase::mimeTypeForFile ←
QAbstractFileIconProvider::type ← QFileInfoGatherer::getInfo。

测试做法：在一个临时目录里放"扩展名看不出类型"的文件，逼 MIME 库只能靠
读内容判断；把 QFile/Python 层都拦不到的读取换成可观测的信号——统计模型
type() 列（第 2 列，正是触发嗅探的那一列）是否来自内容嗅探。
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QFileInfo
from PySide6.QtWidgets import QApplication, QFileSystemModel

from app.fs_tree_model import _NoSniffIconProvider, make_dir_tree_model

_app = QApplication.instance() or QApplication([])


def _wait_loaded(model: QFileSystemModel, path: str, ms: int = 5000) -> None:
    done = []
    model.directoryLoaded.connect(lambda p: done.append(p))
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms and not any(Path(p) == Path(path) for p in done):
        QCoreApplication.processEvents()


class NoSniffTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "sub").mkdir()
        # 无扩展名 + PNG 魔数：只有读内容才能认出是 image/png
        (self.root / "noext").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)

    def tearDown(self):
        self._td.cleanup()

    def test_provider_type_never_sniffs_content(self):
        """提供器的 type() 对"只有读内容才认得出"的文件不能给出 MIME 结论。"""
        p = _NoSniffIconProvider()
        self.assertEqual(p.type(QFileInfo(str(self.root / "noext"))), "File")
        self.assertEqual(p.type(QFileInfo(str(self.root / "sub"))), "Folder")

    def test_default_provider_does_sniff(self):
        """前提自检：Qt 默认提供器确实会嗅探内容——否则上面的测试形同虚设。"""
        from PySide6.QtCore import QMimeDatabase

        mt = QMimeDatabase().mimeTypeForFile(str(self.root / "noext"))
        self.assertEqual(mt.name(), "image/png",
                         "MIME 库未按内容识别，测试前提失效")

    def test_tree_model_uses_no_sniff_provider(self):
        model = make_dir_tree_model()
        self.assertIsInstance(model.iconProvider(), _NoSniffIconProvider)
        idx = model.index(str(self.root))
        model.fetchMore(idx)
        _wait_loaded(model, str(self.root))
        sub = model.index(str(self.root / "sub"))
        self.assertTrue(sub.isValid(), "目录树未列出子文件夹")
        # 第 2 列即 type()：必须是提供器的固定文案，而非 MIME 描述
        self.assertEqual(model.data(sub.siblingAtColumn(2)), "Folder")

    def test_both_trees_use_factory(self):
        """主窗口与播放器面板都必须经工厂建模型，不得再直接 new 默认模型。"""
        src = Path(__file__).resolve().parent.parent / "app"
        for name in ("main_window.py", "playlist_panel.py"):
            text = (src / name).read_text(encoding="utf-8")
            self.assertFalse("QFileSystemModel(" in text,
                             f"{name} 仍直接构造 QFileSystemModel")
            self.assertTrue("make_dir_tree_model(" in text,
                            f"{name} 未使用 make_dir_tree_model")


if __name__ == "__main__":
    unittest.main()
