"""长期记忆的回归测试：记忆里**只放偏好**，不再分层。

锁的是一处设计回退：记忆曾经按语义粒度分 L1 事实 / L2 约定 / L3 偏好三层，
注入时还按任务类型分层检索。可事实（路径 / 端口 / 版本）有工具能现查、
约定（工作方式 / 流程规则）只有架构管得住——两类写进记忆都只是「劝」。
于是分层整套拿掉，记忆收成「一层的偏好清单」。

断言写的是不变量（不是实现细节，改回去就会红）：
- `MemoryStore` 里再没有「层」这个概念（`layer` 字段、`layer_counts`、`LAYERS`、`SCOPE_WEIGHTS`）；
- 落盘的行是 `- [时间 · 任务] 正文`，没有层标签；
- **旧格式（带 ` · L2`）的行仍读得进来，且层标签被就地丢弃**（老记忆文件不用迁移）；
- 注入块只列正文、按时间正序，超出预算丢最旧的（不再按层分配预算）；
- `remember` 工具没有 `layer` 参数；`autonote` 只产出偏好文本、并摘掉模型偶尔给的 `[L1]` 前缀。

运行：.venv/Scripts/python.exe tools/test_memory.py
不需要模型、不需要联网，退出码 0 = 全过。
"""
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import autonote, loop, memory  # noqa: E402
from agentforge.memory import MemoryStore  # noqa: E402

failed = 0


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  —— {detail}"))
    if not cond:
        global failed
        failed += 1


def read(path):
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


