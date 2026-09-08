"""离线测试：不联网、不依赖 API Key。

运行方式：
    python -m unittest discover -s tests -v
或
    python tests/test_chat.py -v

设计：把 chat.Agent 替换成 FakeAgent（可配置抛错），覆盖：
  - run_discussion 的 hooks 事件序列 / 末轮不压缩 / 停止路径 / 失败降级
  - transcript_text 预算与行裁剪 / update_summary 保头（P1-1）
  - clean_reply 清洗逻辑
"""
import contextlib
import io
import threading
import unittest

import chat


class FakeAgent:
    """不联网的假 Agent；fail=True 时 say 抛错模拟 API 故障。

    记录每次 say 收到的 (角色名, transcript) 到类级 history，
    供测试断言"某角色（如记录员）到底看到了什么输入"。
    """
    last_input = None
    history = []

    def __init__(self, name, provider="deepseek"):
        self.name = name
        self.provider = provider
        self.fail = False

    def say(self, transcript_text, extra_instruction="", max_tokens=None):
        FakeAgent.last_input = transcript_text
        FakeAgent.history.append((self.name, transcript_text))
        if self.fail:
            raise RuntimeError("模拟 API 故障")
        return f"我是{self.name}的发言"


def use_fake_agents(testcase, fail_names=()):
    """把 chat.Agent 换成假工厂；测试结束自动还原"""
    original = chat.Agent
    FakeAgent.history = []   # 每个用例从干净历史开始

    def factory(name, provider="deepseek"):
        agent = FakeAgent(name, provider)
        if name in fail_names:
            agent.fail = True
        return agent

    chat.Agent = factory
    testcase.addCleanup(lambda: setattr(chat, "Agent", original))


PARTICIPANTS = [("A", "deepseek"), ("B", "zhipu")]
HOOK_KEYS = ["on_start", "on_round_start", "on_speaker_start",
             "on_speaker", "on_summary", "on_finish", "on_warning"]


class EventRecorder:
    """记录 hooks 调用序列"""

    def __init__(self):
        self.events = []

    def hook(self, name):
        def f(**kw):
            self.events.append((name, kw))
        return f

    def all_hooks(self):
        return {k: self.hook(k) for k in HOOK_KEYS}

    def types(self):
        return [t for t, _ in self.events]


class RunDiscussionTest(unittest.TestCase):
    def setUp(self):
        use_fake_agents(self)

    def test_hooks_full_sequence_two_rounds(self):
        rec = EventRecorder()
        result = chat.run_discussion(
            topic="题", participant_names=PARTICIPANTS, max_rounds=2,
            use_running_summary=True, verbose=False, hooks=rec.all_hooks())
        self.assertEqual(result, "我是总结者的发言")
        expected = [
            "on_start", "on_round_start",
            "on_speaker_start", "on_speaker",
            "on_speaker_start", "on_speaker",
            "on_summary",                 # 第 1 轮末压缩
            "on_round_start",
            "on_speaker_start", "on_speaker",
            "on_speaker_start", "on_speaker",
            "on_finish",
        ]
        self.assertEqual(rec.types(), expected)

    def test_no_summary_after_last_round(self):
        rec = EventRecorder()
        chat.run_discussion(topic="题", participant_names=PARTICIPANTS,
                            max_rounds=2, use_running_summary=True,
                            verbose=False, hooks=rec.all_hooks())
        n = sum(1 for t, _ in rec.events if t == "on_summary")
        self.assertEqual(n, 1)  # 只有第 1 轮压缩（P1-3：末轮不压缩）

    def test_classic_mode_has_no_summary_events(self):
        rec = EventRecorder()
        chat.run_discussion(topic="题", participant_names=PARTICIPANTS,
                            max_rounds=2, use_running_summary=False,
                            verbose=False, hooks=rec.all_hooks())
        self.assertNotIn("on_summary", rec.types())
        self.assertIn("on_finish", rec.types())

    def test_stop_event_returns_none_and_no_finish(self):
        stop_event = threading.Event()
        rec = EventRecorder()
        hooks = rec.all_hooks()

        def on_speaker(**kw):
            if kw.get("rnd") == 1 and kw.get("name") == "B":
                stop_event.set()
        hooks["on_speaker"] = on_speaker
        result = chat.run_discussion(
            topic="题", participant_names=PARTICIPANTS, max_rounds=5,
            use_running_summary=True, verbose=False, hooks=hooks,
            stop_event=stop_event)
        self.assertIsNone(result)
        self.assertNotIn("on_finish", rec.types())

    def test_verbose_false_no_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.run_discussion(topic="题", participant_names=PARTICIPANTS,
                                max_rounds=1, use_running_summary=False,
                                verbose=False, hooks=None)
        self.assertEqual(buf.getvalue(), "")

    def test_failed_speaker_skips_and_keeps_summary_clean(self):
        use_fake_agents(self, fail_names={"B"})
        rec = EventRecorder()
        chat.run_discussion(topic="题", participant_names=PARTICIPANTS,
                            max_rounds=2, use_running_summary=True,
                            verbose=False, hooks=rec.all_hooks())
        failed = [kw for t, kw in rec.events
                  if t == "on_speaker" and kw.get("failed")]
        self.assertEqual(len(failed), 2)          # B 每轮都失败但不中断
        # 失败占位句只在 participants 的上下文里可见（保持过程完整），
        # 但绝不混进"记录员"（滚动摘要压缩）的输入
        recorder_inputs = [t for n, t in FakeAgent.history if n == "记录员"]
        self.assertTrue(recorder_inputs)
        for text in recorder_inputs:
            self.assertNotIn("本轮发言失败", text)
        # 参与者上下文里应能看到占位行（本轮 B 失败后 A 的后续上下文）
        self.assertIn("本轮发言失败", FakeAgent.last_input or "")


