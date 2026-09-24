"""GUI 选择逻辑单测。

GUI 交互改动没法在无桌面环境下目测，所以把其中"可判定"的部分抽成了
gui.py 的模块级纯函数，在这里钉住：

  - resolve_verify_choice：下拉选择 → run_discussion 的 (verify_code, verify_suite)
  - no_verify_hint：选「不验证」时最终总结区要补的灰字提示
  - widget_state：讨论进行中轮数 Spinbox / 套件下拉该置灰

本文件不创建任何 Tk 窗口：import gui 只依赖 tkinter 模块本身（不需要真实显示），
断言只针对纯函数，因此可离线、可重复运行。
"""
import unittest

import evaluation as ev
import gui


class ResolveVerifyChoiceTest(unittest.TestCase):
    """下拉选择 → (verify_code, verify_suite)"""

    def test_no_verify_choice_disables_verification(self):
        """选「不验证」→ 完全不开验证，且套件必须为 None（否则会退回写死的套件）"""
        self.assertEqual(gui.resolve_verify_choice(gui.NO_VERIFY_LABEL), (False, None))

    def test_auto_choice_means_auto_mode(self):
        """选「自动识别」→ 开验证但套件留 None（交给 pick_suite，拿不准则 skipped）"""
        self.assertEqual(gui.resolve_verify_choice(gui.AUTO_SUITE_LABEL), (True, None))

    def test_suite_label_maps_to_suite_key(self):
        """选中某个套件 label → 锁定该套件 key（label 与 key 不是一回事）"""
        suite_key, suite = next(iter(ev.TEST_SUITES.items()))
        self.assertEqual(gui.resolve_verify_choice(suite["label"]), (True, suite_key))

    def test_unknown_choice_falls_back_to_auto(self):
        """未知选项兜底按自动处理，不能悄悄变成"不验证"或锁定到错误的套件"""
        self.assertEqual(gui.resolve_verify_choice("不存在的套件"), (True, None))


class NoVerifyHintTest(unittest.TestCase):
    """选「不验证」时最终总结区的灰字提示"""

    def test_hint_when_verification_disabled(self):
        """未启用验证 → 必须给提示，且提示里点明"是下拉选了不验证"，避免误判为功能坏了"""
        hint = gui.no_verify_hint(False, gui.NO_VERIFY_LABEL)
        self.assertIn("未启用代码验证", hint)
        self.assertIn(gui.NO_VERIFY_LABEL, hint)

    def test_no_hint_when_verification_enabled(self):
        """启用了验证就不该多这一行（验证结论由 on_verification 事件渲染）"""
        self.assertEqual(gui.no_verify_hint(True, gui.AUTO_SUITE_LABEL), "")

    def test_hint_echoes_actual_choice(self):
        """提示回显的是本次实际选中的那一项，而不是写死的字面量"""
        self.assertIn("两数之和", gui.no_verify_hint(False, "两数之和"))


class WidgetStateTest(unittest.TestCase):
    """讨论进行中置灰的控件状态（轮数 Spinbox / 套件下拉）"""

    def test_running_disables_controls(self):
        self.assertEqual(gui.widget_state(True), "disabled")

    def test_idle_restores_controls(self):
        self.assertEqual(gui.widget_state(False), "normal")


if __name__ == "__main__":
    unittest.main()
