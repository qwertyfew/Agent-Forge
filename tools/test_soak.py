"""浸泡台账的抽取规则：既不漏记，也别把每一条都记成「要修」。

这套测试钉的是 `tools/soak.py` 的**判读口径**，不是评测结果本身——
台账是拿来裁定的，口径歪了会比没有台账更糟（人会照着歪的清单去修）。

覆盖三类：
1. 判定失败（必须修的那一类）——层与证据都要在；
2. 「判过了但有话要说」——盖章、写盘失败却宣称完成、只答不做、慢；
3. 指纹与汇总——同样的病在几十轮里要能合并成一条，不稳定用例要单独列出来。

运行：./.venv/Scripts/python.exe tools/test_soak.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.soak as soak  # noqa: E402


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"测试未通过: {name}")


def _rec(**kw) -> dict:
    base = {"id": "x", "category": "coding", "verdict": "pass", "layer": "-",
            "detail": "通过", "steps": 3, "tools": ["ops_batch"], "gates": 0,
            "reply": "做完了。", "duration": 80.0, "process": []}
    base.update(kw)
    return base


def rules(issues: list[dict]) -> set[str]:
    return {i["rule"] for i in issues}


def main() -> int:
    # ① 判定失败 → high，层与 detail 必须跟着走
    a = soak.scan_record(_rec(verdict="fail", layer="check", detail="测试未输出 ALL-OK"))
    _check("① 失败记成 eval_fail/high", "eval_fail" in rules(a)
           and [i for i in a if i["rule"] == "eval_fail"][0]["sev"] == "high")
    _check("② 失败条目带层与说明",
           any("check" in i["note"] and "ALL-OK" in i["note"] for i in a))

    # ③ 通过但盖了「关键内容没回显」的章：必须记，因为可能是误伤
    b = soak.scan_record(_rec(reply="已写入。\n【系统侧核查】系统在最终答复里未能核对到它们"))
    _check("③ 盖章记成 stamp:no_echo（mid）",
           "stamp:no_echo" in rules(b)
           and [i for i in b if i["rule"] == "stamp:no_echo"][0]["sev"] == "mid")

    # ④ 未核实声明：同类但分开计数
    c = soak.scan_record(_rec(reply="以上未经核实，不作为事实依据。"))
    _check("④ 未核实记成 stamp:unverified", "stamp:unverified" in rules(c))

    # ⑤ 写盘失败 + 答复仍宣称完成 —— c6 rerun64 的同款，必须 high
    proc = [{"s": 2, "k": "tool", "name": "ops_batch",
             "out": "【系统核对·写盘结果】\n  - edit_file → pricing.py: 写入失败"}]
    d = soak.scan_record(_rec(process=proc, reply="已完成重构，测试通过。"))
    hit = [i for i in d if i["rule"] == "write_failed"]
    _check("⑤ 最后一次写盘失败 → write_failed/high", bool(hit) and hit[0]["sev"] == "high")
    _check("⑥ 且标出「仍宣称完成」", bool(hit) and "仍宣称完成" in hit[0]["note"])

    # ⑤b 中途失败、后来补写成功 = 模型自愈，只能记 low。
    # 这是 soak02 的 c6：edit_file 用了 content= 报参数错，下一轮改 write_file 才写成。
    # 若按「只要出现过写入失败就 high」，台账会被这类自愈淹没（规则 v1 就误报过）。
    proc = [
        {"s": 2, "k": "tool", "name": "ops_batch",
         "out": "【系统核对·写盘结果】\n  - edit_file → src/p.py: 写入失败\n"
                "  - run_command → (未给 path): 已写入磁盘"},
        {"s": 3, "k": "tool", "name": "ops_batch",
         "out": "【系统核对·写盘结果】\n  - write_file → src/p.py: 已写入磁盘"},
    ]
    e0 = soak.scan_record(_rec(process=proc, reply="重构已完成。"))
    _check("⑥b 先败后成 → 记 write_retry/low",
           "write_retry" in rules(e0) and "write_failed" not in rules(e0))
    _check("⑥c 且 run_command 的成功不算写类成功",
           [i for i in e0 if i["rule"] == "write_retry"][0]["sev"] == "low")

    # ⑤c 只有 run_command 说「已写入磁盘」→ 不算写过，不出 write 类条目
    e1 = soak.scan_record(_rec(process=[{"s": 2, "k": "tool", "name": "ops_batch",
                                         "out": "【系统核对·写盘结果】\n"
                                                "  - run_command → (未给 path): 已写入磁盘"}]))
    _check("⑥d run_command 单独成功 → 不判写盘",
           "write_failed" not in rules(e1) and "write_retry" not in rules(e1))

    # ⑦ 零调用（脚本在三引号里）：mid，不是 high——它常常下一轮就被纠正
    e = soak.scan_record(_rec(process=[{"s": 1, "k": "tool", "name": "ops_batch",
                                       "out": "[ops_batch] 0 次工具调用 —— 脚本执行了"}]))
    hit = [i for i in e if i["rule"] == "tool_error"]
    _check("⑦ 零调用记 mid", bool(hit) and hit[0]["sev"] == "mid")

    # ⑧ 只答不做：该动手的类别零工具要记；纯问答不记（否则每轮都刷）
    f = soak.scan_record(_rec(category="writing", steps=1, tools=[]), category="writing")
    g = soak.scan_record(_rec(category="qa", steps=1, tools=[]), category="qa")
    _check("⑧ writing 一步零工具 → answer_only", "answer_only" in rules(f))
    _check("⑨ qa 一步零工具 → 不记", "answer_only" not in rules(g))

    # ⑩ 闸门活动：种类要能读出来（issues 既可能是 dict 也可能是 str）
    h = soak.scan_record(_rec(process=[
        {"s": 2, "k": "gate", "round": 1, "issues": [{"kind": "zero_retrieval"}]},
        {"s": 3, "k": "gate", "round": 2, "issues": ["retrieval_miss"]},
    ]))
    hit = [i for i in h if i["rule"] == "gate"]
    _check("⑩ 闸门种类读出两种", bool(hit) and "zero_retrieval" in hit[0]["note"]
           and "retrieval_miss" in hit[0]["note"])

    # ⑪ 慢：5 分钟以上才记，299s 不记
    _check("⑪ 400s → slow", "slow" in rules(soak.scan_record(_rec(duration=400))))
    _check("⑫ 299s → 不记", "slow" not in rules(soak.scan_record(_rec(duration=299))))

    # ⑬ 指纹：数字与路径不同，仍应合并成同一条
    k1 = soak.fingerprint({"rule": "eval_fail", "case": "c6", "sev": "high",
                           "note": "check: 缺少 3 个标题（耗 402s）C:\\a\\b.py"})
    k2 = soak.fingerprint({"rule": "eval_fail", "case": "c6", "sev": "high",
                           "note": "check: 缺少 7 个标题（耗 88s）D:\\x\\y\\z.py"})
    _check("⑬ 同病不同数字 → 同一指纹", k1 == k2, f"{k1} != {k2}")

    # ⑭ 真记录回归：rerun64 是 2/5，三条红必须都进台账
    real = ROOT / "evals" / "results" / "rerun64.json"
    if real.exists():
        scan = soak.scan_result(real, soak.load_category_map())
        _check("⑭ rerun64 计到 5 条、2 通过",
               scan["total"] == 5 and scan["passed"] == 2,
               f"{scan['passed']}/{scan['total']}")
        fails = [i for i in scan["issues"] if i["rule"] == "eval_fail"]
        _check("⑮ rerun64 记到 3 条失败", len(fails) == 3, str(len(fails)))
        _check("⑯ 失败里含评测层的 tool 层（f5）",
               any(i["layer"] == "tool" for i in fails),
               str([i["layer"] for i in fails]))
        # 真记录里 c6 那次「edit_file 少参数 → 一个字没改 → 宣称完成」必须仍是 high
        wf = [i for i in scan["issues"] if i["rule"] == "write_failed"]
        _check("⑯b rerun64 的 c6 记成 write_failed/high",
               bool(wf) and wf[0]["case"] == "c6-refactor-discount"
               and wf[0]["sev"] == "high", str(wf)[:200])

    # ⑰ 汇总：过得过、失过失 → 不稳定用例列出来
    rounds = [
        {"label": "t01", "total": 2, "passed": 2, "seconds": 10.0,
         "cases": [{"id": "a", "verdict": "pass"}, {"id": "b", "verdict": "pass"}],
         "issues": []},
        {"label": "t02", "total": 2, "passed": 1, "seconds": 12.0,
         "cases": [{"id": "a", "verdict": "fail", "layer": "check"},
                   {"id": "b", "verdict": "pass"}],
         "issues": [{"rule": "eval_fail", "sev": "high", "case": "a",
                     "layer": "check", "note": "check: 产物没写出"}]},
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "summary.md"
        soak.write_summary(rounds, p, rounds_total=50)
        md = p.read_text(encoding="utf-8")
    _check("⑰ 汇总标出不稳定的 a", "不稳定" in md and "`a`" in md, md[:200])
    _check("⑱ 汇总写明轮次 2/50", "2/50" in md)
    _check("⑲ 汇总按指纹计数 1 次", "| 1 | `eval_fail` | a |" in md, md[-400:])

    # ⑳ 每轮台账：有问题出表格，没问题也要留一行（证明这轮真跑过）
    md_ok = soak._round_md({"label": "t01", "total": 5, "passed": 5,
                            "seconds": 400.0, "issues": [], "cases": []}, "18:00")
    md_bad = soak._round_md({"label": "t02", "total": 5, "passed": 3,
                             "seconds": 420.0, "issues": [
                                 {"rule": "eval_fail", "sev": "high", "case": "c6",
                                  "layer": "check", "note": "a|b"}], "cases": []}, "18:10")
    _check("⑳ 无问题轮次写「本轮无问题」", "本轮无问题" in md_ok)
    _check("㉑ 有问题的说明里竖线被转义", "a\\|b" in md_bad, md_bad)

    # ㉒-㉖ 耗时表：只做统计不改判定。要能被「单轮异常」和「整体变慢」区分开
    tr = [
        {"label": "s1", "total": 2, "passed": 2, "seconds": 100.0,
         "cases": [{"id": "a", "verdict": "pass", "duration": 40.0},
                   {"id": "b", "verdict": "pass", "duration": 60.0}], "issues": []},
        {"label": "s2", "total": 2, "passed": 2, "seconds": 400.0,
         "cases": [{"id": "a", "verdict": "pass", "duration": 30.0},
                   {"id": "b", "verdict": "pass", "duration": 370.0}], "issues": []},
    ]
    tm = "\n".join(soak._timing_md(tr))
    _check("㉒ 每轮一行、墙钟与各用例都在", "| s1 | 100 | 40 | 60 | 2/2 |" in tm, tm[:400])
    _check("㉓ 最慢一轮被点名（400s → 6.7 分钟）", "最长 6.7（s2）" in tm, tm[-600:])
    _check("㉔ 去掉最长一轮后的均值单列（说明是单轮异常）",
           "去掉最长一轮后均值 1.7 分钟" in tm, tm[-600:])
    _check("㉕ 指出最贵的用例决定固定成本", "最贵的是 `b`（占 86%）" in tm, tm[-300:])
    _check("㉖ 空数据不炸", "还没有数据" in "\n".join(soak._timing_md([])))
    # 旧记录没有 duration 时按 0 计，不能抛异常
    old = [{"label": "o1", "total": 1, "passed": 1, "seconds": 60.0,
            "cases": [{"id": "a", "verdict": "pass"}], "issues": []}]
    _check("㉖b 缺 duration 的旧记录按 0 计",
           "| o1 | 60 | 0 | 1/1 |" in "\n".join(soak._timing_md(old)))

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
