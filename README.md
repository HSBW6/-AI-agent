# MultiAgentChat · AI 解题小组

> 让**两个不同厂商的大模型**扮成两个人设角色互相讲题、抬杠、找茬，最后由主持人收尾给出完整可运行代码，
> 再把这段代码丢进**受限沙箱真跑一遍题目用例**，用 ✓ / ✗ 说话。
>
> CLI + 桌面 GUI 双入口 · 纯 Python 标准库 GUI · 91 项离线单测

---

## 实验结果：多智能体讨论到底值不值？

**结论：在这个任务上，让模型「看着报错自己改一遍」比让两个模型「互相讨论」更划算。**

30 道算法题（easy / medium / hard = 9 / 12 / 9）× 4 种配置，共 120 次运行；
代码由**沙箱真实跑测试用例判分**——通过率是客观的，不是人工打分：

| 配置 | 通过率 | 平均耗时 | 平均尝试 |
|---|---|---|---|
| 单模型直答 | 93.3% (28/30) | **2.2s** | 1.00 |
| **单模型自纠**（验证失败 → 错误回喂 → 重答） | **100% (30/30)** | **2.3s** | 1.10 |
| 同质讨论（DeepSeek × 2） | 86.7% (26/30) | 15.3s | 1.00 |
| 异构讨论（DeepSeek + GLM） | 96.7% (29/30) | 92.9s | 1.00 |

**四个发现**

