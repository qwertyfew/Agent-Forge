"""「写对了还写」的三道机制 + 参数名自愈的自测。

运行：python tools/test_write_once.py
全部断言内建，退出码 0 = 全过。

背景（docs/event-health-20261021.md 附节）：
- 31 轮 w5 里 29 轮把同一个最终文件整写 2~5 次，50 次里约 20 次逐字节相同；
  回执本来就印着「内容未变」，模型照写。**没有任何一处机制在管「写对了还写」**。
- 20 次参数名错里 3 次是 `old_content=`（纯改名），12 次是 `edit_file(content=)`
  （语义不同，不能自动转）。

本测试卡四件事：
1. 幂等写：内容与磁盘一致时不落盘、不建版本，回执明说「已是最新内容」；
2. 乐观并发：同一文件第 3 次整写必须带 base_version，带对了照常放行；
3. 参数别名：纯改名（old_content/new_content）就地改成真名，不白烧一轮；
   语义不同的（edit_file 的 content）**不许**自动转换，只能报错讲清楚；
4. 工具索引：一行一个「名字(必填参数) — 用途」，不再是光秃秃的一串名字。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                        # noqa: E402
from agentforge.evidence import EvidenceLedger              # noqa: E402
from agentforge.record import Recorder, RECORDER_CTX        # noqa: E402
from agentforge.permissions import Guard                    # noqa: E402
from agentforge.tools import build_registry                 # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def setup(tmp: Path):
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.state_dir = tmp / ".agentforge"
    cfg.prewrite_gate = True
    cfg.outputs_dir = ""
    guard = Guard(cfg, approver=None)
    guard.ledger = EvidenceLedger()
    registry = build_registry(guard, cfg)
    return cfg, guard, registry


def run_script(ag, script: str) -> str:
    """在 ops_batch 里跑一段脚本（PTC 脚本线程需要 agent 上下文）。"""
    from agentforge.tools.ops import OPS_AGENT_CTX
    token = OPS_AGENT_CTX.set(ag)
    try:
        return ag._execute("ops_batch", {"script": script},
                           getattr(ag, "_full", True), [])
    finally:
        OPS_AGENT_CTX.reset(token)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, guard, registry = setup(tmp)
        wf = registry.get("write_file")
        rec = Recorder(tmp / "history" / "t1", rel_root=tmp)
        RECORDER_CTX.set(rec)

        v1 = "# 提纲\n\n## 结论\nalpha\n\n## 待办\nbeta\n\n## 风险\ngamma\n"

        # ---------- 1. 幂等写 ----------
        out1 = wf.run(path="outline.md", content=v1)
        check("首稿正常落盘", out1.startswith("已") and (tmp / "outline.md").is_file(), out1[:160])
        check("首稿不改印 base_version（不啰嗦）", "base_version=" not in out1, out1[:300])
        check("首稿记 1 次整写", rec.whole_writes(tmp / "outline.md") == 1,
              str(rec.whole_writes(tmp / "outline.md")))

        out2 = wf.run(path="outline.md", content=v1)
        check("逐字节相同的重写被识别为幂等", out2.startswith("已是最新内容"), out2[:200])
        check("幂等写不计入整写", rec.whole_writes(tmp / "outline.md") == 1,
              str(rec.whole_writes(tmp / "outline.md")))
        check("幂等写明确叫停（不要再拿相同内容写一次）", "不要再拿相同内容" in out2, out2[:300])
        check("幂等写改召回 edit_file / fill_section",
              "edit_file" in out2 and "fill_section" in out2, out2[:300])
        on_disk = (tmp / "outline.md").read_text(encoding="utf-8")
        check("幂等写没有破坏磁盘内容", on_disk == v1)

        # ---------- 2. 乐观并发（OCC / If-Match） ----------
        v2 = v1 + "\n## 补充\ndelta\n"
        out3 = wf.run(path="outline.md", content=v2)
        check("第 2 次整写仍放行", out3.startswith("已"), out3[:200])
        check("第 2 次回执印出 base_version=2", "base_version=2" in out3, out3[-260:])

        v3 = v2 + "\n## 再补\nepsilon\n"
        out4 = wf.run(path="outline.md", content=v3)
        check("第 3 次整写缺 base_version 被拒",
              out4.startswith("[错误]") and "第 3 次整写" in out4, out4[:220])
        check("拒绝里给出确切的调用写法", 'base_version=2' in out4, out4[:400])
        check("拒绝里给出更省的 local 修改路径",
              "edit_file" in out4 and "fill_section" in out4, out4[:400])
        check("被拒的整写没有落盘",
              (tmp / "outline.md").read_text(encoding="utf-8") == v2)

        out5 = wf.run(path="outline.md", content=v3, base_version=2)
        check("带对 base_version 的第 3 次整写放行", out5.startswith("已"), out5[:200])
        check("放行后整写次数推进到 3", rec.whole_writes(tmp / "outline.md") == 3,
              str(rec.whole_writes(tmp / "outline.md")))
        check("第 3 次回执给下一轮 base_version=3", "base_version=3" in out5, out5[-260:])

        out6 = wf.run(path="outline.md", content=v3 + "\n尾注\n", base_version=2)
        check("用旧 base_version 蒙混仍被拒",
              out6.startswith("[错误]"), out6[:200])

        # fail-open：连拦两次就放行。闸门只该把「重复生成」换成「改几行」，
        # 不该把交付物卡死——模型坚决要整写时，宁可多花它的生成，也别不给成品。
        out6b = wf.run(path="outline.md", content=v3 + "\n尾注 2\n")
        check("同一路径连拦两次后 fail-open 放行",
              out6b.startswith("已") and "本次放行" in out6b, out6b[:260])
        check("fail-open 也把内容真的写进去了",
              "尾注 2" in (tmp / "outline.md").read_text(encoding="utf-8"))

        # ---------- 2.5 增量填充不算整写（occprobe 的误判回归） ----------
        # 实测漏过一次：w5 写了 1 次 + 填了 3 节，版本号到 4，第 2 次整写就被
        # OCC 拦了——版本数 ≠ 整写次数，fill_section 是增量，不是重写。
        registry.get("write_skeleton").run(
            path="sk.md", outline="## 一\n待填\n\n## 二\n待填\n")
        registry.get("fill_section").run(path="sk.md", heading="一", content="正文一")
        registry.get("fill_section").run(path="sk.md", heading="二", content="正文二")
        check("骨架/分节填充不计入整写次数",
              rec.whole_writes(tmp / "sk.md") == 0, str(rec.whole_writes(tmp / "sk.md")))
        out_sk = wf.run(path="sk.md", content="# 合并稿\n\n## 一\n正文一\n\n## 二\n正文二\n")
        check("填过若干节后再整写仍放行（不误判成第 N 次重写）",
              out_sk.startswith("已"), out_sk[:200])

        # ---------- 3. 没有任务记录器时不启用 OCC（老行为不变） ----------
        RECORDER_CTX.set(None)
        (tmp / "plain.md").write_text("x\n", encoding="utf-8")
        oks = [wf.run(path="plain.md", content=f"版本 {i}\n") for i in range(4)]
        check("无记录器时连续整写不受限（以免单测/CLI 路径被卡）",
              all(o.startswith("已") for o in oks), str([o[:40] for o in oks]))

        # ---------- 4. 参数别名：纯改名自愈，语义不同不许改 ----------
        tmp2 = tmp / "alias"
        tmp2.mkdir()
        cfg2, guard2, registry2 = setup(tmp2)
        (tmp2 / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        from agentforge.loop import Agent
        ag = Agent(cfg2, client=None, registry=registry2, guard=guard2,
                   router=None, hub=None)
        ag._full = True

        out7 = run_script(ag, "from tools import edit_file\n"
                              "print(edit_file(path='src.py', old_text='return 1',"
                              " new_content='return 2'))\n")
        check("old_text + new_content（纯改名）就地改成 new_text 并成功",
              "已" in out7 and "return 2" in (tmp2 / "src.py").read_text(encoding="utf-8"),
              out7[:300])
        check("改了名字会明确告知（不静默改写）", "已按同名参数处理" in out7
              and "new_content → new_text" in out7, out7[:300])

        out8 = run_script(ag, "from tools import edit_file\n"
                              "print(edit_file(path='src.py', content='整篇内容'))\n")
        check("edit_file 的 content 不被自动转换（语义不同）",
              "[错误]" in out8 or "参数不对" in out8, out8[:300])
        check("报错指向最接近的参数并给两条出路",
              "new_text" in out8 and "write_file" in out8, out8[:400])

        # ---------- 5. 工具索引：名字 + 必填参数 + 用途 ----------
        sel = registry2.selector
        idx = sel.index_text()
        lines = [ln for ln in idx.splitlines() if ln.strip()]
        check("索引一行一个工具（不是一串名字）", len(lines) > 5 and all(ln.startswith("- ") for ln in lines),
              idx[:200])
        check("索引带必填参数", any("git_commit(message)" in ln for ln in lines), idx[:400])
        check("索引带用途", any("— " in ln and len(ln.split("— ", 1)[1]) >= 3 for ln in lines), idx[:400])
        check("每行不超过 62 字符（每轮重贴的固定开销）",
              max(len(ln) for ln in lines) <= 62,
              str(max((len(ln), ln) for ln in lines)))
        # 阈值依据：每行 ≤62 有独立断言守着，这里是总量哨兵。工具集累积增长
        # （ssh_upload、fill_section 等入册）后 1800 被踩线（实测 1824，+1.3%），
        # 放宽到 1900；再显著增长应优先裁描述而不是继续抬阈值。
        check("索引整体不超过 1900 字符", len(idx) <= 1900, f"{len(idx)} 字符")

        # ---------- 6. 权威核对块：跳过的写不许写成「已写入磁盘」 ----------
        # fix01 实测：幂等层拦下重写后，核对块仍印「已写入磁盘」，下一行又说
        # 「未重复落盘」——同一段回包自相矛盾，并把一次没发生的写计进重复覆盖。
        from agentforge.loop import Agent as _Agent
        from agentforge.verify import write_outcomes
        import event_audit as _ea
        cfg3, guard3, reg3 = setup(tmp / "three")
        (tmp / "three").mkdir(parents=True, exist_ok=True)
        ag = _Agent(cfg3, client=None, registry=reg3, guard=guard3,
                    router=None, hub=None)
        out = run_script(ag, (
            "from tools import write_file\n"
            "write_file(path='a.md', content='一样的内容\\n')\n"
            "write_file(path='a.md', content='一样的内容\\n')\n"
        ))
        block = out.split("【系统核对·写盘结果】", 1)[-1]
        check("核对块把跳过写成第三态", "已存在未变" in block, block[:400])
        check("核对块不再对跳过的那次说已写入磁盘",
              block.count("已写入磁盘") == 1, block[:400])
        # 纯跳过（文件本来就是这个内容）：整块只有跳过，必须明说不是失败
        out_skip = run_script(ag, (
            "from tools import write_file\n"
            "write_file(path='a.md', content='一样的内容\\n')\n"
        ))
        blk2 = out_skip.split("【系统核对·写盘结果】", 1)[-1]
        check("全跳过时明确说不是失败",
              "跳过的是重复写" in blk2 and "已写入磁盘" not in blk2, blk2[:400])
        check("收尾解析把跳过当成功", write_outcomes(
            [{"role": "tool", "content": out}]).get("a.md") is True,
            str(write_outcomes([{"role": "tool", "content": out}])))
        check("事件体检不把跳过算成落盘",
              len(_ea.RE_WRITE_OK.findall(block)) == 1,
              str(_ea.RE_WRITE_OK.findall(block)))

    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
