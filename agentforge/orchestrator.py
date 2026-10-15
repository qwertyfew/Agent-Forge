"""多模型编排：拆解任务 → 分配模型 → 串行执行，全过程产出可观测事件。

和单模型 Agent.run 的区别：
    Agent.run     一个模型从头干到尾
    run_task      先规划，把不同性质的步骤交给池子里最擅长的模型，逐步接力

每一步都是一次完整的 Agent 循环（可调用工具、可写文件），上一步的产出留在
对话历史里，下一步直接在此基础上继续——所以接力不会丢上下文。

事件流（前端按需渲染）：
    combo     当前启用组合与各维度得分
    plan      拆解出的子任务清单
    assign    第 i/n 步分配给哪个模型、为什么
    subtask   非首步的步骤指令（前端显示为中间步骤）
    delta / thinking / tool_start / tool_result / stats / error / assistant
    complete  全部步骤结束
"""

from __future__ import annotations

import queue
import re
import threading
from typing import Any, Iterator

from .dispatcher import Dispatcher, SharedState, TaskSpec
from .loop import Agent
from .planner import (

    KIND_DIM, KIND_LABEL, Subtask, kind_of, looks_like_change, make_plan,
    needs_plan, plan_summary,
)
from .readcache import ReadCache
from . import runcontrol
from . import sources as sources_gate
from . import verify as verify_gate

# 首步与后续步骤的提示词模板。都强调"只做这一步"，否则模型会越界把后面的活也干了。
STEP_FIRST = """【总体任务】
{message}

【本次只执行第 1/{total} 步】{title}
这一步的要求：{detail}

请只完成这一步，不要越界去做后面的步骤。做完后用一两句话说明本步结果。
"""

STEP_NEXT = """【本次执行第 {index}/{total} 步】{title}
这一步的要求：{detail}

前面步骤的产出已经在对话历史里，请在此基础上继续，不要重复已完成的操作。
做完后用一两句话说明本步结果。
"""


# 多步接力时发给下一步的「进度简报」。全量历史接力会让第 N 步背上前面每一步
# 的工具输出原文（一次 read_file 就可能上万字符），而多数步骤其实只需要知道
# 「做过什么、产出在哪」。真正要细节时模型自己会去 read_file。
PROGRESS_TEMPLATE = """【总体任务】
{message}

【本次执行第 {index}/{total} 步】{title}
这一步的要求：{detail}

【前面的进度】
{done}

【提示】前面步骤的完整工具输出已省略以节省上下文（上面的「产出文件」是它们的
落脚点）。需要细节时直接 read_file / list_dir 去读文件，不要凭空猜测。
请只完成本步，不要重复已完成的操作；做完后用一两句话说明本步结果。
"""


def _progress_lines(done: list[dict], limit: int = 12) -> str:
    if not done:
        return "（这是第一步）"
    rows = []
    for d in done[-limit:]:
        line = f"  {d['index']}. {d['title']} —— {d['summary'] or '（已完成）'}"
        if d.get("files"):
            line += "\n     产出文件：" + "、".join(d["files"][:8])
        rows.append(line)
    return "\n".join(rows)


def _last_assistant(messages: list[dict]) -> str:
    """从历史里回捞最后一条助手正文，作为该步的摘要兜底。"""
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
            return m["content"].strip()
    return ""


def window_notice(entries: list[dict]) -> dict | None:
    """池子里窗口最大与最小差 ≥4 倍时给个提醒。

    多模型接力时，整条链路的可用上下文 = 参与模型里**最小的那个窗口**
    （历史逐步变长，一路接力），所以混用 128K 与 8K 的模型，前者完全用不上。
    """
    wins = [(e["name"], e.get("window") or 0) for e in entries if e.get("window")]
    if len(wins) < 2:
        return None
    lo_name, lo = min(wins, key=lambda x: x[1])
    hi_name, hi = max(wins, key=lambda x: x[1])
    if lo <= 0 or hi / lo < 4:
        return None
    return {
        "min_name": lo_name, "min": lo,
        "max_name": hi_name, "max": hi,
        "ratio": round(hi / lo, 1),
    }


def _entries_of(pool) -> list[dict]:
    return [
        {"id": e.id, "key": e.key, "name": e.display, "scores": e.scores,
         "total": e.total, "window": getattr(e, "context_window", 0) or 0}
        for e in pool
    ]


def _short(text: str, limit: int = 26) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _single_task(message: str, entries: list[dict]) -> Subtask:
    """不需要拆解时，按整体任务性质挑一个模型。"""
    kind = kind_of(message)
    dim = KIND_DIM[kind]
    best = max(entries, key=lambda e: (e["scores"].get(dim, 0), e["total"]))
    return Subtask(
        index=1,
        title=_short(message),
        detail=message,
        kind=kind,
        model_key=best["key"],
        model_name=best["name"],
        model_id=best["id"],
        score=int(best["scores"].get(dim, 0)),
        reason=f"{KIND_LABEL.get(kind, kind)}维度最强（{best['scores'].get(dim, 0)} 分）",
    )


# ---------------------------------------------------------------- 多 agent 编排

# 交给某个 agent 的任务提示。强调「只做你这一个任务」，并附上前面 agent 的进度简报。
AGENT_TASK_TEMPLATE = """【总任务】
{message}

【你负责第 {index}/{total} 个任务】{title}
任务要求：{goal}
所需能力：{capability}
交接说明：{handoff}

【前面 agent 的进度】
{brief}
{bridged}
请只完成你负责的这个任务，不要越界去做别的任务。上面「进度」里是前面 agent 的结果摘要
与产出文件；需要细节就直接 read_file 去读那些文件，不要凭空猜测。
复用优先：项目里已有的工具、脚本、函数先用 find_tools / search_code 查证，已有能力不要重复实现。
完成后用一两句话说明本步结果。
"""

# 翻译桥接时附加的说明：让弱中文模型放心用英文干活。
# 「产出文件仍用中文」是四测踩坑后补的：桥接模型把任务书译成英文照做，
# 连报告文件都写成英文，长期记忆里「报告一律简体中文」的偏好被桥接覆盖——
# 用户要的是「交付物中文」，英文只该是它的工作语言。
BRIDGE_NOTE = """
【注意】你不太擅长处理中文，本任务的输入已由调度器翻译成英文，请用英文完成；
你的产出会被调度器译回中文交给用户。
【例外——文件产物语言】写进文件的交付物（报告、文档、注释等）必须用简体中文撰写，
文件内容不会被翻译，译回中文的只是你的总结。"""

BRIDGE_CN_THRESHOLD = 70      # 中文维度低于此分的模型才做翻译桥接


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _mostly_chinese(text: str, ratio: float = 0.15) -> bool:
    s = text or ""
    if not s:
        return False
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(len(s), 1) >= ratio


