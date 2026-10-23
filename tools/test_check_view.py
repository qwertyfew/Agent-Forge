"""程序侧核查声明：一个版本、单独成卡。

事故（2026-10-21 用户报）：「有答案文案，然后又会有文案，说上面这些信息没有核实，
推荐我核实。」两件事同时在犯：

  ① **同一任务两副面孔**。六道核查闸门把声明拼在 summary 末尾，但台账 history 里
     那条 assistant 消息还是模型的原文——实时画面有声明，切走再切回来（回看读
     history）声明就没了。打回 2 轮的声明走 loop.py，本来就写进了正文，所以这是
     两条路径不一致，不是新语义。
  ② **形态错了**。声明和正文挤在同一段，读者先把结论读完才被告知「别信」；而且
     视觉上像「答案之后又冒出第二段答案」。

修法：
  - 后端：`_verify_gate` 拼完声明后，把带声明的正文写回最后一条**有正文的**
    assistant 消息（只管有正文的，别把声明挂到纯 tool_calls 消息上）。
  - 前端：`splitCheckTrailer` 把句尾的声明段剥出来，`mountCheckCard` 渲染成独立
    卡片；实时收尾与回看重绘调用同一个函数，两边长得一模一样。

本测试钉两件事：
  A. 后端：声明进 summary 的同时必进 history，且两者逐字相等（回看 == 实时）
  B. 前端：尾注识别的边界（只剥句尾的声明段，正文里的 `---` 不许吞）

运行：python tools/test_check_view.py（B 需要 node；找不到就跳过并说明）
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.orchestrator import _verify_gate        # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _call(name: str, args: dict | None = None) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}


def _tool(name: str) -> dict:
    return {"role": "tool", "content": f"{name} 的结果"}


# 真实事故里的总结原文（与 tools/test_sources_gate.py 同源）
REA_SUMMARY = (
    "REA 是一个把逆向工程流程交给 agent 的项目。\n"
    "- 活跃度：Star 数已达 1w+，单日涨星超 4600。\n"
    "- 最近更新：2026 年 7 月仍有显著动态。\n"
)


# ---------------------------------------------------------------------------
# A. 后端：summary 与 history 必须同一版
# ---------------------------------------------------------------------------
def test_parity() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_check_"))

    class FakeCfg:
        verify_gate = True
        sources_gate = True
        local_facts_gate = False       # 另有专测，这里只钉「外部事实」这一道
        workspace_root = str(tmp)

    class FakeAgent:
        cfg = FakeCfg()

    brief = "检索 GitHub 上的项目 REA，介绍一下它现在的热度"
    search_only = [_call("web_search"), _tool("web_search")]

    # A1 命中：声明进 summary，也要进 history，且逐字相同
    convo = search_only + [{"role": "assistant", "content": REA_SUMMARY}]
    holder = {"summary": REA_SUMMARY, "convo": convo}
    list(_verify_gate(FakeAgent(), brief, holder))
    last = [m for m in holder["convo"] if m.get("role") == "assistant"
            and str(m.get("content") or "").strip()][-1]
    check("A1 命中时答复末尾带系统侧声明", "【系统侧核查】" in holder["summary"],
          holder["summary"][-160:])
    check("A1 回看（history 最后一条正文）与实时逐字相同",
          last.get("content") == holder["summary"],
          f"hist={len(str(last.get('content')))} summary={len(holder['summary'])}")
    check("A1 声明只出现一次（没有叠加两遍）",
          holder["summary"].count("【系统侧核查】") == 1,
          str(holder["summary"].count("【系统侧核查】")))
    check("A1 前面的工具消息没被改动",
          holder["convo"][0] == _call("web_search") and holder["convo"][1] == _tool("web_search"))

    # A2 只搜索又抓了一手来源：不附声明，正文与 history 都原样
    convo2 = search_only + [_call("fetch_json"), _tool("fetch_json"),
                            {"role": "assistant", "content": REA_SUMMARY}]
    holder2 = {"summary": REA_SUMMARY, "convo": convo2}
    list(_verify_gate(FakeAgent(), brief, holder2))
    check("A2 有了一手来源就不附声明", "【系统侧核查】" not in holder2["summary"])
    check("A2 不附声明时正文一字不改", holder2["summary"] == REA_SUMMARY)
    check("A2 不附声明时 history 也不改",
          holder2["convo"][-1]["content"] == REA_SUMMARY)

    # A3 历史里没有「有正文的 assistant」（只有工具调用）：声明另起一条，
    #    不许挂到纯 tool_calls 消息上——那等于凭空多出一段正文
    convo3 = list(search_only)
    holder3 = {"summary": REA_SUMMARY, "convo": convo3}
    list(_verify_gate(FakeAgent(), brief, holder3))
    asst_with_text = [m for m in holder3["convo"]
                      if m.get("role") == "assistant" and str(m.get("content") or "").strip()]
    check("A3 纯工具消息不带正文", convo3[0].get("content") == "")
    check("A3 另起一条带声明的答复消息",
          len(asst_with_text) == 1 and "【系统侧核查】" in asst_with_text[0]["content"],
          f"n={len(asst_with_text)}")


# ---------------------------------------------------------------------------
# B. 前端：尾注识别（在 node 里跑真实前端代码，不维护第二份实现）
# ---------------------------------------------------------------------------
test_parity()


def find_node() -> str | None:
    for cand in (shutil.which("node"),
                 r"C:\Users\admin\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"):
        if cand and Path(cand).exists():
            return cand
    return None


NODE = find_node()
if NODE is None:
    print("[SKIP] 找不到 node，跳过前端尾注识别测试")
else:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("const CHECK_HEAD_RX")
    end = html.index("function renderHistory(")
    fn_src = html[start:end]

    cases = {
        # 外部事实核查（最常见的一种）
        "one": "正文结论：star 1w+。\n\n---\n【系统侧核查】本回复含来自联网检索的外部事实，"
               "下列内容属「转述 · 未经核实」，请勿直接采信：\n- 1w+",
        # 两道闸门同时出手：两条声明
        "two": "正文。\n\n---\n【系统侧核查】缺一手来源。\n- 1w+\n\n---\n"
               "【系统侧核查 · 任务未交付】\n文件不存在",
        # 打回 2 轮放行时 loop.py 写进正文的那一段
        "loop": "正文。\n\n---\n⚠ **程序核查未通过（已打回 2 轮仍不合格）**：2 条命令找不到。",
        # 验证收尾报告（可能通过）
        "verify": "正文。\n\n---\n【系统侧验证】任务收尾时由系统亲自运行了 `pytest` → **通过**。",
        # 正文中间的正常分隔线：不是声明，不许剥
        "mid": "第一段。\n\n---\n第二段：这是正文的一部分。",
        # 根本没有声明的普通答复
        "none": "就是一段普通答复，没有任何核查。",
    }

    script = f"""
