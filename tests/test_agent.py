"""Agent 重试策略（3.3）单测：只验分级与退避时长，不发真实请求、不读 Key"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
