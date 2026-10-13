"""任务台账的回归测试：提问原话必须在任务「一开始」就记下来。

锁的是这个坑：网页上看一条正在跑的任务，没有问题气泡、没有头像行、没有计时——
而且要等到任务结束、从侧栏点进去回看才补齐。根因不在渲染，在真相来源：
提问原文只活在发起方的内存里，台账的 history 要到任务结束才写，
中途别的端（网页、CLI、另一个标签页）想画这条任务的提问，除了截断过的
title 没有别的可用。

断言写的是：「begin 之后 prompt 就是原话（不截断、不压空白），
list 与 get 都带得出来，finish 不会把它冲掉」。

运行：.venv/Scripts/python.exe tools/test_taskstore.py
不需要模型、不需要联网，退出码 0 = 全过。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.taskstore import TaskStore  # noqa: E402

failed = 0


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  —— {detail}"))
    if not cond:
        global failed
        failed += 1


tmp = Path(tempfile.mkdtemp(prefix="af_taskstore_"))
try:
    store = TaskStore(tmp / "tasks.json")

    # ---------- 用例 1：原话原样进台账 ----------
    raw = "帮我查一下 git 上那个 REA 项目（Reverse Engineer Anything）是干什么的，介绍一下。"
    t1 = store.begin(raw, permission="full")
    rec = store.get(t1) or {}
    check("begin 之后 get 就带得出来 prompt", rec.get("prompt") == raw,
          repr(rec.get("prompt"))[:120])
    check("prompt 与用户原话逐字一致（含中文全角标点）", rec.get("prompt") == raw)

    # ---------- 用例 2：title 是派生物，不是原话 ----------
    multiline = "第一行：做件事\n\n第二行   带着   多余空白"
    t2 = store.begin(multiline)
    r2 = store.get(t2) or {}
    check("prompt 保留换行与多余空白（那是原话）",
          r2.get("prompt") == multiline, repr(r2.get("prompt")))
    check("title 压掉换行与多余空白（那是侧栏标签）",
          r2.get("title") == "第一行：做件事 第二行 带着 多余空白", repr(r2.get("title")))

    # ---------- 用例 3：超长指令——标题截断、原话不截 ----------
    long_msg = "把这段话记住：" + "细节" * 200
    t3 = store.begin(long_msg)
    r3 = store.get(t3) or {}
    check("超长指令的 title 被截断到 90 字以内", len(r3.get("title") or "") <= 90,
          f"len={len(r3.get('title') or '')}")
    check("超长指令的 prompt 一字不少", r3.get("prompt") == long_msg,
          f"len={len(r3.get('prompt') or '')} 期望={len(long_msg)}")

    # ---------- 用例 4：列表也要带 prompt ----------
    # 网页接管的路径读的是列表（5 秒一次轮询），拿不到 prompt 就回到「用标题充提问」
    listed = store.list()
    hit = next((x for x in listed if x.get("id") == t1), None)
    check("list() 里每条都带 prompt（接管路径靠它画提问）",
          hit is not None and hit.get("prompt") == raw, repr((hit or {}).get("prompt"))[:120])
    check("list() 里不带 history（那是按需单独取的）",
          all("history" not in x for x in listed))

    # ---------- 用例 5：空消息兜底 ----------
    t5 = store.begin("")
    r5 = store.get(t5) or {}
    check("空消息也有标题兜底", r5.get("title") == "未命名任务", repr(r5.get("title")))
    check("空消息的 prompt 是空串而不是 None", r5.get("prompt") == "")

    # ---------- 用例 6：finish 不会冲掉原话 ----------
    store.finish(t1, status="done", summary="查完了",
                 history=[{"role": "user", "content": raw},
                          {"role": "assistant", "content": "查完了"}])
    r6 = store.get(t1) or {}
    check("落最终状态后 prompt 仍在", r6.get("prompt") == raw, repr(r6.get("prompt"))[:120])
    check("落最终状态后 history 也写进来了", bool(r6.get("history")))
    check("收尾后 as_dict 的 has_history 为真", r6.get("has_history") is True)

    # ---------- 用例 7：老台账（没有 prompt 字段）读得进来 ----------
    # 升级前的 .tasks.json 里没有这个字段；读不进来会让整条任务消失
    legacy = tmp / "legacy.json"
    legacy.write_text(json.dumps({"tasks": [
        {"id": "tOld", "title": "老任务", "status": "done", "created_at": 1.0,
         "ended_at": 2.0, "history": [{"role": "user", "content": "老任务的提问"}]},
        {"id": "tRunning", "title": "重启前还在跑", "status": "running"},
    ]}, ensure_ascii=False), encoding="utf-8")
    old = TaskStore(legacy)
    old_rec = old.get("tOld") or {}
    check("老台账读得进来（不会因为缺字段整条丢掉）", old_rec.get("title") == "老任务",
          repr(old_rec))
    check("老台账缺 prompt 时补成空串", old_rec.get("prompt") == "",
          repr(old_rec.get("prompt")))
    check("老台账的提问可以从 history 里取（前端的兜底顺序）",
          (old_rec.get("history") or [{}])[0].get("content") == "老任务的提问")

    # ---------- 用例 8：落盘再读回，原话仍在 ----------
    store2 = TaskStore(store.path)
    r8 = store2.get(t2) or {}
    check("重新加载台账后原话仍在（重启后回看/接管都还能画提问）",
          r8.get("prompt") == multiline, repr(r8.get("prompt")))

    # ---------- 用例 9：重启后 running 归正为中断（既有约定别被改坏） ----------
    r9 = old.get("tRunning") or {}
    check("启动时把遗留的 running 标成中断", r9.get("status") == "error",
          repr(r9.get("status")))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("")
if failed:
    print(f"{failed} 项失败。")
    sys.exit(1)
print("全部通过。")
