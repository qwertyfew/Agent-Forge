"""任务现场（TaskSession）：不可变事件日志（Event Log）+ 上下文视图（Surface）。

参照 DeepSeek Harness 的三段式设计（Header / Event Log / Surface）：

- **Event Log**：JSONL 追加写（``.sessions/<sid>/events.jsonl``）。事件一旦落盘
  不改、不删、不重排——本任务发生过什么的**唯一权威来源**。员工自述可以撒谎
  （rerun12 实证）、leader 转述可以走样（rerun13「假设已存在」实证），日志不会。
- **Surface**：从内存事件列表按需重建的「上下文视图」。员工 prompt 注入的
  「任务现场」块、澄清自动答复，都从这里取材——蒸馏可以错，日志不会错。
- **Inbox**（Follow-up / Steering / Inject 三通道）属于后续阶段，见
  ``docs/harness-redesign-20261019.md``；本模块先落 Event Log + Surface。

解决的具体问题（rerun13 实证）：leader 双环路径里员工上下文 = 空对话 + 自己的
goal 文本，下游员工对前面员工的产出一无所知；步骤失败后，下游员工拿着 leader
的「假设已存在」去找文件，找不到就弹窗问用户。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

# 从自然语言里扒项目相对路径的 token（evals/foo.json、agentforge/loop.py …）。
# 要求至少一段「目录/名字.后缀」，避免把普通英文单词误当路径。
_PATH_RX = re.compile(
    r"[A-Za-z0-9_\-\u4e00-\u9fff]+(?:[/\\][A-Za-z0-9_\-\u4e00-\u9fff.]+)+"
    r"\.[A-Za-z0-9]{1,5}")

# leader 批内依赖的语言信号：出现这些说法而 leader 又没声明 depends_on 时，
# 程序强制把该批降级为串行——「假设已存在」式规划不能再引发并行竞态。
_DEPEND_WORDS = ("上一步", "前面步骤", "之前步骤", "已生成", "已存在",
                 "已完成的分析", "上一步的结果", "前面 agent")


def extract_paths(text: str) -> list[str]:
    """从文本里提取疑似项目路径的 token（去重、保序）。"""
    seen: list[str] = []
    for m in _PATH_RX.findall(text or ""):
        if m not in seen:
            seen.append(m)
    return seen


# 契约判定的句子级信号：路径所在分句出现**写入动词**才算「要求写出的交付」；
# 出现**对冲词**（上游欠账 / 条件句）则不算——「读取上一步生成的 evals/x.json」
# 是在等别人的产出，不该拦本步骤收工。
_WRITE_V_RX = re.compile(r"写入|写出|写成|写到|更新|改写|重写|保存到|保存至|存为|落盘|输出到|交付到|写(?![在进])")
_HEDGE_RX = re.compile(r"上一步|上游|前面步骤|之前步骤|已有实现|如果|若|已存在|存在则|跳过|可选")


def contract_paths(goal: str, root: str | Path) -> list[str]:
    """任务书点名要求**写出**的交付文件（写入动词判定）。

    无论磁盘上是否已存在都算契约项：不存在 = 必须写出；已存在 = 必须本轮
    更新（mtime 变化）——rerun15 实测的盲点：上一轮任务留下的交付物糊弄了
    「存在即完成」，收尾闸门的 stale 核查只能事后抓。已存在的路径若出现在
    读取语境（参考/读取/基于）不会进契约——写入动词判定已排除。只认工作区
    内的相对路径，逃出工作区 / 解析失败的一律跳过（不猜、不误报）。
    """
    rootp = Path(root)
    req: list[str] = []
    # 分句（分号/句号/换行）后做句子级判定：写入动词 + 无对冲词才算交付要求，
    # 避免「参考 agentforge/loop.py」这类读材料路径被误当成要写出的东西。
    for sent in re.split(r"[；;\n。]+", goal or ""):
        if _HEDGE_RX.search(sent):
            continue
        for p in extract_paths(sent):
            if p in req:
                continue
            # 写入动词必须在路径**之前**——「写入 tmp/x.txt」是交付要求；
            # 「参考 agentforge/loop.py 写总结」的动词在材料路径后面，不算。
            head = sent[:sent.find(p)]
            if not _WRITE_V_RX.search(head):
                continue
            try:
                target = (rootp / p).resolve()
                target.relative_to(rootp.resolve())
            except (OSError, ValueError):
                continue
            req.append(p)
    return req


def contract_block(goal: str, root: str | Path) -> str:
    """开工时亮出的「验收契约」块：做之前就知道做对的标准长什么样。
    与 loop 的停止拦截构成契约两端——开头声明、收工程序对账。"""
    req = contract_paths(goal, root)
    if not req:
        return ""
    return (
        "\n【验收契约】任务书要求本轮写出以下文件，收工前程序会核对磁盘"
        "（不存在或本轮未更新都不放行，会拦截并要求当场补写）：\n- "
        + "\n- ".join(req)
        + "\n分析完成后先用 write_file 把内容落盘（路径一字不差），再给最终答复。\n")


class TaskSession:
    """一个任务一份。append-only 日志 + 按需重建的 Surface。"""

    def __init__(self, sid: str, root: str | Path) -> None:
        self.sid = sid
        self.root = Path(root)
        base = self.root / ".sessions" / sid
        if base.exists():                     # 同任务重跑：另开目录，绝不混流
            base = self.root / ".sessions" / f"{sid}.{time.strftime('%H%M%S')}"
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "events.jsonl"
        self.events: list[dict] = []          # Surface 的数据源（与日志同序）

    # ---------------- Event Log：唯一写入入口 ----------------

    def append(self, etype: str, **fields) -> dict:
        ev = {"seq": len(self.events) + 1,
              "ts": datetime.now().isoformat(timespec="seconds"),
              "type": etype, **fields}
        self.events.append(ev)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    # ---------------- 步骤级便捷事件 ----------------

    def step_start(self, index: int, task_id: str, title: str,
                   goal: str, model: str = "") -> None:
        self.append("step_start", index=index, task_id=task_id,
                    title=title, goal=(goal or "")[:600], model=model)

    def step_end(self, index: int, title: str, summary: str,
                 files: list[str], failed: bool) -> None:
        self.append("step_end", index=index, title=title,
                    summary=(summary or "")[:400], files=list(files or []),
                    failed=bool(failed))

    # ---------------- Surface：上下文视图（按需重建） ----------------

    def steps(self) -> list[dict]:
        return [e for e in self.events if e["type"] == "step_end"]

    def artifacts(self) -> dict[str, bool]:
        """全任务产出登记表：path -> 磁盘上现在是否真的存在。"""
        out: dict[str, bool] = {}
        for e in self.events:
            for p in e.get("files") or []:
                if p not in out:
                    try:
                        out[p] = (self.root / p).exists()
                    except OSError:
                        out[p] = False
        return out

    def worker_brief(self, limit: int = 12) -> str:
        """注入员工 prompt 的「任务现场」块。没有历史步骤时返回空串。"""
        done = self.steps()
        if not done:
            return ""
        rows = []
        for e in done[-limit:]:
            st = "失败" if e.get("failed") else "完成"
            files = "、".join(e.get("files") or []) or "（无产出文件）"
            rows.append(f"  {e['index']}. {e['title']} —— {st}\n"
                        f"     摘要：{(e.get('summary') or '（无）')[:300]}\n"
                        f"     产出：{files}")
        arts = self.artifacts()
        exist = [p for p, ok in arts.items() if ok]
        miss = [p for p, ok in arts.items() if not ok]
        exist_s = "、".join(exist[-10:]) or "（无）"
        miss_s = "、".join(miss[-6:]) if miss else "（无）"
        return (
            "\n【任务现场】（系统维护的本任务事实记录，来自事件日志，非模型自述）\n"
            "已完成步骤：\n" + "\n".join(rows) +
            f"\n产出文件核验（磁盘实查）：存在：{exist_s}｜缺失：{miss_s}"
            "\n【硬规则】上面「存在」清单之外的文件一律当作不存在。若本步骤需要"
            "读取的输入文件不在清单里，说明负责生成它的上游步骤未完成或失败——"
            "请基于现有材料继续完成本步骤，并在回复里如实说明缺了什么；"
            "不要假设文件存在，也不要向用户追问该文件的下落。\n")

    # ---------------- 澄清自动答复：把「问用户」拦在系统层 ----------------

    def auto_answer(self, question: str) -> str | None:
        """员工想向用户追问文件下落时，若该文件是本任务自己的产物/预期产物，
        系统直接作答，不劳烦用户。认不出就返回 None（真问用户）。"""
        for p in extract_paths(question):
            arts = self.artifacts()
            if p in arts:
                if arts[p]:
                    return (f"[系统自动答复] 文件 {p} 已由本任务前面的步骤生成，"
                            "直接 read_file 读取即可，无需向用户确认。")
                return (f"[系统自动答复] 文件 {p} 是本任务的预期产物，"
                        "但负责生成它的步骤失败或文件已不存在。请勿等待或追问该"
                        "文件：基于现有材料继续，并在回复中如实说明它缺失。")
            owner = self._owner_step(p)
            if owner:
                return (f"[系统自动答复] 文件 {p} 由本任务第 {owner} 步负责生成，"
                        "该步尚未执行到产出。请勿等待或追问该文件：基于现有材料"
                        "继续，并在回复中如实说明它尚不存在。")
        return None

    def _owner_step(self, path: str) -> int:
        """该路径最早被哪个步骤的 goal 提到（1-based 步序；找不到回 0）。"""
        for e in self.events:
            if e["type"] == "step_start" and path in (e.get("goal") or ""):
                return e.get("index") or 0
        return 0


# ---------------- leader 批内依赖的程序级侦测 ----------------

def batch_dependency_reason(batch) -> str | None:
    """一批任务里若存在程序可证的依赖信号，返回串行化理由；否则 None。

    两条判据（命中任一即串行）：
    1. 路径共享：两个步骤的 goal 提到同一条文件路径——很可能一个是产出方、
       一个是消费方（rerun13 的 l4/l5 就这样，leader 却让它们并行）；
    2. 依赖措辞：goal 里出现「上一步/已生成/假设已存在」类说法。
    """
    batch = list(batch)
    if len(batch) < 2:
        return None
    owner: dict[str, object] = {}
    for t in batch:
        for p in extract_paths(getattr(t, "goal", "") or ""):
            if p in owner and owner[p] is not t:
                return (f"第{getattr(owner[p], 'index', '?')}步与"
                        f"第{getattr(t, 'index', '?')}步都涉及 {p}，可能存在"
                        "产出→消费依赖")
            owner[p] = t
    for t in batch:
        goal = getattr(t, "goal", "") or ""
        if any(w in goal for w in _DEPEND_WORDS):
            return f"第{getattr(t, 'index', '?')}步的描述含上游依赖措辞"
    return None
