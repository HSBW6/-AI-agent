"""Multi-Agent 互聊主程序：不同模型的两个角色轮流发言 + 总结收尾 + 代码评测闭环"""
import config
from evaluation import (
    ensure_utf8_stdio,          # 新增：统一父子进程的 stdout/stderr 为 UTF-8
    format_verification,
    run_code_verification,
    pick_suite,
)

from agent import Agent

# 默认题目（单一来源：chat.py 与 gui.py 共用，避免两份文案各自漂移）
DEFAULT_TOPIC = (
    "题目：两数之和（LeetCode 1）。给定一个整数数组 nums 和一个整数目标值 target，"
    "请你在该数组中找出和为目标值 target 的那两个整数，并返回它们的数组下标。"
    "请只讨论思路与复杂度，先不要写完整代码（最后主持人小马会统一给出可运行代码）。"
)

# 默认参与者阵容（单一来源：chat.py __main__ 与 gui.py 共用）。
# 角色名必须命中 personas.py 的人设键（agent.config_persona 按名字取人设），
# 要换参赛角色/厂商只改这一处。
DEFAULT_PARTICIPANTS = [
    ("DeepSeek", "deepseek"),      # 傲娇鱼 + 算法大神
    ("智谱", "zhipu"),             # 腹黑绿茶 + 抬杠面试官
]
DEFAULT_SUMMARIZER_PROVIDER = "deepseek"   # 总结者用的厂商；改这里即可（可换 "zhipu"）

# hooks 事件名完整清单（单一来源：run_discussion 文档与 gui.py 注册均以此为准）
HOOK_EVENT_NAMES = (
    "on_start",
    "on_round_start",
    "on_speaker_start",
    "on_speaker",
    "on_summary",
    "on_finish",
    "on_warning",
    "on_verification",
)


def providers_in_use(participants, summarizer_provider):
    """计算本次运行实际用到的厂商集合（给 config.check_config 校验 Key）"""
    return {prov for _, prov in participants} | {summarizer_provider}


