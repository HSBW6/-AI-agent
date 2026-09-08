"""Agent 类：一个人设 = 一个 Agent 实例，支持接入不同模型厂商"""
import time

from openai import (
    OpenAI,
    APIError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
)

import config


class Agent:
    """带人设的对话角色，负责调用对应模型 API 生成自己的发言。

    provider 可选值：
      - "deepseek": 用 DeepSeek 的 Key / 接口
      - "zhipu": 用智谱 GLM 的 Key / 接口（config.ZHIPU_*）
    """

    PROVIDERS = ("deepseek", "zhipu")

    def __init__(self, name, provider="deepseek", temperature=None):
        if provider not in self.PROVIDERS:
            raise ValueError(
                f"不支持的 provider: {provider!r}，可选：{'、'.join(self.PROVIDERS)}"
            )
        self.name = name
        self.provider = provider
        self.persona = config_persona(name)
        # temperature 只有传 None 才用默认值；传 0（要完全确定性输出）是合法值，
        # 不能用 `temperature or 默认`，否则 0 会被吞成 0.8
        self.temperature = (
            config.DEFAULT_TEMPERATURE if temperature is None else temperature
        )

        # 按 provider 挑选自己的 API 配置
        if provider == "zhipu":
            self.api_key = config.ZHIPU_API_KEY
            self.base_url = config.ZHIPU_BASE_URL
            self.model = config.ZHIPU_MODEL
        else:  # deepseek
            self.api_key = config.API_KEY
            self.base_url = config.BASE_URL
            self.model = config.MODEL

        # 客户端延迟创建并复用：每次 say() 都新建 client 会丢掉 TCP 连接，
        # 每个请求都要重新握手/排队（智谱免费档对"新连接"的首次请求尤其慢，实测达 20s+）。
        # 一个 Agent 实例整场讨论复用同一个 client，连接 keep-alive 可显著降低后续请求延迟。
        self._client = None

    @property
    def client(self):
        if self._client is None:
            # 显式设 timeout：SDK 默认 600 秒太久，智谱免费档慢响应时请求会长时间挂起，
            # 不设的话下面的 APITimeoutError 重试分支几乎永远不会触发（光标闪烁/假死的另一半根因）。
            # max_retries=1：SDK 层只补一次，配合下面的业务层退避重试，避免叠加过多等待。
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
                max_retries=1,
            )
        return self._client

    def say(self, transcript_text, extra_instruction="", max_tokens=None):
        """根据当前对话剧本，生成这个角色的下一句话。

        extra_instruction: 可选的独立任务指令（比如"输出总结"）。
                           它作为单独指示拼在提示里，不会混进 transcript 冒充对话内容。
        max_tokens: 覆盖默认 token 预算；None=按 provider 默认（角色普通发言够用，
                    需要输出长内容如代码时传更大的值，例如总结者用 config.SUMMARIZER_MAX_TOKENS）。
        """
        if max_tokens is None:
            max_tokens = (
                config.ZHIPU_MAX_TOKENS if self.provider == "zhipu" else config.MAX_TOKENS
            )
        client = self.client

        # 组指令：有特殊任务就用任务；没有就正常接话
        if extra_instruction:
            instruction = extra_instruction.rstrip("。") + "。"
        else:
            instruction = (
                f"现在轮到你（{self.name}）发言了。请以你的身份自然地接话。\n"
                "讨论规则（重要）：先针对上一位发言者最新的观点表态，"
                "且表态必须给出依据——同意就说明你认可对方哪一步（可补充），"
                "反驳就指出对方论证里具体哪个环节站不住。"
                "认错只基于论证质量、不基于对方态度：对方论证确实更优就大方认错并采纳，"
                "不许为了维持人设硬撑到底；但对方语气再好、只要论证有漏洞，"
                "坚持正确观点不算硬撑，要指出漏洞并坚持。"
                "如果觉得对方思路有哪里不对劲但一时说不上来，"
                "可以把疑问原样抛出来，不必勉强接受。"
                "如果目前还没有其他发言者的观点（你是全场第一个发言），"
                "就直接给出你的完整观点。"
            )
        # 通用规则：只管输出正文 + 全程用中文
        instruction += (
            "只输出你要说的内容本身，不要输出你的名字，不要加引号，"
            "不要复述或重复对话记录里的内容（包括你自己之前说过的话）。"
            "你的思考过程与回答都请使用中文。"
        )

        messages = [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    "下面是大家到目前为止的对话记录（每一行是某个人说过的话）。\n"
                    "====================\n"
                    f"{transcript_text}\n"
                    "====================\n"
                    f"{instruction}"
                ),
            },
        ]

        # 限流/网络抖动/超时重试最多 3 次（实测智谱免费档经常 429 限流）
        last_error = None
        for attempt in range(3):
            try:
                request_kwargs = dict(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                )
                if self.provider == "zhipu":
                    # glm-4.7-flash 是"混合思考"模型，思考会先吃掉大量 token、
                    # 拉长响应；讨论场景直接输出正文即可，显式关闭思考
                    request_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                response = client.chat.completions.create(**request_kwargs)
                content = (response.choices[0].message.content or "").strip()
                if content:
                    return content

                # content 为空：多半是"思考模型"把输出预算全花在推理上、还没写正文就被截断。
                # 首次遇到就翻倍预算重试一次（实测智谱 glm-4.7-flash 正是如此）。
                reason = response.choices[0].finish_reason
                if reason == "length" and attempt == 0:
                    max_tokens = max_tokens * 3
                    continue
                raise RuntimeError(
                    f"模型 {self.model} 返回了空内容（finish_reason={reason}）"
                )
            except (RateLimitError, APIError, APIConnectionError, APITimeoutError) as e:
                last_error = e
                time.sleep(2 * (attempt + 1))  # 简单退避：2s、4s、6s
        raise last_error


def config_persona(name):
    """从人设库里按名字取人设词，找不到就报错"""
    from personas import PERSONAS

    if name not in PERSONAS:
        raise ValueError(f"人设库里找不到角色: {name}，请先在 personas.py 里添加")
    return PERSONAS[name]