def _worth_dispatch(message: str, pool_size: int) -> bool:
    """是否值得动用决策模型拆解：包含多个动作，或请求本身较长。"""
    return needs_plan(message, pool_size) or len((message or "").strip()) >= 60


# 停机拦截的连续上限（Claude Code Stop hook 同款机制）：验证失败最多拦这么多轮，
# 每轮结束重新亲跑验证。Claude Code 默认 8、社区实践常用 4；本地评测批量跑，
# 每轮拦截都是完整的 agent 循环（分钟级），取 2 兼顾纠错机会与跑批墙钟。
_VERIFY_BLOCK_ROUNDS = 2


def _verify_gate(agent, message: str, holder: dict,
                 auto_approve=None, stop_check=None, read_cache=None,
                 snapshot: dict[str, float] | None = None,
                 exhausted: set[str] | None = None):
    """收尾验证闸门（系统侧强制）：模型只有解读权，没有裁定权。

    - 任务书里声明了验证命令（如「验证：python tools/x.py」）→ 程序亲自运行，
      真实结果通过 verify 事件播报；失败时给模型一轮修正机会（可修可如实说明），
      之后无论模型总结说什么，都会在答复末尾附上程序写的系统侧验证报告。
    - 没声明验证命令、但模型总结声称「验证通过」且全程没跑过 run_command →
      在答复末尾追加「未经系统确认」声明。
    - 独立第二道核查：任务书声明的交付文件与总结声称「已写入 X」的文件，
      系统核查三类问题——不存在（missing）/ 本轮未更新（stale，mtime 与任务
      开始快照一致，吃老本）/ 幽灵声明（ghost，全程零写操作却说已写入）；
      命中给一轮修正机会，复查仍不过就在答复末尾附上系统侧核查声明。
      snapshot 由调用方在任务开始时用 verify_gate.snapshot_artifacts 生成。

    holder 进出各带 {"summary", "convo"}：闸门可能改写总结（追加报告/被修正轮
    重写），调用方从 holder 里取最终值。
    """
    cfg = agent.cfg
    if not getattr(cfg, "verify_gate", True):
        return
    convo = holder.get("convo") or []
    summary = holder.get("summary") or ""
    cmd = verify_gate.extract_verify_cmd(message, cfg.workspace_root)
    expect = ""
    corrected = False
    # 程序侧出具的声明统一收进 notes、最后一次性拼接：修正轮只重写模型正文，
    # 绝不吞掉已出具的系统声明。第四轮评测实测（c3）：C 分支先追加了「未经
    # 系统确认」，随后的产物核查修正轮把 summary 整个重写，声明被抹掉——
    # 谎报静默放行。声明由程序出具，就必须由程序保证它活着到用户眼前。
    notes: list[str] = []
    # 任务级闸门拦到上限仍失败的验证命令记入 exhausted，汇总级闸门不再重跑——
    # 否则同一条命令被两级闸门各拦三轮，违背「至多三次执行」的流程约定
    if exhausted is None:
        exhausted = set()
    if cmd:
        # 停机拦截（Claude Code Stop hook 同款）：验证失败就不许收工，把真实
        # 结果回灌当下一轮指令，连续拦截最多 _VERIFY_BLOCK_ROUNDS 轮防死循环；
        # 每轮拦截结束后重新亲跑验证——修好了立即放行，修不了如实说明后放行
        # 并附权威报告。模型说修好不算，程序说了算。
        blocks = 0
        res = verify_gate.run_verify_cmd(cmd, cfg.workspace_root)
        # 任务级闸门拦到上限仍失败的命令（exhausted 里有记录）不再重跑：报告
        # 已由任务级闸门随子任务摘要出具，这里再拦只会把修正轮翻倍
        if cmd.strip().lower() in exhausted:
            res = {"ok": True, "code": 0, "output": "",
                   "duration": "0.0", "exhausted_skip": True}
        else:
            # 任务书写了「确认输出 24」这类期望 → 程序亲自比对：exit=0 但输出
            # 不含期望值也算验证失败（c1 测出的盲区——谎报「输出 24」实际是
            # 10，旧闸门只看退出码，等于没验）。
            expect = verify_gate.extract_expect_output(message, cmd)
            res = verify_gate.apply_expect(res, expect)
        yield {
            "type": "verify", "cmd": cmd, "ok": res["ok"],
            "exit": res["code"], "duration": res["duration"],
            "output": res["output"], "expect": expect,
        }
        while not res["ok"] and blocks < _VERIFY_BLOCK_ROUNDS:
            blocks += 1
            corrected = True
            prompt = verify_gate.CORRECTIVE_PROMPT.format(
                cmd=cmd, verdict="失败", code=res["code"],
                duration=res["duration"],
                output=res["output"]
                        + (f"\n（任务书期望输出含：{expect}）" if expect else ""),
                summary=summary or "（无）",
            )
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            # 修正轮结束后复核：重新亲跑，通过就放行，仍失败且没到拦截上限
            # 就再拦一轮（第 _VERIFY_BLOCK_ROUNDS 轮后放行，报告照附）。
            res = verify_gate.run_verify_cmd(cmd, cfg.workspace_root)
            res = verify_gate.apply_expect(res, expect)
            yield {
                "type": "verify", "cmd": cmd, "ok": res["ok"],
                "exit": res["code"], "duration": res["duration"],
                "output": res["output"], "expect": expect,
                "recheck": True,
            }
        if not res["ok"]:
            # 拦到上限仍失败：记入 exhausted，同一轮编排里更靠后的闸门不再重跑
            holder.setdefault("exhausted_cmds", set()).add(cmd.strip().lower())
            exhausted.add(cmd.strip().lower())
        notes.append(verify_gate.report_block(cmd, res))
    elif (verify_gate.claimed_verified(summary)
          and not verify_gate.ran_verification(convo)):
        notes.append(verify_gate.UNVERIFIED_NOTE)

    # 声明产物核查（独立于验证命令的第二道核查）：三类问题——
    #   missing 文件不存在（REA 一测：零 write_file 谎报「已写入」）
    #   stale   文件在但 mtime 与任务开始快照一致——本轮从未更新，吃老本
    #           （REA 三测：没重写声明的报告，上一轮的旧文件糊弄了存在性核查）
    #   ghost   文件在但全程零写操作——它不可能是本轮写出的
    # 任一命中先给一轮修正机会（真写文件或如实说明），复查仍不过就在答复
    # 末尾附上系统侧核查声明——模型只有解读权。
    declared = verify_gate.extract_declared_artifacts(message)
    dset = {d.lower() for d in declared}
    claimed = [p for p in verify_gate.claimed_artifacts(summary)
               if p.lower() not in dset]
    if declared or claimed:
        # 裸文件名声称先对齐（f1 教训：声称 summary.md、实际写在 reports/2026/
        # 之下，按根目录判不存在就误报）
        entries = verify_gate.resolve_claim_paths(
            [(p, True) for p in declared] + [(p, False) for p in claimed],
            cfg.workspace_root)
        produced = (verify_gate.ran_writes(convo)
                    or verify_gate.ran_verification(convo))
        issues = verify_gate.find_artifact_issues(
            entries, cfg.workspace_root, snapshot=snapshot, produced=produced)
        if issues and not corrected:
            # 修正前先记下这些文件的 mtime：复查就看它本轮有没有真的变
            pre = {c["path"].lower(): verify_gate.path_mtime(c["path"], cfg.workspace_root)
                   for c in issues}
            prompt = verify_gate.ARTIFACT_CORRECTIVE_PROMPT.format(
                issues="\n".join(verify_gate.issue_line(c) for c in issues),
                summary=summary or "（无）",
            )
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            issues = [c for c in issues
                      if not verify_gate.artifact_fixed(c, pre, cfg.workspace_root)]
        if issues:
            yield {"type": "verify", "cmd": "", "ok": False, "exit": "",
                   "duration": "",
                   "artifacts": [{"path": c["path"], "declared": c["declared"],
                                  "status": c["status"]} for c in issues]}
            notes.append(verify_gate.artifact_issue_block(issues))

    # 第五道核查：**自己承认还没做 / 做不了**，同样不许当成交付。
    # 上面几道治的都是「声称做了但没做」；这是它的反面，一直没人管——模型如实招了
    # 「下一步我将…」「我无法修改…」，系统却照样把这条回复当 done 收下。
    # 事故现场（用户原话「其两个指令都没有执行」）：一条含两件事的消息被拆成 2 个子任务，
    # 各只调了一次只读工具就收尾，一个写「下一步：我将检索 docs/ 目录」，另一个写
    # 「我无法直接修改自身的思维链显示机制，因为其可见性由平台底层的 UI 渲染逻辑控制」，
    # 然后「已记录您的偏好要求」。两条都被判成功，任务一步没动。
    #
    # 三道门同时成立才出手：任务书本身就是「动手改东西」的活（looks_like_change）、
    # 本轮**零文件改动**（ran_writes 为假）、总结里模型自己招了。纯问答任务里一句
    # 「我无法联网核实」是如实回答，不是怠工——所以不能只看最后一条。
    if getattr(cfg, "unfinished_gate", True):
        claim = verify_gate.unfinished_claim(summary)
        if claim and not verify_gate.ran_writes(convo) and looks_like_change(message):
            if not corrected:
                corrected = True
                prompt = verify_gate.UNFINISHED_CORRECTIVE_PROMPT.format(
                    quote=verify_gate.unfinished_quote(summary, claim),
                    summary=summary or "（无）",
                )
                for ev in agent.run(convo, prompt, auto_route=False,
                                    stop_check=stop_check, auto_approve=auto_approve,
                                    read_cache=read_cache):
                    k = ev.get("type")
                    if k in ("user", "route"):
                        continue
                    if k == "assistant":
                        convo = ev["history"]
                        summary = (ev.get("content") or "").strip() or summary
                    yield ev
                claim = verify_gate.unfinished_claim(summary)
                # 修正轮真的动了手，就不再追加声明；否则如实标注没交付
                if verify_gate.ran_writes(convo):
                    claim = ""
            if claim:
                yield {"type": "verify", "cmd": "", "ok": False, "exit": "",
                       "duration": "", "unfinished": claim}
                notes.append(verify_gate.UNFINISHED_NOTE)

    # 第三道核查：外部事实的一手来源。用过联网搜索、却没抓过任何一手来源，
    # 而总结里又出现了数字 / 版本 / 日期这类外部事实时——由程序在答复末尾追加
    # 「转述 · 未经核实」声明。这治的是 REA 检索那个病：搜索摘要里的「star 1w+」
    # 被直接当结论写出来，而真实值是 17 万。人不一定看穿，程序一眼看穿。
    # 与上面两道核查同一思路：模型只有解读权，没有裁定权。
    if getattr(cfg, "sources_gate", True):
        hits = sources_gate.needs_external_note(summary, convo)
        if hits:
            notes.append(sources_gate.external_unverified_block(hits))

    # 第四道核查：本机事实的来源。任务要的就是「工作区里现在有什么」（列目录 /
    # 有哪些文件 / 看现状），模型却全程一次读取类工具都没调，答复里却列着具体文件
    # 名——那是编的。治的是端到端实测那个病：25 秒、0 次工具调用，凭空列出
    # agent-forge.json / core/ / ui/ 这些工作区里根本不存在的路径。
    # 与外部事实那道同源（结论必须有来源），只不过这回的来源是工作区本身。
    # 硬约束：先给一轮修正机会去真看一眼——「技能加载了但没拦住」的解法不是把
    # 提示词写得更重，而是让系统替它把这一步做实；修正后仍然没看，才由程序附声明。
    if getattr(cfg, "local_facts_gate", True):
        hits = sources_gate.needs_local_note(summary, convo, message)
        if hits and not corrected:
            corrected = True
            prompt = sources_gate.LOCAL_FACTS_CORRECTIVE_PROMPT.format(
                tokens="\n".join(f"- {t}" for t in hits),
                summary=summary or "（无）",
            )
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            hits = sources_gate.needs_local_note(summary, convo, message)
        if hits:
            yield {"type": "verify", "cmd": "", "ok": False, "exit": "",
                   "duration": "", "unverified_local": hits}
            notes.append(sources_gate.local_unverified_block(hits))

    # 程序侧声明统一在这里拼回正文末尾——无论中途经历几轮修正轮重写
    if notes:
        summary = (summary + "\n\n---\n" + "\n\n---\n".join(notes)).strip()

    if summary != holder.get("summary"):
        holder["summary"] = summary
        holder["convo"] = convo


