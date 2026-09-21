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
  - 用例级 time_limit（任务 2）：结果正确但超限判该用例 fail 并报出实际耗时；
    限时内完成仍 pass（延时用纯计算循环制造，沙箱禁 __import__）

注意：run_code_verification 每次真实启动一个受限子进程（毫秒级），
本文件刻意不 mock，保证"离线单测也在验证真实沙箱管线"。
"""
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest

import evaluation as ev





# --- 进程存活探测（批次 5.1②）---------------------------------------------
def _is_pid_alive(pid):
    """跨平台判断某个 pid 是否还活着。

    坑：Windows 上 os.kill(pid, 0) 不是 POSIX 那句"只探测、不真发信号"——
    CPython 在 Windows 走的是 OpenProcess + TerminateProcess，那个 0 会被当成
    退出码，等于顺手把目标进程强杀了（想探测却杀人）。原先这条用例能过属于"恰好"。
    所以：Windows 改用 tasklist 查询，POSIX 才用 os.kill(pid, 0)。
    """
    pid = int(pid)
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        ).stdout
        # 逐行解析第 2 列（PID）。不要图省事写 `str(pid) in out`：
        # 查 123 会误命中 1234，也会撞上"内存占用"列里的数字
        for line in out.splitlines():
            parts = [c.strip().strip('"') for c in line.split(",")]
            if len(parts) >= 2 and parts[1] == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)          # POSIX：信号 0 仅做存在性/权限检查，不真发信号
    except OSError:
        return False
    return True

def _wait_pid_gone(pid, timeout=2.0, interval=0.1):
    """轮询等待 pid 消失：taskkill 是异步生效的，杀完立刻断言会偶发翻红。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(interval)
    return not _is_pid_alive(pid)    # 最后再探一次，避免刚好卡在边界上

