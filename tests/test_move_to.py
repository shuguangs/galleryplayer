"""「移动到」+ 播放器/图片右键文件操作的回归测试。

背景（用户需求）：所有右键菜单同步一组文件操作——移动到…、删除到回收站、
复制路径/文件名、资源管理器中显示。图片查看器以前只有"复制图片/文件"，
删除和移动必须切回浏览器才能做。

覆盖：
- fileops.move_to：基本移动 / 同文件夹拒绝 / 冲突覆盖（旧文件进回收站）/
  冲突跳过 / 取消选择器 / 记住上次目标文件夹 / 失败不炸
- main_window._apply_file_moves：同文件夹=改名原地保留，跨文件夹=移出列表
- 菜单接线：三处菜单都有入口（源码断言）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.runtime import init_libmpv  # noqa: E402

try:
    init_libmpv()
except RuntimeError as exc:
    raise unittest.SkipTest(f"libmpv unavailable: {exc}")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app import fileops  # noqa: E402
from app.config import settings  # noqa: E402
from app.i18n import set_language, t  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.media import MediaItem  # noqa: E402


def _mkfile(root: Path, name: str, size: int = 8) -> Path:
    p = root / name
    p.write_bytes(b"x" * size)
    return p


class MoveToTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="move-to-"))
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        settings["move_to_last_dir"] = ""

    def _pick(self, dest: Path):
        # move_to 内部是局部 `from PySide6.QtWidgets import QFileDialog`，
        # patch 类属性对局部导入同样生效
        return mock.patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory",
                          return_value=str(dest))

    def _fake_box(self, choice: str):
        """替身 QMessageBox：点「覆盖」或「跳过同名」。

        fileops 模块顶部自行 `from ... import QMessageBox`，必须 patch
        模块属性才拦得住（patch PySide6 原类无效，真对话框 exec 会挂死）。
        """

        class FakeBox:
            def __init__(self, *args, **k):
                self.buttons = {}

            def setWindowTitle(self, *a):
                pass

            def setIcon(self, *a):
                pass

            def setText(self, *a):
                pass

            def addButton(self, text, role=None):
                btn = mock.MagicMock(name=f"btn[{text}]")
                self.buttons[text] = btn
                return btn

            def setDefaultButton(self, *a):
                pass

            def exec(self):
                return 0

            def clickedButton(self):
                return self.buttons.get(choice)

        return mock.patch.object(fileops, "QMessageBox", side_effect=FakeBox)

    def test_basic_move(self):
        a = _mkfile(self.src, "a.mp4")
        with self._pick(self.dst):
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(1, moved)
        self.assertFalse(a.exists())
        self.assertTrue((self.dst / "a.mp4").is_file())
        self.assertEqual([(a, self.dst / "a.mp4")], moves)
        self.assertIn("1", msg)

    def test_same_folder_is_rejected(self):
        a = _mkfile(self.src, "a.mp4")
        with self._pick(self.src):
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(0, moved)
        self.assertEqual([], moves)
        self.assertTrue(a.exists())  # 没动
        self.assertTrue(msg)  # 提示"已在该文件夹"

    def test_cancelled_picker_moves_nothing(self):
        a = _mkfile(self.src, "a.mp4")
        with mock.patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory",
                        return_value=""):
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(0, moved)
        self.assertEqual([], moves)
        self.assertEqual("", msg)
        self.assertTrue(a.exists())

    def test_missing_files_reported(self):
        ghost = self.src / "ghost.mp4"
        with self._pick(self.dst):
            moved, msg, moves = fileops.move_to(None, [ghost])
        self.assertEqual(0, moved)
        self.assertEqual([], moves)
        self.assertTrue(msg)

    def test_collision_overwrite_recycles_target_first(self):
        a = _mkfile(self.src, "a.mp4", size=16)
        existing = _mkfile(self.dst, "a.mp4", size=4)
        recycled: list[Path] = []

        def fake_recycle(paths):
            for p in paths:
                p.unlink()
                recycled.append(p)
            return len(paths), ""

        with self._pick(self.dst), \
                mock.patch.object(fileops, "recycle", side_effect=fake_recycle), \
                self._fake_box(t("fileops.move_overwrite")):
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(1, moved, f"覆盖移动应成功: {msg}")
        self.assertEqual([existing], recycled)  # 旧文件先"进回收站"
        self.assertFalse(a.exists())
        self.assertEqual(16, (self.dst / "a.mp4").stat().st_size)  # 是新文件

    def test_collision_skip_keeps_both(self):
        a = _mkfile(self.src, "a.mp4", size=16)
        existing = _mkfile(self.dst, "a.mp4", size=4)

        with self._pick(self.dst), \
                self._fake_box(t("fileops.move_skip")):
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(0, moved)
        self.assertEqual([], moves)
        self.assertTrue(a.exists())          # 原文件留在原地
        self.assertEqual(4, existing.stat().st_size)  # 目标没被覆盖

    def test_collision_cancel_moves_nothing(self):
        a = _mkfile(self.src, "a.mp4", size=16)
        _mkfile(self.dst, "a.mp4", size=4)

        with self._pick(self.dst), \
                self._fake_box("__no_button__"):  # clickedButton 返回 None → 取消
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(0, moved)
        self.assertEqual([], moves)
        self.assertTrue(a.exists())

    def test_remember_last_dir(self):
        a = _mkfile(self.src, "a.mp4")
        with self._pick(self.dst):
            fileops.move_to(None, [a])
        self.assertEqual(str(self.dst), str(settings["move_to_last_dir"]))

    def test_move_failure_is_reported_not_raised(self):
        a = _mkfile(self.src, "a.mp4")
        with self._pick(self.dst), \
                mock.patch("shutil.move", side_effect=OSError(13, "拒绝访问")):
            moved, msg, moves = fileops.move_to(None, [a])
        self.assertEqual(0, moved)
        self.assertEqual([], moves)
        self.assertIn("失败", msg)


class ApplyFileMovesTests(unittest.TestCase):
    """main_window._apply_file_moves：列表同步语义。"""

    def _item(self, path: Path) -> MediaItem:
        return MediaItem(path=path, is_video=True, size=1, mtime=0.0,
                         is_archive=False)

    def _run(self, items, moves):
        stub = mock.MagicMock()
        stub.all_items = list(items)
        MainWindow._apply_file_moves(stub, moves)
        return stub

    def test_same_folder_move_retargets_in_place(self):
        src = Path("I:/v")
        a = self._item(src / "a.mp4")
        b = self._item(src / "b.mp4")
        stub = self._run([a, b], [(src / "a.mp4", src / "a2.mp4")])
        self.assertEqual(2, len(stub.all_items))          # 没有项被移出
        self.assertEqual(src / "a2.mp4", stub.all_items[0].path)
        self.assertEqual(src / "b.mp4", stub.all_items[1].path)
        stub._apply_view.assert_called_once()

    def test_cross_folder_move_drops_items(self):
        src, dst = Path("I:/v"), Path("I:/other")
        a = self._item(src / "a.mp4")
        b = self._item(src / "b.mp4")
        stub = self._run([a, b], [(src / "a.mp4", dst / "a.mp4")])
        self.assertEqual(1, len(stub.all_items))
        self.assertEqual(src / "b.mp4", stub.all_items[0].path)
        stub._apply_view.assert_called_once()

    def test_mixed_batch(self):
        src, dst = Path("I:/v"), Path("I:/other")
        a = self._item(src / "a.mp4")
        b = self._item(src / "b.mp4")
        c = self._item(src / "c.mp4")
        stub = self._run(
            [a, b, c],
            [(src / "a.mp4", src / "a2.mp4"),      # 改名
             (src / "c.mp4", dst / "c.mp4")])       # 移出
        self.assertEqual(2, len(stub.all_items))
        self.assertEqual(src / "a2.mp4", stub.all_items[0].path)
        self.assertEqual(src / "b.mp4", stub.all_items[1].path)


class MenuWiringTests(unittest.TestCase):
    """三处右键菜单都必须有移动/删除入口（源码断言 + i18n 齐全）。"""

    def test_viewer_menu_has_file_ops(self):
        src = (ROOT / "app" / "viewer.py").read_text(encoding="utf-8")
        self.assertIn('t("main_window.move_to")', src)
        self.assertIn('t("main_window.recycle")', src)
        self.assertIn('t("main_window.copy_path")', src)
        self.assertIn("move_files", src)
        self.assertIn("_recycle_current_media", src)

    def test_browser_menus_have_move(self):
        src = (ROOT / "app" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('t("main_window.move_to")', src)
        self.assertIn('t("main_window.multi_move")', src)
        self.assertIn("_move_media", src)
        self.assertIn("files_moved", src)
        self.assertIn("files_recycled", src)

    def test_panel_menu_has_move(self):
        src = (ROOT / "app" / "playlist_panel.py").read_text(encoding="utf-8")
        self.assertIn('t("panel.move_ellipsis")', src)
        self.assertIn("def _move(", src)

    def test_i18n_keys_exist(self):
        for key in ("fileops.move_to_title", "fileops.move_same_dir",
                    "fileops.move_done", "fileops.move_partial",
                    "fileops.move_fail", "fileops.move_collision_text",
                    "fileops.move_overwrite", "fileops.move_skip",
                    "main_window.move_to", "main_window.multi_move",
                    "panel.move_ellipsis", "viewer.recycled_toast",
                    "viewer.recycle_fail_toast"):
            with self.subTest(key=key):
                self.assertNotEqual(key, t(key))
        set_language("en")
        try:
            for key in ("fileops.move_to_title", "main_window.move_to"):
                with self.subTest(en=key):
                    self.assertNotEqual(key, t(key))
        finally:
            set_language("zh")

    def test_move_to_last_dir_default_exists(self):
        self.assertIn("move_to_last_dir", settings._data)


if __name__ == "__main__":
    unittest.main()