def _topo_order(tasks: list[TaskSpec]) -> list[TaskSpec]:
    """按依赖关系排执行顺序（同批任务保持原序）；依赖缺失/成环时直接放行，避免卡死。"""
    remaining = list(tasks)
    done: set[str] = set()
    ordered: list[TaskSpec] = []
    guard = 0
    while remaining and guard < 1000:
        guard += 1
        ready = [t for t in remaining if all(d in done for d in t.depends_on)]
        if not ready:
            ready = [remaining[0]]
        for t in ready:
            remaining.remove(t)
            ordered.append(t)
            done.add(t.id)
    return ordered


def _is_local_model(agent, model_key: str) -> bool:
    try:
        return getattr(agent.cfg.model(model_key), "kind", "local") == "local"
    except Exception:  # noqa: BLE001
        return True


def _ready_batch(tasks: list[TaskSpec], done: set[str]) -> list[TaskSpec]:
    """取出当前可执行的一批任务（依赖都已完成的算就绪）。

    依赖成环或指向不存在的任务时，就绪集合会为空——这时退回队首一个任务，
    宁可慢一点也不能卡死。
    """
    ready = [t for t in tasks if all(d in done for d in t.depends_on)]
    return ready or [tasks[0]]


def run_multi_agent(agent, store, entries: list[dict], history: list[dict],
                    message: str, dispatcher: Dispatcher,
                    stop_check=None, auto_approve: bool | None = None,
                    file_log: list | None = None) -> Iterator[dict[str, Any]]:
    """多 agent 编排：决策模型拆解 → 共享黑板 → 任务在 agent 之间流转。

    调度模型先把请求拆成若干【语义独立】的任务并指派执行 agent，每个 agent 都
    拿到「共享进度简报」，因此后面的 agent 不会从头开始。执行分两种：

    - 串行接力（默认）：一个任务跑完再跑下一个，上下文一路继承。
    - 并行（dispatcher_parallel 且处于完全访问权限）：同一批就绪的**互不相关**
      任务同时跑，各自一个 agent 实例、各自一路流式输出（事件带 task_id，前端按
      task 分行渲染）。本地模型受两道闸约束——llm 层的请求锁 + 这里的「本地闸门」
      （local_serial 打开时同一时刻只有一个本地模型任务在跑），在线模型不受影响。

    冲突处理：并行任务写同一个文件时由 permissions.claim_write 排队写入；跑完
    一批再检出重叠写入并发 conflict 事件提醒用户核对。

    事件与单模型路径兼容（plan/assign/subtask/delta/…），另加 dispatch / task_done /
    bridge / conflict。
    """
    stopped = stop_check or (lambda: False)
    plan = dispatcher.plan(message, entries)
    ordered = _topo_order(plan.tasks)
    if not ordered:
        return
    total = len(ordered)
    # 让 index 与真实执行顺序一致（拓扑排序可能打乱原计划次序），前端计划卡
    # 与 assign 事件才能对上号
    for i, t in enumerate(ordered):
        t.index = i + 1

    # 并行前提：① 配置允许；② 完全访问权限——默认权限下每次写文件/执行命令都要
    # 人工确认，多路同时弹确认会互相打架，所以退回串行接力。
    full_access = auto_approve is True or bool(getattr(agent.guard, "auto_approve_all", False))
    parallel_on = bool(getattr(agent.cfg, "dispatcher_parallel", True)) and full_access

    yield {
        "type": "dispatch",
        "tasks": [t.as_dict() for t in ordered],
        "rationale": plan.rationale,
        "source": plan.source,
        "dispatcher": plan.dispatcher_name,
        "multi": total >= 2,
        "parallel": parallel_on and total >= 2,
        "local_serial": bool(getattr(agent.cfg, "local_serial", True)),
    }
    if total >= 2:
        yield {"type": "plan", "subtasks": [t.as_dict() for t in ordered],
               "summary": plan.summary()}

    state = SharedState(message)
    state.register(ordered)

    # 产物核查的起点快照：任务书声明交付文件的 mtime 在此刻定格，收尾时
    # 拿它判断「本轮是否真的更新过」——上一轮的旧文件糊弄不了这关。
    snap = verify_gate.snapshot_artifacts(message, agent.cfg.workspace_root)

    # 整任务共享的只读调用缓存：所有泳道/子任务 + 收尾修正轮共用一份。
    # REA 二测里 t2/t3/t4 各自 list_dir docs、各读一遍 HANDOFF.md——
    # 各泳道私有缓存互相看不见，同参数只读调用被白跑多遍。写类工具执行后
    # 缓存全量作废（在 loop.py 里做），所以共享是安全的。
    read_cache = ReadCache()

    weak_cn = {e["key"]: int((e.get("scores") or {}).get("chinese", 100)) for e in entries}
    translate_on = bool(getattr(agent.cfg, "dispatcher_translate", True))
    cn_threshold = int(getattr(agent.cfg, "dispatcher_bridge_threshold", BRIDGE_CN_THRESHOLD))

    base_convo = list(history)
    # 本地闸门：开启「本地串行」时，同一时刻至多一个本地模型任务在跑，避免两个
    # 本地模型同时驻留显存互相拖累。在线模型不受影响，真正并行。
    local_gate = threading.Semaphore(1) if getattr(agent.cfg, "local_serial", True) else None

    shared_log: list = file_log if file_log is not None else []
    log_lock = threading.Lock()
    sink: dict[str, dict] = {}          # task_id -> {"files": [...], "failed": bool, "convo": [...]}
    hist_holder: dict = {"convo": base_convo}   # 串行接力时最后一份完整历史
    exhausted_cmds: set = set()         # 任务级闸门拦到上限仍失败的验证命令

    def spawn_agent():
        """给并行任务起一个独立的 Agent：Agent 持有 per-run 可变状态，不能共用。"""
        return Agent(agent.cfg, agent.client, agent.registry, agent.guard,
                     agent.router, skill_registry=agent.skills, hub=agent.hub)

    def one_task(t: TaskSpec, ag, convo_in: list[dict]) -> Iterator[dict]:
        """跑一个任务；事件原样产出，结果写进 sink。串行直用，也可在线程里跑。"""
        # 模型可能刚从设置里被删掉，退回池内第一个可用模型
        if t.model_key not in agent.cfg.models:
            t.model_key = entries[0]["key"]
            t.model_name = entries[0]["name"]

        yield {
            "type": "assign",
            "index": t.index, "total": total,
            "model_key": t.model_key, "model_name": t.model_name,
            "kind": t.kind, "kind_label": KIND_LABEL.get(t.kind, t.kind),
            "reason": t.reason, "title": t.title, "score": t.score,
            "multi": total >= 2, "task_id": t.id,
            "reassigned": bool(getattr(t, "reassigned", False)),
            "role": f"{KIND_LABEL.get(t.kind, t.kind)} agent",
            "capability": t.capability,
            "depends_on": list(t.depends_on),
            "handoff": t.handoff,
        }

        state.start(t.id)
        brief = state.brief(t)

        # 能力桥接：执行模型中文很弱、任务又需要中文时，先把输入译成英文
        goal = t.goal
        bridged = False
        if (translate_on and _has_cjk(goal)
                and weak_cn.get(t.model_key, 100) < cn_threshold):
            en = dispatcher.translate(goal, "English")
            if en and en != goal:
                goal = en
                bridged = True
                yield {"type": "bridge", "task_id": t.id, "to": "English",
                       "reason": f"{t.model_name} 中文能力较弱，调度器已把任务译成英文"}

        prompt = AGENT_TASK_TEMPLATE.format(
            message=message, index=t.index, total=total, title=t.title,
            goal=goal, capability=t.capability or "（未特别指定）",
            handoff=t.handoff or "（无）", brief=brief,
            bridged=BRIDGE_NOTE if bridged else "",
        )
        yield {"type": "subtask", "index": t.index, "total": total,
               "text": t.goal, "task_id": t.id}

        ag.set_model(t.model_key)
        local = _is_local_model(agent, t.model_key)
        gate = local_gate if (local and local_gate is not None) else None
        if gate is not None:
            gate.acquire()          # 本地模型排队；在线模型直接过

        local_log: list = []
        summary = ""
        failed = False
        convo = list(convo_in)
        try:
            for ev in ag.run(convo, prompt, auto_route=False, stop_check=stopped,
                             auto_approve=auto_approve, file_log=local_log,
                             read_cache=read_cache):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue                       # 用户气泡本地渲染；路由由编排层决定
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip()
                if k == "error":
                    # 子任务自己的失败只属于这个子任务。往上换一个事件名（subtask_error），
                    # 别让外层把它当成「整条回复失败」——外层的 error 是终态：界面会立刻
                    # 收尾（把复制工具条挂在回答中途）、台账会把整条任务记成 error，
                    # 而这个子任务失败在设计上明确「不该拖垮同批其它任务」。
                    failed = True
                    yield {"type": "subtask_error", "task_id": t.id,
                           "message": ev.get("message") or ""}
                    continue
                if k == "stopped":
                    yield {"type": "history", "messages": ev.get("history") or convo}
                    yield {"type": "stopped", "steps": t.index, "total": total,
                           "reason": ev.get("reason") or "用户停止"}
                    return
                if k in ("delta", "thinking", "tool_start", "tool_result", "stats", "step"):
                    ev = {**ev, "task_id": t.id}
                yield ev
        finally:
            if gate is not None:
                gate.release()

        # 任务级闸门（流程第 6 步：依次验证每个任务的结果）：子任务一收尾，
        # 系统就亲自核验它任务书（goal）里声明的验证命令与交付物——失败当场
        # 拦截，把真实输出回灌给同一个 agent 继续做（初次执行 + 至多
        # _VERIFY_BLOCK_ROUNDS 次修正 = 至多三次执行，到限仍失败则放行并附
        # 系统侧报告，汇总时如实带出），不等全部任务跑完再算总账。调度器被
        # 明确要求把交付物与验证命令原样写进对应任务的 goal（见 dispatcher
        # 提示词），这里照着 goal 核对；总任务级的声明仍由最后的汇总闸门把守。
        if not failed:
            task_book = t.goal or t.title or message
            task_snap = verify_gate.snapshot_artifacts(
                task_book, agent.cfg.workspace_root)
            task_holder = {"summary": summary or _last_assistant(convo),
                           "convo": convo}
            for ev in _verify_gate(ag, task_book, task_holder,
                                   auto_approve=auto_approve, stop_check=stopped,
                                   read_cache=read_cache, snapshot=task_snap,
                                   exhausted=exhausted_cmds):
                k = ev.get("type")
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                if k in ("delta", "thinking", "tool_start", "tool_result",
                         "stats", "step", "verify"):
                    ev = {**ev, "task_id": t.id}
                yield ev
            exhausted_cmds.update(task_holder.get("exhausted_cmds") or ())

        # 桥接后把结果译回中文，保证给用户的是中文
        if bridged and summary and not _mostly_chinese(summary):
            zh = dispatcher.translate(summary, "Chinese")
            if zh:
                summary = zh

        new_files: list[str] = []
        for rec in local_log:
            p = str(rec.get("path") or "")
            if p and p not in new_files:
                new_files.append(p)
        if local_log:
            with log_lock:                  # 各任务的产出文件合并进总台账
                shared_log.extend(local_log)
        final_line = summary or _last_assistant(convo)
        state.finish(t.id, final_line, new_files, status="error" if failed else "done")
        sink[t.id] = {"files": new_files, "failed": failed, "convo": convo}
        hist_holder["convo"] = convo
        yield {"type": "task_done", "task_id": t.id, "index": t.index, "total": total,
               "status": "error" if failed else "done", "title": t.title,
               "summary": final_line, "files": new_files}

    def conflict_events(batch: list[TaskSpec]):
        """一批并行任务都改了同一个文件时明确告警（写入已被排队，但结果要核对）。"""
        owner: dict[str, str] = {}
        name_of = {t.id: t.title for t in ordered}
        for t in batch:
            for p in (sink.get(t.id, {}).get("files") or []):
                if p in owner and owner[p] != t.id:
                    yield {"type": "conflict", "path": p,
                           "tasks": [name_of.get(owner[p], owner[p]),
                                     name_of.get(t.id, t.id)],
                           "note": "两个并行任务都改了同一个文件，已按先后顺序串行写入，请核对最终内容"}
                else:
                    owner[p] = t.id

    def run_parallel(wave: list[TaskSpec]) -> Iterator[dict]:
        """同一批就绪任务并行跑：各自 agent 实例，事件经队列汇聚后按到达顺序产出。"""
        q: queue.Queue = queue.Queue()
        done_marker = object()
        # 泳道跑在独立线程里，threading.local 不会继承调用方线程的 current_task；
        # 不补设的话 remember / task_notes 会把记忆记到「adhoc」——「清空时按任务
        # 精确删除」对并行子任务失效。串行路径跑在调用方线程里天然带着归属，
        # 这里在进入线程前取一次、进线程后补设，两条路口径一致。
        owner_task = runcontrol.current_task()

        def worker(t: TaskSpec) -> None:
            runcontrol.set_current_task(owner_task)
            try:
                ag = spawn_agent()
                for ev in one_task(t, ag, base_convo):
                    q.put(ev)
            except Exception as e:  # noqa: BLE001  单个任务崩了不该拖垮同批其它任务
                # 同样是「子任务级失败」：事件名不能叫 error，否则外层会收尾整条回复
                q.put({"type": "subtask_error", "task_id": t.id,
                       "message": f"{t.title}：{type(e).__name__}: {e}"})
                sink.setdefault(t.id, {"files": [], "failed": True})
            finally:
                runcontrol.clear_current_task()
                q.put(done_marker)

        for t in wave:
            threading.Thread(target=worker, args=(t,), daemon=True).start()

        pending = len(wave)
        stopping = False
        while pending > 0:
            if stopping:
                try:
                    ev = q.get(timeout=0.2)     # 停止后只收尾，不再转发
                except queue.Empty:
                    continue
            else:
                ev = q.get()
            if ev is done_marker:
                pending -= 1
                continue
            if not stopping and stopped():
                stopping = True
                continue
            if not stopping:
                yield ev

    def run_serial() -> Iterator[dict]:
        convo = base_convo
        remaining = list(ordered)
        done_ids: set[str] = set()
        while remaining:
            if stopped():
                yield {"type": "history",
                       "messages": convo or [{"role": "user", "content": message}]}
                yield {"type": "stopped", "steps": len(done_ids), "total": total,
                       "reason": "用户停止"}
                return
            t = _ready_batch(remaining, done_ids)[0]
            remaining = [x for x in remaining if x is not t]
            for ev in one_task(t, agent, convo):
                if ev.get("type") == "assistant":
                    convo = ev["history"]
                yield ev
                if ev.get("type") == "stopped":
                    return
            done_ids.add(t.id)
            if sink.get(t.id, {}).get("failed"):
                return                          # 接力链上一步失败，后面的不再硬跑

    if not parallel_on or total == 1:
        yield from run_serial()
    else:
        remaining = list(ordered)
        done_ids: set[str] = set()
        while remaining:
            if stopped():
                yield {"type": "history",
                       "messages": base_convo or [{"role": "user", "content": message}]}
                yield {"type": "stopped", "steps": len(done_ids), "total": total,
                       "reason": "用户停止"}
                return
            wave = _ready_batch(remaining, done_ids)
            remaining = [x for x in remaining if x not in wave]
            if len(wave) > 1:
                yield from run_parallel(wave)
            else:
                for ev in one_task(wave[0], agent, base_convo):
                    yield ev
                    if ev.get("type") == "stopped":
                        return
            yield from conflict_events(wave)
            done_ids.update(t.id for t in wave)
            # 这批里某个任务失败不影响别的批（它们彼此独立），交给收尾汇总时如实说明

    # 完整历史：串行接力时最后一份 convo 已包含全部；并行时各任务历史相互独立，
    # 需要按任务顺序把各自新增的部分拼回去（否则下一次对话会丢掉它们做过的事）。
    if parallel_on and total > 1:
        final_convo = list(base_convo)
        for t in ordered:
            c = sink.get(t.id, {}).get("convo") or []
            if len(c) > len(base_convo):
                final_convo.extend(c[len(base_convo):])
    else:
        final_convo = hist_holder["convo"]

    # 收尾：决策模型把各 agent 的结果汇总成一份答复（多任务时才做）
    final = ""
    if total >= 2 and getattr(agent.cfg, "dispatcher_synthesis", True):
        final = dispatcher.synthesize(message, state.result_items()) or ""
    if final:
        # 输出边界的语言闸门：整合模型也可能是英文倾向的弱模型，而桥接执行的
        # 子任务摘要常常整段英文。用户记忆里「回复一律中文」这类偏好不能只靠
        # 提示词自觉——最后这里程序化兜底，不是中文就强制回译。
        if not _mostly_chinese(final):
            zh = dispatcher.translate(final, "Chinese")
            if zh:
                final = zh
        # 收尾验证闸门在语言闸门之后：附上的系统侧报告随最终答复一起出去
        holder = {"summary": final, "convo": final_convo}
        yield from _verify_gate(agent, message, holder,
                                auto_approve=auto_approve, stop_check=stopped,
                                read_cache=read_cache, snapshot=snap,
                                exhausted=exhausted_cmds)
        final = holder["summary"]
        final_convo = holder["convo"]
        final_convo = [*final_convo, {"role": "assistant", "content": final}]
        yield {"type": "assistant", "content": final, "history": final_convo}
    else:
        # 没做汇总（单任务、关掉 synthesis，或 synthesize() 返回空）时同样要过
        # 闸门：拿最后一份历史的末条助手正文当总结。第四轮评测实测（c3/f1
        # 静默的根因）：原本这里只在 total>=2 且 synthesis 开启时才进来，而
        # synthesize() 返回空时 if final 不成立、外层 else 又配不上——两处
        # 收尾闸门全部被跳过，谎报与假产出静默溜过。
        holder = {"summary": _last_assistant(final_convo), "convo": final_convo}
        yield from _verify_gate(agent, message, holder,
                                auto_approve=auto_approve, stop_check=stopped,
                                read_cache=read_cache, snapshot=snap,
                                exhausted=exhausted_cmds)
        if holder["summary"] and holder["summary"] != _last_assistant(final_convo):
            final_convo = holder["convo"]
            yield {"type": "assistant", "content": holder["summary"],
                   "history": final_convo}
        else:
            final_convo = holder["convo"]

    yield {"type": "complete", "steps": total, "multi": total >= 2}


