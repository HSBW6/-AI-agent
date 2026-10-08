"""讨论状态快照：让 run_discussion 在进程崩溃/重启后能从断点续聊。

设计取舍：
  - 单文件（默认项目根 discussion_snapshot.json）：一场讨论一个现场，实现与排查都简单；
    批量实验这类"必须干净"的场景由调用方显式传 resume=False 关掉（见 experiments/runner.py）。
  - 原子写：先写同目录 .tmp 再 os.replace（同卷才原子），进程被杀不会留下半截 JSON。
  - 读失败一律返回 None：文件不存在、JSON 损坏、字段缺失/类型不对、版本不符
    都当作"没有快照"；损坏文件顺手删掉，避免每次启动都在同一个坏文件上反复报错。
  - SNAPSHOT_FILE 是模块级常量：测试可 patch 到临时目录，不污染项目根。
"""
import json
import os
import time
from pathlib import Path

SNAPSHOT_VERSION = 1

# 默认落盘位置：与 chat.py 同目录（项目根）。测试用 patch 覆盖此常量做隔离。
SNAPSHOT_FILE = Path(__file__).resolve().parent / "discussion_snapshot.json"


def _tmp_path():
    """临时文件与目标同目录：os.replace 同卷才是原子的（跨卷会退化成复制+删除）。"""
    f = SNAPSHOT_FILE
    return f.with_name(f.name + ".tmp")


def save_snapshot(rnd, topic, participant_names, max_rounds,
                  summary_text, recent_lines, saved_at=None):
    """每轮检查点：把讨论现场序列化落盘（UTF-8 JSON，ensure_ascii=False, indent=2）。

    参数与字段一一对应（version 固定为 SNAPSHOT_VERSION）：
      rnd               已完成的轮次（1 起；0 表示一轮都没完成）
      topic             话题（恢复时用来判定是不是同一场讨论）
      participant_names 参与者 [(角色名, provider), ...]（同上，参与判定）
      max_rounds        总轮数
      summary_text      滚动摘要 = 跨轮记忆（可能为 None）
      recent_lines      最近原始对话行（轮末压缩成功后会清空）
      saved_at          落盘时间；缺省取当前时间，测试可固定传入

    返回 True=已落盘 / False=写失败。快照是"尽力而为"：磁盘问题只吞掉返回 False，
    绝不抛异常拖垮正在进行的讨论。
    """
    payload = {
        "version": SNAPSHOT_VERSION,
        "rnd": int(rnd),
        "topic": topic,
        "participant_names": [[name, prov] for name, prov in participant_names],
        "max_rounds": int(max_rounds),
        "summary_text": summary_text,
        "recent_lines": list(recent_lines),
        "saved_at": saved_at or time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        tmp = _tmp_path()
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, SNAPSHOT_FILE)   # 原子替换：读者要么看到旧现场，要么看到新现场
        return True
    except OSError:
        return False


def load_snapshot():
    """读回上一次检查点。

    以下情况一律返回 None（并顺手删掉坏文件，避免反复报错）：
    文件不存在、读不动、JSON 损坏、顶层不是对象、version 不符、必需字段缺失或类型不对。
    读成功后 participant_names 归一化成元组列表，与 save_snapshot 的入参形状一致，
    调用方可以拿它直接和本次的 participant_names 比较。
    """
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):        # ValueError 含 json.JSONDecodeError
        clear_snapshot()
        return None

    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        clear_snapshot()
        return None
    try:
        names = data["participant_names"]
        if not isinstance(names, list):
            raise TypeError("participant_names 不是列表")
        pairs = []
        for p in names:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise TypeError("participant_names 元素不是二元组")
            pairs.append((str(p[0]), str(p[1])))
        recent = data["recent_lines"]
        if not isinstance(recent, list):
            raise TypeError("recent_lines 不是列表")
        summary = data["summary_text"]
        if summary is not None and not isinstance(summary, str):
            raise TypeError("summary_text 不是字符串")
        return {
            "version": SNAPSHOT_VERSION,
            "rnd": int(data["rnd"]),
            "topic": str(data["topic"]),
            "participant_names": pairs,
            "max_rounds": int(data["max_rounds"]),
            "summary_text": summary,
            "recent_lines": [str(line) for line in recent],
            "saved_at": str(data["saved_at"]),
        }
    except (KeyError, TypeError, ValueError):
        clear_snapshot()
        return None


def snapshot_matches(snap, topic, participant_names):
    """这份快照是不是"这一场"讨论的现场：题目与参与者名单都要对得上。

    对不上说明是陈旧残留（上次换了题/换了阵容就崩了），调用方应当丢弃它另起新讨论，
    否则会把上一题的上下文喂给这一题（GUI 换题后点开始最容易踩）。
    max_rounds 不参与判定：轮数是"跑到哪算哪"，本次调用少传几轮不该让已完成的现场作废。
    """
    if snap.get("topic") != topic:
        return False
    saved = [tuple(p) for p in snap.get("participant_names") or []]
    return saved == [tuple(p) for p in participant_names]


def clear_snapshot():
    """删除快照（连同可能残留的 .tmp）。文件不存在也算成功，绝不抛异常。"""
    for path in (SNAPSHOT_FILE, _tmp_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
