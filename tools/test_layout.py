"""任务目录布局自测：案卷结构（record/ + output/）。

运行：python tools/test_layout.py

要证明的四件事：
1. 一次任务的全部文件都在 history/<任务>/ 里：过程记录在 record/，最终产出在 output/；
2. 中间/过渡文件按模型给的相对路径落进 record/work/（保留子目录），不摊在任务根上；
3. 任务书点名的交付物仍写在声明路径（不搬动工程文件）；
4. 收尾镜像把交付物复制进 output/，并跳过 history/ 自身（不自我嵌套）。
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
    finally:
        OUTDIR_CTX.reset(ot)
        WRITE_ALLOW.reset(wt)
    check("B 中间文件进 record/work 并保留子目录",
          p_mid == (tmp / outdir / "record" / "work" / "notes" / "step1.json").resolve(),
          str(p_mid))
    check("B 任务书点名的交付物写声明路径（不搬动工程文件）",
          p_out == (tmp / "evals" / "report.md").resolve(), str(p_out))

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

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
