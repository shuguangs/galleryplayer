"""音量上限策略：鼠标最多 100%，键盘 ↑ 才能放大到 130%。

超过 100% 是 mpv 的软件放大（数字增益），音源本身响的片子会破音。
所以拖滑块 / Ctrl+滚轮这类"随手一拉"的操作封顶 100%，只有有意识地按
键盘 ↑ 才进入放大区；上限 130% 与 mpv 默认的 volume-max 一致。
"""
from __future__ import annotations

MOUSE_MAX = 100
BOOST_MAX = 130


def clamp(value: float, keyboard: bool) -> int:
    top = BOOST_MAX if keyboard else MOUSE_MAX
    return int(max(0, min(top, round(value))))


def step(current: float, delta: int, keyboard: bool) -> int:
    """按一次增减后的音量。

    鼠标在放大区（>100%）继续往上时保持不变——不能把 120% "调大"成 100%；
    往下则正常减。
    """
    cur = int(round(current))
    if not keyboard and delta > 0 and cur >= MOUSE_MAX:
        return min(cur, BOOST_MAX)
    return clamp(cur + delta, keyboard=keyboard or cur > MOUSE_MAX)