1. **自纠完胜**：把沙箱的失败信息回喂给模型，通过率 93.3% → **100%**，而耗时几乎不变（2.2s → 2.3s）；30 题里只有 3 题需要重试，全部修对。
2. **同质讨论不但没用，还有害**：配对对比显示它**救回 0 题、弄坏 2 题**（净收益 **−2**），同时慢 7 倍。
   这独立复现了 [*The Cost of Consensus*](https://dl.acm.org/doi/full/10.1145/3786335.3813137)（ACM）的结论：**无引导的同质多智能体讨论，不如孤立自纠**。
3. **讨论在简单题上是有害的**：按难度分层，同质讨论把 easy 从 9/9 拉到 7/9；而自纠把 hard 从 7/9 拉到 9/9。
   ——**该开讨论的是难题，不是所有题。**
4. **异构优于同质但极贵**：96.7% vs 86.7%，代价是 92.9s vs 15.3s——**慢 42 倍换 3 个百分点**。

**为什么？** 自纠拿到的是**确定性的真值反馈**（哪个用例、期望什么、实际什么）；讨论拿到的是**另一个模型的意见**——它可能也是错的，而且没有人验证它。

**局限（请勿过度解读）**：30 题样本、配对差异仅 2 题，方向明确但统计功效弱；`temperature=0.8`、单次运行、未做重复实验；结论仅适用于算法解题类任务。

**复现方式**

```bash
python experiments/runner.py --mode single_self_correct --provider deepseek --limit 30 --out experiments/results/r.jsonl
python experiments/report.py experiments/results/r.jsonl
```

---

## 它是什么

一个多 Agent 协作的**实验性小项目**：不做"一个模型独角戏"，而是让异构模型互相制衡。

| 角色 | 模型 | 人设 | 职责 |
|---|---|---|---|
| DeepSeek | `deepseek-chat` | 傲娇鲸鱼公主 + 算法大神 | 递进式讲题、给最优解与复杂度 |
| 智谱 | `glm-4.7-flash` | 腹黑绿茶 + 抬杠面试官 | 把找茬包装成关心：边界 case、复杂度、最优性 |
| 小马（总结者） | 可配置 | 中立主持人 | 归纳共识/分歧，输出完整可运行代码 |
| 记录员 | 可配置 | 中立记录 | 每轮把对话压缩成滚动摘要 |

流程：`讨论（多轮）→ 滚动摘要 → 总结者输出代码 → 沙箱跑用例 → ✓ / ✗`

---

## 亮点

- **带对照实验的工程结论，而不是"我做了个多智能体"**：用 30 道题 × 4 种配置，在沙箱客观判分下测出"这个任务上讨论不如自纠"（见上方实验结果）。实验层（`runner.py` / `report.py` / `selfcheck_engine.py`）可复现、可扩展，且**没有改动产品代码一行**。
- **异构多模型协作**：同一场讨论里接两家厂商（DeepSeek + 智谱 GLM），`Agent` 层抹平差异——智谱是"混合思考"模型，已按实测结论显式关闭思考（`extra_body={"thinking": {"type": "disabled"}}`），否则思考 token 会吃光预算导致**空回复**。
- **滚动摘要控制上下文成本**：每轮结束把"旧摘要 + 本轮发言"压成一条新摘要（末轮不压），上下文不随轮数线性膨胀；摘要超长时也会给原始发言留足预算。
- **评测闭环，让代码不再自嗨**：解析总结里的 ` ```python ` 代码块 → 受限命名空间 `exec` → 独立子进程执行 → 逐用例断言。结果分 `pass / fail / no_code / error / timeout / skipped` 六态。
- **不假红的取舍**：题目→用例套件是**启发式识别**，识别不了就 `skipped`（中性灰），绝不硬套一个套件把好代码判成 ✗；同一题族的语义变体（如「两数之和 II」要求 1-based 下标）也会主动跳过。
- **91 项离线单测**：用 `FakeAgent` 顶替真模型，**不联网、不读 Key、不花额度**，实测约 3 秒跑完（测试文件里其余用例走真实子进程，唯 taskkill 失败路径用 mock 定向替身——那条路径在普通机器上无法稳定构造）。
- **一堆踩坑修出来的工程细节**：客户端复用（keep-alive）、可中断退避（点停止立刻响应）、重试分级（4xx 不重试 / 429 读 `Retry-After`）、截断告警、超时**进程树**击杀、沙箱子进程只给最小环境变量。

---

## 快速开始

**环境**：Python 3.9+（开发与实测版本 3.11.8）

```bash
# 1. 依赖（只有两个）
pip install -r requirements.txt

# 2. 配置 Key：项目根新建 .env
DEEPSEEK_API_KEY=sk-xxxxxxxx        # https://platform.deepseek.com
ZHIPU_API_KEY=xxxxxxxx.xxxxxxxx     # https://open.bigmodel.cn
```

> 启动时只校验**本次实际用到**的厂商 Key——只填一家也能跑（换掉阵容即可）。

```bash
# 3. 跑起来（三种方式）
python chat.py                       # 命令行：粘贴 LeetCode 题目，直接回车用默认题
python gui.py                        # 桌面 GUI：实时对话区 + 滚动摘要日志 + 最终总结区
启动解题小组GUI.bat                   # Windows 一键启动（pythonw 无黑框）
```

CLI 默认讨论 3 轮，GUI 默认 5 轮（1~10 可选）；GUI 还能选「代码验证」策略：自动识别 / 指定套件 / 不验证。

**跑对照实验**（`experiments/` 是独立实验层，通过运行时注入套件工作，不改产品代码一行）：

```bash
python experiments/runner.py --mode single              --provider deepseek --limit 30 --out experiments/results/r.jsonl
python experiments/runner.py --mode single_self_correct --provider deepseek --limit 30 --out experiments/results/r.jsonl
python experiments/runner.py --mode homogeneous         --provider deepseek --limit 30 --out experiments/results/r.jsonl
python experiments/runner.py --mode heterogeneous       --limit 30 --rounds 3 --delay 15 --out experiments/results/r.jsonl
python experiments/report.py experiments/results/r.jsonl    # 出统计表（含配对对比）
python experiments/selfcheck_engine.py                      # 集成自检：30 题参考实现过真实评测链路（不花钱）
```

输出格式示意（实际发言由模型生成）：

```text
============================================================
话题: 题目：两数之和（LeetCode 1）……
参与者: DeepSeek、智谱
RunningSummary: 开
============================================================

----- 第 1 轮 -----
DeepSeek：哼，这题不是有手就行？……
智谱：思路不错呢～可是呀，要是数组里全是负数，你还能跑吗？……

[摘要] 第 1 轮已压缩入滚动摘要（约 480 字）
----- 总结 -----
总结者：1. 双方共识…… 2. 分歧点……

----- 代码验证（评测闭环） -----
[代码验证 ✓] 两数之和（LeetCode 1）：6/6 用例通过（命中函数 two_sum）
```

---

## 目录结构

```
agent.py          Agent 封装：人设 + 厂商适配 + 重试/退避/截断告警
chat.py           讨论主循环、滚动摘要、预算分配、总结与评测触发（hooks 事件源）
personas.py       人设词库（改人设只动这个文件）
config.py         全局配置与 Key 校验（常量单一来源）
evaluation.py     评测引擎：代码块提取 → 受限沙箱 → 用例断言 → 结果渲染
gui.py            Tkinter GUI（后台线程 + queue + hooks，界面不阻塞）
tests/            91 项离线单测（test_chat / test_evaluation / test_agent / test_gui）
experiments/      对照实验层（与产品代码解耦，运行时注入套件）：
  runner.py           四配置执行器（单模型 / 自纠 / 同质讨论 / 异构讨论）
  report.py           统计报告（总览 / 难度分层 / 失败分类 / 配对对比）
  selfcheck_engine.py 集成自检：参考实现过真实评测链路（不花钱）
  validate_dataset.py 数据集验收器（格式 + 沙箱跑通 + 期望值自洽）
  dataset_raw*.json   30 题题库（期望值经人工核对）
  results/            实验产出（JSONL，逐题增量落盘）
```

---

## 扩展新题目

**两条路**：产品层（`chat.py` / GUI 用）在 `evaluation.py` 的 `TEST_SUITES` 里注册 key；**批量题目**走 `experiments/` 的数据集（`dataset_raw*.json`）——`runner.py` 运行时把题目转成套件注入，**不需要改产品代码**，并由 `validate_dataset.py`（格式与期望值自洽）+ `selfcheck_engine.py`（过真实评测链路）双重把关。

产品层注册方式：

```python
"reverse_string": {
    "label": "反转字符串（LeetCode 344）",
    "function_names": ["reverse_string", "reverseString"],
    "class_names": ["Solution"],
    "checkers": {"expect": "assert result == 'olleh', f'期望 olleh，实际 {result!r}'"},
    "cases": [{"name": "基本", "args": ["hello"], "checker": "expect"}],
},
```

**契约（最容易踩的一点）**：`args` 只放**函数实参**（会被 `fn(*args)` 展开），期望值写进 `checker` 断言——断言里可引用 `result` 与 `args`。用例还支持可选的 `time_limit`（秒）：断言全过但单例超时同样判失败，用来筛掉结果对但复杂度过高的解法。

---

## 测试

```bash
python -m unittest discover -s tests      # 91 项，离线，约 3 秒
```

---

## 设计取舍（想了解"为什么这么写"可以看）

- **沙箱是"防意外破坏"级，不是强安全沙箱**：受限 builtins（72 项白名单）+ 独立子进程 + 15s 超时 + 进程树击杀 + 最小环境变量。**请勿用它执行不可信的高危代码**。
- **性能门槛按本机标定取几何中点**：`time_limit` 比较的是单例内部耗时，太贴近"低效解的真实耗时"会只剩约 10% 余量、判定随机器负载翻转（实测同一份代码既被判 fail 又被判 pass）。
- **规模越小越稳**：限时是事后判定、无法中途打断被测函数，所以规模越大跑得越久、沙箱压力越大（12000 规模时实测约 8% 概率触发子进程异常）。
- **宁可 skipped，不要假红**：识别不确定时跳过验证，比给用户一个错误的 ✗ 更有价值。

## 已知限制

- **主程序内置套件只有「两数之和」**：`chat.py` / GUI 的自动识别拿不准时会走 `skipped`；实验层（`experiments/`）另有 **30 题题库**，通过运行时注入 `TEST_SUITES` 使用（见上方「实验结果」），**不改产品代码**。
- 题目→套件是关键词启发式识别；`1. Two Sum`、`两数之和`、`Two Sum` 等常见写法都能认出，但会被「两数之和 II」这类语义变体主动排除。
- 讨论质量依赖人设提示词与模型本身。**实测免费档（智谱）存在账号级速率限制**：连续调用会返回 `429 / code 1302`，单题耗时从 2s 恶化到 46s（退避重试堆积），`debate` 模式因此必须限速或改用付费档（见 `experiments/runner.py --delay`）。

---

*个人学习/实验项目，AI 生成的讨论内容仅供思路参考。*