def run_discussion(
    topic, participant_names, max_rounds=config.DEFAULT_MAX_ROUNDS,
    summarizer_provider=DEFAULT_SUMMARIZER_PROVIDER,
    use_running_summary=True, summary_max_len=500,
    hooks=None, stop_event=None, verbose=True,
    verify_code=False, verify_suite=None,
):
    """让参与者围绕话题轮流发言，最后总结者收尾。

    参数说明：
      topic                话题描述（str）
      participant_names    参与者列表，每项是 (角色名, provider)
      max_rounds           每人发言几轮，默认 config.DEFAULT_MAX_ROUNDS（CLI=3）
      summarizer_provider  总结者/摘要者用的厂商，默认 DEFAULT_SUMMARIZER_PROVIDER
                           （chat.py 模块常量，"deepseek"，可改 "zhipu"）
      use_running_summary  是否启用 RunningSummary（滚动摘要）：
                           True  = 每轮结束后把"旧摘要+本轮发言"压缩成新摘要，
                                   下一轮参与者只看 摘要+最近对话
                                   （上下文不随轮数膨胀，但每轮多花 1 次 API，见坑①）
                           False = 经典模式：每次都喂全部历史（对照实验用）
      summary_max_len      滚动摘要目标长度（字符）。把它控制小，
                           摘要就永远远小于 MAX_TRANSCRIPT_LEN，不会触发兜底裁剪
      hooks               可选回调 dict（供 GUI 逐步取结果），支持的事件键：
                           HOOK_EVENT_NAMES（chat.py 模块级单一来源）——
                           on_start(topic, participant_names)
                           on_round_start(rnd)
                           on_speaker_start(rnd, name)   # 某角色开始调 API（等待提示用）
                           on_speaker(rnd, name, reply, failed, error)
                           on_summary(rnd, summary_text)
                           on_finish(summary)
                           on_warning(message)
                           on_verification(passed, status, tests, error, code, suite_label)
                                 # 评测闭环结果（仅 verify_code=True 时在 on_finish 后触发）
                           任一键缺省即不回调；回调抛异常不影响讨论流程。
      stop_event          可选 threading.Event：置位后在下一个检查点停止讨论
      verbose             True=照常打印全部过程输出（CLI 默认，与旧版完全一致）；
                          False=静默，仅通过 hooks 回调取结果（GUI 场景）
      verify_code         True=总结完成后把总结里的 ```python 代码块放进受限沙箱
                          执行并跑题目用例断言（评测闭环），结果经
                          on_verification 回调 + verbose 打印，不影响返回值；
                          False=不做验证（默认，保证旧行为/旧事件序列不变）
      verify_suite        题目用例套件名（evaluation.TEST_SUITES 的 key）；
                          默认 None = 自动模式：按题面调 evaluation.pick_suite()
                          自动挑套件，挑不准则 skipped（不猜、不假红，属启发式
                          识别而非精确判定）；显式传套件名则钉死该套件——
                          不传默认值的调用方因此不会再被迫落到 "two_sum"
                          （旧默认值曾导致"非两数之和题被 fake red"）；
                          新题目在 evaluation.py 扩展
    """
    # GUI 复用接口（见 docstring hooks）：事件回调 + 输出开关
    def _out(*args, **kwargs):
        if verbose:
            print(*args, **kwargs)

    def _emit(event, **payload):
        if hooks and callable(hooks.get(event)):
            try:
                hooks[event](**payload)
            except Exception as e:   # GUI 回调异常不拖垮讨论流程
                _out(f"[警告] GUI 回调 {event} 异常：{e}")

    # 1. 开场：话题行是整场讨论的锚点（坑②：摘要绝不能吞掉话题）
    #    第 1 轮它以原始行在场；从第 1 轮压缩起，话题由摘要指令强制保留在摘要开头
    opening_line = f"话题：{topic}\n请大家围绕这个话题开始讨论。"
    _out("=" * 60)
    _out("话题:", topic)
    _out("参与者:", "、".join(name for name, _ in participant_names))
    _out("RunningSummary:", "开" if use_running_summary else "关")
    _out("=" * 60)
    _emit("on_start", topic=topic, participant_names=participant_names)

    # 2. 建 Agent：参与者 + 总结者 + 记录员（默认用 DeepSeek）
    agents = [Agent(name, provider=prov) for name, prov in participant_names]
    # 复用 Agent 类就自动带上厂商特化逻辑（坑③：智谱关思考 extra_body 自动生效）
    summarizer = Agent("总结者", provider=summarizer_provider)
    # 记录员只做滚动摘要压缩，与"总结者"人设分离（P1-2）：
    # 主持人小马的"最后总结/分条/≤200字"约束不适合当记录员
    recorder = Agent("记录员", provider=summarizer_provider) if use_running_summary else None

    # 3. RunningSummary 状态
    summary_text = None             # 滚动摘要；None = 第一轮还没有摘要
    # "还没进摘要"的原始行。注意：每轮压缩成功后会被清空，
    # 话题锚点从第 2 轮起只存在于摘要开头（由 update_summary 指令强制保留）
    recent_lines = [opening_line]

    # 4. 主循环：每轮每人发言一次（GUI 场景可通过 stop_event 在检查点停止）
    stopped = False
    for rnd in range(1, max_rounds + 1):
        if stop_event is not None and stop_event.is_set():
            stopped = True
            break
        _out(f"\n----- 第 {rnd} 轮 -----")
        _emit("on_round_start", rnd=rnd)
        round_lines = []  # 收集本轮原始发言，轮末用于更新摘要
        for agent in agents:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            # 4.1 组装本轮上下文：摘要 + 最近对话
            #     参与者的 prompt 会看到"摘要+最近对话"，让模型知道自己在读摘要
            context = transcript_text(summary_text, recent_lines)
            failed = False
            error = None
            # 通知 UI"即将调用 XX 的 API"：GUI 可借此显示"思考中…"等待提示
            # （智谱免费档服务端波动大，实测单次可等 10~20s，无提示会像卡死）
            _emit("on_speaker_start", rnd=rnd, name=agent.name)
            try:
                reply = agent.say(
                    context,
                    # 3.1：把停止信号传进 Agent，退避等待才可被打断
                    stop_event=stop_event,
                    # 3.2：把 Agent 的截断告警接到既有 on_warning 事件上
                    #（事件名早就注册好了，GUI 侧一行都不用改）
                    on_warning=lambda message: _emit("on_warning", message=message),
                )
            except Exception as e:

                # 单个角色失败（限流/网络等）不拖垮整场讨论：记录后继续
                error = f"{type(e).__name__}: {e}"
                _out(f"[警告] {agent.name} 发言失败：{error}")
                reply = f"（{agent.name} 本轮发言失败，跳过）"
                failed = True
            # 3.1：若"停止"是在这次发言的退避等待里按下的，say() 会返回空串，
            # 这里直接收工——不把这句空回复当成一次真实发言记进 transcript
            if not reply and stop_event is not None and stop_event.is_set():
                stopped = True
                break
            reply = clean_reply(reply, agent.name)

            if not reply:
                reply = "……"
            line = f"{agent.name}：{reply}"
            # 先发言者的本轮内容立刻进 recent_lines，
            # 同轮后发言者也能看到（保持原来 transcript 的语义）
            recent_lines.append(line)
            if not failed:
                round_lines.append(line)   # 失败占位句不压进摘要，避免污染记忆
            _out(line)
            _out()
            _emit("on_speaker", rnd=rnd, name=agent.name, reply=reply,
                  failed=failed, error=error)
        if stopped:
            break

        # 4.2 每轮末尾：把"旧摘要 + 本轮发言"压缩成一条新滚动摘要
        #     （坑①：这是额外一次 API 调用，每轮 +1 成本，换取上下文不再线性膨胀）
        #     最后一轮不压缩（P1-3）：省一次调用，且最终总结能读到最后一轮原文
        if use_running_summary and rnd < max_rounds:
            new_summary = update_summary(
                recorder, topic, summary_text, round_lines,
                rnd, summary_max_len, verbose,
                stop_event=stop_event,   # 3.1：轮末压缩往往要等 10~20s，也必须能被打断
            )

            if new_summary is not None:
                # 摘要更新成功：本轮原文并入摘要，清空 recent_lines
                summary_text = new_summary
                recent_lines = []
                _out(f"[摘要] 第 {rnd} 轮已压缩入滚动摘要（{len(new_summary)} 字）")
                _emit("on_summary", rnd=rnd, summary_text=new_summary)
            else:
                _emit("on_warning", message=f"第 {rnd} 轮滚动摘要更新失败，沿用旧摘要")

    if stopped:
        _out("\n[已停止] 讨论已被手动停止")
        return None

    # 5. 总结收尾：基于"最终摘要 + 最后最近对话"做最终总结
    _out("----- 总结 -----")
    try:
        summary = summarizer.say(
            transcript_text(summary_text, recent_lines),
            extra_instruction=(
                "讨论到此结束，请作为主持人输出最终总结："
                "先用分条列表简要总结讨论要点，"
                "再根据上面的讨论给出这道题的具体可运行代码"
                "（Python，用代码块输出，代码要完整、含注释、可直接运行）。"
            ),
            max_tokens=config.SUMMARIZER_MAX_TOKENS,
        )
    except Exception as e:
        _out(f"[警告] 总结失败：{type(e).__name__}: {e}")
        _emit("on_warning", message=f"总结失败：{type(e).__name__}: {e}")
        summary = "（总结失败，请查看上方警告信息）"
    _out(f"总结者：{summary}")
    _emit("on_finish", summary=summary)

    # 6.（评测闭环 · 交接文档 §10.3-1）总结者输出的代码不再"自嗨"：
    #    解析 ```python 代码块 → 受限沙箱子进程执行 → 跑题目用例断言 →
    #    CLI/GUI 展示"代码验证 ✓/✗"。默认关闭，仅 CLI/GUI 入口显式开启，
    #    不改变 run_discussion 默认事件序列与返回值契约（str）。
    if verify_code:
        # verify_suite=None → 自动模式：按题面挑套件，拿不准则 skipped（不猜、不假红）
        suite_name = verify_suite if verify_suite is not None else pick_suite(topic)
        try:
            verification = run_code_verification(summary, suite_name=suite_name)
            _out("----- 代码验证（评测闭环） -----")
            _out(format_verification(verification))
            _emit(
                "on_verification",
                passed=verification.passed,
                status=verification.status,
                tests=[
                    {"name": t.name, "passed": t.passed, "detail": t.detail}
                    for t in verification.tests
                ],
                error=verification.error,
                code=verification.code,
                suite_label=verification.suite_label,
            )
        except Exception as e:   # 评测失败绝不能拖垮已经完成的讨论/总结
            _out(f"[警告] 代码验证过程异常：{type(e).__name__}: {e}")
            _emit("on_warning", message=f"代码验证过程异常：{type(e).__name__}: {e}")
    return summary


