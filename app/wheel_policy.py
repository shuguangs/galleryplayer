"""播放器里滚轮切换上一部 / 下一部的节流。

用户反馈：随手滚一下就跳三四个视频。原因是一次"拨动"滚轮其实会连发好几格
（每格 120），以前每格都切一部。现在的规则：

- 一次滚动的第一格立刻切一部，跟以前一样跟手；
- 之后同一次滚动里多出来的格不再逐格生效，滚轮还在持续滚时，每隔
  REPEAT_S 秒最多再切一部；
- 停止滚动超过 GAP_S 秒算一次新的滚动，下一格又能立刻切；
- 中途反向滚动也算新的一次，立刻往回切。
"""
from __future__ import annotations

import time

NOTCH = 120        # 一格滚轮的 angleDelta
GAP_S = 0.25       # 停顿超过这么久算一次新的滚动
REPEAT_S = 0.35    # 持续滚动时最多每隔这么久切一部


class WheelStepper:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._accum = 0
        self._last_event = None
        self._last_step = None
        self._dir = 0

    def feed(self, dy: int) -> int:
        """喂一次滚轮事件；返回要切的方向：-1 上一部 / +1 下一部 / 0 不切。"""
        if not dy:
            return 0
        now = self._clock()
        direction = -1 if dy > 0 else 1
        new_gesture = (
            self._last_event is None
            or now - self._last_event > GAP_S
            or direction != self._dir
        )
        self._last_event = now
        if new_gesture:
            self._accum = 0
            self._last_step = None
            self._dir = direction
        self._accum += abs(dy)
        if self._accum < NOTCH:          # 触控板的小增量：攒够一格再算
            return 0
        if self._last_step is not None and now - self._last_step < REPEAT_S:
            self._accum = min(self._accum, NOTCH)   # 多余的格丢掉，不囤着
            return 0
        self._accum = 0
        self._last_step = now
        return direction
