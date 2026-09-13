"""evaluation（评测闭环）离线单测：零第三方依赖，纯标准库 unittest。

运行：
    python -m unittest discover -s tests -v

覆盖：
  - extract_code_blocks：语言围栏识别 / 大小写 / 多块 / 非 python 围栏过滤
  - run_code_verification：正确解（函数 / 驼峰 / class Solution）通过；
    错误逻辑失败且可追溯；未找到函数 / 无代码 / 语法错误 / 危险内建拦截 /
    死循环超时；多代码块回退语义（都没命中解题函数时才取最后一块）
  - pick_verification_block（批次 1.3）：多代码块优先选定义了 function_names /
    class_names 的块；命中多块取最后一块；都不命中才回退 use_last_block
  - TEST_SUITES 默认两数之和套件结构与可扩展性（新增题目在 TEST_SUITES 加 key）
  - validate_suite 父进程预校验（批次 1.2）：空套件 / args 非 list / checker
    缺失都必须被拦成 status="error"，绝不放行出"假 pass"

注意：run_code_verification 每次真实启动一个受限子进程（毫秒级），
本文件刻意不 mock，保证"离线单测也在验证真实沙箱管线"。
"""
import textwrap
import unittest

import evaluation as ev

FENCE = "```python\n{code}\n```"


def wrapped(code):
    return FENCE.format(code=textwrap.dedent(code).strip())


GOOD_TWO_SUM = wrapped("""
    def two_sum(nums, target):
        seen = {}
        for i, v in enumerate(nums):
            if target - v in seen:
                return [seen[target - v], i]
            seen[v] = i
""")


class ExtractCodeBlocksTest(unittest.TestCase):
    def test_returns_python_blocks_in_order(self):
        text = "思路\n```python\na = 1\n```\n然后\n```python\nb = 2\n```\n"
        self.assertEqual(ev.extract_code_blocks(text), ["a = 1", "b = 2"])

    def test_case_insensitive_language_marker(self):
        self.assertEqual(ev.extract_code_blocks("```Python\nx = 1\n```"), ["x = 1"])
        self.assertEqual(ev.extract_code_blocks("```py\nx = 1\n```"), ["x = 1"])

    def test_filters_non_python_fences(self):
        text = "```text\n说明文字\n```\n```bash\necho hi\n```\n```python\nok = 1\n```\n"
        self.assertEqual(ev.extract_code_blocks(text), ["ok = 1"])

    def test_empty_or_no_fence(self):
        self.assertEqual(ev.extract_code_blocks(""), [])
        self.assertEqual(ev.extract_code_blocks("只有文字没有围栏"), [])
        self.assertEqual(ev.extract_code_blocks(None), [])

    def test_strips_blank_lines_but_keeps_indentation(self):
        text = "```python\n\n\ndef f():\n    return 1\n\n\n```"
        self.assertEqual(ev.extract_code_blocks(text), ["def f():\n    return 1"])


