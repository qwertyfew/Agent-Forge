"""预热块按任务类型裁剪（warmup._profile）的回归。

背景：上下文体检实测「项目速览」占系统提示 47.8%（6142/12851），而它对
「读一个 README 并回答三句话」这类任务几乎全是废话——偏偏这块处在免压缩区，
每一轮都要重发。裁剪判据只用任务原文里的词，不问模型（§6.4）。

每组都要有「该裁」和「不该裁」两类断言：裁过头会把开局地图弄丢，模型又开始
瞎猜路径（rerun22 的 `agentforge/core` 幻觉就是这么来的）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import warmup                      # noqa: E402
from agentforge.core.config import Config               # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _ws(tmp: Path) -> None:
    """造一个像本仓的最小工作区：源码目录 + README + external。"""
    (tmp / "README.md").write_text(
        "# Demo\n\n这是一个演示项目，核心原则是：先修上游再加闸门。\n" * 20,
        encoding="utf-8")
    pkg = tmp / "agentforge"
    pkg.mkdir()
    for n in ("loop.py", "context.py", "permissions.py"):
        (pkg / n).write_text("class A:\n    def run(self):\n        pass\n",
                             encoding="utf-8")
    (tmp / "external" / "some-repo").mkdir(parents=True)
    for i in range(3):
        (tmp / "tools").mkdir(exist_ok=True)
        (tmp / "tools" / f"util_{i}.py").write_text("x = 1\n", encoding="utf-8")


def _cfg(tmp: Path) -> Config:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    return cfg


def test_profile_buckets() -> None:
    p = warmup._profile
    check("点名文件 + 只读 → read_named",
          p("读一下工作区 README.md 的前 20 行，然后三句话告诉我项目是什么")["intent"]
          == "read_named")
    check("点名文件 + 改造 → edit_named",
          p("把 agentforge/loop.py 里的澄清触发改一下")["intent"] == "edit_named")
    check("纯问（无文件） → qa",
          p("解释一下这个项目怎么做上下文压缩")["intent"] == "qa")
    check("要动代码（无文件） → code",
          p("重构 ops_batch 的脚本执行逻辑，然后跑一下测试")["intent"] == "code")
    check("read_named 不给目录树", p("读 README.md")["tree"] == 0)
    check("read_named 不给模块清单", p("读 README.md")["modules"] is False)
    check("code 保留模块清单", p("重构脚本执行逻辑")["modules"] is True)


def test_block_trimming() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _ws(tmp)
        cfg = _cfg(tmp)

        b_read, s_read = warmup.build_block(cfg, "读一下 README.md 前 20 行，用三句话说说项目是什么")
        check("read_named：块体量被压到 1/3 以内",
              s_read["chars"] < 1200, str(s_read))
        check("read_named：不再注入目录结构", "【目录结构】" not in b_read)
        check("read_named：不再注入源码模块清单", "【源码模块清单】" not in b_read)
        check("read_named：不给 README 摘要（任务本来就要读它）",
              "【README 摘要】" not in b_read)

        b_code, s_code = warmup.build_block(cfg, "重构脚本执行逻辑并跑测试")
        check("code：保留目录结构（开局地图不能丢）", "【目录结构】" in b_code)
        check("code：保留源码模块清单（防路径幻觉）", "【源码模块清单】" in b_code)
        check("code：保留 README 摘要", "【README 摘要】" in b_code)
        check("code：块比 read_named 大（该给的地图还在）",
              s_code["chars"] > s_read["chars"], f"{s_code['chars']} vs {s_read['chars']}")

        b_qa, s_qa = warmup.build_block(cfg, "解释一下这个项目是做什么的")
        check("qa：给短目录但砍掉模块清单",
              "【目录结构】" in b_qa and "【源码模块清单】" not in b_qa)
        check("qa：块体量小于 code", s_qa["chars"] < s_code["chars"],
              f"{s_qa['chars']} vs {s_code['chars']}")

        b_edit, s_edit = warmup.build_block(cfg, "改一下 agentforge/loop.py 的注释")
        check("edit_named：保留少量地图（改完可能连带改别的）",
              "【源码模块清单】" in b_edit, b_edit[:200])
        check("edit_named：不给外部仓库清单", "【外部仓库清单】" not in b_edit)

        # 上限仍然生效：任务写得再泛，也不能把系统提示撑爆
        cfg.warmup_max_chars = 500
        b_cap, s_cap = warmup.build_block(cfg, "重构脚本执行逻辑")
        check("上限截断仍生效（<= 上限 + 截断提示）",
              0 < s_cap["chars"] <= 520, str(s_cap["chars"]))

        # 开关关掉：一个字都不注入
        cfg.warmup_enabled = False
        b_off, s_off = warmup.build_block(cfg, "重构脚本执行逻辑")
        check("开关关闭 → 空块", b_off == "" and s_off["chars"] == 0)


if __name__ == "__main__":
    test_profile_buckets()
    test_block_trimming()
    print("全部通过。")
