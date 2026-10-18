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
    depth: int = 0        # 相对打开那层的嵌套深度（根目录与一级子文件夹 = 0）
    hidden: bool = False  # 某个上级文件夹被折叠了：标题栏和内容都不显示
    total: int = 0        # 含所有下级文件夹的文件数
    # 布局结果（内容坐标，视图填写）
    top: int = 0          # 标题栏顶
    body_top: int = 0     # 第一行文件顶
    bottom: int = 0

    @property
    def count(self) -> int:
        return self.end - self.start

    @property
    def folded(self) -> bool:
        """这一组的文件不显示（自己折叠了，或上级文件夹折叠了）。"""
        return self.collapsed or self.hidden


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


def _rel_parts(folder: Path, root: Path | None) -> tuple[str, ...] | None:
    if root is None:
        return None
    try:
        return folder.relative_to(root).parts
    except ValueError:
        return None


def _ancestors(folder: Path, root: Path | None) -> list[Path]:
    """folder 在打开那层之下的所有上级文件夹（由浅到深，不含 root 和自己）。"""
    parts = _rel_parts(folder, root)
    if not parts:
        return []
    out: list[Path] = []
    anc = root
    for part in parts[:-1]:
        anc = anc / part
        out.append(anc)
    return out


def build_groups(items, root: Path | None, collapsed: set[str] | None = None) -> list[Group]:
    """按"直接所在文件夹变了"切段。

    子文件夹 / 子子文件夹：某个上级文件夹自己没有文件时，也在它第一个下级
    文件夹前补一条空的标题栏（count=0），这样总有地方能点，一次收起整棵子树。
    """
    raw: list[tuple[Path, int, int]] = []
    prev: Path | None = None
    for i, it in enumerate(items):
        parent = it.path.parent
        if prev is None or parent != prev:
            if raw:
                raw[-1] = (raw[-1][0], raw[-1][1], i)
            raw.append((parent, i, i + 1))
            prev = parent
    if raw:
        raw[-1] = (raw[-1][0], raw[-1][1], len(items))

    real = {f for f, _s, _e in raw}
    groups: list[Group] = []
    virtual: set[Path] = set()
    for folder, s, e in raw:
        for anc in _ancestors(folder, root):
            if anc not in real and anc not in virtual:
                virtual.add(anc)
                groups.append(Group(anc, s, s, folder_label(anc, root),
                                    depth=len(_rel_parts(anc, root) or ()) - 1))
        parts = _rel_parts(folder, root)
        groups.append(Group(folder, s, e, folder_label(folder, root),
                            depth=max(0, len(parts) - 1) if parts else 0))

    by_folder = {g.folder: g for g in groups}
    for g in groups:
        g.total += g.count
        if g.count:
            for anc in _ancestors(g.folder, root):
                a = by_folder.get(anc)
                if a is not None:
                    a.total += g.count
    apply_fold(groups, collapsed, root)
    return groups


def apply_fold(groups: list[Group], collapsed: set[str] | None, root: Path | None) -> None:
    """按折叠集合刷新每组的 collapsed / hidden（上级折叠 → 整棵子树隐藏）。"""
    keys = collapsed or set()
    for g in groups:
        g.collapsed = folder_key(g.folder) in keys
        g.hidden = bool(keys) and any(folder_key(a) in keys for a in _ancestors(g.folder, root))


def ancestor_keys(folder: Path, root: Path | None) -> list[str]:
    return [folder_key(a) for a in _ancestors(folder, root)]


def group_index_of_row(starts: list[int], row: int) -> int:
    """starts = [g.start ...]（升序）；返回 row 所在分组下标，找不到 -1。"""
    gi = bisect_right(starts, row) - 1
    return gi if gi >= 0 else -1


def is_group_head(items, row: int) -> bool:
    """播放列表用：这一行是不是一个文件夹段的第一项。"""
    if row <= 0:
        return row == 0
    return items[row].path.parent != items[row - 1].path.parent