class RunVerificationTest(unittest.TestCase):
    def test_correct_two_sum_passes_all_cases(self):
        r = ev.run_code_verification(GOOD_TWO_SUM)
        self.assertTrue(r.passed)
        self.assertEqual(r.status, "pass")
        self.assertEqual(len(r.tests), 6)
        self.assertIsNone(r.error)
        self.assertIn("两数之和", r.suite_label)

    def test_camel_case_twoSum_passes(self):
        code = wrapped("""
            def twoSum(nums, target):
                for i in range(len(nums)):
                    for j in range(i + 1, len(nums)):
                        if nums[i] + nums[j] == target:
                            return [i, j]
        """)
        r = ev.run_code_verification(code)
        self.assertTrue(r.passed, msg=r.error)
        self.assertEqual(r.function, "twoSum")

    def test_class_solution_template_passes(self):
        code = wrapped("""
            class Solution:
                def twoSum(self, nums, target):
                    m = {}
                    for i, v in enumerate(nums):
                        if target - v in m:
                            return [m[target - v], i]
                        m[v] = i
        """)
        r = ev.run_code_verification(code)
        self.assertTrue(r.passed, msg=r.error)
        self.assertIn("Solution", r.function or "")

    def test_wrong_same_element_twice_fails_with_detail(self):
        code = wrapped("""
            def two_sum(nums, target):
                for i in range(len(nums)):
                    if nums[i] * 2 == target:
                        return [i, i]
                return [0, 1]
        """)
        r = ev.run_code_verification(code)
        self.assertFalse(r.passed)
        self.assertEqual(r.status, "fail")
        failed = [t for t in r.tests if not t.passed]
        self.assertTrue(failed)  # [3,3],6 用例应被抓出"同一元素用两次"
        self.assertTrue(any("同一元素" in t.detail for t in failed))

    def test_no_code_block_returns_no_code_status(self):
        r = ev.run_code_verification("只有分条总结，没写代码")
        self.assertFalse(r.passed)
        self.assertEqual(r.status, "no_code")
        self.assertIn("未找到", r.error)

    def test_function_not_found(self):
        code = wrapped("""
            def add(a, b):
                return a + b
        """)
        r = ev.run_code_verification(code)
        self.assertFalse(r.passed)
        self.assertEqual(r.status, "error")
        self.assertIn("未找到解题函数", r.error)

    def test_syntax_error_reported(self):
        code = wrapped("""
            def two_sum(nums, target):
                return [0
        """)
        r = ev.run_code_verification(code)
        self.assertFalse(r.passed)
        self.assertEqual(r.status, "error")
        self.assertIn("SyntaxError", r.error)

    def test_dangerous_builtin_open_blocked(self):
        # open 不在受限白名单：顶层调用在 exec 阶段即 NameError
        code = wrapped("""
            x = open(r"C:/Windows/win.ini")
            def two_sum(nums, target):
                return [0, 1]
        """)
        r = ev.run_code_verification(code)
        self.assertFalse(r.passed)
        self.assertEqual(r.status, "error")
        self.assertIn("open", r.error)
        self.assertIn("NameError", r.error)

    def test_timeout_kills_infinite_loop(self):
        code = wrapped("""
            def two_sum(nums, target):
                while True:
                    pass
        """)
        r = ev.run_code_verification(code, timeout=1)
        self.assertFalse(r.passed)
        self.assertEqual(r.status, "timeout")
        self.assertIn("超时", r.error)

    def test_multiple_blocks_uses_last_one(self):
        # 批 1.3 后本用例走"命中多块取最后一块"分支（两块都定义了 two_sum），
        # 结果与旧语义一致——定稿在最后。"都不命中"的回退见 PickVerificationBlockTest。
        text = (
            "草稿：\n" + wrapped("""
                def two_sum(nums, target):
                    return [0, 0]
            """) +
            "\n最终版：\n" + GOOD_TWO_SUM
        )
        r = ev.run_code_verification(text)
        self.assertTrue(r.passed, msg="应用最后一个代码块（最终版），而非草稿")


