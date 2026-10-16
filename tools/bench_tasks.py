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
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import gaps                       # noqa: E402
from agentforge.cli import CLI                     # noqa: E402
from agentforge.config import Config               # noqa: E402
from agentforge.evals import judge, load_cases     # noqa: E402
from agentforge.orchestrator import run_task       # noqa: E402

RESULTS_DIR = ROOT / "evals" / "results"
RUN_DIR = ROOT / "evals" / ".run"


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


def run_case_once(case, label: str, model: str, timeout: int,
                  check_timeout: int) -> dict:
    """单跑一条任务一次，返回该次的判定记录（不动台账）。

    过程记录（rec["process"]）忠实还原 harness 的处理顺序：轮次、skill
    注入、每次工具调用的完整参数与输出、事件流报错、验证。不做归因分析
    ——结论靠比对结果，报告只负责把事实摆全（通过的用例同样要全录）。
    """
    sandbox = RUN_DIR / label / case.id
    _prep_sandbox(sandbox, case)
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
    final_text = ""
    error = ""
    verify_n = 0
    stopped = False
    stats: dict = {}
    cur_step = 0
    process: list = []
    try:
        for ev in run_task(cli.agent, cli.store, [], case.prompt,
                           auto_plan=True, model_key=model or "",
                           auto_approve=True):
            et = ev.get("type")
            if et == "assistant":
                final_text = ev.get("content") or final_text
            elif et == "step":
                cur_step = int(ev.get("step") or 0)
            elif et == "skill":
                process.append({"s": cur_step, "k": "skill",
                                "name": ev.get("name") or "",
                                "detail": ev.get("detail") or ""})
            elif et == "tool_start":
                # 参数与输出原样保留（工具层本就有 max_tool_output_chars 兜底）
                process.append({"s": cur_step, "k": "tool",
                                "name": ev.get("name") or "",
                                "args": ev.get("args") if isinstance(
                                    ev.get("args"), (dict, list)) else str(ev.get("args") or ""),
                                "out": ""})
            elif et == "tool_result":
                if process and process[-1].get("k") == "tool":
                    process[-1]["out"] = ev.get("output") or ""
            elif et == "verify":
                verify_n += 1
                process.append({"s": cur_step, "k": "verify"})
            elif et == "error":
                error = ev.get("message") or "unknown error"
                process.append({"s": cur_step, "k": "error", "msg": error})
            elif et == "stopped":
                stopped = True
                process.append({"s": cur_step, "k": "stopped"})
            elif et == "stats":
                stats = {k: v for k, v in ev.items() if k != "type"}
            if time.time() - t0 > timeout:
                error = error or f"超时（>{timeout}s）"
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
        rec = judge(case, final_text, sandbox, check_timeout=check_timeout)
        if error:
            rec["detail"] += f"（事件流另有报错：{error}）"
        # must_ask：任务书要求信息不足时发起澄清弹窗——过程里没有 ask_user 就判失败
        if case.expect.get("must_ask") and \
                not any(p.get("name") == "ask_user" for p in process):
            rec = {"verdict": "fail", "layer": "ask", "data": rec.get("data", {}),
                   "detail": "信息不足时应发起澄清弹窗（ask_user），但全程未调用"}
        # tool_called：任务书指定的关键工具至少要用过一个（任一命中即过）。
        # 检索类任务的真证据在过程里（调了什么工具），不在回复措辞里——
        # 回复里有没有 "git" 字样纯属格式运气，不能当判据
        want_tools = [str(x) for x in (case.expect.get("tool_called") or [])]
        if want_tools and not any(p.get("name") in want_tools for p in process):
            rec = {"verdict": "fail", "layer": "tool", "data": rec.get("data", {}),
                   "detail": f"应调用指定工具之一（{'、'.join(want_tools)}），但全程未调用"}
    rec.update({"id": case.id, "category": case.category, "duration": dur,
                "verify_rounds": verify_n, "steps": cur_step,
                # 结果证据：最终回复全文（不截断）、沙盒落点（可亲自去查）
                "reply": final_text.strip(),
                "sandbox": str(sandbox)})
    if process:
        rec["process"] = process
    if error:
        rec["error"] = error
    if stats:
        rec["stats"] = stats
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
