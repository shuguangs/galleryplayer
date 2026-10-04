"""含子文件夹浏览时"按文件夹分组"排序。

需求（用户）：
- 默认排序不变；排序旁加勾选，需要时才分组。
- 按文件直接所在的文件夹分组（"2" 与 "2/子" 是两组）。
- 文件夹之间可按：名称 / 修改时间 / 总大小 / 视频总时长 排，可升降序；
  原排序下拉管文件夹内部顺序。
- 例：父文件夹 1、2、3，1 里的 x 也排在 2 里的 a 前面。
- 根目录（打开的那一层）的文件固定在最前面。
- 视频总时长：图片不计时长，没有视频的文件夹排尾部（升降序都在尾部）。
"""
import unittest
from pathlib import Path

from app.media import (FOLDER_SORT_LABELS, MediaItem, group_sort_items,
                       sort_items)

R = Path("D:/库")


def _v(rel, mtime=0.0, size=0, dur=None, video=True):
    return MediaItem(path=R / rel, is_video=video, size=size, mtime=mtime,
                     duration=dur)


def _rel(items):
    return [str(i.path.relative_to(R)).replace("\\", "/") for i in items]


class GroupSortTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            _v("3/c.mp4", 150, 40, 30),
            _v("2/b.mp4", 400, 20, 10),
            _v("1/x.mp4", 300, 30, 50),
            _v("2/a.mp4", 200, 50, 20),
            _v("根.mp4", 250, 60, 5),
            _v("1/y.mp4", 100, 10, 60),
            _v("2/子/z.mp4", 350, 15, 40),
        ]

    def _sort(self, folder_key="name", folder_desc=False, key="name", desc=False):
        return _rel(group_sort_items(self.items, R, folder_key, folder_desc,
                                     key, desc))

    def test_user_example_folder_name_beats_file_name(self):
        out = self._sort()
        self.assertLess(out.index("1/x.mp4"), out.index("2/a.mp4"),
                        "1 里的 x 应排在 2 里的 a 前面")

    def test_root_files_first_then_folders_by_name(self):
        self.assertEqual(self._sort(), [
            "根.mp4",
            "1/x.mp4", "1/y.mp4",
            "2/a.mp4", "2/b.mp4",
            "2/子/z.mp4",
            "3/c.mp4",
        ])

    def test_direct_parent_grouping_keeps_nested_folder_separate(self):
        out = self._sort(folder_desc=True)
        # 降序：3 > 2/子 > 2 > 1，根仍在最前
        self.assertEqual(out[0], "根.mp4")
        self.assertEqual(out[1:], ["3/c.mp4", "2/子/z.mp4", "2/a.mp4", "2/b.mp4",
                                   "1/x.mp4", "1/y.mp4"])

    def test_inside_folder_uses_file_sort(self):
        out = self._sort(key="mtime", desc=True)
        self.assertEqual(out[out.index("1/x.mp4"):out.index("1/x.mp4") + 2],
                         ["1/x.mp4", "1/y.mp4"])
        out = self._sort(key="mtime", desc=False)
        self.assertEqual(out[out.index("1/y.mp4"):out.index("1/y.mp4") + 2],
                         ["1/y.mp4", "1/x.mp4"])

    def test_folder_mtime_uses_newest_file(self):
        # 最新文件：1→300，2→400，2/子→350，3→150
        out = self._sort(folder_key="mtime")
        self.assertEqual(out, ["根.mp4", "3/c.mp4", "1/x.mp4", "1/y.mp4",
                               "2/子/z.mp4", "2/a.mp4", "2/b.mp4"])

    def test_folder_total_size(self):
        # 总大小：1→40，2→70，2/子→15，3→40（与 1 并列时按名称）
        out = self._sort(folder_key="size")
        self.assertEqual(out, ["根.mp4", "2/子/z.mp4", "1/x.mp4", "1/y.mp4",
                               "3/c.mp4", "2/a.mp4", "2/b.mp4"])

    def test_folder_total_duration_images_ignored_and_videoless_last(self):
        items = self.items + [_v("4/pic.jpg", 999, 999, None, video=False)]
        for desc in (False, True):
            out = _rel(group_sort_items(items, R, "duration", desc, "name", False))
            self.assertEqual(out[-1], "4/pic.jpg",
                             f"没有视频的文件夹应排尾部（desc={desc}）")
        # 总时长：1→110，2→30，2/子→40，3→30（并列按名称）
        out = _rel(group_sort_items(items, R, "duration", False, "name", False))
        self.assertEqual(out, ["根.mp4", "2/a.mp4", "2/b.mp4", "3/c.mp4",
                               "2/子/z.mp4", "1/x.mp4", "1/y.mp4", "4/pic.jpg"])

    def test_unknown_duration_counts_as_zero_not_videoless(self):
        """时长还没探到的视频：文件夹仍按"有视频"处理，不被甩到尾部。"""
        items = [_v("a/1.mp4", dur=None), _v("b/1.jpg", video=False)]
        out = _rel(group_sort_items(items, R, "duration", False, "name", False))
        self.assertEqual(out, ["a/1.mp4", "b/1.jpg"])

    def test_random_inside_folder_stays_grouped(self):
        out = _rel(group_sort_items(self.items, R, "name", False, "random", False, seed=7))
        groups = [p.rsplit("/", 1)[0] if "/" in p else "" for p in out]
        self.assertEqual(groups, ["", "1", "1", "2", "2", "2/子", "3"],
                         "随机只能在组内打乱，不能打散分组")

    def test_items_outside_root_dont_crash(self):
        """root 不是公共祖先（例如拖进来的列表）：退化为按完整父路径分组。"""
        items = [MediaItem(path=Path("E:/x/1.mp4"), is_video=True, size=0, mtime=0)]
        out = group_sort_items(items + self.items, R, "name", False, "name", False)
        self.assertEqual(len(out), len(self.items) + 1)

    def test_default_sort_unchanged(self):
        """不分组时 sort_items 行为原样（回归保护）。"""
        self.assertEqual(_rel(sort_items(self.items, "name", False)),
                         ["1/x.mp4", "1/y.mp4", "2/a.mp4", "2/b.mp4",
                          "2/子/z.mp4", "3/c.mp4", "根.mp4"])

    def test_folder_sort_labels(self):
        self.assertEqual(list(FOLDER_SORT_LABELS), ["name", "mtime", "size", "duration"])


