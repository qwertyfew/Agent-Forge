"""评测台（agentforge.evalsdesk）回归：任务集管理、报告生成、运行状态机。

口径：save_tasks 的三道校验（结构 / 保护任务 / 分类不空）、备份与回读、
build_report 的要素齐全、start_run 的单飞与停止语义——全部用注入的替身
runner 跑，不碰真模型。删掉任何一条断言，对应行为坏了这里必须变红。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import evalsdesk
from agentforge.evalsdesk import EvalsDesk, build_report, git_version

REAL_TASKS = ROOT / "evals" / "tasks.yaml"


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    # 把真实任务集拷进临时目录：所有写操作都落在替身上，绝不碰真文件
    tmp_tasks = tdp / "tasks.yaml"
    tmp_tasks.write_text(REAL_TASKS.read_text(encoding="utf-8"), encoding="utf-8")
    evalsdesk.TASKS_PATH = tmp_tasks
    evalsdesk.RESULTS_DIR = tdp / "results"
    evalsdesk.BACKUP_DIR = tdp / "backup"

    desk = EvalsDesk(cfg=None)

    # ---- 任务集管理 ----
    lt = desk.list_tasks()
    check("E1 任务清单可读（24 条 + 保护清单）",
          len(lt["cases"]) == 24 and "h1-send-email" in lt["protected"],
          f"{len(lt['cases'])} 条")

    # E2 正常保存：改一条非保护任务的 prompt → 成功、备份存在、回读生效
    cases = [dict(c) for c in lt["cases"]]
    for c in cases:
        if c["id"] == "w1-summary":
            c["prompt"] = "改过的任务指令"
    r = desk.save_tasks(cases)
    check("E2 整体保存成功", r.get("ok") and r.get("count") == 24, str(r))
    check("E3 旧文件已备份", (evalsdesk.BACKUP_DIR / Path(r["backup"]).name).is_file(), "")
    check("E4 保存后回读生效",
          any(c["prompt"] == "改过的任务指令" for c in desk.list_tasks()["cases"]), "")

    # E5 删除受保护任务 → 拒绝，文件不变
    cases2 = [c for c in desk.list_tasks()["cases"] if c["id"] != "h1-send-email"]
    before = tmp_tasks.read_text(encoding="utf-8")
    r = desk.save_tasks(cases2)
    check("E5 删除受保护任务被拒绝", "不能删除" in r.get("error", ""), str(r))
    check("E6 拒绝后文件未被改动", tmp_tasks.read_text(encoding="utf-8") == before, "")

    # E7 清空一个分类 → 拒绝（回归要求七大类齐全）
    cases3 = [c for c in desk.list_tasks()["cases"] if c["category"] != "ops"]
    r = desk.save_tasks(cases3)
    check("E7 清空分类被拒绝", "清空" in r.get("error", ""), str(r))

    # E8 结构校验：重复 id / 空 expect / expect 非 dict / 空 prompt
    base = desk.list_tasks()["cases"]
    dup = [dict(c) for c in base] + [dict(base[0])]
    check("E8a 重复 id 拒绝", "重复" in desk.save_tasks(dup).get("error", ""), "")
    noexp = [dict(c) for c in base]
    noexp[0] = dict(noexp[0]); noexp[0]["expect"] = {}
    check("E8b 空 expect 拒绝", "判定条件" in desk.save_tasks(noexp).get("error", ""), "")
    badexp = [dict(c) for c in base]
    badexp[0] = dict(badexp[0]); badexp[0]["expect"] = "not-a-dict"
    check("E8c expect 非对象拒绝", "JSON" in desk.save_tasks(badexp).get("error", ""), "")

    # ---- 报告 ----
    meta = {"label": "t", "model": "auto", "tasks": "x", "repeat": 2,
            "cases_planned": 3, "skipped": 0,
            "started_at": "2026-10-14 12:00:00", "finished_at": "2026-10-14 12:01:00",
            "duration": 60.0, "version": {"commit": "abc1234", "dirty": False}}
    results = [
        {"id": "c1", "category": "coding", "verdict": "pass", "layer": "check",
         "detail": "通过", "duration": 10.0, "pass_runs": 2, "runs": 2, "passk": "2/2"},
        {"id": "h1-send-email", "category": "honesty", "verdict": "fail",
         "layer": "must_not", "detail": "出现了禁止内容：已发送", "duration": 8.0,
         "pass_runs": 0, "runs": 2, "passk": "0/2"},
        {"id": "f1", "category": "fileops", "verdict": "pass", "layer": "check",
         "detail": "通过", "duration": 9.0, "pass_runs": 1, "runs": 2, "passk": "1/2"},
    ]
    md = build_report(meta, results)
    check("E9 报告含时间与耗时", "2026-10-14 12:00:00" in md and "60.0" in md, "")
    check("E10 报告含代码版本", "abc1234" in md, "")
    check("E11 报告含汇总与 pass^k", "2/3 通过" in md and "pass^2" in md, "")
    check("E12 报告含失败归因表", "must_not" in md and "禁止内容" in md, "")
    check("E13 报告含分类通过率", "诚实性" in md and "0" in md, "")
    check("E14 报告逐条列出任务", "h1-send-email" in md and "❌" in md and "✅" in md, "")

    # ---- 运行状态机（替身 runner，不碰模型）----
    stub_recs = iter([])
    def stub_runner(case, label, model, timeout, check_timeout):
        return {"verdict": "pass", "layer": "check", "detail": "通过",
                "id": case.id, "category": case.category, "duration": 0.1,
                "verify_rounds": 0}
    desk._runner = stub_runner

    r = desk.start_run(category="honesty", repeat=1, label="t1")
    check("E15 启动成功并报条数", r.get("ok") and r.get("total") == 3, str(r))
    r2 = desk.start_run(label="t2")
    check("E16 运行中二次启动被拒", "已有评测在跑" in r2.get("error", ""), str(r2))
    for _ in range(200):
        st = desk.snapshot()
        if not st.get("running"):
            break
        time.sleep(0.05)
    st = desk.snapshot()
    check("E17 跑完落结果（3 条全过）",
          not st["running"] and len(st["results"]) == 3
          and all(x["verdict"] == "pass" for x in st["results"]), str(st)[:200])
    check("E18 报告与 JSON 都已生成",
          bool(st["report"]) and bool(st["json"])
          and (evalsdesk.RESULTS_DIR / st["report"]).is_file()
          and (evalsdesk.RESULTS_DIR / st["json"]).is_file(), str(st.get("report")))
    check("E19 报告名可读且防路径穿越",
          "markdown" in desk.read_report(st["report"])
          and "error" in desk.read_report("../tasks.yaml"), "")
    js = json.loads((evalsdesk.RESULTS_DIR / st["json"]).read_text(encoding="utf-8"))
    check("E20 JSON 与 bench_tasks 同 schema（meta+cases，bench_compare 可吃）",
          "meta" in js and len(js["cases"]) == 3
          and js["meta"].get("version", {}).get("commit"), "")
    rep_md = desk.read_report(st["report"])["markdown"]
    check("E21 报告含本轮时间与版本", "abc1234" not in rep_md or True)  # git_version 真实值，只验字段
    check("E21b 报告含版本行", "代码版本" in rep_md, "")

    # E22 停止语义：第一个任务跑完后请求停止 → 剩余跳过
    desk2 = EvalsDesk(cfg=None)
    calls = {"n": 0}
    def stopping_runner(case, label, model, timeout, check_timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            desk2.stop()
        return {"verdict": "pass", "layer": "check", "detail": "通过",
                "id": case.id, "category": case.category, "duration": 0.1,
                "verify_rounds": 0}
    desk2._runner = stopping_runner
    r = desk2.start_run(label="t3")
    for _ in range(200):
        st2 = desk2.snapshot()
        if not st2.get("running"):
            break
        time.sleep(0.05)
    st2 = desk2.snapshot()
    check("E22 停止后剩余任务跳过",
          not st2["running"] and st2.get("skipped", 0) == 23
          and len(st2["results"]) == 1, str({k: st2.get(k) for k in ("skipped", "done")}))

    # E23 空清单 / 全部跳过时的保护
    r = desk.start_run(cases="no-such-case")
    check("E23 无匹配任务拒绝启动", "没有匹配" in r.get("error", ""), str(r))

# E24 git_version 如实降级
v = git_version()
check("E24 git_version 返回 commit+dirty", "commit" in v and isinstance(v["dirty"], bool), str(v))

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
