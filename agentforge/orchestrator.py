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

from .dispatcher import Dispatcher, TaskSpec, _catalog_text, _extract_json
from .leader import (
    LEADER_EVAL_SYSTEM, LEADER_PLAN_SYSTEM, LEADER_SEMANTIC_SYSTEM,
    LeaderContext, StepResult, parse_batch, parse_eval,
)
from .loop import Agent
from .planner import (

    KIND_DIM, KIND_LABEL, Subtask, kind_of, looks_like_change, make_plan,
    needs_plan, plan_summary,
)
from .readcache import ReadCache
from . import artifact
from . import gaps
from . import honesty_guard
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


def _norm_goal(goal: str) -> str:
    """小步骤去重用的归一化：去所有空白、转小写、截断，避免同一意图因措辞/换行
    细微差异被当成不同步骤反复重排（复测里 Ollama 开局不稳时一批 500 的分析步
    就这样被 R2–R5 反复重排，纯烧时长）。"""
    return re.sub(r"\s+", "", str(goal or "")).casefold()[:200]


def _local_model_keys(entries: list[dict], agent) -> list[str]:
    """从模型池里挑出本地（kind=local）模型的 key——健康门禁只对本地 Ollama 模型做
    最小生成探活；在线/付费模型由各自的 provider 负责存活，不在此探。"""
    out: list[str] = []
    for e in entries or []:
        try:
            if getattr(agent.cfg.model(e["key"]), "kind", "local") == "local":
                out.append(e["key"])
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
            model_name = agent.cfg.model(key).name
        except Exception:  # noqa: BLE001
            continue
        yield {"type": "health", "phase": "warmup", "model": model_name}
        ok = False
        last_err = ""
        while time.time() < deadline:
            if stop_check and stop_check():
                return "用户停止"
            ok, last_err = client.warmup(model_name)
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