class WiringTests(unittest.TestCase):
    def test_settings_defaults_off(self):
        from app.config import DEFAULTS

        self.assertIs(DEFAULTS["group_by_folder"], False, "分组默认必须关闭")
        self.assertEqual(DEFAULTS["folder_sort_key"], "name")
        self.assertIs(DEFAULTS["folder_sort_desc"], False)

    def test_panel_sort_uses_injected_sorter(self):
        """播放器右侧面板改排序时也要按分组规则排（否则面板与浏览器顺序不一致）。"""
        root = Path(__file__).resolve().parent.parent / "app"
        panel = (root / "playlist_panel.py").read_text(encoding="utf-8")
        body = panel[panel.index("def _apply_panel_sort"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertTrue("self.sort_fn(" in body, "面板排序没走注入的排序函数")
        mw = (root / "main_window.py").read_text(encoding="utf-8")
        self.assertTrue("panel.sort_fn = self._sorted_for_view" in mw,
                        "主窗口没把分组排序注入给面板")

    def test_apply_view_uses_grouping(self):
        src = (Path(__file__).resolve().parent.parent / "app" / "main_window.py"
               ).read_text(encoding="utf-8")
        body = src[src.index("def _apply_view"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertTrue("_sorted_for_view(" in body, "_apply_view 没走分组排序")


if __name__ == "__main__":
    unittest.main()