def update_summary(recorder, topic, old_summary, new_lines, rnd, max_len, verbose=True,
                   stop_event=None):
    """把【旧摘要】+【本轮新增发言】合并成一条更短的滚动摘要。

    设计取舍（坑①）：这是每轮额外的一次 API 调用，轮数越多成本越高；
    收益是喂给参与者的上下文不会随轮数线性增长，长讨论可控。
    失败返回 None，调用方沿用旧摘要继续跑，不致命。

    组装顺序（P1-1）：预算内【先保旧摘要】（含话题与历史），
    只对"本轮新发言"做行裁剪。绝不能把旧摘要混进 transcript_text 一起裁——
    它从尾部保留、丢最旧，输入超长时先被丢掉的恰恰是最该保住的旧摘要（已复现）。
    """
    head = f"【旧摘要】{old_summary}\n" if old_summary else ""
    body = transcript_text(None, list(new_lines))
    budget = config.MAX_TRANSCRIPT_LEN - len(head)
    if budget <= 0:
        body = ""
    elif len(body) > budget:
        body = body[-budget:]  # 新发言裁到剩余预算（兜底硬切）

    instruction = (
        "请把【旧摘要】与【本轮新增对话】合并成一份新的滚动摘要，"
        "给下一轮参与者阅读。硬性要求：\n"
        f"1. 开头必须保留完整话题：{topic}\n"   # 坑②：话题是锚点，不许吞
        "2. 保留：双方最新核心观点、关键分歧、已达成的共识、尚未解决的争议；\n"
        "3. 出现新约定（如暗号、代号、专属名词）时，原样保留、不许改写；\n"
        f"4. 总长度控制在 {max_len} 字以内，宁缺毋滥；\n"
        "5. 只输出摘要正文，不要寒暄、不要评价、不要出现名字前缀。"
    )
    try:
        # 3.1：被"停止"打断时 say() 返回空串，这里必须转成 None——
        # 否则调用方会误判成"压缩成功"，把滚动摘要清成空串、还顺手丢掉了本轮原话
        text = recorder.say(
            head + body, extra_instruction=instruction, stop_event=stop_event
        ).strip()
        return text or None

    except Exception as e:
        if verbose:
            print(f"[警告] 第 {rnd} 轮滚动摘要更新失败：{type(e).__name__}: {e}，沿用旧摘要")
        return None



