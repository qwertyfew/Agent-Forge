"""「实时画面」与「切回来看到的回看画面」必须同构。

事故（2026-10-21 用户报）：任务流式输出完、侧栏状态点变绿后，切到别的任务
再切回来，正文内容不一样了。根因有两条，都在展示层：

  ① 核查闸门打回的版本已经流到了浏览器（delta 是逐字发的），但那一版不会
     交付。回看读的是台账 history，里面对**每一版**打回稿都留了一条 assistant
     ——rerun61 实测：同一轮回答留下 5 段正文（1415/247/1233/1097/311 字），
     其中两段几乎重复。实时画面则是「打回版 + 重写版」拼成的一段。
     两边都不是最终版，而且互不相同。

  ② 内部注入的 user 消息（核查诊断、记忆回灌、澄清答复、steering）在实时视图
     里不可见，回看却把每一条都渲染成「用户提问」气泡——一次任务能多出 3~4 个
     用户从没说过的问题（rerun61：5 个 user 气泡里只有 1 个是真的）。

修法（本次）：
  - 后端：打回/修正轮发 `gate_discard`；assistant 收尾时如果正文不是「已流出
    内容的追加」就发 `delta{replace:true}` —— 三态对齐见 webui._align_stream。
  - 前端：`gate_discard` 清掉当前正文段；`renderHistory` 过 `normalizeHistory`
    滤掉内部消息与被作废的草稿。

本测试钉两件事：
  A. `_align_stream` 三态（这是「重写版正文能不能到浏览器」的唯一判据）
  B. `normalizeHistory` 的过滤口径（含反例：澄清答复不该作废它前面的答复）

运行：python tools/test_transcript_view.py（B 需要 node；找不到就跳过并说明）
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# A. 三态对齐
# ---------------------------------------------------------------------------
from webui import _align_stream                                  # noqa: E402

# 正常路径：模型边流边写，收尾只在尾部补了系统声明 → 只补差值
d, r = _align_stream("第一段正文", "第一段正文\n\n（系统侧：未交付）")
check("A1 追加尾段只发差值", d == "\n\n（系统侧：未交付）" and not r, f"{d!r} {r}")

# delta 已经流完，summary 与已流出内容相同 → 什么都不发（别把整段重发一遍）
d, r = _align_stream("第一段正文", "第一段正文")
check("A2 完全相同不发", d == "" and not r, f"{d!r} {r}")

# 核查打回后的重写：前后两版毫无前缀关系 → 必须整段替换
d, r = _align_stream("推荐使用 Marp，安装：npm i -g @marp-team/marp-cli",
                     "目前无法提供准确的安装命令，源里没有。")
check("A3 整段重写发 replace", r and d.startswith("目前无法提供"), f"{d!r} {r}")

# 打回后 discard 已经清空 streamed（空 base）→ 整段写入，不能当成追加
d, r = _align_stream("", "第二版正文")
check("A4 discard 后整段写入", r and d == "第二版正文", f"{d!r} {r}")

# 收尾把正文截短（新正文是旧正文的前缀）→ 不是重写，别清屏
d, r = _align_stream("正文\n\n（系统侧核查）", "正文")
check("A5 截短不当作重写", d == "" and not r, f"{d!r} {r}")

# 只差尾部空白：不算重写
d, r = _align_stream("正文   ", "正文")
check("A6 只差尾空白不算重写", d == "" and not r, f"{d!r} {r}")


# ---------------------------------------------------------------------------
# B. normalizeHistory（在 node 里跑真实前端代码，不维护第二份实现）
# ---------------------------------------------------------------------------
def find_node() -> str | None:
    for cand in (shutil.which("node"),
                 r"C:\Users\admin\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"):
        if cand and Path(cand).exists():
            return cand
    return None


NODE = find_node()
if NODE is None:
    print("[SKIP] 找不到 node，跳过前端过滤口径测试")
else:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("const INTERNAL_USER_RX")
    end = html.index("function renderHistory(")
    fn_src = html[start:end]

    fixture = [
        {"role": "user", "content": "帮我把 REA 这个项目介绍清楚"},                 # 真提问
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "ops_batch", "arguments": "{}"}}]},
        {"role": "tool", "content": "[ops_batch] 检索到 3 个候选"},
        {"role": "assistant", "content": "推荐使用 marp-cli，安装：npm i -g @marp-team/marp-cli"},  # 打回1
        {"role": "user", "content": "【答复核查未通过 · 第 1/2 轮】命令在源里没有依据"},  # 诊断
        {"role": "assistant", "content": "源里没有安装命令，我需要再查。"},             # 打回2
        {"role": "user", "content": "【系统侧核查 · 需要修正】你描述了不存在的文件"},   # 诊断
        {"role": "assistant", "content": "最终版：marp-cli 需先 npm i，详见官方 README。"},  # 交付版
        {"role": "user", "content": "【系统侧验证结果（权威，不可推翻）】验证失败"},     # 诊断（末尾）
        {"role": "assistant", "content": "最终版（含验证修正）：marp-cli 需先 npm i。"},   # 交付版
    ]
    clarify = [
        {"role": "assistant", "content": "工作区里没找到 REA。请问您说的是哪一个？"},
        {"role": "user", "content": "（澄清弹窗答复）你刚才追问「哪一个」，用户的答复：git 上的 morluto/rea"},
        {"role": "assistant", "content": "明白了，是 morluto/rea，下面介绍它。"},
    ]

    script = f"""
