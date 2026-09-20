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

    def say(self, transcript_text, extra_instruction="", max_tokens=None,
            stop_event=None, on_warning=None):
        """根据当前对话剧本，生成这个角色的下一句话。

        extra_instruction: 可选的独立任务指令（比如"输出总结"）。
                           它作为单独指示拼在提示里，不会混进 transcript 冒充对话内容。
        max_tokens: 覆盖默认 token 预算；None=按 provider 默认（角色普通发言够用，
                    需要输出长内容如代码时传更大的值，例如总结者用 config.SUMMARIZER_MAX_TOKENS）。
        stop_event: 可选 threading.Event（3.1）。置位后，本次调用会在"退避等待"期间立刻
                    收工并返回空字符串 ""（不再干等 2/4 秒）；空串代表"这次发言作废"，
                    由调用方负责丢弃，不要当成一句真实发言。
        on_warning: 可选回调 on_warning(message: str)（3.2）。Agent 自己一行都不打印，
                    把"回复被 max_tokens 截断"这类不致命、但必须让人知道的情况
                    透传给 CLI/GUI（GUI 复用既有 on_warning 事件，注册表无需改）。
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
        # 3.3 重试分级：不是所有错都值得重试——
        #   可重试：429 限流 / 超时 / 连接失败 / 5xx（服务端临时抽风，等会儿大概率能过）
        #   直接抛：其它 4xx（400 参数写错、401 Key 错、403 权限、404 模型名错）
        #           —— 这类错重试几次结果一样，白等 6 秒还拖慢 GUI
        first_error = None    # 第一个异常：最后用 raise ... from 串起来，保留第一现场
        last_error = None     # 最后一个异常：向上抛它
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
                choice = response.choices[0]
                content = (choice.message.content or "").strip()
                finish_reason = choice.finish_reason

                if content:
                    # 3.2 截断告警：有正文、但 finish_reason=length，
                    # 说明这轮是"说到一半被 max_tokens 掐断"的残缺品。
                    # 不重试（预算没变，重试大概率还是同样的长度），
                    # 但必须让上层知道——否则用户只看到一句莫名其妙的断句，无从排查。
                    if finish_reason == "length" and on_warning is not None:
                        on_warning(
                            f"{self.name} 的发言被 max_tokens={max_tokens} 截断"
                            f"（finish_reason=length），本轮内容是残缺的"
                        )
                    return content

                # content 为空：多半是"思考模型"把输出预算全花在推理上、还没写正文就被截断。
                # 首次遇到就翻倍预算重试一次（实测智谱 glm-4.7-flash 正是如此）。
                if finish_reason == "length" and attempt == 0:
                    max_tokens = max_tokens * 3
                    continue
                raise RuntimeError(
                    f"模型 {self.model} 返回了空内容（finish_reason={finish_reason}）"
                )
            except (RateLimitError, APIError, APIConnectionError, APITimeoutError) as e:
                if not self._should_retry(e):
                    raise                       # 4xx 类错误：立刻抛，不浪费时间重试
                last_error = e
                if first_error is None:
                    first_error = e
                if attempt == 2:
                    break                       # 已是最后一次，跳出循环统一往上抛
                delay = self._retry_delay(e, attempt)   # 429 优先听服务端 Retry-After
                if stop_event is not None:
                    # 3.1：退避期间"停止"必须能立刻生效——用 Event.wait 代替 time.sleep，
                    # 用户点下停止，这里马上返回 True，不用再干等 2/4 秒
                    if stop_event.wait(delay):
                        return ""               # 空串 = 本次发言作废，调用方负责丢弃
                else:
                    time.sleep(delay)           # 无人可打断时，行为与旧版完全一致
        if first_error is not None and first_error is not last_error:
            raise last_error from first_error   # 3.3：带上根因，便于排查"重试到底为哪般"
        raise last_error




    def _should_retry(self, error):
        """3.3 重试分级：判断这个异常值不值得再试一次。

        可重试：429 限流、超时、连接失败、5xx —— 都是临时性故障；
                没有 status_code 的 SDK 层异常也按可重试处理（保持旧行为，不误伤）。
        不可重试：其它 4xx —— 请求本身有问题，重试多少次都一样。
        """
        if isinstance(error, (RateLimitError, APITimeoutError, APIConnectionError)):
            return True
        status = getattr(error, "status_code", None)
        if status is None:
            return True
        return status == 429 or status >= 500

    def _retry_delay(self, error, attempt):
        """3.3 退避时长：429 优先听服务端 Retry-After，拿不到就用 2s / 4s 线性退避。"""
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            retry_after = headers.get("retry-after")
            if retry_after:
                try:
                    seconds = float(retry_after)
                except (TypeError, ValueError):
                    seconds = None          # 也可能是 HTTP 日期格式，不解析，走默认退避
                # 只认 0~60 秒的合理值：服务端给个离谱数字也不能把程序卡死
                if seconds is not None and 0 <= seconds <= 60:
                    return seconds
        return 2 * (attempt + 1)            # 默认 2s、4s（第 3 次不再等待，直接抛）




def config_persona(name):
    """从人设库里按名字取人设词，找不到就报错"""
    from personas import PERSONAS

    if name not in PERSONAS:
        raise ValueError(f"人设库里找不到角色: {name}，请先在 personas.py 里添加")
    return PERSONAS[name]