class TranscriptTextTest(unittest.TestCase):
    def test_keeps_recent_lines_drops_oldest(self):
        # 行长做大到总量必然超预算（4000 字符），验证"从尾部保留完整行"
        lines = ["话题：x" + "旧" * 200] + \
                [f"角色{i}：" + "行" * 200 for i in range(1, 30)]
        out = chat.transcript_text(None, lines)
        self.assertLessEqual(len(out), 4000)
        self.assertIn("角色29：行", out)    # 最新行保留（每行内容为"行"×200）
        self.assertNotIn("角色1：行", out)  # 最旧行被裁
        self.assertNotIn("话题：x", out)

    def test_keeps_topic_when_total_fits(self):
        out = chat.transcript_text(None, ["话题：x", "A：短发言"])
        self.assertIn("话题：x", out)

    def test_overlong_summary_keeps_head_only(self):
        out = chat.transcript_text("话" * 4100, ["A：内容"])
        self.assertEqual(len(out), 4000)
        self.assertTrue(out.startswith("话"))

    def test_summary_plus_recent_within_budget(self):
        out = chat.transcript_text("摘" * 200, ["A：" + "长" * 100, "B：" + "短" * 100])
        self.assertLessEqual(len(out), 4000)
        self.assertTrue(out.startswith("摘"))
        self.assertIn("短", out)   # 最近行仍在


class UpdateSummaryTest(unittest.TestCase):
    def test_overlong_old_summary_kept_at_head(self):
        """P1-1：输入超长时旧摘要（含话题/历史）不被 transcript_text 裁掉"""
        recorder = FakeAgent("记录员")
        old = "【旧摘要】" + "记" * 3000
        new_lines = ["角色A：" + "说" * 600, "角色B：" + "话" * 600]
        chat.update_summary(recorder, "话题", old, new_lines, 1, 500, verbose=False)
        self.assertTrue(FakeAgent.last_input.startswith("【旧摘要】"))
        self.assertLessEqual(len(FakeAgent.last_input), 4000)

    def test_short_path_keeps_old_and_new(self):
        recorder = FakeAgent("记录员")
        chat.update_summary(recorder, "话题", "旧摘要短", ["A：新发言"], 1, 500,
                            verbose=False)
        self.assertIn("旧摘要短", FakeAgent.last_input)
        self.assertIn("A：新发言", FakeAgent.last_input)

    def test_no_old_summary_starts_with_new_lines(self):
        recorder = FakeAgent("记录员")
        chat.update_summary(recorder, "话题", None, ["A：首轮发言"], 1, 500,
                            verbose=False)
        self.assertTrue(FakeAgent.last_input.startswith("A：首轮发言"))


class CleanReplyTest(unittest.TestCase):
    def test_strips_name_prefix(self):
        self.assertEqual(chat.clean_reply("A：你好", "A"), "你好")
        self.assertEqual(chat.clean_reply("A: 你好", "A"), "你好")
        self.assertEqual(chat.clean_reply("A说：你好", "A"), "你好")
        self.assertEqual(chat.clean_reply("A说:你好", "A"), "你好")

    def test_strips_quotes(self):
        self.assertEqual(chat.clean_reply("“你好”", "A"), "你好")
        self.assertEqual(chat.clean_reply('"你好"', "A"), "你好")

    def test_empty_safe(self):
        self.assertEqual(chat.clean_reply(None, "A"), "")
        self.assertEqual(chat.clean_reply("", "A"), "")


if __name__ == "__main__":
    unittest.main()