class PickVerificationBlockTest(unittest.TestCase):
    """批次 1.3 收尾：多代码块拣块策略（pick_verification_block / _block_defines）

    旧语义：机械取最后一块。新语义：优先选"定义了套件 function_names /
    class_names 的块"；一块都没命中才回退 use_last_block（True=最后一块）。
    既测纯函数，也测 run_code_verification 端到端是否真的用上新策略。
    """

    SUITE = {"function_names": ["two_sum"], "class_names": ["Solution"]}

    def test_block_defining_function_wins_over_trailing_debug_block(self):
        good = "def two_sum(nums, target):\n    return [0, 1]"
        debug = "print('debug')"
        self.assertEqual(
            ev.pick_verification_block([good, debug], self.SUITE), good)

    def test_multiple_matching_blocks_prefers_last(self):
        first = "def two_sum(nums, target):\n    return [0, 0]"
        second = "def two_sum(nums, target):\n    return [0, 1]"
        self.assertEqual(
            ev.pick_verification_block([first, second], self.SUITE), second)

    def test_class_name_match_is_recognised(self):
        draft = "print('thinking')"
        solution = ("class Solution:\n"
                    "    def twoSum(self, nums, target):\n"
                    "        return [0, 1]")
        self.assertEqual(
            ev.pick_verification_block([draft, solution], self.SUITE), solution)

    def test_no_match_falls_back_to_last_or_first(self):
        a, b = "print('a')", "print('b')"
        self.assertEqual(ev.pick_verification_block([a, b], self.SUITE), b)
        self.assertEqual(
            ev.pick_verification_block([a, b], self.SUITE, use_last_block=False), a)

    def test_mention_only_does_not_count_as_definition(self):
        # 只在注释/字符串里提到函数名，不算"定义了函数"，不得被误选
        mention = "# two_sum 思路见上\nprint('two_sum')"
        real = wrapped("""
            def two_sum(nums, target):
                return [0, 1]
        """)
        self.assertFalse(ev._block_defines(mention, ["two_sum"]))
        self.assertEqual(
            ev.pick_verification_block([mention, real], self.SUITE), real)

    def test_empty_blocks_returns_none(self):
        self.assertIsNone(ev.pick_verification_block([], self.SUITE))

    def test_end_to_end_prefers_defining_block(self):
        """回归 smoke_1_3 场景：定稿块在前、末尾是调试块，仍应命中定稿块并 pass"""
        text = "final:\n" + GOOD_TWO_SUM + "\ndebug script:\n" + wrapped("print('debug')")
        r = ev.run_code_verification(text)
        self.assertTrue(r.passed, msg=r.error)
        self.assertEqual(r.status, "pass")
        self.assertIn("two_sum", r.code)

    def test_end_to_end_falls_back_when_nothing_matches(self):
        """一块都没定义解题函数 → 回退旧语义（取最后一块），报错而非静默 pass"""
        text = wrapped("print('a')") + "\n" + wrapped("print('b')")
        r = ev.run_code_verification(text)
        self.assertEqual(r.status, "error")
        self.assertIn("未找到解题函数", r.error)
        self.assertIn("print('b')", r.code)


class ValidateSuiteTest(unittest.TestCase):
    """批次 1.2：validate_suite 父进程预校验

    核心回归点：cases 为空时，runner 的 for 循环空转、all_ok 保持 True，
    旧实现会渲染成 [代码验证 ✓]（假 pass）。预校验必须把它拦成 error。
    """

    def test_valid_suite_has_no_problems(self):
        self.assertEqual(ev.validate_suite("two_sum"), [])

    def test_unknown_suite_reported(self):
        problems = ev.validate_suite("_no_such_suite")
        self.assertTrue(problems)
        self.assertIn("不存在", problems[0])

    def test_empty_cases_rejected_and_never_passes(self):
        ev.TEST_SUITES["_empty_cases"] = {
            "label": "演示·空套件",
            "function_names": ["two_sum"],
            "checkers": {},
            "cases": [],
        }
        try:
            problems = ev.validate_suite("_empty_cases")
            self.assertTrue(any("cases 为空" in p for p in problems))
            r = ev.run_code_verification(GOOD_TWO_SUM, suite_name="_empty_cases")
            self.assertFalse(r.passed)                       # 绝不能是 pass
            self.assertEqual(r.status, "error")
            self.assertIn("套件无效", r.error)
            self.assertEqual(r.tests, [])                    # 没跑任何用例
        finally:
            ev.TEST_SUITES.pop("_empty_cases", None)

    def test_args_not_list_reported(self):
        ev.TEST_SUITES["_bad_args"] = {
            "label": "演示·args 非 list",
            "function_names": ["two_sum"],
            "checkers": {"ok": "assert result"},
            "cases": [{"name": "坏用例", "args": "hello", "checker": "ok"}],
        }
        try:
            problems = ev.validate_suite("_bad_args")
            self.assertTrue(any("args 必须是 list" in p for p in problems))
            r = ev.run_code_verification(GOOD_TWO_SUM, suite_name="_bad_args")
            self.assertEqual(r.status, "error")
            self.assertIn("套件无效", r.error)
        finally:
            ev.TEST_SUITES.pop("_bad_args", None)

    def test_missing_checker_reported(self):
        ev.TEST_SUITES["_bad_checker"] = {
            "label": "演示·checker 缺失",
            "function_names": ["two_sum"],
            "checkers": {},
            "cases": [{"name": "无模板", "args": [[2, 7], 9], "checker": "nope"}],
        }
        try:
            problems = ev.validate_suite("_bad_checker")
            self.assertTrue(any("checker 无效" in p for p in problems))
            r = ev.run_code_verification(GOOD_TWO_SUM, suite_name="_bad_checker")
            self.assertEqual(r.status, "error")
            self.assertIn("套件无效", r.error)
        finally:
            ev.TEST_SUITES.pop("_bad_checker", None)

    def test_default_suite_still_passes_after_precheck(self):
        """回归护栏：预校验不得误伤合法套件（two_sum 仍 6/6 通过）"""
        r = ev.run_code_verification(GOOD_TWO_SUM, suite_name="two_sum")
        self.assertTrue(r.passed, msg=r.error)
        self.assertEqual(r.status, "pass")
        self.assertEqual(len(r.tests), 6)


