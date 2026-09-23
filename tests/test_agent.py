"""Agent 重试策略（3.3）单测：只验分级与退避时长，不发真实请求、不读 Key"""
import unittest
from unittest.mock import patch      # 新增：用于 patch 掉 config_persona，构造真 Agent 
from agent import Agent


class _FakeAPIError(Exception):
    """伪造 SDK 异常：只带 status_code，够 _should_retry / _retry_delay 判断用"""

    def __init__(self, status_code=None, retry_after=None):
        super().__init__(f"fake error status={status_code}")
        self.status_code = status_code
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"retry-after": retry_after}})()


class RetryPolicyTest(unittest.TestCase):
    def setUp(self):
        # 只测纯逻辑方法，绕开 __init__（避免读 config 里的 Key）
        self.agent = Agent.__new__(Agent)

    def test_retry_429_and_5xx(self):
        for code in (429, 500, 502, 503):
            self.assertTrue(self.agent._should_retry(_FakeAPIError(code)), code)

    def test_no_retry_4xx(self):
        for code in (400, 401, 403, 404):
            self.assertFalse(self.agent._should_retry(_FakeAPIError(code)), code)

    def test_status_none_is_retryable(self):
        # 连接类异常没有 status_code，按可重试处理，不误伤旧行为
        self.assertTrue(self.agent._should_retry(_FakeAPIError(None)))

    def test_retry_after_respected(self):
        self.assertEqual(self.agent._retry_delay(_FakeAPIError(429, "7"), 0), 7)

    def test_insane_retry_after_falls_back(self):
        # 服务端给 9999 秒这种离谱值不能采纳，否则等于卡死
        self.assertEqual(self.agent._retry_delay(_FakeAPIError(429, "9999"), 0), 2)

    def test_default_backoff_steps(self):
        self.assertEqual(self.agent._retry_delay(_FakeAPIError(429), 0), 2)
        self.assertEqual(self.agent._retry_delay(_FakeAPIError(429), 1), 4)
class _FakeCompletions:
    """假 completions：按脚本顺序吐预置响应，并记录每次调用的实参供断言"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []          # 每次 create(**kwargs) 的实参

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("测试脚本响应不够：产品代码比预期多调了一次 API")
        return self._responses.pop(0)

def _fake_client(completions):
    """拼一个最小 client：只要 .chat.completions.create 存在即可，不碰真实 SDK"""
    return type("C", (), {"chat": type("Chat", (), {"completions": completions})()})()

def _fake_response(content, finish_reason="stop"):
    """拼一个最小响应：say() 只读 choices[0].message.content 与 finish_reason"""
    message = type("Msg", (), {"content": content})()
    choice = type("Ch", (), {"message": message, "finish_reason": finish_reason})()
    return type("Resp", (), {"choices": [choice]})()

def _bare_agent(temperature=None):
    """用 __new__ 造"只有 say 用得到的属性"的 Agent，绕开 __init__（不读 Key、不读人设库）"""
    a = Agent.__new__(Agent)
    a.name = "测试者"
    a.provider = "deepseek"                     # 走非智谱分支，不注入 extra_body
    a.model = "fake-model"
    a.temperature = 0.8 if temperature is None else temperature
    a.persona = "你是测试角色"
    a._client = None                            # 按需塞假 client
    return a

class SayTruncationTest(unittest.TestCase):
    """5.2 剩余路径：截断告警 / 空回复扩容 / 扩容只给一次机会"""

    def test_nonempty_truncated_warns_and_does_not_retry(self):
        """有正文但被截断：必须告警一次，且不重试（预算没变，重试也是同样长度）"""
        comp = _FakeCompletions(
            [_fake_response("说到一半的内容", finish_reason="length")]
        )
        agent = _bare_agent()
        agent._client = _fake_client(comp)

        warnings = []
        # 显式传小 max_tokens，便于断言告警文案里的数字
        out = agent.say("对话记录", max_tokens=100, on_warning=warnings.append)

        self.assertEqual(out, "说到一半的内容")   # 残缺内容照常返回，不丢
        self.assertEqual(len(comp.calls), 1)      # 关键：只调一次，没有重试
        self.assertEqual(len(warnings), 1)        # 关键：必须且只告警一次
        self.assertIn("截断", warnings[0])
        self.assertIn("100", warnings[0])

    def test_empty_length_response_expands_max_tokens(self):
        """空回复 + finish_reason=length：预算 ×3 后重试一次并成功"""
        comp = _FakeCompletions([
            _fake_response("", finish_reason="length"),             # 第一次：思考吃光预算，没正文
            _fake_response("扩容后的正文", finish_reason="stop"),     # 第二次：拿到正文
        ])
        agent = _bare_agent()
        agent._client = _fake_client(comp)

        out = agent.say("对话记录", max_tokens=100)

        self.assertEqual(out, "扩容后的正文")
        self.assertEqual(len(comp.calls), 2)
        self.assertEqual(comp.calls[0]["max_tokens"], 100)   # 首次用原预算
        self.assertEqual(comp.calls[1]["max_tokens"], 300)   # 第二次是 ×3，不是 ×2、也不是叠加翻倍

    def test_empty_length_twice_raises(self):
        """连续两次空回复：扩容只给一次机会，第二次直接抛错，不能无限重试"""
        comp = _FakeCompletions([
            _fake_response("", finish_reason="length"),
            _fake_response("", finish_reason="length"),
        ])
        agent = _bare_agent()
        agent._client = _fake_client(comp)

        with self.assertRaises(RuntimeError):
            agent.say("对话记录", max_tokens=100)

class TemperatureTest(unittest.TestCase):
    """temperature=0 是合法值（要完全确定性输出），不能被 `or 默认` 吞成 0.8"""

    def test_zero_temperature_survives_init(self):
        # __init__ 会调 config_persona 读人设库，patch 掉它：不依赖 personas.py 内容，也不读 Key
        with patch("agent.config_persona", return_value="你是测试角色"):
            agent = Agent("测试者", temperature=0)
        self.assertEqual(agent.temperature, 0)      # 若产品代码写成 `temperature or 默认`，这里会是 0.8

    def test_zero_temperature_is_sent_to_api(self):
        comp = _FakeCompletions([_fake_response("正文")])
        agent = _bare_agent(temperature=0)
        agent._client = _fake_client(comp)

        agent.say("对话记录", max_tokens=50)

        self.assertEqual(comp.calls[0]["temperature"], 0)   # 真发出去的也是 0，不是被兜底顶掉


if __name__ == "__main__":
    unittest.main()
