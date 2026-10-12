"""真实评测：把路由设为「自动配置所有模型（含付费）」，重跑小说整理任务。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from agentforge.config import Config
from webui import serve

NOVEL = Path(r"D:\AI_develop\project\novel")
PROMPT = (
    "上一个任务提取素材时解析备份失败，只拿到了 4151 字节的残缺片段，导致把配角「小师弟」误判成了主角，"
    "大纲《零级空壳的记忆》作废。我这边已经用正确的解析方式把笔记备份完整解开了："
    "全部笔记按文件夹导出在 D:\\AI_develop\\project\\novel\\notes_extracted\\ 目录，"
    "其中「写作.txt」（281 条，约 3.3 万字）才是小说灵感素材，其它文件夹（备忘、日记、游戏、tips 等）"
    "与小说无关，请忽略。\n\n"
    "你的任务：通读 D:\\AI_develop\\project\\novel\\notes_extracted\\写作.txt 全文，重新整理剧情大纲，"
    "覆盖写入 D:\\AI_develop\\project\\novel\\outline.md。\n\n"
    "注意事项：\n"
    "1. 必须先用 read_file 读完全文再动笔，不许凭印象编。\n"
    "2. 笔记是从主角视角记录的：「小师弟」全篇只出现 4 行，是三国故事线里的边缘搞笑角色，绝对不是主角。"
    "思考一下——他如果是主角，别人怎么会叫他小师弟？\n"
    "3. 素材里有多条故事线（三国穿越线、雨林演化寓言线、现代都市线等），请先梳理出每条线的主角和核心设定，"
    "再判断哪条是主线，不要把某个配角的特殊能力当成故事核心。\n"
    "4. 大纲用简体中文写。"
)

cfg = Config.load("config.lanrun.yaml")
httpd, url = serve(cfg, True, 8795)
time.sleep(0.6)


def post(path, payload, timeout=120):
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=60):
    with urllib.request.urlopen(url + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- 1) 路由切到「自动配置所有模型（含付费）」 ----------------
snap = post("/api/models", {"action": "auto_config", "scope": "best"})
print("=" * 78)
print("路由模式:", snap["routing"]["mode"], "| capability:", snap["routing"]["capability"],
      "| 允许升级:", snap["routing"]["allow_upgrade"],
      "| 允许付费升级:", snap["routing"]["allow_paid_upgrade"])
enabled = [m["name"] for m in snap["models"] if m["enabled"]]
print(f"已启用模型（{len(enabled)}/{len(snap['models'])}）:", "、".join(enabled))
print("决策模型:", json.dumps(snap.get("dispatcher"), ensure_ascii=False))
print("=" * 78)

# ---------------- 2) 重跑小说整理任务 ----------------
body = {"message": PROMPT, "history": [], "permission": "full", "auto_plan": True}
req = urllib.request.Request(
    url + "/api/chat", data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")

kinds: dict[str, int] = {}
sql_log: list[str] = []
assigns: list[dict] = []
t0 = time.time()
final_history = []
with urllib.request.urlopen(req, timeout=3600) as r:
    buf = ""
    while True:
        chunk = r.read(8192)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        while "\n\n" in buf:
            part, buf = buf.split("\n\n", 1)
            ev, data = None, ""
            for line in part.split("\n"):
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    data += line[6:]
            if not ev:
                continue
            kinds[ev] = kinds.get(ev, 0) + 1
            d = json.loads(data) if data else {}
            el = time.time() - t0
            if ev == "combo":
                print(f"[{el:6.1f}s] combo: {json.dumps(d.get('combo', {}), ensure_ascii=False)[:150]}")
            elif ev == "route":
                print(f"[{el:6.1f}s] route: auto={d.get('auto')} 选用={d.get('chosen_name')} "
                      f"({d.get('chosen_score')}) 最强={d.get('best_name')} ({d.get('best_score')})")
            elif ev == "dispatch":
                ts = d.get("tasks") or []
                print(f"[{el:6.1f}s] dispatch: 决策模型={d.get('dispatcher')} 来源={d.get('source')} "
                      f"拆出 {len(ts)} 个任务 | parallel={d.get('parallel')}")
                print(f"           理由: {d.get('rationale')}")
                for t in ts:
                    print(f"           - [{t['index']}] {t['kind_label']} | {t['title']} "
                          f"| deps={t['depends_on']}")
            elif ev == "plan":
                print(f"[{el:6.1f}s] plan: {d.get('summary')}")
            elif ev == "assign":
                assigns.append(d)
                print(f"[{el:6.1f}s] assign: 任务#{d.get('index')} → {d.get('model_name')} "
                      f"| 角色 {d.get('role')} | {d.get('reason')}")
            elif ev == "bridge":
                print(f"[{el:6.1f}s] bridge: {d.get('reason')}")
            elif ev == "conflict":
                print(f"[{el:6.1f}s] conflict: {d.get('path')} ← {d.get('tasks')}")
            elif ev == "tool_start":
                print(f"[{el:6.1f}s] tool: {d.get('name')} {json.dumps(d.get('args'), ensure_ascii=False)[:110]}")
            elif ev == "task_done":
                print(f"[{el:6.1f}s] task_done: #{d.get('index')} {d.get('status')} | "
                      f"files={d.get('files')}")
            elif ev == "stats":
                sql_log.append(f"{d.get('model')} · {d.get('tokens')} tok · {d.get('tps')} t/s "
                               f"· {'免费' if d.get('free') else '¥%.4f' % (d.get('cost') or 0)}")
            elif ev in ("complete", "stopped", "error"):
                print(f"[{el:6.1f}s] {ev}: {json.dumps(d, ensure_ascii=False)[:220]}")
            elif ev == "history":
                final_history = d.get("messages") or []

print("\n" + "=" * 78)
print(f"总耗时 {time.time() - t0:.1f}s")
print("事件统计:", json.dumps(kinds, ensure_ascii=False))
print("\n各次模型调用（顺序）:")
for line in sql_log:
    print("  ", line)

out = NOVEL / "outline.md"
print("\n" + "=" * 78)
if out.exists():
    text = out.read_text(encoding="utf-8")
    print(f"outline.md 大小: {len(text)} 字符 / {len(text.splitlines())} 行")
    print("-" * 78)
    print("\n".join(text.splitlines()[:40]))
httpd.shutdown()
