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
import time
from typing import Any, Iterator

from pathlib import Path

from .loop import Agent
from .planner import (

    KIND_DIM, KIND_LABEL, Subtask, kind_of, looks_like_change, make_plan,
    needs_plan, plan_summary,
)
from .evidence import EvidenceLedger
from .readcache import ReadCache
from . import artifact
from . import gaps
from . import interact
from . import honesty_guard
from .tasksession import TaskSession, batch_dependency_reason, contract_block, contract_paths
from . import runcontrol
from . import steering
from . import sources as sources_gate
from . import skeleton as skel
from . import verify as verify_gate
from . import warmup as warmup_gate

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


def _norm_goal(goal: str) -> str:
    """小步骤去重用的归一化：去所有空白、转小写、截断，避免同一意图因措辞/换行
    细微差异被当成不同步骤反复重排（复测里 Ollama 开局不稳时一批 500 的分析步
    就这样被 R2–R5 反复重排，纯烧时长）。"""
    return re.sub(r"\s+", "", str(goal or "")).casefold()[:200]


def _plan_local_keys(plan, agent) -> list[str]:
    """本次任务**真正会用到的**本地模型 key——健康门禁只该探这些。

    为什么不能拿整个模型池去探：探活一个本地大模型 = 一次真实生成 = 一次权重
    加载。20B 权重十几秒，而且一装就把刚驻留的别的模型挤出显存——门禁把池子
    挨个探一遍的结果是：用户白等 30 秒，最后任务要用的那个模型还得再加载一次。
    门禁的本意是「我要用的模型能不能生成」，所以探的集合就该是计划里的模型。
    """
    out: list[str] = []
    seen: set[str] = set()
    for st in plan or []:
        key = getattr(st, "model_key", "") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            if getattr(agent.cfg.model(key), "kind", "local") == "local":
                out.append(key)
        except Exception:  # noqa: BLE001  模型配置查不到就跳过，宁可少探不炸
            continue
    return out


def ollama_health_gate(agent, model_keys: list[str], stop_check=None):
    """开局健康门禁：对每个本地模型做最小生成探活，失败按退避重试；全部就绪才放行，
    否则返回错误描述（调用方据以优雅中止任务，而不是硬冲进主流程被连环 500 拖垮）。

    是个生成器：边探边 yield health 事件给前端展示，最终用 return 传出错误字符串
    （None 表示通过）。探活本身复用 OllamaClient.warmup（含 5xx 退避重试）。
    """
    client = agent.client
    budget = float(getattr(agent.cfg, "ollama_warmup_timeout", 90))
    if budget <= 0 or not model_keys:
        return None
    yield {"type": "health", "phase": "start",
           "detail": f"开局健康门禁：准备探活 {len(set(model_keys))} 个本地模型"}
    deadline = time.time() + budget
    for key in sorted(set(model_keys)):
        try:
            spec = agent.cfg.model(key)
            model_name = spec.name
        except Exception:  # noqa: BLE001
            continue
        yield {"type": "health", "phase": "warmup", "model": model_name}
        # 探活必须带**与真实调用完全相同的加载参数**（num_ctx 等）。
        # Ollama 按「模型 + 影响加载的参数」缓存 runner：探活用默认 4K 窗口、
        # 真实调用用 32K，就是两个 runner——探活刚把模型读进显存，真实调用又
        # 得整个重读一遍。用户看到的「我已经打开了，怎么还在加载」就是这一下。
        opts = {}
        try:
            opts = spec.options()
        except Exception:  # noqa: BLE001
            pass
        ok = False
        last_err = ""
        while time.time() < deadline:
            if stop_check and stop_check():
                return "用户停止"
            ok, last_err = client.warmup(model_name, opts)
            if ok:
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(3.0, remaining))
        if ok:
            yield {"type": "health", "phase": "ready", "model": model_name}
        else:
            yield {"type": "health", "phase": "failed", "model": model_name,
                   "error": last_err}
            return (f"本地模型 {model_name} 开局探活失败：{last_err}"
                    f"（已等待约 {int(budget)}s）。任务中止——请确认 Ollama 已启动、"
                    f"该模型已加载（ollama pull / 重启 Ollama），再重试本任务。")
    return None


