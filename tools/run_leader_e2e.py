# 第 47 步 Leader 双环架构端到端实测（本地模型）：
# 把 402 那条 REA 差距清单任务原样发给 harness，全程采集 SSE 事件，
# 落 .run/leader_e2e_events.jsonl（带时间戳），stdout 打关键事件做实时观察。
import json, sys, time, urllib.request

BASE = "http://127.0.0.1:8787"
PROMPT = open("D:/AI_develop/project/agent-forge/.run/rea_task_prompt.txt", encoding="utf-8").read()
OUT = open("D:/AI_develop/project/agent-forge/.run/leader_e2e_events.jsonl", "w", encoding="utf-8")

T0 = time.time()
def log(tag, extra=""):
    print(f"[{time.time()-T0:7.1f}s] {tag} {extra}", flush=True)

req = urllib.request.Request(
    BASE + "/api/chat",
    data=json.dumps({"message": PROMPT}).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")
log("POST /api/chat", f"prompt_len={len(PROMPT)}")

KEY = {"task", "leader_round", "dispatch", "plan", "assign", "subtask",
       "task_done", "subtask_error", "artifacts", "complete", "done",
       "error", "stopped", "verify", "gate", "stats"}

buf_event = None
try:
    with urllib.request.urlopen(req, timeout=7200) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event: "):
                buf_event = line[7:].strip()
                continue
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except Exception:
                data = {"raw": line[6:][:200]}
            rec = {"t": round(time.time()-T0, 1), "ev": buf_event, "data": data}
            OUT.write(json.dumps(rec, ensure_ascii=False) + "\n")
            OUT.flush()
            if buf_event in KEY:
                brief = ""
                if buf_event == "leader_round":
                    brief = json.dumps({k: data.get(k) for k in ("phase", "round", "done", "batch_n", "note")}, ensure_ascii=False)
                elif buf_event in ("task", "complete", "done", "error", "stopped"):
                    brief = json.dumps(data, ensure_ascii=False)[:180]
                elif buf_event in ("assign", "plan"):
                    brief = json.dumps(data, ensure_ascii=False)[:220]
                elif buf_event == "subtask":
                    brief = str(data.get("title") or data.get("task_id") or "")[:60]
                log(buf_event, brief)
            if buf_event in ("done", "error", "stopped"):
                break
except Exception as e:
    log("STREAM_EXC", repr(e))
finally:
    OUT.close()
    log("END")
