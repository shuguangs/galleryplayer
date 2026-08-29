"""media.py 纯函数（分类/过滤/排序）与 controller 译文回填索引的单测。"""
import unittest
from pathlib import Path

from app.media import MediaItem, apply_filter, classify_name, sort_items


class ClassifyNameTests(unittest.TestCase):
    def test_video_image_and_unknown(self):
        self.assertIs(classify_name("movie.MP4"), True)
        self.assertIs(classify_name("clip.mkv"), True)
        self.assertIs(classify_name("photo.jpg"), False)
        self.assertIs(classify_name("shot.heic"), False)
        self.assertIsNone(classify_name("notes.txt"))
        self.assertIsNone(classify_name("noext"))

    def test_case_and_unicode(self):
        self.assertIs(classify_name("视频.WEBM"), True)
        self.assertIs(classify_name("图片.PNG"), False)


def _item(name: str, size: int = 1, mtime: float = 0.0,
          is_video: bool = True, is_archive: bool = False) -> MediaItem:
    return MediaItem(path=Path("V:/") / name, is_video=is_video,
                     size=size, mtime=mtime, is_archive=is_archive)


class ApplyFilterTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            _item("a.mp4"),
            _item("b.jpg", is_video=False),
            _item("c.zip", is_video=False, is_archive=True),
        ]

    def test_empty_flags_show_all(self):
        self.assertEqual(len(apply_filter(self.items, set())), 3)

    def test_full_flags_show_all(self):
        self.assertEqual(len(apply_filter(self.items, {"image", "video", "archive"})), 3)

    def test_video_only(self):
        out = apply_filter(self.items, {"video"})
        self.assertEqual([i.name for i in out], ["a.mp4"])

    def test_search_substring_case_insensitive(self):
        out = apply_filter(self.items, set(), search="B.J")
        self.assertEqual([i.name for i in out], ["b.jpg"])


class SortItemsTests(unittest.TestCase):
    def test_name_sort_asc_desc(self):
        items = [_item("b.mp4"), _item("a.mp4"), _item("c.mp4")]
        asc = [i.name for i in sort_items(items, "name", False)]
        desc = [i.name for i in sort_items(items, "name", True)]
        self.assertEqual(asc, ["a.mp4", "b.mp4", "c.mp4"])
        self.assertEqual(desc, ["c.mp4", "b.mp4", "a.mp4"])

    def test_random_is_seeded(self):
        items = [_item(n) for n in ("a", "b", "c", "d", "e")]
        first = [i.name for i in sort_items(items, "random", False, seed=7)]
        again = [i.name for i in sort_items(items, "random", False, seed=7)]
        self.assertEqual(first, again)

    def test_custom_order_unmentioned_go_last(self):
        items = [_item("z.mp4"), _item("a.mp4"), _item("m.mp4")]
        out = [i.name for i in sort_items(items, "custom", False,
                                          manual_order=["m.mp4", "z.mp4"])]
        self.assertEqual(out[0], "m.mp4")
        self.assertEqual(out[1], "z.mp4")
        self.assertEqual(out[2], "a.mp4")  # 未点名 → 殿后

    def test_original_list_not_mutated(self):
        items = [_item("b.mp4"), _item("a.mp4")]
        sort_items(items, "name", False)
        self.assertEqual([i.name for i in items], ["b.mp4", "a.mp4"])


if __name__ == "__main__":
    unittest.main()