const fs = require('fs');
{fn_src}
const fixture = {json.dumps(fixture, ensure_ascii=False)};
const clarify = {json.dumps(clarify, ensure_ascii=False)};
const vis = ms => normalizeHistory(ms).filter(m => !m._internal && !m._superseded);
const out = {{}};

// B1 内部诊断消息一个都不能当作用户气泡
const v1 = vis(fixture);
out.user_bubbles = v1.filter(m => m.role === 'user').length;
out.user_texts = v1.filter(m => m.role === 'user').map(m => m.content);

// B2 被打回的草稿全部隐藏，只留最后一版
out.asst_texts = v1.filter(m => m.role === 'assistant' && (m.content || '').trim())
                   .map(m => m.content);
out.superseded = normalizeHistory(fixture).filter(m => m._superseded).length;

// B3 反例：澄清答复是「信息补充」，不能把它前面的追问版作废
const v2 = vis(clarify);
out.clarify_users = v2.filter(m => m.role === 'user').length;
out.clarify_asst = v2.filter(m => m.role === 'assistant' && (m.content || '').trim()).length;

// B4 正常对话（无内部消息）必须原样保留
const plain = [
  {{role:'user', content:'你好'}},
  {{role:'assistant', content:'你好，有什么可以帮你？'}},
  {{role:'user', content:'再讲一段'}},
  {{role:'assistant', content:'好的，这是第二段。'}},
];
out.plain_users = vis(plain).filter(m => m.role === 'user').length;
out.plain_asst = vis(plain).filter(m => m.role === 'assistant').length;

// B5 不能改写调用方传进来的台账对象（回看后还要拿它当上下文继续对话）
const before = JSON.stringify(fixture);
normalizeHistory(fixture);
out.mutated = before !== JSON.stringify(fixture);

// B6 前端脚本整体语法可解析（提取 <script> 用 Function 只解析不执行）
const html = fs.readFileSync('web/index.html', 'utf8');
const m = html.match(/<script>([\\s\\S]*)<\\/script>/);
try {{ new Function(m[1]); out.js_syntax = true; }}
catch (e) {{ out.js_syntax = false; out.js_err = String(e); }}

console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "norm_test.js"
        js.write_text(script, encoding="utf-8")
        res = subprocess.run([NODE, str(js)], cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8")
    if res.returncode != 0:
        check("B0 normalizeHistory 可执行", False, (res.stderr or "")[-400:])
    else:
        out = json.loads(res.stdout.strip().splitlines()[-1])
        check("B1 内部诊断不渲染成用户气泡",
              out["user_bubbles"] == 1 and out["user_texts"] == ["帮我把 REA 这个项目介绍清楚"],
              f"bubbles={out['user_bubbles']} texts={out['user_texts']}")
        check("B2 被打回的草稿全部隐藏，只留交付版",
              out["superseded"] == 3
              and out["asst_texts"] == ["最终版（含验证修正）：marp-cli 需先 npm i。"],
              f"superseded={out['superseded']} asst={out['asst_texts']}")
        check("B3 澄清答复是信息补充，不作废前面的追问版",
              out["clarify_users"] == 0 and out["clarify_asst"] == 2,
              f"users={out['clarify_users']} asst={out['clarify_asst']}")
        check("B4 普通对话原样保留",
              out["plain_users"] == 2 and out["plain_asst"] == 2,
              f"users={out['plain_users']} asst={out['plain_asst']}")
        check("B5 不改写调用方传入的台账对象", out["mutated"] is False, str(out.get("mutated")))
        check("B6 前端脚本语法可解析", out["js_syntax"] is True, str(out.get("js_err"))[:300])

# B7 真实事故数据：rerun61 一次任务留下 5 版正文 + 5 个 user 气泡，过滤后应只剩 1 + 1
run_file = ROOT / ".run" / "rerun61.events.jsonl"
if NODE and run_file.exists():
    hist = None
    for line in run_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "history":
            hist = ev.get("messages") or []
    if hist:
        script2 = f"""
const fs = require('fs');
{fn_src}
const hist = JSON.parse(fs.readFileSync({json.dumps(str(run_file.parent / '_hist61.json'))}, 'utf8'));
const v = normalizeHistory(hist).filter(m => !m._internal && !m._superseded);
console.log(JSON.stringify({{
  users: v.filter(m => m.role === 'user').length,
  asst: v.filter(m => m.role === 'assistant' && (m.content || '').trim()).map(m => m.content.length),
}}));
"""
        tmp_json = run_file.parent / "_hist61.json"
        tmp_json.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            js2 = Path(td) / "real.js"
            js2.write_text(script2, encoding="utf-8")
            r2 = subprocess.run([NODE, str(js2)], cwd=str(ROOT), capture_output=True,
                                text=True, encoding="utf-8")
        try:
            tmp_json.unlink()
        except OSError:
            pass
        if r2.returncode == 0:
            out2 = json.loads(r2.stdout.strip().splitlines()[-1])
            check("B7 rerun61 真实台账：5 气泡 5 版 → 1 气泡 1 版",
                  out2["users"] == 1 and out2["asst"] == [311],
                  str(out2))
        else:
            check("B7 rerun61 真实台账", False, (r2.stderr or "")[-300:])

print()
if FAILED:
    print(f"{len(FAILED)} 条未过：" + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