# T2（token 经济，docs/changes/token-economy-20261019.md）：极简单循环是**默认路径**。
# 「多个动作 / 长文本」不再触发多 agent 拆解——编号列表不等于要并行，任务书里的
# 「1. 逆向分析 A 2. 对比 B」让单 agent 顺序做完就行。rerun14 实测仪式开销：
# 11 个员工各付 7–9k 固定开销 + leader 6 轮判定（每轮 15–34k prompt），而其中
# 真正需要第二个 agent 并行的时刻为零。只有用户**明确**提出并行诉求才拆。
_PARALLEL_ASK_RX = re.compile(
    r"并行|同时跑|多路|分头|双agent|双agent|多agent|多agent|两个?agent|"
    r"两个?Agent|泳道|多线程执行|分开同时", re.I)


def _wants_parallel(message: str) -> bool:
    """用户是否明确要求并行（默认开启拆步的信号）。"""
    return bool(_PARALLEL_ASK_RX.search(message or ""))


def _orchestration_wanted(message: str, cfg) -> bool:
    """本次请求要不要走编排（拆步）。三档：

    - auto（默认）：只有明确并行诉求才拆——极简单循环是默认路径；
    - always：旧行为，多动作/长文即拆（评测对照用）；
    - never：永不拆（单循环压测用）。
    """
    mode = str(getattr(cfg, "orchestration", "auto") or "auto").lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return _wants_parallel(message)


# 停机拦截的连续上限（Claude Code Stop hook 同款机制）：验证失败最多拦这么多轮，
# 每轮结束重新亲跑验证。Claude Code 默认 8、社区实践常用 4；本地评测批量跑，
# 每轮拦截都是完整的 agent 循环（分钟级），取 2 兼顾纠错机会与跑批墙钟。
_VERIFY_BLOCK_ROUNDS = 2


def _file_records(paths: list[str], root) -> list[dict]:
    """把一串文件路径翻成诚实守卫 / 前端要的产物描述（含 rel / exists / action）。

    写类工具产出的文件一律记为 created / tool / certain；前端 addFilesCard 据此
    把产出文件卡片挂在汇总结果下方，保证「汇总 ↔ 文件」对得上。
    """
    recs = []
    for p in paths or []:
        if not p:
            continue
        recs.append({"path": str(p), "action": "created", "via": "tool",
                     "certain": True, "exists": Path(p).exists()})
    return artifact.describe(recs, root)


def _read_artifact(path: str, cap: int = 2500) -> str | None:
    """读取文本文件内容（截断到 cap 字符）；非文本 / 过大 / 二进制返回 None。"""
    p = Path(path)
    try:
        if not p.is_file():
            return None
        if p.stat().st_size > 200_000:
            return None
        text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    if "\x00" in text[:2000]:          # 二进制文件，不当成可内联的内容
        return None
    return text[:cap]


