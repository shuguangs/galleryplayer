"""按文件夹分组：标题栏分隔 + 点标题栏折叠/展开。

需求（用户）：含子文件夹 + 按文件夹分组时，不同文件夹之间要分隔开；
点标题栏折叠或展开这个文件夹。行号必须始终等于列表下标（播放顺序不变）。
"""
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint

from app.browser import HEADER_H, MediaModel, TileView
from app.folder_groups import build_groups, group_index_of_row, is_group_head
from app.media import MediaItem
from app.thumbs import ThumbnailCache

ROOT = Path("C:/lib")


def mk(rel: str) -> MediaItem:
    return MediaItem(ROOT / rel, False, 1, 0.0)


def sample():
    # 根目录 3 个、A 里 5 个（含与 B 同名的 5.jpg）、B 里 2 个
    return ([mk(f"r{i}.jpg") for i in range(3)]
            + [mk(f"A/{i}.jpg") for i in range(1, 6)]
            + [mk("B/5.jpg"), mk("B/6.jpg")])


class GroupTableTests(unittest.TestCase):
    def test_split_keeps_every_row(self):
        items = sample()
        groups = build_groups(items, ROOT)
        self.assertEqual([(g.start, g.end) for g in groups], [(0, 3), (3, 8), (8, 10)])
        self.assertEqual(sum(g.count for g in groups), len(items))
        self.assertEqual(groups[1].label, "A")
        starts = [g.start for g in groups]
        self.assertEqual(group_index_of_row(starts, 7), 1)
        self.assertEqual(group_index_of_row(starts, 8), 2)
        self.assertTrue(is_group_head(items, 3))
        self.assertFalse(is_group_head(items, 4))

    def test_collapsed_state_survives_rebuild(self):
        model = MediaModel()
        model.set_grouping(True, ROOT)
        model.set_items(sample())
        model.toggle_group(1)
        model.set_items(list(reversed(sample()[3:8])) + sample()[:3])  # 换个顺序重设
        a = [g for g in model.groups if g.label == "A"][0]
        self.assertTrue(a.collapsed)


class TileViewGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.thumbs = ThumbnailCache()

    def make_view(self, mode="grid"):
        model = MediaModel()
        model.set_grouping(True, ROOT)
        view = TileView(model, self.thumbs)
        view.resize(800, 2000)
        view.set_columns(4)
        view.set_mode(mode)
        model.set_items(sample())
        view.relayout()
        return model, view

    def test_each_group_starts_new_row_below_header(self):
        for mode in ("grid", "waterfall"):
            model, view = self.make_view(mode)
            for g in model.groups:
                first = view.rect_for(g.start)
                self.assertGreaterEqual(first.top(), g.top + HEADER_H - 1, mode)
            # A 的最后一项和 B 的第一项不在同一行
            self.assertGreater(view.rect_for(8).top(), view.rect_for(7).bottom(), mode)

    def test_hit_test_rows_and_headers(self):
        for mode in ("grid", "waterfall"):
            model, view = self.make_view(mode)
            for row in (0, 4, 9):
                c = view.rect_for(row).center()
                self.assertEqual(view._row_at(c), row, mode)
            g = model.groups[1]
            band = QPoint(30, g.top + 5)
            self.assertEqual(view._header_at(band), 1, mode)
            self.assertEqual(view._row_at(band), -1, mode)

    def test_collapse_hides_rows_and_keeps_order(self):
        model, view = self.make_view()
        model.toggle_group(1)
        for row in range(3, 8):
            self.assertTrue(view.rect_for(row).isNull())
        self.assertNotIn(5, list(view._visible_rows()))
        # B 上移到 A 的标题栏下面
        self.assertLess(model.groups[2].top, model.groups[1].top + HEADER_H + 20)
        # 键盘从根目录末项往右，跳过折叠的 A 直接到 B
        self.assertEqual(view._step_visible(2, 1), 8)
        model.toggle_group(1)
        self.assertFalse(view.rect_for(5).isNull())


if __name__ == "__main__":
    unittest.main()
