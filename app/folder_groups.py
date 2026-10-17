"""按文件夹分组浏览时的分组表：把已排好序的列表切成"一个文件夹一段"。

group_sort_items 保证同一文件夹的文件是连续的一段（根目录那段在最前），
这里只按"直接所在文件夹变了"切段，不重排、不丢项——行号仍等于列表下标，
播放顺序、上一部/下一部都不受影响。视图（缩略图/瀑布流/详细列表/播放
列表）只是在每段前面画一条标题栏；折叠状态按文件夹完整路径记，改排序、
重扫之后仍然保持。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from .i18n import t


@dataclass
class Group:
    folder: Path
    start: int            # 第一项的行号
    end: int              # 末项之后（不含）
    label: str
    collapsed: bool = False
    # 布局结果（内容坐标，视图填写）
    top: int = 0          # 标题栏顶
    body_top: int = 0     # 第一行文件顶
    bottom: int = 0

    @property
    def count(self) -> int:
        return self.end - self.start


def folder_key(folder: Path) -> str:
    return str(folder).lower()


def folder_label(folder: Path, root: Path | None) -> str:
    """标题栏文字：根目录叫"当前文件夹"，其余显示相对打开那层的路径。"""
    if root is not None:
        if folder == root:
            return t("browser.group_current_folder")
        try:
            return folder.relative_to(root).as_posix()
        except ValueError:
            pass
    return str(folder)


def build_groups(items, root: Path | None, collapsed: set[str] | None = None) -> list[Group]:
    groups: list[Group] = []
    prev: Path | None = None
    for i, it in enumerate(items):
        parent = it.path.parent
        if prev is None or parent != prev:
            if groups:
                groups[-1].end = i
            groups.append(Group(parent, i, i + 1, folder_label(parent, root)))
            prev = parent
    if groups:
        groups[-1].end = len(items)
    if collapsed:
        for g in groups:
            g.collapsed = folder_key(g.folder) in collapsed
    return groups


def group_index_of_row(starts: list[int], row: int) -> int:
    """starts = [g.start ...]（升序）；返回 row 所在分组下标，找不到 -1。"""
    gi = bisect_right(starts, row) - 1
    return gi if gi >= 0 else -1


def is_group_head(items, row: int) -> bool:
    """播放列表用：这一行是不是一个文件夹段的第一项。"""
    if row <= 0:
        return row == 0
    return items[row].path.parent != items[row - 1].path.parent
