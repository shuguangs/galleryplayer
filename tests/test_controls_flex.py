"""控制栏响应式收折的点位回归：按钮永不压到时间戳上。

背景 bug（用户截图实测）：折叠循环的行宽测量用 sizeHint——QSS polish
前后同一个按钮的 hint 差 10-12px，循环内 980、settle 后 1030，早停的
约 50px 让行宽超出可用宽度：弹性空隙被压到 0，速度按钮"1×"直接贴到
时间文本后面；更窄时时间标签甚至与音量条真实相交。另外时长是 mpv
异步回报的，set_duration 改了时间戳定宽却不重算折叠，起播后的折叠
状态停留在旧基准上。

修法（controls.ControlBar）：
- _row_width 按显式定宽测量（这一行控件全部 setFixedWidth，定宽即
  布局真实分配的宽度，从不随 polish 时序漂移）；
- FOLD_SLACK=36 呼吸间隙：折叠目标比理论可用宽度再收一档，弹性空隙
  永不为 0；
- "更多"按钮先显示再测量（折叠后它必然可见，循环少算 40px 又早停）；
- 时间戳宽度按本片最长文本定死（_pin_time_label），每次 _update_flex
  用当前字体度量重算（polish 前后 metrics 不一致），set_duration 触发
  重算折叠；
- 极端窄窗兜底 _sync_time_width：池子折光仍放不下时压时间戳定宽
  （裁字），宁可窄也不重叠。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.runtime import init_libmpv

try:
    init_libmpv()
except RuntimeError as exc:
    raise unittest.SkipTest(f"libmpv unavailable: {exc}")

from PySide6.QtWidgets import QApplication

from app.controls import ControlBar
from app.main_window import MainWindow


class FlexFoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.win = MainWindow()
        cls.viewer = cls.win.ensure_viewer()
        cls.bar: ControlBar = cls.viewer.controls

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.win.close()

    def setUp(self):
        self.bar._time_dur = 7261.0          # 2:01:01，时位占满的最坏情形
        # 真实流程 show_index 会先走 set_media_kind(True) 藏掉图片控件；
        # 测试不开媒体，必须手动对齐，否则行里多出 250px 不可折叠控件
        self.bar.set_media_kind(True)
        self.viewer.panel.setVisible(False)

    def _visible_row_widgets(self) -> list:
        row = self.bar._row
        return [row.itemAt(i).widget() for i in range(row.count())
                if row.itemAt(i).widget() is not None
                and not row.itemAt(i).widget().isHidden()]

    def _resize(self, width: int, panel: bool = False) -> None:
        self.viewer.panel.setVisible(panel)
        self.viewer.showNormal()
        self.viewer.resize(width, 400)
        for _ in range(5):
            self.app.processEvents()

    def _assert_no_overlap(self):
        ws = self._visible_row_widgets()
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                self.assertFalse(
                    ws[i].geometry().intersects(ws[j].geometry()),
                    f"控件重叠: {type(ws[i]).__name__}({ws[i].objectName()})"
                    f"{ws[i].geometry()} × {type(ws[j]).__name__}"
                    f"({ws[j].objectName()}){ws[j].geometry()}"
                    f"（窗口 {self.viewer.width()} 控制栏 {self.bar.width()}）")

    def test_row_width_is_stable_across_polish(self):
        """定宽测量不随 polish 漂移（旧 sizeHint 实测 980→1030 漂 50px）。"""
        self._resize(1400)
        before = self.bar._row_width()
        for w in self._visible_row_widgets():
            w.ensurePolished()
        self.bar._update_flex()
        self.assertEqual(before, self.bar._row_width())
        # 行内控件必须全部定宽（测量基准成立的前提）
        for w in self._visible_row_widgets():
            self.assertGreater(w.minimumWidth(), 0,
                               f"{type(w).__name__} 未定宽，基准会漂")

    def test_no_overlap_and_gap_at_all_widths(self):
        """各宽度下：无控件相交；池子没用光时行宽必须收进目标（留出
        呼吸间隙）——这正是"1× 压到时间上"的量化判据。"""
        for width, panel in ((1400, True), (1250, True), (1150, True),
                             (1050, True), (950, True), (1100, False),
                             (1000, False), (900, False), (800, False),
                             (700, False)):
            with self.subTest(width=width, panel=panel):
                self._resize(width, panel)
                self._assert_no_overlap()
                avail = self.bar.width() - 28 - 34 - self.bar.FOLD_SLACK
                pool_left = sum(1 for w, _ in self.bar._flex
                                if not w.isHidden())
                if pool_left:
                    self.assertLessEqual(
                        self.bar._row_width(), avail + 2,
                        f"折叠点位仍贴边：行宽 {self.bar._row_width()} > "
                        f"目标 {avail}（还有 {pool_left} 个可折而未折）")

    def test_time_label_width_pinned_stable_across_positions(self):
        """时间戳定宽：位置文本变化（异步 set_position）不改宽度。"""
        self._resize(1200)
        self.bar.set_duration(7261.0)
        w0 = self.bar.time_label.minimumWidth()
        self.bar.set_position(0.0, 7261.0)
        self.bar.set_position(3599.0, 7261.0)
        self.bar.set_position(5.0, 7261.0)
        self.assertEqual(w0, self.bar.time_label.minimumWidth())
        self.assertGreaterEqual(w0, 120)

    def test_duration_arrival_recomputes_fold(self):
        """时长异步到达 → 定宽变 → 折叠必须重算（旧 bug：停留在 120 基
        准的旧折叠状态上，按钮凭空挤到时间戳上）。"""
        self.bar.time_label.setFixedWidth(120)     # 模拟时长未到时的旧基准
        self._resize(950)
        row_before = self.bar._row_width()
        self.bar.set_duration(7261.0)              # mpv 回报时长
        self.bar._update_flex()
        avail = self.bar.width() - 28 - 34 - self.bar.FOLD_SLACK
        pool_left = sum(1 for w, _ in self.bar._flex if not w.isHidden())
        if pool_left:
            self.assertLessEqual(self.bar._row_width(), avail + 2,
                                 "时长到达后没有重算折叠")
        self.assertGreater(self.bar._row_width(),
                           row_before - 1000)       # 恒真；防手滑写出恒假
        self._assert_no_overlap()

    def test_extreme_narrow_degrades_without_overlap(self):
        """池子折光仍放不下：压时间戳定宽换空间，宁可裁字不重叠；
        拉宽后定宽恢复。"""
        self._resize(450)
        self._assert_no_overlap()
        clamped = self.bar.time_label.minimumWidth()
        self.assertGreaterEqual(clamped, 120)
        self._resize(1400)
        self._assert_no_overlap()
        self.assertEqual(self.bar.time_label.minimumWidth(),
                         self.bar._time_pin_w, "拉宽后时间戳定宽没恢复")

    def test_more_menu_holds_exactly_the_folded_buttons(self):
        """折了几个，"更多"菜单里就应有几项（收进二级菜单不丢项）。"""
        self._resize(900)
        self.bar._rebuild_more_menu()
        folded = sum(1 for w, _ in self.bar._flex if w.isHidden())
        self.assertEqual(folded, len(self.bar._more_menu.actions()))
        self.assertFalse(self.bar._more_btn.isHidden())
        self._resize(1400)
        self.bar._rebuild_more_menu()
        self.assertEqual(0, len(self.bar._more_menu.actions()))


if __name__ == "__main__":
    unittest.main()