def _force_kill_pid(pid):
    """兜底清理：万一孙进程还活着就强杀，绝不留孤儿污染本机。"""
    if not _is_pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


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
        # 示例解用线性哈希：避免被性能用例的 20000 长数组拖到超时（O(n^2) 会被 time_limit=2 筛掉）
        code = wrapped("""
            def twoSum(nums, target):
                seen = {}
                for i, v in enumerate(nums):
                    rest = target - v
                    if rest in seen:
                        return [seen[rest], i]
                    seen[v] = i
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


class ProcessTreeKillTest(unittest.TestCase):

    """超时杀进程树：必须连孙进程一起杀，不留孤儿"""

    def test_terminate_process_tree_kills_grandchild(self):
        # 子进程：先 spawn 一个"孙进程"，再自己进入死循环
        child_code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        # 用 with 托管 Popen：退出时自动 close 掉 stdout/stderr 管道并 wait，
        # 消掉原来那几条 ResourceWarning: unclosed file（批次 5.1②）
        with subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            grandchild_pid = int(child.stdout.readline().strip())   # 等子进程报出孙进程 pid

            try:
                ev._terminate_process_tree(child)      # 模拟"超时杀进程树"
                child.wait(timeout=5)
                # 不再用 os.kill(pid, 0) 探测（Windows 上那是"杀"不是"探"），
                # 改跨平台探测 + 轮询 2s 等 taskkill 连坐生效
                self.assertTrue(
                    _wait_pid_gone(grandchild_pid, timeout=2.0),
                    f"孙进程 {grandchild_pid} 仍存活：杀进程树没有连坐孙进程",
                )
            finally:
                if child.poll() is None:
                    child.kill()
                _force_kill_pid(grandchild_pid)        # 兜底，绝不污染本机


class CaseTimeLimitTest(unittest.TestCase):
    """任务 2（批次 5.3 配套）：用例级 time_limit 机制的单测。

    背景：time_limit 是性能用例（two_sum 的 12000 长数组）唯一的筛子，此前零覆盖
    ——正因为没测，才让"性能用例撞 15s 全局超时、time_limit 从不生效"溜到验收阶段。
    本类钉住两条路径：超限判 fail、限时内仍 pass。

    两条刻意的取舍：
      - 不用真实 O(n^2) 来测（慢，且结果随机器漂移），改在受测代码里塞"纯计算循环"
        制造可控耗时；断言只看 status 与文案，**不比对具体秒数**（绝对计时必然脆弱）；
      - 沙箱禁 __import__，受测代码里写 time.sleep 会直接 ImportError（实测报 error），
        所以延时只能用循环，不能用睡。
    """

    LIMIT = 0.05             # 阈值是配置值而非实测值；下方循环量约为它的 5 倍
    BUSY_LOOPS = 30_000_000  # 本机沙箱内实测该循环约 0.25s，足以稳定越过 LIMIT

    @staticmethod
    def _suite(time_limit):
        return {
            "label": "演示·用例限时",
            "function_names": ["slow_sum"],
            "checkers": {"expect_three": "assert result == 3"},
            "cases": [{"name": "限时用例", "args": [[1, 2]],
                       "checker": "expect_three", "time_limit": time_limit}],
        }

    def test_slow_but_correct_solution_judged_failed(self):
        """限时命中：结果正确但超限 → 该用例 fail（不是 pass，也不是 error/timeout）"""
        ev.TEST_SUITES["_slow_case"] = self._suite(self.LIMIT)
        try:
            code = wrapped(f"""
                def slow_sum(nums):
                    for _ in range({self.BUSY_LOOPS}):
                        pass
                    return sum(nums)
            """)
            r = ev.run_code_verification(code, suite_name="_slow_case")
            self.assertFalse(r.passed)
            self.assertEqual(r.status, "fail")
            self.assertFalse(r.tests[0].passed)
            detail = r.tests[0].detail
            self.assertIn("超出用例限时", detail)   # 与"断言失败"区分开
            self.assertIn("实际耗时", detail)       # 必须报出真实耗时，便于定位
        finally:
            ev.TEST_SUITES.pop("_slow_case", None)

    def test_fast_solution_within_limit_passes(self):
        """限时未命中（护栏）：限时内完成的正确解仍判 pass，且不带任何限时抱怨"""
        ev.TEST_SUITES["_fast_case"] = self._suite(1)
        try:
            code = wrapped("""
                def slow_sum(nums):
                    for _ in range(100000):     # 约 1ms，离 1s 限时极远
                        pass
                    return sum(nums)
            """)
            r = ev.run_code_verification(code, suite_name="_fast_case")
            self.assertEqual(r.status, "pass")
            self.assertTrue(r.tests[0].passed)
            self.assertEqual(r.tests[0].detail, "")
        finally:
            ev.TEST_SUITES.pop("_fast_case", None)


class PickSuiteTest(unittest.TestCase):
    """任务 5：pick_suite 只看标题区，正文"提及式"题面不许命中。

    5 条用例对应验收脚本 T5a~T5d：1 条钉住误命中修复，3 条护栏防"修一个坏一个"，
    另加 1 条钉住"标题是别的题、正文提及两数之和"的边界。
    """

    def test_mention_only_does_not_match(self):
        """正文顺口提一句 ≠ 题面（T5a）"""
        self.assertIsNone(ev.pick_suite("这题比两数之和难很多"))

    def test_default_chinese_topic_matches(self):
        """正常中文题面仍命中（T5b 护栏）；用 chat 的真实默认题面，防两份文案漂移"""
        import chat    # 局部导入：本文件其余用例不依赖 chat，避免连带拉入 agent/openai
        self.assertEqual(ev.pick_suite(chat.DEFAULT_TOPIC), "two_sum")

    def test_english_topic_matches(self):
        """英文题面仍命中（T5c 护栏）"""
        self.assertEqual(ev.pick_suite("Two Sum - LeetCode 1"), "two_sum")

    def test_other_topics_do_not_match(self):
        """其它题目不误伤（T5d 护栏）；"三数之和"是近名题，不能靠子串蒙中"""
        self.assertIsNone(ev.pick_suite("反转字符串（LeetCode 344）"))
        self.assertIsNone(ev.pick_suite("三数之和（LeetCode 15）"))

    def test_body_mention_ignored_when_title_is_another_topic(self):
        """标题是别的题、正文提及两数之和 → 仍不命中（标题区之外一律不参与匹配）"""
        topic = ("反转字符串（LeetCode 344）\n"
                 "说明：本题比两数之和简单，two sum 的哈希做法不适用。")
        self.assertIsNone(ev.pick_suite(topic))


if __name__ == "__main__":
    unittest.main()