def transcript_text(summary, recent_lines):
    """把 (滚动摘要, 最近原始行) 拼成喂给模型的上下文。

    拼接顺序：摘要在前（跨轮记忆），最近对话在后（本轮现场）。
    预算分配（3.5 + 任务3）：摘要最多占 MAX_TRANSCRIPT_LEN 的 60%，而 40% 是
    recent_lines（按"完整行"从尾部保留）的"上限"而非"定额"——摘要没吃满的余量
    必须还给原话。以前摘要"不占预算"，一旦记录员不听话把摘要写长，就会把原话挤到
    一行不剩；3.5 补了 60% 上限，但 recent_budget 被写死成 max_len - 60%
    （4000 → 1600），摘要为空或只有 100 字时照样饿着原话（实测 summary=None 只输出
    1441 字符、100 字摘要只有 1542 字符，而预算本可到 4000）。现在两边都保底：
    摘要把原话挤不没，原话也把跨轮记忆挤不没。
    保留老逻辑语义：不能从行中间切开喂模型（实测复现残句）。
    """
    max_len = config.MAX_TRANSCRIPT_LEN
    # 3.5 预算切分：摘要最多吃 60%（上限，不是定额）。
    # 以前 recent_budget 写死成 max_len - summary_budget，摘要再短也不还余量，
    # 结果"摘要没吃满却在饿着原话"（GUI 5 轮实测会丢最早 1~2 条发言）。
    summary_budget = int(max_len * 0.6)

    parts = []
    # 摘要区：正常远小于预算，直接全留；真超了从尾部砍
    #（话题锚点固定躺在摘要开头，所以砍尾巴、保脑袋）
    if summary:
        kept_summary = summary[:summary_budget]
        parts.append(kept_summary)
        # 摘要没吃满 → 余量归原话（-1 是摘要与原话之间的那个换行）
        recent_budget = max_len - len(kept_summary) - 1
    else:
        recent_budget = max_len

    # 最近对话区：只占用属于它的那份预算，从尾部保留完整行
    kept = []   # 倒序收集被保留的行
    total = 0
    for line in reversed(recent_lines):
        cost = len(line) + 1  # +1 算换行符
        if total + cost > recent_budget:
            if kept:
                break              # 再加就超预算了，丢掉更旧的行
            # 单条就超长（极端情况）：硬切这一行兜底，不然它一行都挤不进来
            kept.append(line[:max(recent_budget - total, 0)])
            total = recent_budget
            break
        kept.append(line)
        total += cost
    return "\n".join(parts + list(reversed(kept)))




