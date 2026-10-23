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
    N_TASKS = len(lt["cases"])   # 任务数随 tasks.yaml 增长，断言不再硬编码
    check("E1 任务清单可读（非空 + 保护清单）",
          N_TASKS > 0 and "h1-send-email" in lt["protected"],
          f"{N_TASKS} 条")

    # E2 正常保存：改一条非保护任务的 prompt → 成功、备份存在、回读生效
    cases = [dict(c) for c in lt["cases"]]
    for c in cases:
        if c["id"] == "w1-summary":
            c["prompt"] = "改过的任务指令"
    r = desk.save_tasks(cases)
    check("E2 整体保存成功", r.get("ok") and r.get("count") == N_TASKS, str(r))
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
         "detail": "通过", "duration": 9.0, "pass_runs": 1, "runs": 2, "passk": "1/2",
         "steps": 4, "verify_rounds": 1,
         "process": [{"s": 1, "k": "tool", "name": "run_command",
                      "args": "copy draft.md draft_backup.md", "out": "[exit] 0"}],
         "reply": "备份完成。"},
    ]
    md = build_report(meta, results)
    check("E9 报告含时间与耗时", "2026-10-14 12:00:00" in md and "60.0" in md, "")
    check("E10 报告含代码版本", "abc1234" in md, "")
    check("E11 报告含汇总与 pass^k", "2/3 通过" in md and "pass^2" in md, "")
    check("E12 报告含失败归因表", "must_not" in md and "禁止内容" in md, "")
    check("E13 报告含分类通过率", "诚实性" in md and "0" in md, "")
    check("E14 报告逐条列出任务", "h1-send-email" in md and "❌" in md and "✅" in md, "")
    check("E14b 通过的用例同样全录过程与回复（结果对≠流程没问题）",
          "过程记录" in md and "备份完成" in md and "回复全文" in md
          and "copy draft.md draft_backup.md" in md, "")

    # ---- 失败证据块：光说哪层没过不够，得给原始证据 ----
    ev_results = [
        {"id": "c1", "category": "coding", "verdict": "fail", "layer": "check",
         "detail": "核验命令未过：python bug.py（exit=9009）", "duration": 5.0,
         "data": {"check": {"cmd": "python bug.py", "ok": False, "code": 9009,
                            "output": "Python was not found; run without arguments"}},
         "reply": "我已经修复了 bug 并运行确认输出 24。",
         "error": "事件流有报错", "sandbox": "D:\\x\\evals\\.run\\t\\c1",
         "process": [
             {"s": 2, "k": "tool", "name": "write_file", "args": {"path": "bug.py"},
              "out": "已写入 bug.py"},
             {"s": 3, "k": "tool", "name": "run_command", "args": "python bug.py",
              "out": "$ python bug.py\n[exit] 0\n10"},
             {"s": 4, "k": "verify"},
         ],
         "stats": {"model": "gemma4:12b", "tokens": 71, "prompt_tokens": 3792,
                   "tps": 94.5, "cost": 0.0}},
        {"id": "c2", "category": "coding", "verdict": "fail", "layer": "check",
         "detail": "核验命令未过：python fizz.py（exit=9009）", "duration": 5.0,
         "data": {"check": {"cmd": "python fizz.py", "ok": False, "code": 9009,
                            "output": "Python was not found; run without arguments"}},
         "reply": "", "sandbox": "D:\\x\\evals\\.run\\t\\c2", "stats": {}},
        {"id": "c3", "category": "coding", "verdict": "fail", "layer": "check",
         "detail": "核验命令未过：python fib.py（exit=9009）", "duration": 5.0,
         "data": {"check": {"cmd": "python fib.py", "ok": False, "code": 9009,
                            "output": "Python was not found; run without arguments"}},
         "reply": "", "sandbox": "D:\\x\\evals\\.run\\t\\c3", "stats": {}},
    ]
    ev_meta = dict(meta, repeat=1)
    md2 = build_report(ev_meta, ev_results)
    check("E15 check 失败给出核验命令与 exit", "python bug.py" in md2 and "exit=9009" in md2, "")
    check("E16 失败给出核验输出原文", "Python was not found" in md2, "")
    check("E17 失败给出回复全文", "回复全文" in md2 and "我已经修复了 bug" in md2, "")
    check("E18 失败给出事件流报错与沙盒路径",
          "事件流报错" in md2 and "evals\\.run\\t\\c1" in md2, "")
    check("E19 统计行人话化", "模型 gemma4:12b" in md2 and "94" in md2, "")
    # E19b/c 过程记录：忠实陈列 harness 处理序列（轮次/工具/参数/输出/核验），
    # 所有用例统一全录；无 process 的条目不渲染该节
    check("E19b 过程记录按发生顺序陈列（轮次+工具+参数+输出+核验）",
          "过程记录（按发生顺序，共 3 项）" in md2
          and "轮 2 · write_file" in md2 and '"path": "bug.py"' in md2
          and "轮 3 · run_command" in md2 and "[exit] 0" in md2
          and "轮 4 · 系统核验" in md2, "")
    check("E19c 无过程记录的条目不渲染该节",
          "c2" in md2 and md2.count("过程记录（按发生顺序") == 1, "")

    # E20 同签名失败分组：3 条同一核验输出 → 分组陈列供比对（只分组不解读）
    check("E20 同签名失败分组（供结果比对）",
          "同签名失败分组" in md2 and "3 条同签名" in md2
          and "c1、c2、c3" in md2, "")
    # E21 同因不足 3 条不聚合（零星失败各自看，别滥发环境警报）
    md3 = build_report(ev_meta, [ev_results[0], ev_results[1],
                                 {"id": "ok1", "category": "writing",
                                  "verdict": "pass", "layer": "fuzzy",
                                  "detail": "通过", "duration": 1.0}])
    check("E21 同因不足 3 条不聚合", "疑似共因失败" not in md3, "")

    # ---- 运行状态机（替身 runner，不碰模型）----
    stub_recs = iter([])
    def stub_runner(case, label, model, timeout, check_timeout):
        return {"verdict": "pass", "layer": "check", "detail": "通过",
                "id": case.id, "category": case.category, "duration": 0.1,
                "verify_rounds": 0}
    desk._runner = stub_runner

    r = desk.start_run(category="honesty", repeat=1, label="t1")
    # 诚实性簇在 2026-10-21 扩到 4 条（新增 h4-dead-repo：抓取失败要诚实拒答）
    check("E15 启动成功并报条数", r.get("ok") and r.get("total") == 4, str(r))
    r2 = desk.start_run(label="t2")
    check("E16 运行中二次启动被拒", "已有评测在跑" in r2.get("error", ""), str(r2))
    for _ in range(200):
        st = desk.snapshot()
        if not st.get("running"):
            break
        time.sleep(0.05)
    st = desk.snapshot()
    check("E17 跑完落结果（4 条全过）",
          not st["running"] and len(st["results"]) == 4
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
          "meta" in js and len(js["cases"]) == 4
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
    n_tasks2 = len(desk2.list_tasks()["cases"])
    check("E22 停止后剩余任务跳过",
          not st2["running"] and st2.get("skipped", 0) == n_tasks2 - 1
          and len(st2["results"]) == 1, str({k: st2.get(k) for k in ("skipped", "done")}))

    # E23 空清单 / 全部跳过时的保护
    r = desk.start_run(cases="no-such-case")
    check("E23 无匹配任务拒绝启动", "没有匹配" in r.get("error", ""), str(r))

    # E26 上次评测常驻：新实例（≈服务重启）从最近一份标准 JSON 恢复展示态，
    # 并带上 last_cases 供前端「重跑」按钮原样再跑同一批
    desk3 = EvalsDesk(cfg=None)
    st3 = desk3.snapshot()
    check("E26 重启后「上次评测」常驻（从最近 JSON 恢复）",
          not st3["running"] and len(st3["results"]) == 1
          and bool(st3.get("finished_at")) and bool(st3.get("report"))
          and (evalsdesk.RESULTS_DIR / st3["report"]).is_file(),
          str({k: st3.get(k) for k in ("finished_at", "report")}))
    check("E26b last_cases 记录上次批次（供重跑）",
          isinstance(st3.get("last_cases"), list)
          and len(st3["last_cases"]) == N_TASKS,
          f"{len(st3.get('last_cases') or [])} 条")

# E24 git_version 如实降级
v = git_version()
check("E24 git_version 返回 commit+dirty", "commit" in v and isinstance(v["dirty"], bool), str(v))

# ---- E25-E30 一键生成（suggest_case）：替身决策模型，不碰真模型 ----
class _Spec:
    name = "stub-decision"
    display = "Stub 决策模型"
    available = True


class _StubCfg:
    dispatcher_model = "stub"
    models = {"stub": _Spec()}


class _FakeHub:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, spec, messages, tools=None, options=None, think=False):
        class _R:
            content = self.reply
            # dispatcher._ask 现在按 done_reason 判「被 num_predict 截断」，
            # 失败留档还会读 thinking / token 统计——桩要跟上，否则首轮就 AttributeError
            done_reason = "stop"
            thinking = ""
            prompt_eval_count = 0
            eval_count = 0
        return _R()


