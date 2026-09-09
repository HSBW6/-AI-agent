"""evaluation（评测闭环）离线单测：零第三方依赖，纯标准库 unittest。

运行：
    python -m unittest discover -s tests -v

覆盖：
  - extract_code_blocks：语言围栏识别 / 大小写 / 多块 / 非 python 围栏过滤
  - run_code_verification：正确解（函数 / 驼峰 / class Solution）通过；
    错误逻辑失败且可追溯；未找到函数 / 无代码 / 语法错误 / 危险内建拦截 /
    死循环超时；多代码块取最后一块
  - TEST_SUITES 默认两数之和套件结构与可扩展性（新增题目在 TEST_SUITES 加 key）

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
        text = (
            "草稿：\n" + wrapped("""
                def two_sum(nums, target):
                    return [0, 0]
            """) +
            "\n最终版：\n" + GOOD_TWO_SUM
        )
        r = ev.run_code_verification(text)
        self.assertTrue(r.passed, msg="应用最后一个代码块（最终版），而非草稿")


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