def clean_reply(reply, name):
    """模型偶尔会把"名字："前缀或引号一起输出，剥掉，避免出现"XX：XX：xxx"双重前缀"""
    reply = (reply or "").strip()
    for prefix in (f"{name}：", f"{name}:", f"{name}说：", f"{name}说:"):
        if reply.startswith(prefix):
            reply = reply[len(prefix):].lstrip()
            break
    if reply and reply[0] in "“\"'" and reply[-1] in "”\"'":
        reply = reply.strip("“”\"'").strip()
    return reply

if __name__ == "__main__":
    # 先切 UTF-8：CLI 在 GBK 控制台/重定向到文件时，中文与 ✓ 符号才不会崩
    ensure_utf8_stdio()

    # 【解题小组模式·融合版】傲娇鱼学霸 vs 绿茶面试官，总结者小马收尾。
    # 参与者阵容 / 总结者厂商 / 轮数默认值均收敛为模块级单一来源
    # （DEFAULT_PARTICIPANTS / DEFAULT_SUMMARIZER_PROVIDER / config.DEFAULT_MAX_ROUNDS），
    # 人设已在 personas.py 里融合了讲题/抬杠职能。
    participants = DEFAULT_PARTICIPANTS
    summarizer_provider = DEFAULT_SUMMARIZER_PROVIDER

    # 启动前只校验实际用到的厂商 Key（参与者 + 总结者），
    # 只填了一家 Key 也能跑，不再强制两家都填
    config.check_config(providers_in_use(participants, summarizer_provider))

    # 让用户粘贴要讨论的 LeetCode 题目：直接回车则回退到默认题（DEFAULT_TOPIC）
    # 没有交互终端（管道/重定向/自动化运行）时 input() 会抛 EOFError，回退默认题不崩
    try:
        topic = input("粘贴要讨论的 LeetCode 题目（直接回车使用默认题）：").strip()
    except EOFError:
        topic = ""
    if not topic:
        topic = DEFAULT_TOPIC

    run_discussion(
        topic=topic,
        participant_names=participants,
        summarizer_provider=summarizer_provider,
        use_running_summary=True,  # True=滚动摘要(省token但每轮+1次API) / False=全量历史(对照)
        verify_code=True,
        verify_suite=None,         # None=按题目自动挑套件，拿不准则跳过验证
        # 评测闭环：总结后沙箱执行代码并跑题目用例断言
    )