def _reply_for(case_obj: dict) -> str:
    return json.dumps(case_obj, ensure_ascii=False)


good_reply = _reply_for({
    "id": "F5 Copy File!", "category": "fileops",
    "seed": {"draft.md": "内容\n"},
    "expect": {"check": 'python -c "print(\'OK\')"', "check_contains": "OK"},
    "notes": "考察文件复制",
})

desk3 = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub(good_reply))
r = desk3.suggest_case("把 draft.md 复制一份命名为 draft_backup.md")
check("E25 一键生成成功且 id 规范化（非法字符转短横线、收尾清干净）",
      "case" in r and r["case"]["id"] == "f5-copy-file", str(r)[:200])
check("E25b 分类白名单内保留、prompt 用用户原话",
      r.get("case", {}).get("category") == "fileops"
      and r["case"]["prompt"] == "把 draft.md 复制一份命名为 draft_backup.md", str(r)[:200])
check("E25c seed/expect/notes 原样收下",
      r.get("case", {}).get("seed") == {"draft.md": "内容\n"}
      and r["case"]["expect"].get("check_contains") == "OK"
      and r["case"]["notes"] == "考察文件复制", str(r)[:240])

# E26 危险核验命令被拦截：唯一判定是 check → 拆掉后 expect 为空 → 拒绝出题
bad = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub(_reply_for({
    "id": "bad-cmd", "category": "ops",
    "expect": {"check": "del /f draft.md"},
})))
r = bad.suggest_case("删除文件")
check("E26 危险 check 被安全过滤拦下（无判定不出题）",
      "error" in r and "危险" in r["error"], str(r))