def _worth_dispatch(message: str, pool_size: int) -> bool:
    """是否值得动用决策模型拆解：包含多个动作，或请求本身较长。"""
    return needs_plan(message, pool_size) or len((message or "").strip()) >= 60


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
                 task_start_ts: float | None = None):
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
        issues = verify_gate.find_artifact_issues(
            entries, cfg.workspace_root, snapshot=snapshot, produced=produced,
            produced_paths=produced_paths, task_start_ts=task_start_ts)
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
        miss_facts = verify_gate.missing_required_facts(summary, message)
        if miss_facts and not corrected:
            corrected = True
            prompt = verify_gate.FACTS_CORRECTIVE_PROMPT.format(
                tokens="\n".join(f"- {t}" for t in miss_facts),
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
            miss_facts = verify_gate.missing_required_facts(summary, message)
        if miss_facts:
            yield {"type": "verify", "cmd": "", "ok": False, "exit": "",
                   "duration": "", "missing_facts": miss_facts}
            notes.append(verify_gate.missing_facts_block(miss_facts))

    # 程序侧声明统一在这里拼回正文末尾——无论中途经历几轮修正轮重写
    if notes:
        summary = (summary + "\n\n---\n" + "\n\n---\n".join(notes)).strip()

    if summary != holder.get("summary"):
        holder["summary"] = summary
        holder["convo"] = convo


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


def run_leader(agent, store, entries: list[dict], history: list[dict],
               message: str, dispatcher: Dispatcher,
               stop_check=None, auto_approve: bool | None = None,
               file_log: list | None = None, read_cache: "ReadCache | None" = None,
               outdir: str = "") -> Iterator[dict[str, Any]]:
    """Leader 双环编排（第 47 步：七步架构更新，取代旧 run_multi_agent）。

    外环（Leader）：每轮只规划【下一批】小步骤（仅相互独立的才同批）→ 员工执行 →
    一次调用完成「完成判定 + 规划下一批 + 精简历史」→ 全部完成后出「拟回答」→
    步骤⑤验收（程序裁定 + Leader 语义判定）。语义不过则把失败原因压回固定槽位
    重新规划，重试上限由配置 leader.max_retries 控制；评估环轮次上限由
    leader.max_rounds 控制。

    内环（员工）：每个小步骤仍是一个完整 Agent 循环（多轮工具调用），但用户提示词
    只有 Leader 写的小步骤提示词——自包含操作信息、不含任务总目标叙事。

    上下文固定槽位（架构级保护）：任务原文与失败原因放独立槽位，历史精简只替换
    摘要槽，最新一批执行结果全量进 Leader 不截断——头部字节稳定，服务端上下文
    缓存可完整命中。

    事件与旧多 agent 路径兼容（dispatch/plan/assign/subtask/…/complete），
    另加 leader_round（每轮的规划/评估结论，前端以胶囊提示播报）。
    """
    stopped = stop_check or (lambda: False)
    cfg = agent.cfg
    max_rounds = max(1, int(getattr(cfg, "leader_max_rounds", 12)))
    max_retries = max(0, int(getattr(cfg, "leader_max_retries", 2)))

    full_access = auto_approve is True or bool(getattr(agent.guard, "auto_approve_all", False))
    parallel_on = bool(getattr(cfg, "dispatcher_parallel", True)) and full_access
    local_gate = threading.Semaphore(1) if getattr(cfg, "local_serial", True) else None

    shared_log: list = file_log if file_log is not None else []
    log_lock = threading.Lock()
    sink: dict[str, dict] = {}      # step_id -> {"files","failed","summary"}
    read_cache = read_cache or ReadCache()

    weak_cn = {e["key"]: int((e.get("scores") or {}).get("chinese", 100)) for e in entries}
    translate_on = bool(getattr(cfg, "dispatcher_translate", True))
    cn_threshold = int(getattr(cfg, "dispatcher_bridge_threshold", 55))

    root = cfg.workspace_root
    snap = verify_gate.snapshot_artifacts(message, root)
    # 任务开始墙钟：交给收尾闸门判定声明交付物是否本轮真实写出。必须在任何写操作
    # 之前定格——闸门据此判定「mtime 晚于任务开始即算本轮产出」，彻底修掉验证竞态
    # （声明交付物被误判 stale：最终 convo 只有 [原文, 拟回答]、快照 mtime 与开局一致）。
    task_start_ts = time.time()

    ctx = LeaderContext(message)
    catalog = _catalog_text(entries)
    exhausted_cmds: set = set()     # 任务级闸门拦到上限仍失败的命令，汇总级不重跑
    system_notes: list[str] = []    # 系统侧声明（轮次到顶/语义验收未过等），随答复如实附出
    all_steps: list[TaskSpec] = []
    # 失败去重状态（步骤④防抖动）：同一小步骤（按归一化目标去重）反复失败时，
    # 最多重排执行 leader_step_retry_cap 次就标记为「已放弃」，不再让 Leader 把它
    # 无限重排进后续批次；已成功的步骤也不重复执行。复测里 Ollama 开局不稳时，
    # 一批 500 的分析步被 R2–R5 反复重排，纯烧时长——门禁把它收敛掉。
    step_fails: dict[str, int] = {}         # norm_goal -> 失败次数
    step_tried: dict[str, int] = {}         # norm_goal -> 尝试次数（含成功）
    step_done: set[str] = set()             # 已成功完成的归一化目标
    abandoned: set[str] = set()             # 已达上限放弃重排的目标
    owner_task = runcontrol.current_task()

    def spawn():
        ag = Agent(cfg, agent.client, agent.registry, agent.guard,
                   agent.router, skill_registry=agent.skills, hub=agent.hub)
        ag.outdir = outdir          # 产出目录规则随任务下发到每个员工
        return ag

    def _join(msgs: list[dict]) -> str:
        return "\n\n".join(m["content"] for m in msgs)

    def _fallback_step(rno: int) -> TaskSpec:
        """Leader 规划不可用时的保底：原文整段交给该维度最强的员工直做。"""
        kind = kind_of(message)
        dim = KIND_DIM.get(kind, "reasoning")
        best = max(entries, key=lambda e: (e["scores"].get(dim, 0), e.get("total", 0)))
        return TaskSpec(
            id=f"lf{rno}", goal=message, kind=kind, title=_short(message),
            model_key=best["key"], model_name=best["name"], model_id=best["id"],
            score=int(best["scores"].get(dim, 0)),
            reason="Leader 规划不可用，退回原文整段直做（该维度最强模型）")

    # ---------- 员工执行（内环）：一个小步骤一个完整 agent 循环 ----------
    def run_one(t: TaskSpec, ag, batch_total: int) -> Iterator[dict]:
        nonlocal_round = ctx.round
        yield {"type": "assign", "index": t.index, "total": batch_total,
               "model_key": t.model_key, "model_name": t.model_name,
               "kind": t.kind, "kind_label": KIND_LABEL.get(t.kind, t.kind),
               "reason": t.reason, "title": t.title, "score": t.score,
               "multi": True, "task_id": t.id, "round": nonlocal_round,
               "reassigned": bool(getattr(t, "reassigned", False)),
               "role": f"{KIND_LABEL.get(t.kind, t.kind)} 员工"}
        # 能力桥接（沿用旧语义）：执行模型中文很弱、步骤又是中文时先译成英文
        prompt = t.goal
        bridged = False
        if (translate_on and _has_cjk(prompt)
                and weak_cn.get(t.model_key, 100) < cn_threshold):
            en = dispatcher.translate(prompt, "English")
            if en and en != prompt:
                prompt = en
                bridged = True
                yield {"type": "bridge", "task_id": t.id, "to": "English",
                       "reason": f"{t.model_name} 中文能力较弱，已把步骤译成英文"}
        yield {"type": "subtask", "index": t.index, "total": batch_total,
               "text": t.goal, "task_id": t.id, "round": nonlocal_round}
        ag.set_model(t.model_key)
        local = _is_local_model(agent, t.model_key)
        gate = local_gate if (local and local_gate is not None) else None
        if gate is not None:
            gate.acquire()
        local_log: list = []
        summary = ""
        failed = False
        convo: list[dict] = []
        last_log_len = len(local_log)
        try:
            for ev in ag.run(convo, prompt, auto_route=False, stop_check=stopped,
                             auto_approve=auto_approve, file_log=local_log,
                             read_cache=read_cache):
                k = ev.get("type")
                if k in ("user", "route"):
                    continue
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip()
                if k == "error":
                    # 员工自己的失败只属于这个步骤，不拖垮同批其它步骤
                    failed = True
                    yield {"type": "subtask_error", "task_id": t.id,
                           "message": ev.get("message") or ""}
                    continue
                if k == "stopped":
                    yield {"type": "history",
                           "messages": ev.get("history") or convo}
                    yield {"type": "stopped", "steps": nonlocal_round,
                           "total": max_rounds,
                           "reason": ev.get("reason") or "用户停止"}
                    return
                if k == "tool_result":
                    # 工作汇报：员工产出新文件时向 leader/前端汇报阶段成果，
                    # 不必等整批跑完——leader 据汇报可提前收敛或只补剩余小步骤。
                    if len(local_log) > last_log_len:
                        new_files = [r.get("path") for r in local_log[last_log_len:]
                                     if r.get("path")]
                        last_log_len = len(local_log)
                        if new_files:
                            yield {"type": "progress", "task_id": t.id,
                                   "round": nonlocal_round, "files": new_files,
                                   "note": f"阶段成果（{t.title}）："
                                           f"{', '.join(new_files[:5])}"}
                if k in ("delta", "thinking", "tool_start", "tool_result",
                         "stats", "step"):
                    ev = {**ev, "task_id": t.id}
                yield ev
        finally:
            if gate is not None:
                gate.release()
        # 程序裁定前置到每个小步骤收尾（步骤⑤的程序部分）：任务书 = Leader 的
        # 小步骤提示词；验证命令亲跑、交付物核查，失败当场拦回同一员工修正
        if not failed:
            task_snap = verify_gate.snapshot_artifacts(prompt, root)
            holder = {"summary": summary or _last_assistant(convo), "convo": convo}
            step_paths = [r.get("path") for r in local_log if r.get("path")]
            task_files = _file_records(step_paths, root)
            for ev in _verify_gate(ag, prompt, holder, auto_approve=auto_approve,
                                   stop_check=stopped, read_cache=read_cache,
                                   snapshot=task_snap, exhausted=exhausted_cmds,
                                   files=task_files, produced_paths=step_paths,
                                   task_start_ts=task_start_ts):
                k = ev.get("type")
                if k == "assistant":
                    convo = ev["history"]
                    summary = (ev.get("content") or "").strip() or summary
                if k in ("delta", "thinking", "tool_start", "tool_result",
                         "stats", "step", "verify"):
                    ev = {**ev, "task_id": t.id}
                yield ev
        if bridged and summary and not _mostly_chinese(summary):
            zh = dispatcher.translate(summary, "Chinese")
            if zh:
                summary = zh
        files_out: list[str] = []
        for rec in local_log:
            p = str(rec.get("path") or "")
            if p and p not in files_out:
                files_out.append(p)
        if local_log:
            with log_lock:
                shared_log.extend(local_log)
        sink[t.id] = {"files": files_out, "failed": failed,
                      "summary": summary or _last_assistant(convo)}
        yield {"type": "task_done", "task_id": t.id, "index": t.index,
               "total": batch_total, "status": "error" if failed else "done",
               "title": t.title, "summary": sink[t.id]["summary"],
               "files": files_out, "round": nonlocal_round}

    # ---------- 一批的执行：并行（各任务一线程）或串行 ----------
    def run_wave(wave: list[TaskSpec]) -> Iterator[dict]:
        q: queue.Queue = queue.Queue()
        done_marker = object()

        def worker(t: TaskSpec) -> None:
            runcontrol.set_current_task(owner_task)
            try:
                ag = spawn()
                for ev in run_one(t, ag, len(wave)):
                    q.put(ev)
            except Exception as e:  # noqa: BLE001  单个步骤崩了不拖垮同批
                q.put({"type": "subtask_error", "task_id": t.id,
                       "message": f"{t.title}：{type(e).__name__}: {e}"})
                sink.setdefault(t.id, {"files": [], "failed": True, "summary": ""})
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
                    ev = q.get(timeout=0.2)
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

    def conflict_events(wave: list[TaskSpec]):
        owner: dict[str, str] = {}
        for t in wave:
            for p in (sink.get(t.id, {}).get("files") or []):
                if p in owner and owner[p] != t.id:
                    yield {"type": "conflict", "path": p,
                           "tasks": [owner[p], t.title],
                           "note": "两个并行步骤都改了同一个文件，已按先后顺序串行写入，请核对最终内容"}
                else:
                    owner[p] = t.id

    def _round_note(base: str, rno: int, batch, results) -> str:
        """Leader 没产出摘要时的程序兜底：把本批压成几行要点追加进摘要槽。"""
        lines = [base] if base else []
        for t, r in zip(batch, results):
            st = "失败" if r.failed else "完成"
            extra = ("；产出：" + "、".join(r.files[:5])) if r.files else ""
            reply = (r.reply or r.error or "").replace("\n", " ")[:200]
            lines.append(f"- 第{rno}批·{t.title}（{st}）{extra}：{reply}")
        return "\n".join(lines)[-4000:]

    # ================= 主循环：外环重试（步骤⑤）× 内轮评估环（步骤④） =================
    # 开局健康门禁：先对每个本地模型做最小生成探活，Ollama 开局不稳（冷启动/显存争用/
    # 刚重启）时能在进主流程前就拦下，避免被连环 500 拖垮几十分钟。任一模型探活不过
    # 就优雅中止并给出清晰错误，而不是硬冲。
    gate_err = yield from ollama_health_gate(agent, _local_model_keys(entries, agent), stopped)
    if gate_err:
        yield {"type": "error", "message": gate_err}
        return

    attempts = 0
    accepted = False
    final_answer = ""
    while True:                                  # 步骤⑤失败重试环
        done_answer = ""
        first_batch = ctx.round == 0
        # ---------- 步骤④：评估环（规划 → 执行 → 判定+规划+精简） ----------
        while ctx.round < max_rounds:
            ctx.add_round()
            round_no = ctx.round
            # ---- 步骤②：Leader 规划本批 ----
            try:
                raw = dispatcher._chat(
                    LEADER_PLAN_SYSTEM.format(catalog=catalog),
                    _join(ctx.plan_messages()),
                    num_predict=2048, temperature=0.2)
                batch, rationale = parse_batch(_extract_json(raw), entries,
                                               resolver=dispatcher._resolve,
                                               start_index=len(all_steps) + 1)
            except Exception as e:  # noqa: BLE001  Leader 挂了不拖垮任务
                batch, rationale = [], ""
                yield {"type": "leader_round", "round": round_no, "phase": "plan",
                       "note": f"Leader 规划调用失败（{type(e).__name__}: {e}），本批退回原文整段直做"}
            if not batch:
                if not rationale:
                    pass
                batch = [_fallback_step(round_no)]
                yield {"type": "leader_round", "round": round_no, "phase": "plan",
                       "note": "Leader 未能产出可解析的批次规划，本批退回原文整段直做"}
            # 复制/备份类步骤的程序侧补强（f4 教训兜底，与旧路径同一道工序）
            for t in batch:
                note = verify_gate.declare_copy_check(t.goal)
                if note:
                    t.goal += note
            # 模型可能刚从设置里被删：退回池内第一个可用模型
            for t in batch:
                if t.model_key not in cfg.models:
                    e0 = entries[0]
                    t.model_key, t.model_name, t.model_id = e0["key"], e0["name"], e0["id"]
                    t.reason = "原指派模型已被移除，改用池内第一个可用模型"
            # ---- 失败去重（步骤④防抖动）----
            # 同一小步骤（按归一化目标去重）反复 500/失败，最多重排执行 cap 次即放弃，
            # 不再让 Leader 把它无限重排进后续批次；已成功的步骤也跳过重做。
            cap = max(1, int(getattr(cfg, "leader_step_retry_cap", 2)))
            kept = []
            for t in batch:
                ng = _norm_goal(t.goal)
                if ng in step_done:
                    continue                      # 已成功，不重复执行
                if step_fails.get(ng, 0) >= cap:
                    if ng not in abandoned:       # 首次放弃：记一笔，并让 Leader 别再规划它
                        abandoned.add(ng)
                        ctx.add_reasons(
                            [f"步骤「{t.title or t.goal[:30]}」已失败 {cap} 次，放弃重试，不再重排；"
                             f"请换一种手段或绕过它"])
                    continue
                kept.append(t)
            batch = kept
            if not batch:
                batch = [_fallback_step(round_no)]
                yield {"type": "leader_round", "round": round_no, "phase": "plan",
                       "note": "本批步骤均已失败/完成，退回原文整段直做兜底"}
            all_steps.extend(batch)
            # ---- 事件：首轮 dispatch（并行泳道靠它），每轮 plan 卡 ----
            if first_batch:
                yield {"type": "dispatch",
                       "tasks": [t.as_dict() for t in batch],
                       "rationale": rationale, "source": "llm",
                       "dispatcher": dispatcher.name(), "multi": True,
                       "parallel": parallel_on and len(batch) > 1,
                       "local_serial": bool(getattr(cfg, "local_serial", True))}
            else:
                yield {"type": "leader_round", "round": round_no, "phase": "plan",
                       "note": rationale or "继续规划下一批"}
            yield {"type": "plan", "subtasks": [t.as_dict() for t in batch],
                   "summary": f"Leader 第 {round_no} 批"
                              + (f"：{rationale}" if rationale else "")}
            # ---- 步骤③：员工执行（并行/串行） ----
            if parallel_on and len(batch) > 1:
                yield from run_wave(batch)
            else:
                for t in batch:
                    for ev in run_one(t, agent, len(batch)):
                        yield ev
                        if ev.get("type") == "stopped":
                            return
            yield from conflict_events(batch)
            if stopped():
                yield {"type": "history",
                       "messages": history or [{"role": "user", "content": message}]}
                yield {"type": "stopped", "steps": round_no, "total": max_rounds,
                       "reason": "用户停止"}
                return
            # ---- 失败去重计数：本批每步的尝试/失败累计（供下一批过滤）----
            for t in batch:
                ng = _norm_goal(t.goal)
                step_tried[ng] = step_tried.get(ng, 0) + 1
                s = sink.get(t.id, {})
                if s.get("failed"):
                    step_fails[ng] = step_fails.get(ng, 0) + 1
                else:
                    step_done.add(ng)            # 成功过一次即视为已完成，不重复执行
            # ---- 步骤④：Leader 评估（判定 + 规划 + 精简，一次调用） ----
            results = []
            for t in batch:
                s = sink.get(t.id, {})
                results.append(StepResult(
                    title=t.title, kind=t.kind, prompt=t.goal,
                    reply=s.get("summary") or "",
                    files=s.get("files") or [],
                    failed=bool(s.get("failed")),
                    error="（执行失败）" if s.get("failed") else ""))
            parsed: dict = {}
            try:
                raw = dispatcher._chat(
                    LEADER_EVAL_SYSTEM.format(catalog=catalog),
                    _join(ctx.eval_messages(results)),
                    num_predict=4096, temperature=0.2)
                parsed = parse_eval(_extract_json(raw))
            except Exception as e:  # noqa: BLE001
                parsed = {"done": False, "answer": "", "note": "",
                          "reasons": [f"Leader 评估调用失败：{type(e).__name__}: {e}"],
                          "raw_batch": None}
            if not parsed:
                parsed = {"done": False, "answer": "", "note": "",
                          "reasons": ["Leader 评估输出无法解析"], "raw_batch": None}
            # 槽位更新（架构级）：历史→note 滚动替换；失败原因累积保留最近 K 条
            if parsed.get("note"):
                ctx.set_note(parsed["note"])
            else:
                ctx.set_note(_round_note(ctx.note, round_no, batch, results))
            ctx.add_reasons(parsed.get("reasons"))
            # 跨批次「工作汇报」：把本批结果累积进 ctx，后续轮次规划/评估复用，
            # leader 不再从零重派（见 leader.LeaderContext.add_progress）。
            ctx.add_progress(round_no, results)
            if parsed.get("done"):
                done_answer = parsed.get("answer") or ""
                break
            reason_head = (parsed["reasons"][0] if parsed.get("reasons") else "继续")
            yield {"type": "leader_round", "round": round_no, "phase": "eval",
                   "note": f"判定未完成：{reason_head[:120]}"}
        if not done_answer:
            system_notes.append(
                f"已达到 Leader 评估环上限（{max_rounds} 批），任务可能未全部完成；"
                "以下为当前进展汇总。")
            done_answer = ctx.note or "（在轮次上限内未产出可汇总的进展）"
        # ---------- 步骤⑤：程序裁定 + Leader 语义验收 ----------
        # 语言闸门在前（沿用旧口径）：拟回答不是中文就强制回译
        if done_answer and not _mostly_chinese(done_answer):
            zh = dispatcher.translate(done_answer, "Chinese")
            if zh:
                done_answer = zh
        final_answer = done_answer
        # 程序裁定：总任务原文作任务书——验证命令亲跑、交付物核查、修正轮
        convo5 = [{"role": "user", "content": message},
                  {"role": "assistant", "content": final_answer}]
        holder = {"summary": final_answer, "convo": convo5}
        # 收集本任务真实产出的文件（权威依据），传给收尾闸门——修复验证竞态：
        # 最终 convo 只有 [任务原文, 拟回答]、ran_writes 缺位，若不告知真实产出，
        # 总结里「已写入 X」会被 find_artifact_issues 误判成 ghost（全程零写操作）。
        # 收集本任务真实产出的文件：用【全局 file_log】(shared_log) 而非 per-step
        # sink——最终交付物那次写入未必落在结构化步骤的 sink 里（尤其兜底整段直做/
        # 收尾重写），但只要是写工具调用就一定进了全局 file_log。以此为权威依据，
        # 传给收尾闸门修复验证竞态。
        agg_paths: list[str] = []
        for rec in shared_log:
            p = str((rec or {}).get("path") or "")
            if p and p not in agg_paths:
                agg_paths.append(p)
        gate_file_records = _file_records(agg_paths, root)
        yield from _verify_gate(agent, message, holder, auto_approve=auto_approve,
                                stop_check=stopped, read_cache=read_cache,
                                snapshot=snap, exhausted=exhausted_cmds,
                                files=gate_file_records,
                                produced_paths=agg_paths,
                                task_start_ts=task_start_ts)
        final_answer = holder["summary"]
        # Leader 语义判定：只看 原文 + 拟回答，判语义完成度
        sem_pass, sem_reasons = True, []
        try:
            raw = dispatcher._chat(
                LEADER_SEMANTIC_SYSTEM,
                f"【任务原文】\n{message}\n\n【拟回答】\n{final_answer}",
                num_predict=1024, temperature=0.2)
            sj = _extract_json(raw) or {}
            sem_pass = bool(sj.get("pass"))
            sem_reasons = [str(r).strip() for r in (sj.get("reasons") or [])
                           if str(r).strip()]
        except Exception as e:  # noqa: BLE001  验收调用失败不拦回答，如实附一句
            system_notes.append(f"语义验收调用失败（{type(e).__name__}: {e}），"
                                "本轮仅通过程序验证。")
        if sem_pass:
            accepted = True
            break
        attempts += 1
        if attempts > max_retries:
            system_notes.append(
                "Leader 语义验收未通过，已达重试上限，如实附上未完成原因：\n- "
                + "\n- ".join(sem_reasons))
            accepted = True
            break
        # 失败原因压入固定槽位，重新执行步骤②到④
        ctx.add_reasons(["语义验收未通过：" + r for r in sem_reasons]
                        or ["语义验收未通过：回答未覆盖原文全部要求"])
        yield {"type": "leader_round", "round": ctx.round, "phase": "retry",
               "note": f"语义验收未通过（第 {attempts}/{max_retries} 次重试）："
                       + (sem_reasons[0][:120] if sem_reasons else "未覆盖原文要求")}

    # ---------- 收尾：文件卡片、台账历史、complete ----------
    gaps.clear_step()
    agg_paths: list[str] = []
    path_title: dict[str, str] = {}
    for rec in shared_log:
        p = str((rec or {}).get("path") or "")
        if p and p not in agg_paths:
            agg_paths.append(p)
    for t in all_steps:
        for p in (sink.get(t.id, {}).get("files") or []):
            if p and p not in agg_paths:
                agg_paths.append(p)
                path_title[p] = t.title
    file_records = _file_records(agg_paths, root)

    final_convo = list(history)
    if not final_convo or final_convo[-1].get("role") != "user":
        final_convo.append({"role": "user", "content": message})
    if system_notes:
        final_answer = final_answer.rstrip() + "\n\n---\n" + "\n\n---\n".join(system_notes)
    final_convo.append({"role": "assistant", "content": final_answer})
    yield {"type": "assistant", "content": final_answer, "history": final_convo}
    if file_records:
        yield {"type": "artifacts", "files": file_records}
    yield {"type": "complete", "steps": ctx.round, "multi": True}


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
    # 复制/备份类任务的任务书补强（f4 教训）：harness 替用户补一条逐字节比对的
    # 验证声明。单任务路径里执行模型看到的任务书（book）与收尾闸门核对的保持
    # 同一份；路由判断（_worth_dispatch / needs_plan）与汇总用的仍是原始 message，
    # 不受补强影响。
    _copy_note = verify_gate.declare_copy_check(message)
    book = message + _copy_note if _copy_note else message
    # 产物核查的起点快照：任务书声明交付文件的 mtime 此刻定格，收尾时用它
    # 判断「本轮是否真的更新过」，上一轮的旧文件糊弄不了核查
    gate_snap = verify_gate.snapshot_artifacts(book, agent.cfg.workspace_root)
    # 任务开始墙钟：交给收尾闸门判定声明交付物是否本轮真实写出（见 run_leader 同款说明）。
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
    # 只读共享缓存提前创建：Leader 员工与单路径接力共用（写类工具执行后自动作废）
    read_cache = ReadCache()
    # 产出目录：统一收进 history/（如 history/20261017-213730），避免任务产出
    # 目录散落在项目根；注入每个员工的系统提示词——中间/最终产物一律进这个
    # 文件夹，除非必须放特定位置才能生效。只对「真正动手干活」的任务设置；
    # 纯问答不占提示词字节。
    outdir = "history/" + time.strftime("%Y%m%d-%H%M%S")
    agent.outdir = outdir
    if (auto_plan and not pinned
            and getattr(agent.cfg, "dispatcher_enabled", False)
            and _worth_dispatch(message, len(entries))):
        try:
            disp = Dispatcher(agent.cfg, agent.hub, store)
        except Exception:  # noqa: BLE001
            disp = None
        if disp is not None and disp.available():
            yield from run_leader(
                agent, store, entries, history, message, disp,
                stop_check=stop_check, auto_approve=auto_approve, file_log=file_log,
                read_cache=read_cache, outdir=outdir,
            )
            return

    # 非 Leader 路径（单模型 / 计划接力）同样过开局健康门禁：Ollama 开局不稳时
    # 在这里就被拦下，避免后续 Agent.run 直接连环 500。Leader 路径已在 run_leader
    # 内探过活，上面早已 return，不会重复探。
    gate_err = yield from ollama_health_gate(agent, _local_model_keys(entries, agent), stopped)
    if gate_err:
        yield {"type": "error", "message": gate_err}
        return

    plan: list[Subtask] = []
    if auto_plan and needs_plan(message, len(entries)):
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

    # 展示/回看用的全量历史（一路接力，内容完整）
    hist = list(history)
    # 只读共享缓存（跨步骤/跨员工复用；写类工具执行后自动作废）——
    # 单路径与 Leader 双环共用同一个，这里创建一次往下游传
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
                                   task_start_ts=task_start_ts):
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
                            task_start_ts=task_start_ts)
    if holder["summary"] != summary:
        summary, hist = holder["summary"], holder["convo"]
        yield {"type": "assistant", "content": summary, "history": hist}

    yield {"type": "complete", "steps": total, "multi": multi}


__all__ = ["run_task", "run_leader"]
