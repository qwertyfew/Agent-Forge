# -*- coding: utf-8 -*-
"""向本机 AgentForge 派发一个任务，边跑边把 SSE 落盘并汇总。

用法：python .run/dispatch_task.py <tag> "<任务文本>"

SSE 的坑（上一版栽在这）：事件类型在 `event: <type>` 行里，`data:` 的 JSON **不含** type 字段。
只读 `data:` 会让每个事件都变成 type=None，汇总打印成空——不是任务没跑，是解析错了。

落盘：.run/<tag>.events.jsonl（逐条事件）、.run/<tag>.raw.log（原始 SSE）
       .run/<tag>.reply.txt（最终答复）
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

tag = sys.argv[1] if len(sys.argv) > 1 else "task"
MESSAGE = sys.argv[2] if len(sys.argv) > 2 else "帮我查一下 git 上那个 REA 项目（Reverse Engineer Anything）是干什么的，介绍一下。"

payload = json.dumps({
    "message": MESSAGE,
    "history": [],
    "model_key": "auto",
    "permission": "full",
    "no_user": True,   # 无人值守派发：ask_user 不弹窗不阻塞，直接降级
}).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:8787/api/chat",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
# 本机 shell 带 http_proxy（沙箱透明代理），会劫持 127.0.0.1 → 强制直连
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ev_out = open(ROOT / ".run" / f"{tag}.events.jsonl", "w", encoding="utf-8")
raw_out = open(ROOT / ".run" / f"{tag}.raw.log", "w", encoding="utf-8")

tools: list[str] = []
tool_rows: list[str] = []
deltas: list[str] = []
final_assistant = ""      # 收尾 assistant 事件（系统侧声明挂在这上面，delta 流里没有）
history_msgs: list[dict] = []
timeline: list[tuple[float, str]] = []
t0 = time.time()

# 模块耗时累加（harness 已在每轮 stats / tool_result 事件里带上计时字段）
llm_ms_total = 0.0
eval_ms_total = 0.0
tool_ms_total = 0.0
tool_ms_by_name: dict[str, float] = {}
llm_calls = 0

with opener.open(req, timeout=5400) as resp:
    ev_type = ""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace")
        raw_out.write(line)
        raw_out.flush()
        s = line.rstrip("\r\n")
        if s.startswith("event:"):
            ev_type = s[6:].strip()
            continue
        if not s.startswith("data:"):
            continue
        body = s[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            ev = json.loads(body)
        except json.JSONDecodeError:
            continue
        ev["type"] = ev_type                      # 类型在 event: 行里
        ev_out.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        ev_out.flush()

        ts = time.time() - t0
        if ev_type == "tool_start":
            name = ev.get("name", "?")
            args = json.dumps(ev.get("args") or {}, ensure_ascii=False)
            tools.append(name)
            tool_rows.append(f"{name} {args}")
            timeline.append((ts, f"tool_start  {name}  {args[:150]}"))
        elif ev_type == "tool_result":
            out = str(ev.get("output") or "")
            head = out.strip().splitlines()[0] if out.strip() else ""
            d = float(ev.get("dur_ms") or 0)
            tool_ms_total += d
            _nm = ev.get("name") or "?"
            tool_ms_by_name[_nm] = tool_ms_by_name.get(_nm, 0.0) + d
            timeline.append((ts, f"tool_result {ev.get('name')}  <- {head[:110]}"))
        elif ev_type == "assign":
            timeline.append((ts, f"assign      #{ev.get('index')} {ev.get('kind')} -> "
                                 f"{ev.get('model_name')} (reassigned={ev.get('reassigned')})"))
        elif ev_type == "skill":
            timeline.append((ts, f"skill       {ev.get('name')}"))
        elif ev_type == "usage":
            timeline.append((ts, f"usage       第 {ev.get('round')} 轮 · "
                                 f"{ev.get('tool_calls')} 次工具 · tokens={ev.get('tokens')}"))
        elif ev_type == "stats":
            llm_calls += 1
            llm_ms_total += float(ev.get("llm_ms") or 0)
            eval_ms_total += float(ev.get("eval_ms") or 0)
        elif ev_type == "delta":
            deltas.append(ev.get("text") or "")
        elif ev_type == "assistant":
            # 收尾闸门会把系统侧核查声明（未交付 / 未经核实）拼在 summary 末尾，
            # 再作为 assistant 事件回吐。SSE 只发 delta，这条不采集就等于声明
            # 从未到达读者——rerun26 的「0 工具调用却像通过了」正是这么来的。
            final_assistant = (ev.get("content") or "").strip()
        elif ev_type == "history":
            history_msgs = ev.get("messages") or []
        elif ev_type == "error":
            timeline.append((ts, f"ERROR       {ev.get('message')}"))

ev_out.close()
raw_out.close()
elapsed = time.time() - t0

reply = "".join(deltas).strip()
# 收尾 assistant 事件 = 同一段正文 + 系统侧声明。它比 delta 流长且以流内容开头时，
# 以它为准，否则系统声明在评测记录里会被整段丢掉（闸门判了「未交付」，读者看不到）。
if (final_assistant and len(final_assistant) > len(reply)
        and (not reply or final_assistant.startswith(reply[:80]))):
    reply = final_assistant
if not reply and history_msgs:                    # 兜底：从 history 取最后一条 assistant
    for m in reversed(history_msgs):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            reply = m["content"]
            break

# 工具序列以 history 为准（SSE 的 tool_start 可能因压缩丢几条）
seq = [((tc.get("function") or {}).get("name") or tc.get("name"))
       for m in history_msgs for tc in (m.get("tool_calls") or [])]
seq = [s for s in seq if s]

first_party = [t for t in seq if t in ("fetch_url", "fetch_json", "browser_snapshot")]
declared = ("未经核实" in reply) or ("系统侧核查" in reply)

print("=" * 74)
print(f"提示词：{MESSAGE}")
print(f"耗时：{elapsed:.0f}s   工具调用：{len(tools)} 次")
# ---- 模块耗时拆解：回答「慢在流程/工具，还是慢在 LLM 生成」----
_total_ms = elapsed * 1000.0
_overhead_ms = max(_total_ms - llm_ms_total - tool_ms_total, 0.0)
_pct = lambda x: (x / _total_ms * 100.0) if _total_ms else 0.0  # noqa: E731
print("耗时拆解：")
print(f"  总耗时    {_total_ms/1000:7.1f}s  100.0%")
print(f"  LLM 回复  {llm_ms_total/1000:7.1f}s  {_pct(llm_ms_total):5.1f}%"
      f"   （{llm_calls} 次调用；其中 Ollama 纯推理 {eval_ms_total/1000:.1f}s）")
print(f"  工具执行  {tool_ms_total/1000:7.1f}s  {_pct(tool_ms_total):5.1f}%"
      f"   （含 ops_batch 脚本内工具）")
print(f"  流程开销  {_overhead_ms/1000:7.1f}s  {_pct(_overhead_ms):5.1f}%"
      f"   （路由/编排/序列化/收尾）")
if tool_ms_by_name:
    print("  工具耗时明细：")
    for _n, _v in sorted(tool_ms_by_name.items(), key=lambda kv: -kv[1]):
        print(f"    {_n:16s} {_v/1000:7.1f}s")
print("-" * 74)
for ts, msg in timeline:
    print(f"  [{ts:6.1f}s] {msg}")
print("-" * 74)
print(f"工具调用序列（以 history 为准）：{seq}")
print(f"一手来源调用：{first_party or '（无）'}")
print(f"用过 web_search：{'web_search' in seq}")
print(f"答复里出现「未经核实」类声明：{declared}")
print("=" * 74)
print("\n----- 最终答复（全文） -----")
print(reply)

(ROOT / ".run" / f"{tag}.reply.txt").write_text(reply, encoding="utf-8")
(ROOT / ".run" / f"{tag}.tools.txt").write_text("\n".join(tool_rows), encoding="utf-8")


def _relocate(tag: str) -> None:
    """把这一轮的运行日志归进案卷：history/<任务>/record/run/。

    派发时还不知道任务目录名（由编排层按发起时间算），跑完从台账就能对上。
    这样「这一轮怎么跑的」和「这一轮产出了什么」都在同一个文件夹里，
    复盘不用在 .run 和 history 之间来回对编号。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from agentforge import layout
        dst = layout.record_run_dir(ROOT, tag)
    except Exception:  # noqa: BLE001 —— 归档失败不该影响派发结果
        return
    if dst is None:
        return
    pairs = [(".events.jsonl", "events.jsonl"), (".raw.log", "raw.log"),
             (".reply.txt", "reply.txt"), (".tools.txt", "tools.txt"),
             (".prompt.txt", "prompt.txt")]
    moved = []
    for suf, name in pairs:
        src = ROOT / ".run" / f"{tag}{suf}"
        if src.is_file():
            dst.mkdir(parents=True, exist_ok=True)
            src.replace(dst / name)
            moved.append(name)
    if moved:
        print(f"\n运行日志已归入案卷：{dst.relative_to(ROOT).as_posix()}/"
              f"（{', '.join(moved)}）")


_relocate(tag)