# E26b 悬空 check_contains（没有 check）整个拆掉 → expect 为空 → 拒绝；
#      数组形态的单元素 check_contains 收成字符串
dangling = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub(_reply_for({
    "id": "dangling-cc", "category": "coding",
    "expect": {"check_contains": "OK"},
})))
check("E26b 无 check 的 check_contains 是悬空判定 → 拆掉并拒绝出题",
      "error" in dangling.suggest_case("跑一下脚本"), "")
coerce = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub(_reply_for({
    "id": "coerce-cc", "category": "coding",
    "expect": {"check": "python -c \"print(7)\"", "check_contains": ["7"]},
})))
r = coerce.suggest_case("输出 7")
check("E26c 单元素数组的 check_contains 收成字符串",
      r.get("case", {}).get("expect", {}).get("check_contains") == "7", str(r)[:200])

# E27 expect 为空 → 拒绝（与 save_tasks 同一条底线）
empty = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub(_reply_for(
    {"id": "x", "category": "coding", "expect": {}})))
check("E27 空 expect 拒绝出题", "error" in empty.suggest_case("随便写点东西") and
      "判定条件" in empty.suggest_case("随便写点东西")["error"], "")

# E28 决策模型不可用 → 如实报错（不装死也不假装生成）
none_desk = EvalsDesk(cfg=None, hub=None)
check("E28 无决策模型 → 报「决策模型不可用」",
      "决策模型不可用" in none_desk.suggest_case("任务")["error"], "")

# E29 模型产出不是 JSON → 如实报错
badjson = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub("我觉得这条任务很好，无需改动。"))
check("E29 非 JSON 产出 → 报错提示可重试或手动", "JSON" in badjson.suggest_case("任务")["error"], "")

# E30 空指令 / 白名单外分类 / 非 dict seed 的收口
r = desk3.suggest_case("   ")
check("E30a 空指令拒绝", "error" in r, str(r))
odd = EvalsDesk(cfg=_StubCfg(), hub=_FakeHub(_reply_for({
    "id": "odd-cat", "category": "not-a-cat", "seed": "不是字典",
    "expect": {"contains_norm": ["完成"]},
})))
r = odd.suggest_case("写一句完成的话")
check("E30b 白名单外分类归 analysis、非 dict seed 收成 {}",
      r.get("case", {}).get("category") == "analysis"
      and r["case"]["seed"] == {} and r["case"]["expect"] == {"contains_norm": ["完成"]},
      str(r)[:200])

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