# 多路径尝试：失败后让当前模型拟几条互不相同的实现路径，再逐条试。
# 换「路」的核心是换手段（工具/命令/文件组织/步骤拆分），不是换模型——
# 本地模型免费且空闲，路径数量不受成本约束；但每条路径都要真跑过才算数。
APPROACH_SYSTEM = (
    "你是执行规划器。一个任务步骤刚刚尝试失败了。请提出 {k} 条互不相同的实现路径，"
    "每条一行、以序号开头，只写「怎么做」的核心手段，不要执行、不要解释理由。"
    "路径之间必须在手段上有实质区别（例如：换工具 / 换命令 / 换文件组织方式 / "
    "把任务拆成更小的子步骤），不允许给出只换了说法的同一条路。"
)
FALLBACK_APPROACHES = [
    "先用 list_dir 和 find_files 查清工作区现状，再选最朴素的手段（读文件→改→运行验证）完成",
    "把这一步拆成更小的子步骤逐个完成，每完成一小步就验证一次，不要一口气做完再验",
    "放弃刚才失败的工具或命令，改用其他已下发的工具组合完成任务",
]


def _propose_approaches(agent, step_desc: str, k: int = 3) -> list[str]:
    """让当前模型拟 k 条互不相同的实现路径（只拟不执行）。失败返回空表。

    用编排模型直接对话（不走 agent.run 的完整工具循环）：拟路径是纯思考动作，
    不该消耗工具轮次，也不该把这段对话漏进任务历史。
    """
    try:
        res = agent.hub.chat(
            agent.model_spec,
            [{"role": "system", "content": APPROACH_SYSTEM.format(k=k)},
             {"role": "user", "content": f"任务步骤：{step_desc}\n刚才的尝试失败了。"
                                         f"请给出 {k} 条不同的实现路径。"}],
            tools=None,
            options={"temperature": 0.7, "num_predict": 512},
            think=False,
        )
    except Exception:  # noqa: BLE001  拟不出来就退回内置路径，绝不能炸主流程
        return []
    # 只认「编号 / 列表」行：模型回了句客套话或解释文字时，宁可判为拟不出
    # （退回内置路径），也不能把一句人话当成实现路径拿去跑。
    numbered = re.compile(r"^\s*(?:\d+[.、)）]|[①②③④⑤⑥⑦⑧⑨⑩]|[-*•])\s*")
    lines: list[str] = []
    for raw in (res.content or "").splitlines():
        if not numbered.match(raw):
            continue
        ln = numbered.sub("", raw).strip().strip("：:　 ")
        if len(ln) >= 6:
            lines.append(ln[:200])
        if len(lines) >= k:
            break
    return lines


