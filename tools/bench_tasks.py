"""评测运行器：把任务集逐条喂给完整的 agent 管线，按层判定，落结果。

运行：python tools/bench_tasks.py                 # 全量任务集
     python tools/bench_tasks.py -c coding       # 只跑编码类
     python tools/bench_tasks.py --cases c1-fix-area,w1-summary
     python tools/bench_tasks.py --label base --model gemma   # A 组
     python tools/bench_tasks.py --label new  --model qwen    # B 组
     python tools/bench_tasks.py --repeat 3      # 每条跑 3 遍，报 pass^k
之后 python tools/bench_compare.py base new       # 出对比表

隔离保证：每条任务在 evals/.run/<label>/<case-id>/ 下有独立沙盒，
agent 的 workspace_root 被钉在沙盒里，不碰真实项目；权限模式为完全访问
（评测要在无人值守下跑完，沙盒就是边界）。

失败归因：记录第一个失败的判定层（exact/norm/numeric/fuzzy/must_not/check）
与事件流证据（error/verify/stopped），"拆错了、答错了、产物坏了"由此区分。
失败会同时记入能力缺口台账（agentforge.gaps，kind=eval_fail）——同类任务
反复 fail 在同一层，就是该补 skill 或工具的信号；台账用未改 workspace 的
Config 落盘，不跟着沙盒走（沙盒每条任务都要清场重建）。

pass^k（--repeat N）：同一条任务跑 N 遍、全部通过才算过，并报 per-case 的
通过比例。单次通过有运气成分，pass^k 量的是稳定性——这正是权威评测
（如 tau-bench 的 pass^k）已经验证过的口径。
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import gaps                       # noqa: E402
from agentforge import grounding                   # noqa: E402
from agentforge.cli import CLI                     # noqa: E402
from agentforge.config import Config               # noqa: E402
from agentforge.evals import judge, load_cases     # noqa: E402
from agentforge.orchestrator import run_task       # noqa: E402

RESULTS_DIR = ROOT / "evals" / "results"
RUN_DIR = ROOT / "evals" / ".run"

# PTC 常驻下，模型真正调的工具都写在 ops_batch 脚本里：顶层 tool_start 只有一个
# `ops_batch`，`github_search` / `fetch_url` 这些名字只出现在脚本源码里。
# 评测若只看顶层事件，所有的 `tool_called` 都会判成「没调用」——那是把架构改动
# 记成模型退化（rerun64 实测：f5 用 `from tools import github_search` 真检索了，
# 却被记成「全程未调用」）。解析口径统一由 agentforge.tooltrace 提供，产品侧的
# 闸门（sources/verify）也在用同一份——评测与产品各写一套，迟早互相打架。
from agentforge import tooltrace  # noqa: E402
from agentforge import streamprobe  # noqa: E402


def _inner_calls(name: str, args) -> list[str]:
    """从一次顶层工具调用里取出脚本内层调用名（非 PTC 调用返回空）。"""
    if name != "ops_batch":
        return []
    if isinstance(args, dict):
        script = args.get("script") or args.get("code") or ""
    else:
        script = args or ""
    return tooltrace.names_from_script(str(script))


def _summary_calls(output: str) -> list[str]:
    """从 ops_batch 自己的执行小结里取内层工具名（最权威：执行后统计的）。"""
    return tooltrace.summary_names(output)


def _used_tools(process: list[dict]) -> set[str]:
    """这条任务实际用过的工具名（含 ops_batch 脚本里的内层调用）。

    `inner` 是采集时算好的；没有这个字段的旧记录（或手搓的 process）就地补算一次，
    这样历史结果也能用同一套口径重读。
    """
    out: set[str] = set()
    for p in process:
        n = str(p.get("name") or "")
        if n:
            out.add(n)
        for inner in (p.get("inner") or _inner_calls(n, p.get("args"))):
            out.add(str(inner))
    return out



def _prep_sandbox(sandbox: Path, case) -> None:
    """整目录重建沙盒并写入 seed 文件。"""
    if sandbox.exists():
        for p in sorted(sandbox.rglob("*"), reverse=True):
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            except OSError:
                pass
    sandbox.mkdir(parents=True, exist_ok=True)
    for rel, content in case.seed.items():
        fp = sandbox / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")


def citation_issue(mode: str, reply: str, process: list[dict]) -> str:
    """`citations_from_tools` 的判定：返回空串=通过，否则是失败理由（P2）。

    模式：
    - `all`：答复里每个 owner/name 都必须在本次工具输出里出现过；
    - `any`：在 all 的基础上还要求至少点名一条——但**只在证据里本来就有候选时**
      才这么要求，避免惩罚「检索确实为空、如实说没找到」的诚实答复。

    为什么要分开：旧判据（回复含 REA + 调过检索）对「编的」和「真搜到的」一视同仁，
    等于没测；而一旦改成"必须有仓库名"，诚实空结果又会被误伤——两个错都踩过。
    """
    if mode not in ("all", "any"):
        return ""
    evidence = "\n".join(str(p.get("out") or "") for p in process or []
                         if p.get("k") == "tool")
    good, bad = grounding.repo_citation_split(reply, evidence)
    if bad:
        return f"答复里的仓库名在本次工具输出里找不到：{bad[:5]}"
    if mode == "any" and not good and grounding.evidence_has_repo(evidence):
        return ("工具结果里有仓库候选，答复却一个都没点名"
                "（等于没有引用检索结果）")
    return ""


def run_case_once(case, label: str, model: str, timeout: int,
                  check_timeout: int) -> dict:
    """单跑一条任务一次，返回该次的判定记录（不动台账）。

    过程记录（rec["process"]）忠实还原 harness 的处理顺序：轮次、skill
    注入、每次工具调用的完整参数与输出、事件流报错、验证。不做归因分析
    ——结论靠比对结果，报告只负责把事实摆全（通过的用例同样要全录）。
    """
    sandbox = RUN_DIR / label / case.id
    _prep_sandbox(sandbox, case)
    # 观测哨按任务隔离：上一条任务留下的流记录会把「卡住的是谁」搅浑。
    streamprobe.reset()
    # 澄清弹窗脚本答案：评测模式 ask_user 按序弹任务书给的 ask_answers
    from agentforge import interact as _interact
    _interact.begin(answers=getattr(case, "ask_answers", None))

    cfg = Config.load(ROOT / "config.yaml")
    # 必须是 Path 不是 str：shell 工具收尾会调 workspace_root.resolve()，
    # 传 str 会让 run_command（不传 cwd 时）全灭——第三轮评测 17/24 的元凶，
    # c2/c5 的代码其实是对的，死在工具层 AttributeError 上。
    cfg.workspace_root = Path(sandbox)
    try:
        cli = CLI(cfg, auto_approve=True, model_key=model or None)
    except Exception as e:  # noqa: BLE001
        return {"verdict": "fail", "layer": "setup",
                "detail": f"CLI 初始化失败：{type(e).__name__}: {e}"}

    t0 = time.time()
    # 卡死时的第二重证据（P17）：主线程阻塞在生成里时自己 dump 不出栈，而
    # faulthandler 的定时器是独立线程——到点把所有线程的栈写进文件。只在超过
    # 墙钟 10s 后触发一次；正常结束就取消，没写过东西的文件当场删掉。
    stack_path = RUN_DIR / label / f"{case.id}.stack.txt"
    stack_f = None
    try:
        stack_path.parent.mkdir(parents=True, exist_ok=True)
        stack_f = stack_path.open("w", encoding="utf-8")
        faulthandler.dump_traceback_later(int(timeout) + 10, repeat=False,
                                          file=stack_f)
    except (OSError, ValueError):
        stack_f = None
    # 硬墙钟（P8）：以前 `--timeout` 只写在事件循环体里，模型不回包就一条事件都没有，
    # 于是永远走不到那行判断（soak32 卡 10 分钟、soak04 的 c6 记到 960s）。
    # 现在同一份期限也交给 stop_check：流式循环每收一行都会问一次，到点立刻中断生成。
    deadline = t0 + float(timeout)

    def _deadline_hit() -> bool:
        return time.time() > deadline

    final_text = ""
    error = ""
    verify_n = 0
    # 回合数（turns）与步号（steps）不是一回事：编排层把大任务拆成多个小步骤时，
    # 各泳道是各自线程并行推进的（orchestrator.py:1015），事件流里混着两条泳道的
    # 计数器，`step` 会回退（31 轮台账里 12 次运行如此）。所以「一共转了几回合」
    # 只能数 `step` 事件，不能取最后一个 `step` 值。
    turns_n = 0
    stopped = False
    stats: dict = {}
    cur_step = 0
    process: list = []
    # 事件统计（P17）：被墙钟砍掉的那一次，事后唯一能看的就是这些数。
    # 尤其 `text_chars` 与 `silent_s`：前者说明「模型一直在出字、只是没出完」，
    # 后者说明「最后一次收到内容到现在隔了多久」——两者把「生成退化」和
    # 「端侧不回包」区分开，不用猜。
    ev_counts: dict[str, int] = {}
    delta_chars = 0
    tail = ""
    last_ev_t = t0
    try:
        for ev in run_task(cli.agent, cli.store, [], case.prompt,
                           auto_plan=True, model_key=model or "",
                           auto_approve=True, stop_check=_deadline_hit):
            et = ev.get("type")
            ev_counts[et or "?"] = ev_counts.get(et or "?", 0) + 1
            last_ev_t = time.time()
            if et in ("delta", "thinking"):
                _piece = str(ev.get("text") or "")
                delta_chars += len(_piece)
                tail = (tail + _piece)[-300:]
            if et == "assistant":
                final_text = ev.get("content") or final_text
            elif et == "step":
                cur_step = int(ev.get("step") or 0)
                turns_n += 1
            elif et == "skill":
                process.append({"s": cur_step, "k": "skill",
                                "name": ev.get("name") or "",
                                "detail": ev.get("detail") or ""})
            elif et == "tool_start":
                # 参数与输出原样保留（工具层本就有 max_tool_output_chars 兜底）
                _args = ev.get("args") if isinstance(
                    ev.get("args"), (dict, list)) else str(ev.get("args") or "")
                _name = ev.get("name") or ""
                _rec = {"s": cur_step, "k": "tool", "name": _name,
                        "args": _args, "out": ""}
                # 哪条泳道的调用：多泳道并发出的事件流里，步号只在泳道内单调，
                # 不记 task 就没法把「同一回合连调两次」和「两条泳道各调一次」分开。
                _lane = ev.get("task_id")
                if _lane:
                    _rec["task"] = str(_lane)
                _inner = _inner_calls(_name, _args)
                if _inner:
                    _rec["inner"] = _inner
                process.append(_rec)
            elif et == "tool_result":
                if process and process[-1].get("k") == "tool":
                    process[-1]["out"] = ev.get("output") or ""
                    # 执行后的小结才是权威：脚本写法再花，工具层自己也点了名
                    _sum = _summary_calls(process[-1]["out"])
                    if _sum:
                        process[-1]["inner"] = list(dict.fromkeys(
                            (process[-1].get("inner") or []) + _sum))
            elif et == "verify":
                verify_n += 1
                process.append({"s": cur_step, "k": "verify"})
            elif et == "grounding_gate":
                # 闸门事件必须进过程记录：handoff §6.3 要求「每跑记一次误伤数」，
                # 误伤的证据就在 issues 里（判了什么、凭什么），不看它就只剩猜。
                process.append({"s": cur_step, "k": "gate",
                                "round": ev.get("round"),
                                "exhausted": bool(ev.get("exhausted")),
                                "issues": [str(x)[:200] for x in
                                           (ev.get("issues") or [])][:6],
                                "refs": [str(x)[:80] for x in
                                         (ev.get("refs") or [])][:6]})
            elif et == "error":
                error = ev.get("message") or "unknown error"
                process.append({"s": cur_step, "k": "error", "msg": error})
            elif et == "stopped":
                stopped = True
                process.append({"s": cur_step, "k": "stopped"})
                # 是「到点了被硬墙钟掐掉」还是「用户手动停」：两者在台账里不能混。
                if _deadline_hit():
                    error = error or f"超时（>{timeout}s，硬墙钟）"
            elif et == "stats":
                stats = {k: v for k, v in ev.items() if k != "type"}
            if time.time() - t0 > timeout:
                # 到点掐掉：把「当时模型到底在干什么」写进错误文案，台账第一眼
                # 就能分类（等锁 / 一直在出字但没收尾 / 完全没字节），P17 要的就是这句。
                error = error or (f"超时（>{timeout}s）："
                                  f"{streamprobe.summary_line()}")
                process.append({"s": cur_step, "k": "error", "msg": error})
                break
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        process.append({"s": cur_step, "k": "error", "msg": error})

    dur = round(time.time() - t0, 1)
    if error and not final_text:
        rec = {"verdict": "fail", "layer": "crash", "detail": error}
    elif stopped and not final_text:
        rec = {"verdict": "fail", "layer": "stopped", "detail": "任务被停止且无产出"}
    elif not final_text.strip():
        rec = {"verdict": "fail", "layer": "empty", "detail": "模型没有产出任何回复"}
    else:
        # P28：check 层在交付区 output/ 里跑（种子与交付物都在那里）
        _outdir = getattr(cli.agent, "outdir", "") if cli is not None else ""
        _check_root = sandbox / _outdir / "output" if _outdir else sandbox
        rec = judge(case, final_text, sandbox, check_timeout=check_timeout,
                    check_root=_check_root)
        if error:
            rec["detail"] += f"（事件流另有报错：{error}）"
        # must_ask：任务书要求信息不足时发起澄清弹窗——过程里没有 ask_user 就判失败
        if case.expect.get("must_ask") and \
                not any(p.get("name") == "ask_user" for p in process):
            rec = {"verdict": "fail", "layer": "ask", "data": rec.get("data", {}),
                   "detail": "信息不足时应发起澄清弹窗（ask_user），但全程未调用"}
        # tool_called：任务书指定的关键工具至少要用过一个（任一命中即过）。
        # 检索类任务的真证据在过程里（调了什么工具），不在回复措辞里——
        # 回复里有没有 "git" 字样纯属格式运气，不能当判据。
        # PTC 下内层调用名在 ops_batch 脚本里，_used_tools 一并算进来。
        used = _used_tools(process)
        want_tools = [str(x) for x in (case.expect.get("tool_called") or [])]
        if want_tools and not (used & set(want_tools)):
            rec = {"verdict": "fail", "layer": "tool", "data": rec.get("data", {}),
                   "detail": f"应调用指定工具之一（{'、'.join(want_tools)}），"
                             f"实际用过：{'、'.join(sorted(used)) or '无'}"}
        # tool_must_not：该直接回答的问题不该动手（纯问答类）。判据同上，看过程。
        ban_tools = [str(x) for x in (case.expect.get("tool_must_not") or [])]
        if ban_tools and (used & set(ban_tools)):
            rec = {"verdict": "fail", "layer": "tool", "data": rec.get("data", {}),
                   "detail": f"不该调用工具，但用了："
                             f"{'、'.join(sorted(used & set(ban_tools)))}"}
        # citations_from_tools：答复点名的仓库必须在本次工具输出里出现过（P2）。
        # 旧判据只看「回复含 REA + 调过检索」，臆造名和有据名都算过——这个 pass
        # 没有信息量。改成可证伪比对后，"说出来的名字是不是搜来的"才能判。
        cite_mode = str(case.expect.get("citations_from_tools") or "").strip().lower()
        cite_bad = citation_issue(cite_mode, final_text, process)
        if cite_bad:
            rec = {"verdict": "fail", "layer": "cite", "data": rec.get("data", {}),
                   "detail": cite_bad}
    rec.update({"id": case.id, "category": case.category, "duration": dur,
                "verify_rounds": verify_n, "steps": cur_step,
                # 真实回合数（数 step 事件）。旧台账没有这个字段，读的人要退回
                # steps —— 那是最后一条泳道的计数器，多泳道时会少算。
                "turns": turns_n,
                # 实际用过的工具（含 PTC 内层调用）：判读「它到底动没动手」的第一眼证据
                "tools": sorted(_used_tools(process)),
                # 闸门命中次数（exhausted = 打满仍不合格）：§6.3 的误伤监控口径
                "gates": len([p for p in process if p.get("k") == "gate"]),
                # 结果证据：最终回复全文（不截断）、沙盒落点（可亲自去查）
                "reply": final_text.strip(),
                "sandbox": str(sandbox)})
    if process:
        rec["process"] = process
    if error:
        rec["error"] = error
    if stats:
        rec["stats"] = stats
    # 事件流统计（P17）：无论成败都记——成功轮里它是「这一步出了多少字」的
    # 侧面账，失败轮里它是唯一的现场。
    rec["progress"] = {
        "events": ev_counts,
        "text_chars": delta_chars,
        "last_event_s": round(last_ev_t - t0, 1),
        "silent_s": round(time.time() - last_ev_t, 1),
        "tail": tail,
    }
    # 流记录只在「出过事」或「逼近墙钟」时落盘：正常轮记了也只是噪声。
    _snaps = streamprobe.snapshot(6)
    if _snaps and (error or dur >= timeout * 0.6):
        rec["streams"] = _snaps
    faulthandler.cancel_dump_traceback_later()
    if stack_f is not None:
        try:
            stack_f.close()
            # 定时器没触发就没写过东西。**刻意不删**：本仓库的 safe-delete 守卫会拦
            # 批量 unlink（fixc10 实测：一个空文件的 unlink 让整轮 bench 退出码 1、
            # 5 条结果全丢）。`evals/.run/` 是临时目录，留个 0 字节文件不脏任何地方。
        except OSError:
            pass
    _interact.end()          # 清线程本地模式，防串到同线程的下一次运行
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentForge 评测运行器")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--tasks", default=str(ROOT / "evals" / "tasks.yaml"))
    ap.add_argument("--label", default="run", help="本次跑批的名字（结果文件名）")
    ap.add_argument("--model", default="", help="钉定模型 key（空 = 走正常路由）")
    ap.add_argument("--cases", default="", help="逗号分隔的任务 id，空 = 全部")
    ap.add_argument("-c", "--category", default="", help="只跑某一类")
    ap.add_argument("--timeout", type=int, default=900, help="单条任务墙钟上限（秒）")
    ap.add_argument("--check-timeout", type=int, default=60, help="check 层命令超时（秒）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每条任务跑几遍（pass^k 口径：全过才算过）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    cases = load_cases(args.tasks)
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.cases:
        want = {s.strip() for s in args.cases.split(",") if s.strip()}
        cases = [c for c in cases if c.id in want]
    if not cases:
        print("没有匹配的任务。")
        return 1
    reps = max(1, args.repeat)

    # 缺口台账的落盘位置跟真实工作区走，不跟沙盒走
    ledger_cfg = Config.load(args.config)

    results: list[dict] = []
    t_all = time.time()
    for i, case in enumerate(cases, 1):
        recs = [run_case_once(case, args.label, args.model,
                              args.timeout, args.check_timeout)
                for _ in range(reps)]
        p = sum(1 for r in recs if r["verdict"] == "pass")
        final = dict(recs[-1])
        if reps > 1:
            first_bad = next((r for r in recs if r["verdict"] == "fail"), None)
            if first_bad is not None:
                final.update({"verdict": "fail", "layer": first_bad.get("layer", "?"),
                              "detail": first_bad.get("detail", "")})
            final["pass_runs"] = p
            final["runs"] = reps
            final["passk"] = f"{p}/{reps}"
        results.append(final)

        # 失败入缺口台账：同类任务反复 fail 在同一层 = 该补 skill/工具的信号
        if final["verdict"] != "pass":
            gaps.record("eval_fail", f"{case.category}/{case.id}",
                        f"失败层 {final.get('layer', '?')}: "
                        f"{str(final.get('detail', ''))[:120]}"
                        + (f"（pass^{reps}={p}/{reps}）" if reps > 1 else ""),
                        ledger_cfg, step=getattr(case, "prompt", ""))

        for r_i, r in enumerate(recs):
            tag = f"#{r_i + 1}" if reps > 1 else ""
            mark = "PASS" if r["verdict"] == "pass" else "FAIL"
            print(f"[{i}/{len(cases)}]{tag} {mark} {case.id:<18} "
                  f"layer={r.get('layer', '?'):<9} {r['duration']:>6}s  "
                  f"{r.get('detail', '')[:70]}")

    total = len(results)
    passed = sum(1 for r in results if r["verdict"] == "pass")
    head = f"\n汇总：{passed}/{total} 通过"
    if reps > 1:
        pk = sum(1 for r in results if r.get("pass_runs") == reps)
        head += f"（pass^{reps} 严格口径：{pk}/{total}）"
    print(head + f"，总耗时 {round(time.time() - t_all, 1)}s")
    by_layer: dict[str, int] = {}
    for r in results:
        if r["verdict"] != "pass":
            by_layer[r.get("layer", "?")] = by_layer.get(r.get("layer", "?"), 0) + 1
    if by_layer:
        print("失败归因分布：", json.dumps(by_layer, ensure_ascii=False))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{args.label}.json"
    out.write_text(json.dumps({
        "meta": {"label": args.label, "model": args.model or "auto",
                 "config": args.config, "tasks": args.tasks,
                 "repeat": reps,
                 "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "cases": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"结果已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