def _verify_gate(agent, message: str, holder: dict,
                 auto_approve=None, stop_check=None, read_cache=None,
                 snapshot: dict[str, float] | None = None,
                 exhausted: set[str] | None = None,
                 files: list[dict] | None = None,
                 produced_paths: list[str] | None = None,
                 task_start_ts: float | None = None,
                 file_log: list | None = None):
    """收尾验证闸门（系统侧强制）：模型只有解读权，没有裁定权。

    - 任务书里声明了验证命令（如「验证：python tools/x.py」）→ 程序亲自运行，
      真实结果通过 verify 事件播报；失败时给模型一轮修正机会（可修可如实说明），
      之后无论模型总结说什么，都会在答复末尾附上程序写的系统侧验证报告。
    - 独立第二道核查：任务书声明的交付文件与总结声称「已写入 X」的文件，
      系统核查三类问题——不存在（missing）/ 本轮未更新（stale，mtime 与任务
      开始快照一致，吃老本）/ 幽灵声明（ghost，全程零写操作却说已写入）；
      命中给一轮修正机会，复查仍不过就在答复末尾附上系统侧核查声明。
      snapshot 由调用方在任务开始时用 verify_gate.snapshot_artifacts 生成。
    - produced_paths 是「本任务真实产出过的文件」权威清单（来自执行轨迹，解析
      绝对路径匹配）：只要某个声称/声明文件在其中，就不许误判为 ghost——
      收尾闸门 convo 只有 [任务原文, 拟回答]、ran_writes 缺位时，这是避免
      验证竞态误判真实交付物「全程零写操作」的关键依据。

    holder 进出各带 {"summary", "convo"}：闸门可能改写总结（追加报告/被修正轮
    重写），调用方从 holder 里取最终值。
    """
    cfg = agent.cfg
    # P28：验证命令的 cwd = 交付区 output/（种子与交付物都在），没有产出目录
    # （纯问答/老流程）退回工作区根。
    from pathlib import Path as _Path
    _root = _Path(str(cfg.workspace_root))
    _deliver = (_root / agent.outdir / "output"
                if getattr(agent, "outdir", "") else _root)
    if not getattr(cfg, "verify_gate", True):
        return
    # 证据台账跨修正轮共用：首轮读过的文件，第二轮引用时仍算「读过」。
    # 不共用的话每轮 run() 都会重置台账，引用核查与写前门槛在修正轮里
    # 恒判「本任务从未 read_file 过」——rerun27 三轮读同一批文件、零交付。
    evidence = getattr(getattr(agent, "guard", None), "ledger", None)
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
        res = verify_gate.run_verify_cmd(cmd, _deliver)
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
            # 修正轮会整段重写最终答复：告诉前端「上一版作废」。
            # 不通知的话界面把两版拼成一段，而回看（台账 history）只有最终版。
            yield {"type": "gate_discard", "reason": "corrective"}
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache, ledger=evidence,
                                file_log=file_log):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            # 修正轮结束后复核：重新亲跑，通过就放行，仍失败且没到拦截上限
            # 就再拦一轮（第 _VERIFY_BLOCK_ROUNDS 轮后放行，报告照附）。
            res = verify_gate.run_verify_cmd(cmd, _deliver)
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
        # 写盘末次失败时，自验报告不许说「以本结果为准」——命令是在没改成的
        # 文件上跑的，一样能过（P3）。判定材料是 harness 自己印的权威块。
        write_failed = [p for p, ok in verify_gate.write_outcomes(convo).items() if not ok]
        notes.append(verify_gate.report_block(cmd, res, write_failed=write_failed))
    # 【设计原则·2026-10-15】删除原「声称验证通过但未调用 run_command → 追加『未经系统确认』」
    # 的分支。闸门只判定产物是否正确（上面的验证命令分支）与产物/声明的真实性
    # （下面几道核查）；是否调用过 run_command 属于执行过程，与任务是否完成无关，
    # 不构成闸门的职责。任务成果已由上面的验证命令分支独立验收，无需再检验过程。

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
        # 任务书必填项：显式列举的「覆盖这些…：A、B、C」在骨架阶段就铺成了
        # 结构（skeleton.seed_from_brief），这里用同一份清单判「该写的写了没有」。
        # 提取不到就不传（None），退回纯存在性/时效性核查，不猜。
        required = skel.required_items(message) or None
        issues = verify_gate.find_artifact_issues(
            entries, cfg.workspace_root, snapshot=snapshot, produced=produced,
            produced_paths=produced_paths, task_start_ts=task_start_ts,
            required=required)
        if issues and not corrected:
            # 修正前先记下这些文件的 mtime：非内容类问题（missing/stale/ghost）
            # 复查就看它本轮有没有真的变
            pre = {c["path"].lower(): verify_gate.path_mtime(c["path"], cfg.workspace_root)
                   for c in issues}
            prompt = verify_gate.ARTIFACT_CORRECTIVE_PROMPT.format(
                issues="\n".join(verify_gate.issue_line(c) for c in issues),
                summary=summary or "（无）",
            )
            # 修正轮会整段重写最终答复：告诉前端「上一版作废」。
            # 不通知的话界面把两版拼成一段，而回看（台账 history）只有最终版。
            yield {"type": "gate_discard", "reason": "corrective"}
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache, ledger=evidence,
                                file_log=file_log):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            issues = [c for c in issues
                      if not verify_gate.artifact_fixed(c, pre, cfg.workspace_root)]
            # 缺项（incomplete）例外：它是**内容判定**，mtime 变了不算修好——
            # 补了两行、还缺两项的时候 mtime 早就不一样了，凭时间放行等于把
            # 覆盖核查退化成「写过就算」。这一类重算覆盖，重算的成本是读一个文件。
            if any(c.get("status") == "incomplete" for c in issues):
                fresh = verify_gate.find_artifact_issues(
                    entries, cfg.workspace_root, snapshot=snapshot, produced=produced,
                    produced_paths=produced_paths, task_start_ts=task_start_ts,
                    required=required)
                still = {c["path"].lower() for c in fresh
                         if c.get("status") == "incomplete"}
                issues = [c for c in issues
                          if c.get("status") != "incomplete"
                          or c["path"].lower() in still]
        if issues:
            yield {"type": "verify", "cmd": "", "ok": False, "exit": "",
                   "duration": "",
                   "artifacts": [{"path": c["path"], "declared": c["declared"],
                                  "status": c["status"]} for c in issues]}
            notes.append(verify_gate.artifact_issue_block(issues))
            # 未交付硬闸（rerun26 事故）：声明交付物复核后仍缺失，且整条轨迹
            # **一次写类动作都没有**——这不是「写错地方」，是压根没动手。
            # 旧行为只附一段声明就判成功，模型连写 7 轮「下一步计划」也能过。
            # 只认这一种确定情形：全程零写盘 + 全部缺失，避免误伤「写了但没到位」。
            # 不用 looks_like_change：它只认「改代码」的活，写报告类任务会判 False，
            # 而 rerun26 恰恰就是报告任务。
            # 只认两种确定情形：①全程零写盘 + 全部缺失（压根没动手）；
            # ②写了骨架但正文一节没填（看上去动手了，交付物却是空壳）。
            # 其余「写了但没写到点子上」仍只提示，避免误伤。
            statuses = {c.get("status") for c in issues}
            # 「动过手」必须是**写成功过**，不是「调用过写类工具」。rerun27：
            # 两次 write_file 全被核查拒绝（引用路径错 / 写前门槛），旧判据
            # produced_possible 只看轨迹里出现过写类调用，于是放过硬闸——
            # 任务状态照旧记 done，交付物一个字都没有。
            # run_command 仍算「可能产出文件」，避免误伤脚本生成类任务。
            untouched = not verify_gate.wrote_ok(convo)
            skeleton_only = ("skeleton" in statuses
                             and statuses <= {"missing", "skeleton"})
            if (getattr(cfg, "delivery_hard_gate", True)
                    and statuses <= {"missing", "skeleton"}
                    and (untouched or skeleton_only)):
                holder["delivery_failed"] = [c["path"] for c in issues]
                notes.append(verify_gate.delivery_failed_block(
                    [c["path"] for c in issues],
                    skeleton_only=bool(skeleton_only and not untouched)))

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
                yield {"type": "gate_discard", "reason": "corrective"}
                for ev in agent.run(convo, prompt, auto_route=False,
                                    stop_check=stop_check, auto_approve=auto_approve,
                                    read_cache=read_cache, ledger=evidence,
                                file_log=file_log):
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
        # 本轮程序已确认的产出文件不算「对现状的陈述」（模型自己刚写的），
        # 把 rel 路径拼进排除文本，避免「已创建 spider.py 并验证」被当成编造。
        produced = "\n".join(
            str(f.get("rel") or f.get("path") or "") for f in (files or []) if f
        )
        hits = sources_gate.needs_local_note(summary, convo, message,
                                             produced=produced)
        if hits and not corrected:
            corrected = True
            prompt = sources_gate.LOCAL_FACTS_CORRECTIVE_PROMPT.format(
                tokens="\n".join(f"- {t}" for t in hits),
                summary=summary or "（无）",
            )
            # 修正轮会整段重写最终答复：告诉前端「上一版作废」。
            # 不通知的话界面把两版拼成一段，而回看（台账 history）只有最终版。
            yield {"type": "gate_discard", "reason": "corrective"}
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache, ledger=evidence,
                                file_log=file_log):
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

    # 第六道核查（通用诚实守卫）：基于执行轨迹的证据比对，而非枚举能力。
    # 判定只问两件事——① 产物 / 动作是否真实（文件真写出来 / 命令真跑成）；
    # ② 没做成时有没有坦白。任务未达成却把锅甩给用户 / 假装做了 → 给一轮修正机会
    # 逼其如实说明（覆盖 h3「发布到 npm」甩锅这类旧 preflight 枚举不到的情形）。
    # 与上面几道同源：模型只有解读权，没有裁定权；判定与任务类型 / 数量无关，可扩展。
    if getattr(cfg, "honesty_gate", True):
        hres = honesty_guard.assess(message, summary, convo, files or [], cfg)
        if hres["verdict"] == "force_confess" and not corrected:
            corrected = True
            prompt = honesty_guard.corrective_prompt(summary)
            yield {"type": "gate_discard", "reason": "corrective"}
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache, ledger=evidence,
                                file_log=file_log):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            # 修正后再判一次；仍不坦白才在答复末尾附上系统侧诚实声明
            hres2 = honesty_guard.assess(message, summary, convo, files or [], cfg)
            if hres2["verdict"] == "force_confess":
                notes.append(
                    "（系统侧诚实核查：任务未达成，已在最终答复中要求如实说明未达成的原因，"
                    "不得把责任甩回给用户。）")

    # 第七道核查（关键事实回显）：任务书点名「保留 X / 确保 Y / 写明 Z」时，
    # 程序提取其中的硬 token（引号内容 / 时间 / 日期 / 数字），比对答复是否包含。
    # 缺了给一轮修正；仍缺则在答复末尾附系统侧声明。治 w3 类「文件写对了、
    # 回复把关键事实丢了」——方案 B 保证信息在场，这里保证信息进答复。
    if getattr(cfg, "facts_gate", True):
        # 比对面扩到「已确认写成过的产物」（P1）：写盘类任务的主出口是文件，
        # 只比答复会把「文件里明明有」判成「未能核对到」（w5 误伤 5/31 轮）。
        art_paths = [str(f.get("path") or "") for f in (files or []) if f]
        art_paths += [str(p) for p in (produced_paths or [])]
        art_text = verify_gate.artifacts_text(art_paths, cfg.workspace_root)
        miss_facts = verify_gate.missing_required_facts(summary, message,
                                                        artifacts=art_text)
        if miss_facts and not corrected:
            corrected = True
            prompt = verify_gate.FACTS_CORRECTIVE_PROMPT.format(
                tokens="\n".join(f"- {t}" for t in miss_facts),
                summary=summary or "（无）",
            )
            # 修正轮会整段重写最终答复：告诉前端「上一版作废」。
            # 不通知的话界面把两版拼成一段，而回看（台账 history）只有最终版。
            yield {"type": "gate_discard", "reason": "corrective"}
            for ev in agent.run(convo, prompt, auto_route=False,
                                stop_check=stop_check, auto_approve=auto_approve,
                                read_cache=read_cache, ledger=evidence,
                                file_log=file_log):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            miss_facts = verify_gate.missing_required_facts(summary, message,
                                                            artifacts=art_text)
        if miss_facts:
            yield {"type": "verify", "cmd": "", "ok": False, "exit": "",
                   "duration": "", "missing_facts": miss_facts}
            notes.append(verify_gate.missing_facts_block(miss_facts))
        else:
            # 产物里有、答复没复述：提示一句就够，不再判缺失（P1 的口径）。
            unrestated = verify_gate.unrestated_facts(summary, message,
                                                      artifacts=art_text)
            if unrestated:
                notes.append(verify_gate.unrestated_facts_block(unrestated))

    # 程序侧声明统一在这里拼回正文末尾——无论中途经历几轮修正轮重写
    if notes:
        summary = (summary + "\n\n---\n" + "\n\n---\n".join(notes)).strip()
        # 台账 history 必须与界面显示同一个版本：把带声明的正文写回最后一条
        # **有正文的** assistant 消息。不写回的话，实时画面有这段声明、回看
        # （读 history）没有——同一个任务两副面孔（用户 2026-10-21 报的「切走
        # 再切回来内容不一样」正是这一类；打回 2 轮的声明在 loop.py 里本来就是
        # 写进正文的，这里只是把另一条路径对齐，不是新语义）。
        # 只认有正文的那条：纯 tool_calls 的 assistant 消息不是「答复」，把声明
        # 挂在它上面等于凭空多出一段正文。
        for i in range(len(convo) - 1, -1, -1):
            if convo[i].get("role") == "assistant" and str(convo[i].get("content") or "").strip():
                convo[i] = {**convo[i], "content": summary}
                break
        else:
            convo.append({"role": "assistant", "content": summary})

    if summary != holder.get("summary"):
        holder["summary"] = summary
        holder["convo"] = convo


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
    task_id: str = "",
) -> Iterator[dict[str, Any]]:
    """执行一次用户请求。store 提供当前启用的模型池。

    model_key 非空且不是 "auto" 时表示用户手动指定了模型：跳过自动分配，
    所有步骤钉在该模型上执行。

    preferred_key 是能力路由建议的「优先模型」：不视为手动钉定。

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
    # 复制/备份类任务的任务书补强（f4 教训）：harness 替用户补一条逐字节比对的
    # 验证声明。单任务路径里执行模型看到的任务书（book）与收尾闸门核对的保持
    # 同一份；拆步判断（needs_plan）与汇总用的仍是原始 message，
    # 不受补强影响。
    _copy_note = verify_gate.declare_copy_check(message)
    book = message + _copy_note if _copy_note else message
    # 产物核查的起点快照：任务书声明交付文件的 mtime 此刻定格，收尾时用它
    # 判断「本轮是否真的更新过」，上一轮的旧文件糊弄不了核查
    gate_snap = verify_gate.snapshot_artifacts(book, agent.cfg.workspace_root)
    # 任务开始墙钟：交给收尾闸门判定声明交付物是否本轮真实写出。
    task_start_ts = time.time()
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

    # 能力路由的自动升级只当作「优先模型」：它不等同于用户手动钉模型——
    # 只有手动钉（model_key）才跳过自动分配。
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

    # 只读共享缓存提前创建（写类工具执行后自动作废）
    read_cache = ReadCache()
    # 产出目录：统一收进 history/（如 history/20261017-213730），避免任务产出
    # 目录散落在项目根——中间/最终产物一律进这个
    # 文件夹，除非必须放特定位置才能生效。只对「真正动手干活」的任务设置；
    # 纯问答不占提示词字节。
    outdir = "history/" + time.strftime("%Y%m%d-%H%M%S")
    agent.outdir = outdir
    # 任务级操作记录（文件）：读过哪些文件的哪些行、每次写盘的全文版本、引用核查
    # 结论，全部 append-only 落在这个任务自己的目录里。它是「证据」的事实源，
    # 台账只是它的内存视图——所以修正轮、重启、乃至另一个进程，看到的都是同一份。
    # 记录由 harness 写，不需要模型有权限，也不会被算成任务的产出文件。
    try:
        from pathlib import Path as _P

        from . import layout as _layout
        from .record import Recorder

        # 过程记录放在 history/<任务>/record/（evidence / versions / citations），
        # 与最终产物 output/ 分开：复盘时「文件对不对」看 output，「过程有没有
        # 问题」看 record，不用在一个平铺目录里猜。
        agent.recorder = Recorder(_P(agent.cfg.workspace_root) / outdir / "record",
                                  rel_root=agent.cfg.workspace_root)
        # P28：交付区 output/ 是模型的主工作目录——发起时把种子拷进去（有界），
        # 之后模型读写、run_command、脚本 cwd 都在这里，任务结束整个目录即案卷。
        try:
            _layout.seed_output(_P(agent.cfg.workspace_root), outdir)
        except Exception:  # noqa: BLE001 —— 拷不全由读侧镜像的根回退兜底
            pass
    except Exception:  # noqa: BLE001 —— 记录不可用不该阻断任务
        agent.recorder = None

    # 任务书骨架（契约骨架）：任务书里显式列举的必填项——「至少覆盖这些…：A、B、C」、
    # 「末尾附一节「X」」——在任务开始就落成一个骨架文件，每个必填项一个待填位。
    # 这样缺项在**骨架阶段**就露（进度里直接是「还没填：会话恢复」），而不是等到
    # 收尾读一份「写得很顺但少两项」的成品（rerun29）。
    # 只在文件还不存在时铺；模型已经写了的路径一律不碰。
    for _rel in verify_gate.extract_declared_artifacts(message):
        if not str(_rel).lower().endswith((".md", ".markdown", ".txt")):
            continue
        try:
            _info = skel.seed_from_brief(agent.cfg.workspace_root, _rel, message)
        except Exception:  # noqa: BLE001 —— 铺骨架失败不该阻断任务
            _info = None
        if _info:
            yield {"type": "skeleton", "path": _info["path"], "kind": _info["kind"],
                   "items": _info["items"], "source": "brief"}

    # 任务开局预热注入：单模型/接力执行先给 agent 一份项目速览。开关关闭时
    # 置空串，不会把上一个任务残留的预热块带进本任务。
    warm_block, warm_stats, warm_reused = warmup_gate.build_once(
        agent.cfg, message, task_id=task_id)
    agent.warmup_block = warm_block
    # 续接追问（reused）复用原任务的工作区速览：不重扫、也不再播报一次「预热注入」。
    if warm_block and not warm_reused:
        yield {"type": "warmup", **warm_stats}

    plan: list[Subtask] = []
    # T2：接力拆步（单模型顺序多步）同样只在明确并行诉求时启用——默认一个
    # agent 从头做到尾，配合 T3 的工具结果老化与 ops_batch（PTC）控住 token。
    if auto_plan and _orchestration_wanted(message, agent.cfg) and needs_plan(message, len(entries)):
        plan = [s for s in make_plan(message, entries) if (s.detail or "").strip()]
        # 计划循环的任务书补强：复制/备份语义落在某一步时，给那步的 detail
        # 补逐字节比对声明（prompt 与步骤级闸门都用 detail，天然同份）
        for _st in plan:
            _note = verify_gate.declare_copy_check(_st.detail)
            if _note:
                _st.detail += _note

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

    # 单模型 / 计划接力路径过开局健康门禁。放在「选完模型之后」是刻意的：
    # 门禁要回答的是「本次要用的模型能不能生成」，不是「整张模型清单体检」。
    # 池子里多模型挨个探活，每个都要加载十几 GB 权重，还会把将要使用的模型
    # 挤出显存——那是纯等待（实测三个本地模型串着探 = 33 秒，比一次生成还贵）。
    gate_err = yield from ollama_health_gate(agent, _plan_local_keys(plan, agent), stopped)
    if gate_err:
        yield {"type": "error", "message": gate_err}
        return

    # 展示/回看用的全量历史（一路接力，内容完整）
    hist = list(history)
    # 只读共享缓存（跨步骤复用；写类工具执行后自动作废）
    # 接力省法：每一步单独记「做了什么、动了哪些文件」，下一步只看这份简报
    brief_relay = bool(getattr(agent.cfg, "relay_brief", True)) and multi
    done: list[dict] = []
    total = len(plan)
    # 任务级证据台账：本任务所有步骤、所有重跑共用一份。取证是任务级的动作，
    # 不该因为「换了步骤 / 换了模型重跑同一步」就当作没读过——否则第二步写
    # 交付物时，第一步读过的文件全被判「从未 read_file」，引用核查与写前
    # 门槛双杀（rerun27 的零交付就是这么来的）。
    # 带上 recorder：即使这条对象因为某种路径没被传下去，新建的台账也会从
    # evidence.jsonl 里把自己读回来（文件才是事实源）。
    task_ledger = EvidenceLedger(recorder=getattr(agent, "recorder", None))

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
                            relay=relay_, read_cache=read_cache, ledger=task_ledger,
                            steer_check=lambda: steering.take(task_id, f"step{step_idx}")):
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

    # 全任务真实产出清单（跨步骤累积）：收尾闸门用它修复验证竞态——new_files 每步
    # 重置，循环后只剩最后一步的文件，单独传给收尾闸门会漏掉之前步骤的产出。
    task_files: list[str] = []

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
            ) if multi else book
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

        # 小步骤标记：缺口台账要说明「当时在做哪一步」。多任务用该步任务书，
        # 单任务用原始消息（gaps.norm_step 剥掉「帮我/请」等客套前缀——
        # 「帮我在git检索REA」→「在git检索REA」）；多路径重试时会被下面的
        # 方案文本覆盖成更细的一步。
        gaps.set_step(st.detail or message)

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
                # 架构拟出的实现路径就是当前小步骤（如「先在git检索REA」）——
                # 这一步里发生的缺口登记，台账会记到这条路径上
                gaps.set_step(ap)
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

        # 本步真实产出的文件（来自全局 file_log 的本步增量），供步骤级闸门做
        # 验证竞态修复依据；必须在闸门之前算出来。
        new_files: list[str] = []
        if file_log is not None and len(file_log) > files_before:
            for rec in file_log[files_before:]:
                p = str(rec.get("path") or "")
                if p and p not in new_files:
                    new_files.append(p)
        # 累积进全任务清单（去重）
        for p in new_files:
            if p and p not in task_files:
                task_files.append(p)

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
                                   exhausted=exhausted_cmds,
                                   produced_paths=new_files,
                                   task_start_ts=task_start_ts,
                                   file_log=file_log):
                k = ev.get("type")
                if k == "assistant":
                    hist = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                yield ev
            exhausted_cmds.update(step_holder.get("exhausted_cmds") or ())

        # 记下这一步的摘要与产出，供下一步的简报使用
        done.append({
            "index": i + 1,
            "title": st.title,
            "summary": summary or _last_assistant(hist),
            "files": new_files,
        })

        if failed:
            return

    # 收尾验证闸门：验证命令系统亲自跑，谎报在这里现形（见 _verify_gate）。
    # 单任务路径用补强后的 book（与执行模型看到的一致）；多步任务各步已按
    # detail 各自核验过，这里仍守原始总任务。
    holder = {"summary": summary, "convo": hist}
    yield from _verify_gate(agent, message if multi else book, holder,
                            auto_approve=auto_approve, stop_check=stopped,
                            read_cache=read_cache, snapshot=gate_snap,
                            exhausted=exhausted_cmds,
                            produced_paths=task_files,
                            task_start_ts=task_start_ts,
                            file_log=file_log)
    if holder["summary"] != summary:
        summary, hist = holder["summary"], holder["convo"]
        yield {"type": "assistant", "content": summary, "history": hist}
    # 未交付硬闸：声明交付物一件没写、复核后仍不存在。程序给出终态，
    # 上层据此把任务标成未完成（不再由模型自己的话决定成败）。
    if holder.get("delivery_failed"):
        yield {"type": "delivery_failed", "missing": holder["delivery_failed"]}

    yield {"type": "complete", "steps": total, "multi": multi}


__all__ = ["run_task"]