const fs = require('fs');
{fn_src}
const cases = {json.dumps(cases, ensure_ascii=False)};
const out = {{}};
for(const k in cases){{
  const r = splitCheckTrailer(cases[k]);
  out[k] = {{ body: r.body, n: r.checks.length, fails: checkFails(r.checks),
              joined_len: r.checks.join('').length,
              text_len: cases[k].length }};
}}
const html = fs.readFileSync('web/index.html', 'utf8');
const m = html.match(/<script>([\\s\\S]*)<\\/script>/);
try {{ new Function(m[1]); out.js_syntax = true; }}
catch (e) {{ out.js_syntax = false; out.js_err = String(e); }}
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "check_view.js"
        js.write_text(script, encoding="utf-8")
        res = subprocess.run([NODE, str(js)], cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8")
    if res.returncode != 0:
        check("B0 尾注识别可执行", False, (res.stderr or "")[-400:])
    else:
        out = json.loads(res.stdout.strip().splitlines()[-1])
        one = out.get("one", {})
        check("B1 单条声明：正文剥干净、卡片 1 条",
              one.get("n") == 1 and "系统侧核查" not in one.get("body", "")
              and one.get("fails") is True, str(one)[:200])
        two = out.get("two", {})
        check("B2 两道闸门 → 两条声明（带后缀的【…· 任务未交付】也认）",
              two.get("n") == 2 and "任务未交付" not in two.get("body", ""), str(two)[:200])
        loop = out.get("loop", {})
        check("B3 打回 2 轮那段也单独成卡", loop.get("n") == 1 and loop.get("fails") is True,
              str(loop)[:200])
        ver = out.get("verify", {})
        check("B4 验证收尾报告也成卡，但不算「未通过」",
              ver.get("n") == 1 and ver.get("fails") is False, str(ver)[:200])
        mid = out.get("mid", {})
        check("B5 正文中间的分隔线不许吞（反例）",
              mid.get("n") == 0 and "第二段" in mid.get("body", ""), str(mid)[:200])
        none = out.get("none", {})
        check("B6 没有声明的答复原样不动",
              none.get("n") == 0 and none.get("body") == "就是一段普通答复，没有任何核查。",
              str(none)[:200])
        check("B7 前端脚本语法可解析", out.get("js_syntax") is True,
              str(out.get("js_err"))[:300])

print()
if FAILED:
    print(f"{len(FAILED)} 条未过：" + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