tmp = Path(tempfile.mkdtemp(prefix="af_memory_"))
try:
    # ---------- 用例 A：分层机制整体拿掉（符号级） ----------
    check("memory 模块不再导出 LAYERS / LAYER_LABEL / DEFAULT_LAYER",
          not any(hasattr(memory, n) for n in ("LAYERS", "LAYER_LABEL", "DEFAULT_LAYER")))
    check("memory 模块不再有 classify_layer / normalize_layer",
          not any(hasattr(memory, n) for n in ("classify_layer", "normalize_layer")))
    check("memory 模块不再有分层检索预算 SCOPE_WEIGHTS / SCOPE_ORDER",
          not any(hasattr(memory, n) for n in ("SCOPE_WEIGHTS", "SCOPE_ORDER")))
    check("MemoryStore 不再有 layer_counts（界面那行分层统计的来源）",
          not hasattr(MemoryStore, "layer_counts"))
    check("__all__ 收成 MemoryStore / PROMPT_MAX_CHARS",
          set(memory.__all__) == {"MemoryStore", "PROMPT_MAX_CHARS"}, str(memory.__all__))
    check("loop 不再有 memory_scope（按任务类型挑检索深度的那把尺）",
          not hasattr(loop, "memory_scope"))
    check("autonote 不再有 _LAYER_PREFIX 层前缀",
          not hasattr(autonote, "_LAYER_PREFIX"))
    check("autonote 不再导出 parse_model_notes_typed（层与文本的二元组入口）",
          not hasattr(autonote, "parse_model_notes_typed"))

    # ---------- 用例 B：写入的行没有层标签 ----------
    store = MemoryStore(tmp / "memory.md")
    e1 = store.append("用户偏好：回复先给结论", "t1")
    check("append 返回的条目没有 layer 字段", "layer" not in e1, repr(e1))
    line1 = (tmp / "memory.md").read_text(encoding="utf-8").splitlines()[-1]
    check("落盘行是「- [时间 · 任务] 正文」形态", line1.startswith("- [") and " · t1] " in line1,
          line1)
    check("落盘行里没有 L1/L2/L3 标记", " · L" not in line1, line1)

    # ---------- 用例 C：entries 不带层 ----------
    store.append("用户偏好：产出文档一律简体中文", "t1")
    es = store.entries()
    check("entries 里每条都没有 layer 字段", all("layer" not in e for e in es))
    check("entries 能取到两条", len(es) == 2, str(len(es)))

    # ---------- 用例 D：旧格式（带 · L2）读得进来、层标签被丢弃 ----------
    legacy = tmp / "legacy.md"
    legacy.write_text(
        "# AgentForge 工作区记忆\n\n"
        "> 说明行不是条目\n\n"
        "- [2026-10-13 13:16 · manual · L2] 项目约定：改动前先跑一遍测试\n"
        "- [2026-10-13 14:00 · t9 · L3] 用户偏好：先给结论\n",
        encoding="utf-8",
    )
    ls = MemoryStore(legacy)
    le = ls.entries()
    check("旧格式行仍被解析成条目（老的 memory.md 不用迁移）", len(le) == 2, str(le))
    check("表头与说明行不算条目", all(not e["text"].startswith(("#", ">")) for e in le))
    check("旧格式行尾的层标签被丢弃，不混进正文",
          le[0]["text"] == "项目约定：改动前先跑一遍测试", repr(le[0]["text"]))
    check("旧格式条目的 task 也解析正确（没有把 · L2 吃进 task）",
          le[0]["task"] == "manual", repr(le[0]["task"]))

    # ---------- 用例 E：注入块只列正文、按时间正序 ----------
    ls.append("用户偏好：注释用中文", "t9")
    block = ls.prompt_block()
    check("注入块无分层标题（不再有 ### 分组）", "###" not in block, repr(block))
    check("注入块每行是「- 正文」", all(l.startswith("- ") for l in block.splitlines()))
    lines = block.splitlines()
    check("注入块按时间正序（最旧的在前）",
          lines[0].endswith("改动前先跑一遍测试") and lines[-1].endswith("注释用中文"),
          repr(lines))

    # ---------- 用例 F：超出预算丢最旧的，不再按层分配 ----------
    big = MemoryStore(tmp / "big.md")
    for i in range(6):
        big.append(f"偏好第{i}条：" + "细" * 40, "t2")
    one = big.prompt_block(max_chars=200)
    check("超预算时只放得下最近几条（丢最旧的）",
          "偏好第0条" not in one and "偏好第5条" in one, one[:80])
    check("prompt_block 只收 max_chars 一个预算参数（没有 scope）",
          list(inspect.signature(MemoryStore.prompt_block).parameters) == ["self", "max_chars"])
    check("prompt_block 不再接受 scope 关键字",
          "scope" not in inspect.signature(MemoryStore.prompt_block).parameters)
    check("append 只收 note / task_id（没有 layer 参数）",
          list(inspect.signature(MemoryStore.append).parameters) == ["self", "note", "task_id"])

    # ---------- 用例 G：remember 工具没有 layer 参数 ----------
    from agentforge.tools.memory import build_memory_tools

    class _Cfg:
        def state(self):
            return tmp

    tools = build_memory_tools(guard=None, cfg=_Cfg())
    remember = next(t for t in tools if t.name == "remember")
    props = remember.schema()["function"]["parameters"]["properties"]
    check("remember 的参数里没有 layer", "layer" not in props, str(list(props)))
    check("remember 只需要 note", set(props) == {"note"}, str(list(props)))

    # ---------- 用例 H：模型抽取只输出偏好文本、摘掉层前缀 ----------
    # 层前缀的几种写法都要摘掉——记忆已不分层，但模型偶尔还会按旧习惯标一行
    for raw_line, want in [
        ("- [L1] 本地 Ollama 端口是 11434", "本地 Ollama 端口是 11434"),
        ("1. L2: 改动前先跑一遍测试", "改动前先跑一遍测试"),
        ("（L3）用户偏好：回复先给结论", "用户偏好：回复先给结论"),
        ("【L3】用户偏好：回复要简洁", "用户偏好：回复要简洁"),
        ("- L2- 不要记录任务过程", "不要记录任务过程"),
    ]:
        got = autonote.parse_model_notes(raw_line)
        check(f"层前缀被摘掉：{raw_line[:10]}…", got == [want], repr(got))

    notes = autonote.parse_model_notes("\n".join([
        "NONE",
        "请只完成这一步，不要越界去做后面的步骤",       # 过程指令 → 过滤
        "本次任务产出的报告已落盘到 D:\\work\\r.md",     # 一次性结论 → 过滤
    ]))
    check("NONE / 过程指令 / 一次性结论都不入记忆", notes == [], repr(notes))

    many = autonote.parse_model_notes("\n".join([
        "用户偏好：回复先给结论",
        "用户偏好：产出文档一律简体中文",
        "用户偏好：第三条应当被上限截掉",
    ]))
    check("parse_model_notes 返回纯文本列表（不再是 (层, 文本) 二元组）",
          many and all(isinstance(x, str) for x in many), repr(many))
    check("最多只放 MAX_MODEL 条（上限仍在）",
          len(many) <= autonote.MAX_MODEL, repr(many))
    check("capture 写进去的条目也没有 layer",
          all("layer" not in e for e in autonote.capture(store, [
              {"role": "user", "content": "记住：以后回复先给结论。"},
          ], "t3")))
    check("spec 为 None 时 capture_model 直接返回空",
          autonote.capture_model(store, hub=None, spec=None, messages=[], task_id="t3") == [])

    # ---------- 用例 H2：update 改写单条——正文可改，出处（时间/任务）保留 ----------
    up = MemoryStore(tmp / "update.md")
    up.append("用户偏好：回复先给结论", "t9")
    up.append("用户偏好：代码注释用中文", "t10")
    ent = up.entries()
    r = up.update(ent[0]["index"], "  用户偏好：回复永远先给结论，再展开细节  ")
    check("update 返回更新后的条目", r and r["text"] == "用户偏好：回复永远先给结论，再展开细节", repr(r))
    after = up.entries()
    check("update 后仍是两条、其他条目原样",
          len(after) == 2 and after[1]["text"] == "用户偏好：代码注释用中文", repr(after))
    check("update 保留原时间戳与任务标记（出处不因改写失真）",
          after[0]["ts"] == ent[0]["ts"] and after[0]["task"] == "t9", repr(after[0]))
    check("update 后文件表头仍在（表头不许被带走）",
          (tmp / "update.md").read_text(encoding="utf-8").startswith("# AgentForge 工作区记忆"), "")
    check("update 不存在的行号返回空", up.update(999, "x") == {}, "")
    check("update 空文本拒绝", up.update(ent[0]["index"], "   ") == {}, "")
    # 旧格式 raw 行（解析不出标记）也能改：改后按标准格式落盘
    legacy2 = MemoryStore(tmp / "legacy2.md")
    legacy2.path.write_text("- 一条老格式的记忆\n", encoding="utf-8")
    r2 = legacy2.update(legacy2.entries()[0]["index"], "改成新格式")
    got = legacy2.entries()
    check("raw 行可编辑且改后带默认出处标记",
          r2 and got and got[0]["text"] == "改成新格式" and got[0]["task"] == "手动", repr(got))

    # ---------- 用例 I：源码级——分层不许被悄悄加回界面/工具 ----------
    check("webui 侧不再引用 layer_counts / LAYER_LABEL",
          "layer_counts" not in read("webui.py") and "LAYER_LABEL" not in read("webui.py"))
    check("前端不再有分层统计与层徽标",
          all(k not in read("web/index.html")
              for k in ("memLayerStat", "memLayerRow", ".mtag.lay", "L1 事实")),
          "index.html 里还残留分层 UI")
    check("tools/memory.py 的 remember 不再出现 layer 形参",
          "layer: str" not in read("agentforge/tools/memory.py"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("")
if failed:
    print(f"{failed} 项失败。")
    sys.exit(1)
print("全部通过。")
