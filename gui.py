"""MultiAgentChat（LeetCode 解题小组版）Tkinter 桌面 GUI

用可视化界面替代黑窗口：
  - 顶部粘贴 LeetCode 题目（留空用默认题"两数之和"），开始 / 停止按钮
  - 中部实时对话区：DeepSeek 与智谱按不同颜色/标签实时追加
  - 底部左侧滚动摘要日志：每轮压缩成功后显示"第 N 轮已压缩入滚动摘要（xx 字）"与摘要正文
  - 底部右侧最终总结区：结束时显示总结者「小马」的总结
  - 非阻塞：讨论在后台线程跑（chat.run_discussion + hooks 回调），
    通过 queue 把事件送回主线程，root.after 轮询刷新界面

仅使用 Python 标准库（tkinter + threading + queue），不引入第三方依赖。
启动方式：激活 .venv 后执行 python gui.py
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

import config
import chat as chat_mod
import evaluation

# 默认题目单一来源：直接复用 chat.py 的 DEFAULT_TOPIC，避免两份文案漂移
DEFAULT_TOPIC = chat_mod.DEFAULT_TOPIC
# 代码验证下拉的哨兵项：与 _resolve_verify 一一对应
AUTO_SUITE_LABEL = "自动识别"
NO_VERIFY_LABEL = "不验证"


def resolve_verify_choice(choice):
    """下拉选择 → run_discussion 的 (verify_code, verify_suite)。纯函数，便于单测。

    契约（三项 + 一条兜底）：
      「不验证」   → (False, None)：完全不跑验证，也不会产生 on_verification 事件
      「自动识别」 → (True, None)：按题面自动挑套件，拿不准则 skipped（不假红/假绿）
      套件 label   → (True, 套件 key)：锁定该套件
      未知选项     → (True, None)：兜底按自动处理（下拉项由 _suite_choices 生成，正常到不了）
    """
    if choice == NO_VERIFY_LABEL:
        return False, None
    if choice == AUTO_SUITE_LABEL:
        return True, None
    for name, suite in evaluation.TEST_SUITES.items():
        if suite["label"] == choice:
            return True, name
    return True, None


def widget_state(running):
    """讨论进行中，"参数已读取、再改也不生效"的控件该设的 state。纯函数，便于单测。

    轮数 Spinbox 与套件下拉的值在 _start 时一次性读走，讨论中再改看着生效、
    其实不生效（假可交互），所以运行期间统一置灰。
    """
    return "disabled" if running else "normal"


def no_verify_hint(verify_enabled, choice=NO_VERIFY_LABEL):
    """选「不验证」时该往最终总结区追加的灰字提示；启用了验证则返回空串。

    为什么需要：verify_code=False 不会产生 on_verification 事件，最终总结区
    只剩总结正文、验证部分一片空白，用户会以为验证功能坏了。纯函数，便于单测。
    """
    if verify_enabled:
        return ""
    return f"本次未启用代码验证（下拉选择：{choice}）\n"


# 参与者阵容 / 总结者厂商 / hooks 事件名清单：单一来源在 chat.py（DEFAULT_PARTICIPANTS /
# DEFAULT_SUMMARIZER_PROVIDER / HOOK_EVENT_NAMES），GUI 只 import 不重抄，防止双份漂移。
PARTICIPANTS = chat_mod.DEFAULT_PARTICIPANTS
SUMMARIZER_PROVIDER = chat_mod.DEFAULT_SUMMARIZER_PROVIDER
HOOK_EVENT_NAMES = chat_mod.HOOK_EVENT_NAMES


class MultiAgentGUI:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()      # 后台线程 -> UI 的事件队列
        self.worker = None               # 当前讨论线程
        self.stop_event = None           # 停止信号（threading.Event）
        # 等待提示状态：正在等某角色 API 返回时非空
        self._waiting = None             # (rnd, name) 当前正在等待的角色
        self._waiting_since = None       # time.monotonic() 开始等待的时刻
        self._last_shown_sec = -1        # 上次状态栏展示的等待秒数（避免无谓刷新）
        # 本次运行的代码验证选择快照（_start 时写入）：done 事件据此决定要不要补提示
        self._verify_enabled = True      # 默认与下拉初值「自动识别」一致
        self._verify_choice = AUTO_SUITE_LABEL
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 主线程轮询队列刷新界面（不阻塞 UI）
        self.root.after(100, self._poll_queue)

    # ---------- UI 构建 ----------
    def _build_ui(self):
        self.root.title("MultiAgentChat · LeetCode 解题小组")
        self.root.geometry("1000x760")
        self.root.minsize(860, 640)

        # 顶部：题目输入
        top = tk.LabelFrame(self.root,
                            text=" 1. 粘贴要讨论的 LeetCode 题目（留空使用默认题，见下方预填内容） ",
                            padx=8, pady=6)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.topic_text = tk.Text(top, height=4, wrap="word", font=("Microsoft YaHei", 10))
        self.topic_text.insert("1.0", DEFAULT_TOPIC)
        self.topic_text.pack(fill="x")
        ctrl = tk.Frame(top)
        ctrl.pack(fill="x", pady=(6, 0))
        self.start_btn = tk.Button(ctrl, text="开始讨论", width=12, command=self._start)
        self.start_btn.pack(side="left")
        # 文案诚实化：点「停止」只是不再进入下一轮，正在飞行中的 HTTP 请求无法中断，
        # 必须等它返回（单次请求超时 60s）才真正退出，故按钮不写宽度、由文案自适应。
        self.stop_btn = tk.Button(
            ctrl, text="停止（等待当前请求返回，最多 ~60s）",
            command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        # 讨论轮数选择器：默认值 / 范围来自 config.py 集中常量（改默认轮数只改 config.py 一处）
        tk.Label(ctrl, text="讨论轮数:", fg="#333333").pack(side="left", padx=(12, 2))
        self.rounds_var = tk.IntVar(value=config.GUI_DEFAULT_MAX_ROUNDS)
        # 存成属性：讨论期间要连同套件下拉一起置灰（这两个值在 _start 已读走）
        self.rounds_spin = tk.Spinbox(ctrl, from_=config.GUI_ROUNDS_MIN,
                                      to=config.GUI_ROUNDS_MAX,
                                      textvariable=self.rounds_var, width=3,
                                      justify="center")
        self.rounds_spin.pack(side="left")
        tk.Label(ctrl, text="代码验证:", fg="#333333").pack(side="left", padx=(12, 2))
        self.suite_var = tk.StringVar(value=AUTO_SUITE_LABEL)
        self.suite_menu = tk.OptionMenu(ctrl, self.suite_var, *self._suite_choices())
        self.suite_menu.config(width=13)
        self.suite_menu.pack(side="left")

        self.status_var = tk.StringVar(value="就绪：输入题目后点击「开始讨论」")
        tk.Label(ctrl, textvariable=self.status_var, fg="#555555").pack(side="left", padx=8)

        # 中部：实时对话区
        mid = tk.LabelFrame(self.root, text=" 2. 实时对话 ", padx=6, pady=4)
        mid.pack(fill="both", expand=True, padx=10, pady=4)
        self.chat_text = scrolledtext.ScrolledText(
            mid, wrap="word", state="disabled", font=("Microsoft YaHei", 10),
            background="#fafafa", width=40, height=16,
        )
        self.chat_text.pack(fill="both", expand=True)
        # 文本标签配色：DeepSeek 蓝 / 智谱 绿 / 系统灰 / 错误红
        self.chat_text.tag_configure("meta", foreground="#999999")
        self.chat_text.tag_configure("role_DeepSeek", foreground="#0b5394", font=("Microsoft YaHei", 10, "bold"))
        self.chat_text.tag_configure("role_智谱", foreground="#1e7a1e", font=("Microsoft YaHei", 10, "bold"))
        self.chat_text.tag_configure("role_other", foreground="#7f4f00", font=("Microsoft YaHei", 10, "bold"))
        self.chat_text.tag_configure("text", foreground="#222222")
        self.chat_text.tag_configure("error", foreground="#c00000")
        self.chat_text.tag_configure("system", foreground="#666666")

        # 底部：滚动摘要日志 + 最终总结
        bottom = tk.Frame(self.root)
        bottom.pack(fill="both", padx=10, pady=(4, 10))
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(0, weight=1)

        sum_frame = tk.LabelFrame(bottom, text=" 3. 滚动摘要日志（每轮压缩结果 + 摘要正文） ", padx=6, pady=4)
        sum_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.summary_text = scrolledtext.ScrolledText(
            sum_frame, wrap="word", state="disabled", font=("Microsoft YaHei", 9),
            background="#ffffff", width=40, height=12,
        )
        self.summary_text.pack(fill="both", expand=True)
        self.summary_text.tag_configure("meta", foreground="#555555", font=("Microsoft YaHei", 9, "bold"))
        self.summary_text.tag_configure("body", foreground="#1a1a1a")
        self.summary_text.tag_configure("warn", foreground="#c00000")

        fin_frame = tk.LabelFrame(bottom, text=" 4. 最终总结（主持人·小马） ", padx=6, pady=4)
        fin_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.final_text = scrolledtext.ScrolledText(
            fin_frame, wrap="word", state="disabled", font=("Microsoft YaHei", 10),
            background="#fffdf3", width=40, height=12,
        )
        self.final_text.pack(fill="both", expand=True)
        self.final_text.tag_configure("title", foreground="#0b5394", font=("Microsoft YaHei", 10, "bold"))
        self.final_text.tag_configure("body", foreground="#222222")
        self.final_text.tag_configure("meta", foreground="#999999")
        # 评测闭环（代码验证 ✓/✗）配色：通过与失败醒目区分
        self.final_text.tag_configure("verify_ok", foreground="#1e7a1e", font=("Microsoft YaHei", 10, "bold"))
        self.final_text.tag_configure("verify_fail", foreground="#c00000", font=("Microsoft YaHei", 10, "bold"))
        self.final_text.tag_configure("verify_detail", foreground="#8a3c00", font=("Consolas", 9))

    # ---------- 工具方法 ----------
    def _append(self, widget, text, tag=None):
        widget.config(state="normal")
        widget.insert("end", text, tag)
        widget.see("end")
        widget.config(state="disabled")

    def _append_chat_line(self, rnd, name, reply, failed, error):
        """按角色配色向对话区追加一条发言"""
        w = self.chat_text
        w.config(state="normal")
        w.insert("end", f"[第{rnd}轮] ", ("meta",))
        role_tag = f"role_{name}" if f"role_{name}" in w.tag_names() else "role_other"
        w.insert("end", f"{name}：", (role_tag,))
        if failed:
            # reply 本身已是"（…本轮发言失败，跳过）"占位句，不要再包一层括号
            w.insert("end", f"{reply}\n", ("error",))
            if error:
                w.insert("end", f"    原因：{error}\n", ("error",))
        else:
            w.insert("end", f"{reply}\n", ("text",))
        w.see("end")
        w.config(state="disabled")

    def _set_running(self, running):
        state = widget_state(running)
        self.start_btn.config(state=state)
        self.stop_btn.config(state="normal" if running else "disabled")
        self.topic_text.config(state=state)
        # 轮数 / 套件下拉也一并置灰：它们的值在 _start 时已传给后台线程，
        # 讨论中再改看着生效、其实不生效（假可交互）
        self.rounds_spin.config(state=state)
        self.suite_menu.config(state=state)

    def _suite_choices(self):
        """下拉项：自动识别 / 各套件 label / 不验证"""
        return ([AUTO_SUITE_LABEL]
                + [s["label"] for s in evaluation.TEST_SUITES.values()]
                + [NO_VERIFY_LABEL])

    def _resolve_verify(self):
        """下拉选择 → run_discussion 的 (verify_code, verify_suite)

        判定逻辑抽到模块级纯函数 resolve_verify_choice（可离线单测），此处只负责取值。
        """
        return resolve_verify_choice(self.suite_var.get())


    def _clear_outputs(self):
        for w in (self.chat_text, self.summary_text, self.final_text):
            w.config(state="normal")
            w.delete("1.0", "end")
            w.config(state="disabled")

    def _render_verification(self, msg):
        """在最终总结区渲染代码验证结论（评测闭环）：✓ 绿 / ✗ 红 / - 灰（跳过）"""
        label = msg.get("suite_label") or "代码"
        status = msg.get("status")
        error = msg.get("error") or "验证失败"
        tests = msg.get("tests") or []
        passed_n = sum(1 for t in tests if t.get("passed"))
        total = len(tests)
        sep = "─" * 42 + "\n"
        if status == "pass":
            self._append(self.final_text, sep, "meta")
            self._append(self.final_text,
                         f"[代码验证 ✓] {label}：{total}/{total} 用例通过\n", "verify_ok")
        elif status == "fail":
            self._append(self.final_text, sep, "meta")
            self._append(self.final_text,
                         f"[代码验证 ✗] {label}：通过 {passed_n}/{total}，失败 {total - passed_n} 个用例\n",
                         "verify_fail")
            for t in tests:
                if not t.get("passed"):
                    self._append(self.final_text,
                                 f"  ·「{t.get('name')}」{t.get('detail') or '断言失败'}\n",
                                 "verify_detail")
        elif status == "skipped":
            # 自动识别没匹配到套件：灰字中性提示，不当作失败。
            # 此时 suite_label 可能为空（题目已知、只是没注册套件），别硬贴"代码"前缀
            prefix = f"{msg.get('suite_label')}：" if msg.get("suite_label") else ""
            self._append(self.final_text, sep, "meta")
            self._append(self.final_text, f"[代码验证 -] {prefix}{error}\n", "meta")
        else:
            # no_code / error / timeout：直接展示原因
            self._append(self.final_text, sep, "meta")
            self._append(self.final_text, f"[代码验证 ✗] {label}：{error}\n", "verify_fail")

        if status == "skipped":
            self.status_var.set("代码验证已跳过（未匹配到用例套件）")
        else:
            self.status_var.set(f"代码验证 {'通过 ✓' if status == 'pass' else '未通过 ✗'}（评测闭环）")


    # ---------- 按钮行为 ----------
    def _start(self):
        topic = self.topic_text.get("1.0", "end").strip() or DEFAULT_TOPIC
        try:
            max_rounds = int(self.rounds_var.get())
        except (tk.TclError, ValueError):
            max_rounds = config.GUI_DEFAULT_MAX_ROUNDS
        # 兜底 clamp：边界与 Spinbox 同源（config.GUI_ROUNDS_MIN/MAX），防手输越界
        max_rounds = max(config.GUI_ROUNDS_MIN,
                         min(max_rounds, config.GUI_ROUNDS_MAX))
        self._clear_outputs()
        self._set_running(True)
        self.status_var.set(f"讨论进行中…（共 {max_rounds} 轮，可在任意发言间隙点「停止」）")
        self._append(self.chat_text, "系统：讨论开始，后台调用两家模型 API，界面实时刷新。\n", "system")
        self.stop_event = threading.Event()
        verify_code, verify_suite = self._resolve_verify()
        # 快照本次选择：选「不验证」时不产生 on_verification 事件，
        # done 事件要据此在最终总结区补一行说明（见 no_verify_hint）
        self._verify_enabled = verify_code
        self._verify_choice = self.suite_var.get()
        self.worker = threading.Thread(
            target=self._worker_run,
            args=(topic, max_rounds, verify_code, verify_suite),
            daemon=True)

        self.worker.start()

    def _stop(self):
        if self.stop_event is not None and not self.stop_event.is_set():
            self.stop_event.set()
            self.status_var.set("正在停止…（等待当前 API 调用返回后退出）")
            self._clear_waiting()
            self.stop_btn.config(state="disabled")

    def _clear_waiting(self):
        """清除等待提示状态（发言返回/摘要/总结/停止时调用）"""
        self._waiting = None
        self._waiting_since = None
        self._last_shown_sec = -1

    def _worker_run(self, topic, max_rounds, verify_code, verify_suite):
        """后台线程：跑 run_discussion，把事件经 hooks 塞进 queue"""
        hooks = {
            "on_start": lambda **kw: self.events.put({"type": "start", **kw}),
            "on_round_start": lambda **kw: self.events.put({"type": "round", **kw}),
            "on_speaker_start": lambda **kw: self.events.put({"type": "speaker_start", **kw}),
            "on_speaker": lambda **kw: self.events.put({"type": "speaker", **kw}),
            "on_summary": lambda **kw: self.events.put({"type": "summary", **kw}),
            "on_finish": lambda **kw: self.events.put({"type": "finish", **kw}),
            "on_verification": lambda **kw: self.events.put({"type": "verification", **kw}),
            "on_warning": lambda **kw: self.events.put({"type": "warning", **kw}),
        }
        try:
            # 防漂移闸门：chat.py 新增 hooks 事件而 GUI 忘注册时，启动即报错而不是静默漏事件
            missing = [ev for ev in HOOK_EVENT_NAMES if ev not in hooks]
            if missing:
                raise RuntimeError(
                    f"GUI 未注册 hooks: {missing}（chat.HOOK_EVENT_NAMES 已更新，请补注册）")
            # Key 校验失败会抛 SystemExit，捕获后展示给用户。
            # 用到的厂商集合 = 参与者 + 总结者，判定逻辑单一来源在 chat.py
            config.check_config(chat_mod.providers_in_use(PARTICIPANTS, SUMMARIZER_PROVIDER))
            chat_mod.run_discussion(
                topic=topic,
                participant_names=PARTICIPANTS,
                max_rounds=max_rounds,
                summarizer_provider=SUMMARIZER_PROVIDER,
                use_running_summary=True,
                hooks=hooks,
                stop_event=self.stop_event,
                verbose=False,   # GUI 场景静默 stdout，结果全部走 hooks
                verify_code=verify_code,
                verify_suite=verify_suite,   # None=自动 / 套件名 / 配合 verify_code=False 则关闭
            )
        except BaseException as e:   # noqa: BLE001 —— 线程内兜底，任何错误都展示到 UI
            self.events.put({"type": "fatal", "message": f"{type(e).__name__}: {e}"})
        else:
            self.events.put({"type": "done", "stopped": bool(self.stop_event and self.stop_event.is_set())})

    # ---------- 事件处理 ----------
    def _poll_queue(self):
        try:
            while True:
                msg = self.events.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        # 等待提示的秒数每秒刷新一次（不用每次都 set，避免无谓 UI 更新）
        if self._waiting is not None and self._waiting_since is not None:
            sec = int(time.monotonic() - self._waiting_since)
            if sec != self._last_shown_sec:
                self._last_shown_sec = sec
                rnd, name = self._waiting
                self.status_var.set(f"第{rnd}轮 {name} 思考中…（已等待 {sec} 秒）")
        self.root.after(100, self._poll_queue)

    def _handle(self, msg):
        mtype = msg["type"]
        if mtype == "start":
            topic = (msg.get("topic") or "")[:120]
            self._append(self.chat_text, f"系统：题目：{topic}…（完整题目见顶部输入框）\n", "system")
        elif mtype == "round":
            self._append(self.chat_text, f"———— 第 {msg['rnd']} 轮 ————\n", "meta")
        elif mtype == "speaker_start":
            # 某角色开始调用 API：状态栏显示"思考中 + 已等待秒数"，避免等待像卡死
            self._clear_waiting()
            self._waiting = (msg["rnd"], msg["name"])
            self._waiting_since = time.monotonic()
            self._last_shown_sec = -1
            self.status_var.set(f"第{msg['rnd']}轮 {msg['name']} 思考中…")
        elif mtype == "speaker":
            self._clear_waiting()
            self._append_chat_line(msg["rnd"], msg["name"], msg["reply"],
                                   msg.get("failed", False), msg.get("error"))
            if msg.get("failed"):
                self.status_var.set(f"第{msg['rnd']}轮 {msg['name']} 发言失败，已跳过（详见对话区红色提示）")
            else:
                self.status_var.set("讨论进行中…（可在任意发言间隙点「停止」）")
        elif mtype == "warning":
            self._clear_waiting()
            self._append(self.chat_text, f"警告：{msg['message']}\n", "error")
            self._append(self.summary_text, f"警告：{msg['message']}\n", "warn")
            self.status_var.set(f"警告：{msg['message']}")
        elif mtype == "summary":
            self._clear_waiting()
            body = msg["summary_text"] or ""
            self._append(self.summary_text,
                         f"【第{msg['rnd']}轮】已压缩入滚动摘要（{len(body)} 字）\n", "meta")
            self._append(self.summary_text, f"{body}\n", "body")
            self._append(self.summary_text, "─" * 42 + "\n", "meta")
            self.status_var.set(f"第{msg['rnd']}轮摘要已更新（{len(body)} 字），讨论继续…")
        elif mtype == "finish":
            self._clear_waiting()
            self._append(self.final_text, "主持人 · 总结者「小马」\n", "title")
            self._append(self.final_text, f"{msg['summary']}\n", "body")
        elif mtype == "verification":
            # 评测闭环结果：最终总结区下方渲染"代码验证 ✓/✗"
            self._clear_waiting()
            self._render_verification(msg)
        elif mtype == "fatal":
            self._clear_waiting()
            self._append(self.chat_text, f"致命错误：{msg['message']}\n", "error")
            self._set_running(False)
            self.status_var.set("出错：见对话区红色提示")
        elif mtype == "done":
            self._clear_waiting()
            self._set_running(False)
            if msg.get("stopped"):
                self._append(self.chat_text, "系统：讨论已手动停止，未生成最终总结。\n", "system")
                self.status_var.set("已停止")
            else:
                # 选「不验证」时没有 on_verification 事件，最终总结区只剩总结正文，
                # 验证区一片空白会被当成"功能坏了" —— 补一行灰字说明本次没跑验证
                hint = no_verify_hint(self._verify_enabled, self._verify_choice)
                if hint:
                    self._append(self.final_text, hint, "meta")
                self.status_var.set("讨论完成 ✓")

    def _on_close(self):
        # 后台线程是 daemon，随主进程退出即可；先尝试给个停止信号
        if self.stop_event is not None:
            self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    MultiAgentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