def run_task(
    agent,
    store,
    history: list[dict],
    message: str,
    auto_plan: bool = True,
    model_key: str = "",
    preferred_key: str = "",
    stop_check=None,
    auto_approve: bool | None = None,
    file_log: list | None = None,
    multi_path: bool = False,
) -> Iterator[dict[str, Any]]:
    """执行一次用户请求。store 提供当前启用的模型池。

    model_key 非空且不是 "auto" 时表示用户手动指定了模型：跳过自动分配，
    所有步骤钉在该模型上执行。

    preferred_key 是能力路由建议的「优先模型」：不视为手动钉定，调度模型可用时
    仍由它按能力重新指派；只有调度模型不可用时才退化为整体使用该优先模型。

    stop_check 返回 True 时在下一个步骤边界停下，已完成的步骤产出照常保留。

    auto_approve 本次任务的权限模式（True = 完全访问），file_log 用来收集
    本任务写过的文件，两者都透传给每一步的 Agent.run。

    multi_path 开启时，某一步失败（agent 报 error）会先让当前模型拟几条互不
    相同的实现路径，再逐条重试同一步（每条路径真跑一遍，直到某条成功或试完）。
    换的是「路」不是「模型」：模型不变，变的是手段。本地模型空闲且无 token
    计费，多条路径近乎零成本；关闭时行为与旧版完全一致。仅作用于本函数的
    计划路径——多 agent 调度路径里子任务级失败另有 subtask_error 汇报机制，
    不在此重试。
    """
    stopped = stop_check or (lambda: False)
    # 产物核查的起点快照：任务书声明交付文件的 mtime 此刻定格，收尾时用它
    # 判断「本轮是否真的更新过」，上一轮的旧文件糊弄不了核查
    gate_snap = verify_gate.snapshot_artifacts(message, agent.cfg.workspace_root)
    exhausted_cmds: set = set()         # 步骤级闸门拦到上限仍失败的验证命令
    pinned_key = (model_key or "").strip()
    pinned: dict | None = None
    # 任务池 = 「勾选且本机能跑」。一个都没有时退回到所有能跑的模型——
    # 换台电脑 clone 下来、本地模型一个都还没 pull 时，正是靠这条路
    # 直接用上在线模型开工。（不能调 catalog()，那会抹掉可用性判断）
    pool = store.fallback_pool()
    pool_fallback = not store.usable() and bool(pool)
    if pinned_key and pinned_key != "auto":
        found = store.find_any(pinned_key)
        if found is None:
            yield {"type": "error", "message": f"指定的模型不存在：{pinned_key}"}
            return
        if not found.available:
            tip = (f"先 `ollama pull {found.name}`" if found.kind == "local"
                   else "检查它的 API Key / base_url 配置")
            yield {
                "type": "error",
                "message": f"指定的模型本机跑不起来：{found.display}（{tip}）",
            }
            return
        pinned = {"id": found.id, "key": found.key, "name": found.display,
                  "scores": found.scores, "total": found.total}

    # 能力路由的自动升级只当作「优先模型」：它不阻断多 agent 调度，也不等同于
    # 用户手动钉模型——只有手动钉（model_key）才跳过自动分配。调度模型可用时，
    # 由它按能力重新指派更合适；不可用时才退化为整体使用这个优先模型。
    preferred: dict | None = None
    pref_key = (preferred_key or "").strip()
    if pref_key and pref_key != "auto" and not pinned:
        f = store.find_any(pref_key)
        if f is not None and f.available:
            preferred = {"id": f.id, "key": f.key, "name": f.display,
                         "scores": f.scores, "total": f.total}

    entries = _entries_of(pool)
    if not entries:
        yield {
            "type": "error",
            "message": "当前没有可用的模型：本机没装本地模型，且还没有配置可用的在线模型。"
                       "请先 `ollama pull` 一个本地模型，或到设置 → 可用模型管理里添加在线模型。",
        }
        return

    yield {
        "type": "combo",
        "combo": store.combo_info(pool),
        "models": [{"id": e["id"], "name": e["name"]} for e in entries],
        # 窗口跨度警告：接力式多模型协作会被最小窗口封顶
        "window_notice": window_notice(entries),
        # 勾选的模型本机全跑不起来、已自动改用别的模型时告诉用户一句
        "pool_notice": (
            "勾选的模型在这台机器上都跑不起来（本地模型可能还没安装），"
            f"本次已改用可用模型：{'、'.join(e['name'] for e in entries[:3])}"
            if pool_fallback else ""
        ),
    }

    # 多 agent：由本地决策模型拆解 → 指派 → 接力。仅在未手动指定模型、且请求
    # 值得拆解（多动作或较长）时启用；决策模型不可用时自动落回下面的规则快路。
    if (auto_plan and not pinned
            and getattr(agent.cfg, "dispatcher_enabled", False)
            and _worth_dispatch(message, len(entries))):
        try:
            disp = Dispatcher(agent.cfg, agent.hub, store)
        except Exception:  # noqa: BLE001
            disp = None
        if disp is not None and disp.available():
            yield from run_multi_agent(
                agent, store, entries, history, message, disp,
                stop_check=stop_check, auto_approve=auto_approve, file_log=file_log,
            )
            return

    plan: list[Subtask] = []
    if auto_plan and needs_plan(message, len(entries)):
        plan = [s for s in make_plan(message, entries) if (s.detail or "").strip()]

    multi = len(plan) >= 2
    if not multi:
        plan = [_single_task(message, entries)]

    if pinned:
        for st in plan:
            st.model_key = pinned["key"]
            st.model_name = pinned["name"]
            st.model_id = pinned["id"]
            st.reason = "手动指定模型"
    elif preferred:
        for st in plan:
            st.model_key = preferred["key"]
            st.model_name = preferred["name"]
            st.model_id = preferred["id"]
            st.reason = "能力路由自动升级"

    if multi:
        yield {
            "type": "plan",
            "subtasks": [s.as_dict() for s in plan],
            "summary": plan_summary(plan),
        }

    # 展示/回看用的全量历史（一路接力，内容完整）
    hist = list(history)
    # 单路径多步接力的只读共享缓存（跨步骤复用；写类工具执行后自动作废）
    read_cache = ReadCache()
    # 接力省法：每一步单独记「做了什么、动了哪些文件」，下一步只看这份简报
    brief_relay = bool(getattr(agent.cfg, "relay_brief", True)) and multi
    done: list[dict] = []
    total = len(plan)

    def _step_events(step_idx: int, prompt_: str, relay_: list | None):
        """跑一步 agent：透传事件流，返回 (summary, failed, halted, hist)。

        单独提出来是因为「多路径尝试」要在一步失败后换模型重跑同一步——
        事件处理逻辑必须能原样跑第二次，不能复制粘贴一份。
        """
        summary_ = ""
        failed_ = False
        halted_ = False
        hist_ = hist
        for ev in agent.run(hist_, prompt_, auto_route=False, stop_check=stopped,
                            auto_approve=auto_approve, file_log=file_log,
                            relay=relay_, read_cache=read_cache):
            t = ev.get("type")
            if t == "user":
                continue                      # 用户气泡由前端本地渲染
            if t == "assistant":
                hist_ = ev["history"]         # 接力上下文（全量，供回看）
                summary_ = (ev.get("content") or "").strip()
            if t == "route":
                continue                      # 编排模式下路由由我们决定
            if t == "error":
                failed_ = True
            if t == "stopped":
                # 单步内部被停：把步数补齐后如实上报，不要伪装成正常收尾。
                # 带上 agent 内部已经累积的上下文，用户回看时能看到问的是什么。
                halted_ = True
                yield {"type": "history", "messages": ev.get("history") or hist_}
                yield {"type": "stopped", "steps": step_idx + 1, "total": total,
                       "reason": ev.get("reason") or "用户停止"}
                return summary_, failed_, halted_, hist_
            yield ev
        return summary_, failed_, halted_, hist_

    for i, st in enumerate(plan):
        if stopped():
            # 先回吐已完成部分的上下文，再报停止，界面与台账都不会丢已干的活；
            # 首步就停时 hist 还是空的，至少把用户的原始要求放进去
            yield {"type": "history",
                   "messages": hist or [{"role": "user", "content": message}]}
            yield {
                "type": "stopped",
                "steps": i,
                "total": total,
                "reason": "用户停止",
            }
            return
        # 模型可能刚从设置里被删掉，退回池内第一个可用模型
        if st.model_key not in agent.cfg.models:
            st.model_key = entries[0]["key"]
            st.model_name = entries[0]["name"]

        agent.set_model(st.model_key)
        yield {
            "type": "assign",
            "index": i + 1,
            "total": total,
            "model_key": st.model_key,
            "model_name": st.model_name,
            "kind": st.kind,
            "kind_label": KIND_LABEL.get(st.kind, st.kind),
            "reason": st.reason,
            "title": st.title,
            "score": st.score,
            "multi": multi,
        }

        if i == 0:
            prompt = STEP_FIRST.format(
                message=message, total=total, title=st.title, detail=st.detail
            ) if multi else message
            relay = None
        else:
            if brief_relay:
                # 只带：总任务 + 本步要求 + 前几步的摘要与产出文件
                prompt = PROGRESS_TEMPLATE.format(
                    message=message, index=i + 1, total=total,
                    title=st.title, detail=st.detail,
                    done=_progress_lines(done),
                )
                relay = list(history)      # 之前几轮聊天的内容仍保留
            else:
                prompt = STEP_NEXT.format(
                    index=i + 1, total=total, title=st.title, detail=st.detail
                )
                relay = None
            yield {"type": "subtask", "index": i + 1, "total": total, "text": st.detail}

        files_before = len(file_log) if file_log is not None else 0
        summary, failed, halted, hist = yield from _step_events(i, prompt, relay)
        if halted:
            return

        # 多路径尝试（开关开启时）：这一步走不通，先让模型拟几条互不相同的
        # 实现路径，再逐条真跑，直到某条成功或试完。拟路径失败（模型没产出
        # 可用条目）就退回内置的三条通用路径。模型不变，变的是手段。
        if failed and multi_path:
            yield {"type": "multipath", "index": i + 1, "total": total, "phase": "plan"}
            approaches = _propose_approaches(agent, f"{st.title}：{st.detail}")
            if not approaches:
                approaches = FALLBACK_APPROACHES
            for ap_i, ap in enumerate(approaches, 1):
                yield {"type": "multipath", "index": i + 1, "total": total,
                       "phase": "try", "approach_index": ap_i,
                       "approach_total": len(approaches), "approach": ap}
                retry_prompt = (
                    prompt + f"\n\n[换路重试 · 方案 {ap_i}/{len(approaches)}] "
                    "上一条路径没能完成这一步。这次按下面这条实现路径来做"
                    "（其余要求不变；若该路径执行中行不通，如实说明卡在哪）：\n" + ap)
                summary, failed, halted, hist = yield from _step_events(
                    i, retry_prompt, relay)
                if halted:
                    return
                if not failed:
                    break

        # 任务级闸门（流程第 6 步）：多步任务每步一收尾就核验该步任务书
        # （st.detail）里声明的验证命令与交付物，失败当场拦截修正——真实输出
        # 回灌给同一 agent 继续做，至多三次执行。单任务不在此拦（detail 就是
        # 原始消息，收尾闸门把守即可，避免同一命令验两遍）；总任务级的声明
        # 仍由最后的收尾闸门把守。
        if multi and not failed:
            step_book = st.detail or message
            step_snap = verify_gate.snapshot_artifacts(
                step_book, agent.cfg.workspace_root)
            step_holder = {"summary": summary or _last_assistant(hist),
                           "convo": hist}
            for ev in _verify_gate(agent, step_book, step_holder,
                                   auto_approve=auto_approve, stop_check=stopped,
                                   read_cache=read_cache, snapshot=step_snap,
                                   exhausted=exhausted_cmds):
                k = ev.get("type")
                if k == "assistant":
                    hist = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            exhausted_cmds.update(step_holder.get("exhausted_cmds") or ())

        # 记下这一步的摘要与产出，供下一步的简报使用
        new_files: list[str] = []
        if file_log is not None and len(file_log) > files_before:
            for rec in file_log[files_before:]:
                p = str(rec.get("path") or "")
                if p and p not in new_files:
                    new_files.append(p)
        done.append({
            "index": i + 1,
            "title": st.title,
            "summary": summary or _last_assistant(hist),
            "files": new_files,
        })

        if failed:
            return

    # 收尾验证闸门：验证命令系统亲自跑，谎报在这里现形（见 _verify_gate）
    holder = {"summary": summary, "convo": hist}
    yield from _verify_gate(agent, message, holder,
                            auto_approve=auto_approve, stop_check=stopped,
                            read_cache=read_cache, snapshot=gate_snap,
                            exhausted=exhausted_cmds)
    if holder["summary"] != summary:
        summary, hist = holder["summary"], holder["convo"]
        yield {"type": "assistant", "content": summary, "history": hist}

    yield {"type": "complete", "steps": total, "multi": multi}


__all__ = ["run_task", "run_multi_agent"]
