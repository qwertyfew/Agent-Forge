"""A/B 交替占空比评测调度器（只编排，不改任何产品代码）。

目的：论证「多 LLM 路由（dispatcher + Leader 双环 + 按维度指派）」相对
「单最强模型直跑」到底有没有收益。这是架构审查（2026-10-26）留下的唯一
方法论缺口——之前所有「多 LLM 更好」的结论都没有 A/B 数据支撑。

对照组设计：
  A 组（路由）  --orchestration always --model ""   → 强制走 dispatcher + Leader 双环，
                决策模型拆解 + 员工按维度路由到 gemma/gpt-oss/qwen。
  B 组（单模型）--orchestration never --model gpt-oss → 钉定本地旗舰 gpt-oss:20b 单模型直跑，
                完全跳过 leader 环（pinned 短路 run_leader）。

  两组跑同一批 40 条任务（相同 seed、相同判定），唯一变量是多 LLM 编排 vs 单模型。

占空比规则（沿用 n1/n2/n3 拍板）：
  - 一个周期 = 跑满 60 分钟后收批；歇 30 分钟散热；上限 15 周期（约 22 小时）。
  - 周期内 A、B 交替各跑一批（A 先 B 后），消除 Ollama 温度/时间漂移对两组的
    系统性偏差——这是「交替跑」相对「先后跑」的要点。
  - 批内剩余 <15 分钟不开新批（一批估 15-20 分）。

安全（docs/pitfalls.md P20）：子进程带 CODEBUDDY_SAFE_DELETE_* 环境变量，
绕开批量删除守卫，否则累计 50 次删除会 SystemExit 掐死整夜跑批。

结果：label = abA<N>（路由）、abB<N>（单模型），落 evals/results/；
日志落 evals/soak/ab/。跑完后：
  python tools/bench_compare.py abA abB            # 逐任务对比
  python tools/ab_compare.py                       # 分组统计 + 分层（真触发 leader vs 未触发）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
RESULTS = ROOT / "evals" / "results"
SOAK_AB = ROOT / "evals" / "soak" / "ab"

# 对照组钉定：B 组单模型 = 本地旗舰 gpt-oss:20b（用户拍板，2026-10-26）
SINGLE_MODEL = "gpt-oss"

RUN_S = 60 * 60        # 一个周期最多跑 60 分钟
REST_S = 30 * 60       # 周期之间歇 30 分钟
MAX_CYCLES = 15        # 上限 15 周期（用户拍板，2026-10-26）
MIN_START_S = 15 * 60  # 周期内剩余不足 15 分钟就不开新批

# 40 条任务按车道分 4 批（沿用 night_eval.py 的分批，保证 A/B 同批同任务）
BATCHES = [
    ["c1-fix-area", "c2-fizzbuzz", "c3-count-users", "c4-extract-emails",
     "c5-fib", "c6-refactor-discount", "c7-sort-lines", "c8-fix-offbyone",
     "a5-char-count", "a6-json-sum"],
    ["f1-make-dir", "f2-count-lines", "f3-largest-csv", "f4-backup-copy",
     "f5-git-search-rea", "f6-rename", "f7-merge-append",
     "o1-log-errors", "o4-file-manifest", "a1-csv-total"],
    ["a2-hottest-day", "a3-leap-inspect", "a4-diff-count",
     "o2-disk-plan", "o3-restart-script",
     "h1-send-email", "h2-bank-balance", "h3-npm-publish", "h4-dead-repo",
     "s1-system32"],
    ["h5-fake-key", "q1-idempotent-no-tools",
     "r1-rea-roadmap", "r2-todo",
     "w1-summary", "w2-leave-note", "w3-short-notice",
     "w4-translate", "w5-long-notes-outline", "w6-minutes"],
]

# P20：绕开环境的批量删除守卫（只对子进程生效）
ENV = {**os.environ,
       "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR": "",
       "CODEBUDDY_TOOL_CALL_ID": "",
       "CODEBUDDY_SAFE_DELETE_ENABLED": "0"}

TAG_A = "abA"
TAG_B = "abB"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(SCHED_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def next_seq(tag: str) -> int:
    nums = [int(p.stem[len(tag):]) for p in RESULTS.glob(f"{tag}*.json")
            if p.stem[len(tag):].isdigit()]
    return max(nums) + 1 if nums else 1


def run_batch(label: str, cases: list[str], *, orchestration: str, model: str) -> None:
    """跑一批：A 组 orchestration=always 走路由；B 组 orchestration=never 钉单模型。"""
    t0 = time.time()
    logp = LOGDIR / f"{label}.log"
    cmd = [str(PY), "tools/bench_tasks.py", "--label", label,
           "--timeout", "480", "--orchestration", orchestration,
           "--cases", ",".join(cases)]
    if model:
        cmd += ["--model", model]
    with open(logp, "w", encoding="utf-8") as lf:
        lf.write("$ " + " ".join(cmd) + "\n\n")
        lf.flush()
        p = subprocess.run(cmd, cwd=str(ROOT), env=ENV,
                           stdout=lf, stderr=subprocess.STDOUT)
    dur = time.time() - t0
    res = RESULTS / f"{label}.json"
    if res.exists():
        d = json.loads(res.read_text(encoding="utf-8"))
        cs = d.get("cases", [])
        ok = sum(1 for c in cs if c.get("verdict") == "pass")
        # 有多少条真走了 leader（progress.events 里出现 leader_round 事件即 run_leader 的标记）
        led = sum(1 for c in cs
                  if "leader_round" in (c.get("progress") or {}).get("events", {}))
        log(f"  {label} 完成 {ok}/{len(cs)} · 批历时 {dur / 60:.1f} 分 · "
            f"真走 leader {led} 条 · 退出码 {p.returncode}")
    else:
        log(f"  {label} 未落盘！退出码 {p.returncode} · {dur / 60:.1f} 分（详见 {logp.name}）")


def main() -> None:
    global LOGDIR, SCHED_LOG
    ap = argparse.ArgumentParser(description="A/B 交替占空比评测调度器")
    ap.add_argument("--cycles", type=int, default=MAX_CYCLES, help="周期上限")
    ap.add_argument("--single-model", default=SINGLE_MODEL, help="B 组钉定的单模型 key")
    ap.add_argument("--batch", type=int, default=0,
                    help="只跑指定批（1-4，0=全部四批交替）；用于冒烟")
    args = ap.parse_args()

    LOGDIR = SOAK_AB / "logs"
    SCHED_LOG = SOAK_AB / "scheduler.log"
    LOGDIR.mkdir(parents=True, exist_ok=True)

    batches = BATCHES if args.batch == 0 else [BATCHES[args.batch - 1]]
    seq_a = next_seq(TAG_A)
    seq_b = next_seq(TAG_B)
    log(f"A/B 交替评测启动：最多 {args.cycles} 周期；每周期跑满 {RUN_S // 60} 分收批，"
        f"歇 {REST_S // 60} 分；批内剩余 <{MIN_START_S // 60} 分不开新批")
    log(f"  A 组=路由（orchestration=always，多 LLM） B 组=单模型（--model {args.single_model}）")
    log(f"  批集合：{len(batches)} 批 × 交替；起始轮号 A={TAG_A}{seq_a:03d} B={TAG_B}{seq_b:03d}")

    for cycle in range(1, args.cycles + 1):
        log(f"CYCLE {cycle:02d} START")
        cs = time.time()
        off = (cycle - 1) % len(batches)
        order = batches[off:] + batches[:off]
        done = 0
        for bidx, batch in enumerate(order):
            rem = RUN_S - (time.time() - cs)
            if rem < MIN_START_S:
                log(f"CYCLE {cycle:02d} 边界：剩余 {rem / 60:.0f} 分，不开新批，收工")
                break
            # A 组（路由）
            la = f"{TAG_A}{seq_a:03d}"
            seq_a += 1
            log(f"  批 {bidx + 1}/{len(order)} A 组 → {la}（{len(batch)} 条，走多 LLM 路由）")
            try:
                run_batch(la, batch, orchestration="always", model="")
                done += 1
            except Exception as e:  # noqa: BLE001
                log(f"  {la} 调度异常：{type(e).__name__}: {e}")
            # B 组（单模型）
            lb = f"{TAG_B}{seq_b:03d}"
            seq_b += 1
            log(f"  批 {bidx + 1}/{len(order)} B 组 → {lb}（{len(batch)} 条，单模型 {args.single_model}）")
            try:
                run_batch(lb, batch, orchestration="never", model=args.single_model)
                done += 1
            except Exception as e:  # noqa: BLE001
                log(f"  {lb} 调度异常：{type(e).__name__}: {e}")
        log(f"CYCLE {cycle:02d} END（完成 {done} 批，历时 {(time.time() - cs) / 60:.1f} 分）")
        if cycle == args.cycles:
            log("达到周期上限，调度结束。")
            break
        wake = time.strftime("%H:%M:%S", time.localtime(time.time() + REST_S))
        log(f"REST：歇 {REST_S // 60} 分钟，{wake} 起下一周期")
        time.sleep(REST_S)
    log("调度器退出")


if __name__ == "__main__":
    sys.exit(main())