class TestSuitesExtensionTest(unittest.TestCase):
    def test_two_sum_suite_shape_and_cases(self):
        suite = ev.TEST_SUITES.get("two_sum")
        self.assertIsNotNone(suite, "默认套件 two_sum 必须存在")
        self.assertIn("label", suite)
        self.assertIn("function_names", suite)
        self.assertIn("checkers", suite)
        self.assertTrue(len(suite["cases"]) >= 5, "默认用例应覆盖多种输入与边界")
        # 每个用例引用存在的 checker
        for case in suite["cases"]:
            self.assertIn(case["checker"], suite["checkers"],
                          f"用例 {case['name']} 引用了不存在的校验模板")

    def test_extending_new_problem_is_structurally_supported(self):
        """扩展演示：仿照新增题目注册新套件后应能直接验证（无侵入）

        契约：args 会被展开为 fn(*args)，仅放函数实参；
              期望值在 checker 断言里表达（可引用 result 与 args）。
        """
        ev.TEST_SUITES["_demo_reverse"] = {
            "label": "演示·反转字符串",
            "function_names": ["rev"],
            "class_names": ["Solution"],
            "checkers": {"expect": "assert result == 'olleh', f'期望 olleh，实际 {result!r}'"},
            "cases": [
                {"name": "基本", "args": ["hello"], "checker": "expect"},
            ],
        }
        try:
            r = ev.run_code_verification(wrapped("""
                def rev(s):
                    return s[::-1]
            """), suite_name="_demo_reverse")
            self.assertTrue(r.passed, msg=r.error)
        finally:
            ev.TEST_SUITES.pop("_demo_reverse", None)


if __name__ == "__main__":
    unittest.main()
class ProcessTreeKillTest(unittest.TestCase):
    """超时杀进程树：必须连孙进程一起杀，不留孤儿"""

    def test_terminate_process_tree_kills_grandchild(self):
        import os
        import subprocess
        import sys
        import time

        # 子进程：先 spawn 一个"孙进程"，再自己进入死循环
        child_code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        grandchild_pid = int(child.stdout.readline().strip())   # 等子进程报出孙进程 pid

        try:
            ev._terminate_process_tree(child)   # 模拟"超时杀进程树"
            child.wait(timeout=5)
            time.sleep(0.5)                     # 给 taskkill 一点生效时间
            # 孙进程应已被连坐：对它发信号 0 会报"进程不存在"（OSError 系）
            with self.assertRaises(OSError):
                os.kill(grandchild_pid, 0)
        finally:
            if child.poll() is None:
                child.kill()
            try:
                os.kill(grandchild_pid, 0)
            except OSError:
                pass
            else:
                # 兜底：万一孙进程还活着就手动清掉，别污染本机
                subprocess.run(
                    ["taskkill", "/PID", str(grandchild_pid), "/T", "/F"],
                    capture_output=True,
                )
