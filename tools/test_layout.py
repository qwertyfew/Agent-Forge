"""任务目录布局自测：案卷结构（record/ + output/）。

运行：python tools/test_layout.py

要证明的五件事：
1. 一次任务的全部文件都在 history/<任务>/ 里：过程记录在 record/，最终产出在 output/；
2. 中间/过渡文件按模型给的相对路径落进 record/work/（保留子目录），不摊在任务根上；
3. 任务书点名的交付物（含 P23 裸文件名、P27 组合路径）进交付区 output/<声明路径>；
4. 收尾镜像把根上的产出复制进 output/，并跳过 history/ 自身（不自我嵌套）；
5. 读侧镜像（P28）：output/ → record/work/ → 根 的解析顺序，写进哪、从哪读。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import layout                                # noqa: E402
from agentforge.config import Config                         # noqa: E402
from agentforge.permissions import (                         # noqa: E402
    Guard, OUTDIR_CTX, WRITE_ALLOW)
from agentforge.record import Recorder                        # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_layout_"))
    outdir = "history/20261020-120000"

    # ---------- A) 目录名约定 ----------
    check("A 记录目录", layout.record_dir(outdir) == f"{outdir}/record",
          layout.record_dir(outdir))
    check("A 中间文件目录", layout.work_dir(outdir) == f"{outdir}/record/work",
          layout.work_dir(outdir))
    check("A 产出目录", layout.output_dir(outdir) == f"{outdir}/output",
          layout.output_dir(outdir))
    check("A history 判定", layout.under_history("history/x/output/a.md")
          and not layout.under_history("evals/a.md"))

    # ---------- B) 写盘落点 ----------
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    guard = Guard(cfg, approver=None)
    ot = OUTDIR_CTX.set(outdir)
    wt = WRITE_ALLOW.set(frozenset({"evals/report.md"}))
    try:
        p_mid = guard.resolve_for_write("notes/step1.json")
        p_out = guard.resolve_for_write("evals/report.md")
        # 裸文件名放行（P23 修复）：任务书点名的 fizzbuzz.py 必须落在 workspace
        # 根——run_command/核验都在根上跑，落进 record/work/ 会让「写完就运行」
        # 的编码主流程断掉。任务书没点名的裸文件名仍重定向（中间文件的保证不变）。
        wt2 = WRITE_ALLOW.set(frozenset({"evals/report.md", "fizzbuzz.py"}))
        try:
            p_bare = guard.resolve_for_write("fizzbuzz.py")
            p_bare2 = guard.resolve_for_write("draft.md")
        finally:
            WRITE_ALLOW.reset(wt2)
    finally:
        OUTDIR_CTX.reset(ot)
        WRITE_ALLOW.reset(wt)
    check("B 中间文件进 record/work 并保留子目录",
          p_mid == (tmp / outdir / "record" / "work" / "notes" / "step1.json").resolve(),
          str(p_mid))
    # P28：点名交付物进交付区 output/（按声明路径保留子目录）
    check("B 任务书点名的交付物进交付区 output/（按声明路径）",
          p_out == (tmp / outdir / "output" / "evals" / "report.md").resolve(), str(p_out))
    check("B 任务书点名的裸文件名进交付区 output/（P23+P28）",
          p_bare == (tmp / outdir / "output" / "fizzbuzz.py").resolve(), str(p_bare))
    check("B 没点名的裸文件名仍进 record/work（中间文件保证不变）",
          p_bare2 == (tmp / outdir / "record" / "work" / "draft.md").resolve(),
          str(p_bare2))

    # ---------- B2) P27 组合路径：目录零件 × 裸文件名 ----------
    # （提取逻辑内联自 loop.py 的 _write_allow 构造；此处验证语义而非实现）
    import re as _re
    text = "创建 reports/2026 两级目录，并在 reports/2026 里新建一个空的 summary.md。"
    allow: set[str] = set()
    for m in _re.finditer(r"(?:\./|(?:[\w.\-]+/)+)[\w.\-]+\.[A-Za-z0-9]{1,8}", text):
        allow.add(m.group(0).replace("\\", "/").lstrip("./"))
    bare: set[str] = set()
    for m in _re.finditer(r"(?<![\w.\-/])[\w\-]+\.[A-Za-z0-9]{1,8}(?![\w.\-/])", text):
        bare.add(m.group(0))
    allow |= bare
    dirs: set[str] = set()
    for m in _re.finditer(r"(?<![\w.\-/])(?:[\w\-]+/)+[\w\-]+(?![\w.\-/])", text):
        d = m.group(0).strip("./")
        if "/" in d and d not in allow:
            dirs.add(d)
    if dirs and bare and len(dirs) * len(bare) <= 400:
        for d in dirs:
            for b in bare:
                allow.add(f"{d}/{b}")
    check("B2 组合路径进放行名单（f1 的根因）",
          "reports/2026/summary.md" in allow, str(sorted(allow)))
    ot3 = OUTDIR_CTX.set(outdir)
    wt3 = WRITE_ALLOW.set(frozenset(allow))
    try:
        p_comp = guard.resolve_for_write("reports/2026/summary.md")
    finally:
        WRITE_ALLOW.reset(wt3)
        OUTDIR_CTX.reset(ot3)
    check("B2 组合路径落交付区 output/",
          p_comp == (tmp / outdir / "output" / "reports" / "2026" / "summary.md").resolve(),
          str(p_comp))

    # ---------- B3) 读侧镜像（P28）：写进哪、从哪读 ----------
    (tmp / outdir / "record" / "work" / "notes").mkdir(parents=True, exist_ok=True)
    (tmp / outdir / "record" / "work" / "notes" / "step1.json").write_text(
        "{}", encoding="utf-8")
    (tmp / outdir / "output" / "evals").mkdir(parents=True, exist_ok=True)
    (tmp / outdir / "output" / "evals" / "report.md").write_text(
        "# 报告\n", encoding="utf-8")
    (tmp / "seed_root_only.txt").write_text("root", encoding="utf-8")
    ot2 = OUTDIR_CTX.set(outdir)
    try:
        r_mid = guard.resolve_for_read("notes/step1.json")
        r_out = guard.resolve_for_read("evals/report.md")
        r_root = guard.resolve_for_read("seed_root_only.txt")
        r_dot = guard.resolve_for_read(".")
        r_miss = guard.resolve_for_read("不存在.md")
    finally:
        OUTDIR_CTX.reset(ot2)
    check("B3 写进 work/ 的中间文件同路径可读",
          r_mid == (tmp / outdir / "record" / "work" / "notes" / "step1.json").resolve(),
          str(r_mid))
    check("B3 交付物从 output/ 读回",
          r_out == (tmp / outdir / "output" / "evals" / "report.md").resolve(),
          str(r_out))
    check("B3 只在根上的文件根回退可读",
          r_root == (tmp / "seed_root_only.txt").resolve(), str(r_root))
    check("B3 list_dir('.') 的家是交付区 output/",
          r_dot == (tmp / outdir / "output").resolve(), str(r_dot))
    check("B3 都不存在时返回第一候选（output/）且不越界",
          r_miss == (tmp / outdir / "output" / "不存在.md").resolve(), str(r_miss))

    # ---------- C) 过程记录落在 record/ ----------
    rec = Recorder(tmp / outdir / "record", rel_root=tmp)
    rec.read(tmp / "src.py", ranges=[(1, 20)], total_lines=50)
    check("C evidence.jsonl 在 record/ 下",
          (tmp / outdir / "record" / "evidence.jsonl").is_file())

    # ---------- D) 收尾镜像 ----------
    (tmp / "evals").mkdir(parents=True, exist_ok=True)
    (tmp / "evals" / "report.md").write_text("# 报告\n", encoding="utf-8")
    (tmp / outdir / "record" / "work").mkdir(parents=True, exist_ok=True)
    (tmp / outdir / "record" / "work" / "notes.json").write_text("{}", encoding="utf-8")
    files = [{"path": "evals/report.md"}, {"path": "history/x/output/old.md"}]
    done = layout.mirror_outputs(files, tmp, outdir)
    check("D 交付物被镜像进 output/",
          (tmp / outdir / "output" / "evals" / "report.md").is_file(), str(done))
    check("D 镜像清单只含任务外的产出", done == ["evals/report.md"], repr(done))
    check("D 不镜像 history/ 里的文件（避免自我嵌套）",
          not (tmp / outdir / "output" / "history").exists())
    check("D 中间文件不进 output/",
          not (tmp / outdir / "output" / "record").exists())

    # ---------- E) 轮次编号 → 案卷目录（运行日志归档用） ----------
    import json
    import time
    from datetime import datetime
    ts = datetime(2026, 10, 20, 10, 55, 39).timestamp()
    (tmp / "history" / "20261020-105539").mkdir(parents=True, exist_ok=True)
    (tmp / ".tasks.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "created_at": ts, "prompt": "【本轮编号：rerun31】\n# 任务…"}]},
        ensure_ascii=False), encoding="utf-8")
    d = layout.task_dir_for_run(tmp, "rerun31")
    check("E 按台账把轮次编号映射到任务目录",
          d is not None and d.name == "20261020-105539", str(d))
    rd = layout.record_run_dir(tmp, "rerun31")
    check("E 运行日志目录是 record/run/",
          rd is not None and rd.parent.name == "record" and rd.name == "run", str(rd))
    check("E 台账里没有的轮次返回 None", layout.task_dir_for_run(tmp, "rerun99") is None)
    # 目录名与 created_at 差 1 秒（截断 vs 取整）也要能对上
    (tmp / "history" / "20261020-105539").rename(tmp / "history" / "20261020-105540")
    d2 = layout.task_dir_for_run(tmp, "rerun31")
    check("E 目录名差 1 秒仍能按窗口匹配",
          d2 is not None and d2.name == "20261020-105540", str(d2))

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
