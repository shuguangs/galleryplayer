"""按文件夹分组：标题栏分隔 + 点标题栏折叠/展开。

需求（用户）：含子文件夹 + 按文件夹分组时，不同文件夹之间要分隔开；
点标题栏折叠或展开这个文件夹。行号必须始终等于列表下标（播放顺序不变）。
"""
import unittest
from pathlib import Path

from PySide6.QtCore import QPoint, Qt

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


def nested():
    # 根 1 个；A 1 个、A/B 1 个、A/B/C 1 个；X 自己没文件、X/Y 2 个；Z 1 个
    return [mk(r) for r in ["r.jpg", "A/1.jpg", "A/B/2.jpg", "A/B/C/3.jpg",
                            "X/Y/4.jpg", "X/Y/5.jpg", "Z/6.jpg"]]


class NestedCollapseTests(unittest.TestCase):
    """点子文件夹的标题栏 = 收起它下面所有子子文件夹。"""

    def test_virtual_parent_and_totals(self):
        gs = build_groups(nested(), ROOT)
        self.assertEqual([g.label for g in gs][1:], ["A", "A/B", "A/B/C", "X", "X/Y", "Z"])
        self.assertEqual(sum(g.count for g in gs), 7)
        by = {g.label: g for g in gs}
        self.assertEqual(by["X"].count, 0)          # 补出来的空标题栏
        self.assertEqual(by["X"].total, 2)
        self.assertEqual(by["A"].total, 3)
        self.assertEqual(by["A/B/C"].depth, 2)

    def test_collapse_parent_hides_subtree(self):
        model = MediaModel()
        model.set_grouping(True, ROOT)
        model.set_items(nested())
        gi = [g.label for g in model.groups].index("A")
        model.toggle_group(gi)
        by = {g.label: g for g in model.groups}
        self.assertTrue(by["A"].collapsed and not by["A"].hidden)
        self.assertTrue(by["A/B"].hidden and by["A/B/C"].hidden)
        self.assertFalse(by["X/Y"].folded)
        for row in (1, 2, 3):
            self.assertTrue(model.row_collapsed(row))
        # 定位到被收起的子子文件夹里的项：整条链展开
        model.expand_row(3)
        self.assertFalse(any(model.row_collapsed(r) for r in range(7)))

    def test_tile_layout_skips_hidden_subtree(self):
        model = MediaModel()
        model.set_grouping(True, ROOT)
        view = TileView(model, ThumbnailCache())
        view.resize(800, 2000)
        view.set_columns(4)
        model.set_items(nested())
        view.relayout()
        by = {g.label: g for g in model.groups}
        model.toggle_group(model.groups.index(by["A"]))
        self.assertTrue(view.rect_for(2).isNull())
        # 被收起的 A/B 不占位置：原来它的位置现在是 X 的标题栏
        self.assertEqual(view._header_at(QPoint(60, by["A/B"].top + 5)), model.groups.index(by["X"]))
        # X 紧跟在 A 的标题栏下面
        self.assertLessEqual(by["X"].top, by["A"].top + HEADER_H + 20)

    def test_scrollbar_marks_locate_folders(self):
        model = MediaModel()
        model.set_grouping(True, ROOT)
        view = TileView(model, ThumbnailCache())
        view.resize(400, 300)
        view.set_columns(2)
        model.set_items(nested())
        view.relayout()
        bar = view.verticalScrollBar()
        bar.resize(11, 300)
        marks = bar.bar_marks()
        self.assertEqual(len(marks), len(model.groups))
        ys = [y for y, _g, _i in marks]
        self.assertEqual(ys, sorted(ys))
        # 鼠标在某道刻度上 → 认出这个文件夹；点它 → 滚到那个文件夹
        y, g, gi = marks[-1]
        self.assertEqual(bar.mark_at(y), gi)
        view.jump_to_group(gi)
        self.assertEqual(bar.value(), min(bar.maximum(), max(0, g.top - 4)))


class ScrollBarClickTests(unittest.TestCase):
    """滑块盖住好几个文件夹时，点滑块里的任意文件夹也要跳过去。"""

    def test_click_any_mark_jumps_even_under_handle(self):
        from PySide6.QtCore import QPointF, QEvent
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        model = MediaModel()
        model.set_grouping(True, ROOT)
        view = TileView(model, ThumbnailCache())
        view.resize(400, 300)
        view.set_columns(2)
        model.set_items(nested())
        view.relayout()
        view.show()
        QApplication.processEvents()
        bar = view.verticalScrollBar()
        jumped = []
        bar._jump_fn = jumped.append

        def click(y):
            pt = QPointF(bar.width() / 2, y)
            for t_ in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
                ev = QMouseEvent(t_, pt, bar.mapToGlobal(pt), Qt.LeftButton,
                                 Qt.LeftButton if t_ == QEvent.MouseButtonPress else Qt.NoButton,
                                 Qt.NoModifier)
                QApplication.sendEvent(bar, ev)

        for y, _g, gi in bar.bar_marks():
            jumped.clear()
            click(y + 1)
            self.assertEqual(jumped, [gi])
