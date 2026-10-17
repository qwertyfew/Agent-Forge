# 上下文圈悬停 + 任务回看贴底的真机验证（Edge headless + CDP）：
#   1) 圈条目不再挂原生 title（无方角气泡），说明在 data-tip 上，光标不是 help（无跟随问号）
#   2) 点进一条历史任务后，对话区强制贴底（scrollTop + clientHeight >= scrollHeight - 2）
# 退出码 0 = 全过。
import json, subprocess, tempfile, time, urllib.request, sys

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9334
URL  = "http://127.0.0.1:8787/"
TASK = "t1016094933f2c5"   # 台账里的一条 done 任务（详情含 8 条历史）

def http_json(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode())

seq = 1
def cdp(ws, method, **params):
    global seq
    ws.send(json.dumps({"id": seq, "method": method, "params": params}))
    seq += 1
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == seq - 1:
            return msg

def main():
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*", "--window-size=1440,900",
        "--no-first-run", "--disable-gpu",
        "--user-data-dir=" + tempfile.mkdtemp(prefix="af_edge2_"),
        URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(50):
            time.sleep(0.3)
            try:
                tabs = http_json("/json")
                target = next((t for t in tabs if t.get("type") == "page" and URL in t.get("url", "")), None)
                if target: break
            except Exception:
                pass
        if not target:
            print("FAIL: 没找到页面 target"); return 1
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10)
        cdp(ws, "Runtime.enable")
        time.sleep(2.0)

        # --- 1) 造两个圈，检查 hover 三要素 ---
        cdp(ws, "Runtime.evaluate", expression="""
          noteModelCtx({model_key:'smart-mini', model:'qwen3.5:4b', prompt_tokens:12000});
          noteModelCtx({model_key:'deepseek-x', model:'deepseek-v4-pro', prompt_tokens:90000});
          'ok'""", returnByValue=True)
        r = cdp(ws, "Runtime.evaluate", expression="""(() => {
          const ws = [...document.querySelectorAll('#ctxRings .ctx-ring-wrap')];
          return JSON.stringify({
            n: ws.length,
            noTitle: ws.every(w => !w.hasAttribute('title')),
            hasTip: ws.every(w => (w.dataset.tip||'').includes('的上下文占用')),
            cursor: getComputedStyle(ws[0]).cursor,
          });
        })()""", returnByValue=True)
        d = json.loads(r["result"]["result"]["value"])
        ok1 = d["n"] == 2 and d["noTitle"] and d["hasTip"] and d["cursor"] == "default"
        print(f"rings n={d['n']} noTitle={d['noTitle']} hasTip={d['hasTip']} cursor={d['cursor']} -> {'PASS' if ok1 else 'FAIL'}")

        # --- 2) 点进历史任务，验证贴底 ---
        cdp(ws, "Runtime.evaluate", expression=f"openTask('{TASK}'); 'ok'", returnByValue=True)
        time.sleep(1.5)   # 等 /api/tasks?id= 回来 + renderHistory
        r = cdp(ws, "Runtime.evaluate", expression="""(() => {
          const t = document.getElementById('thread');
          return JSON.stringify({
            bottom: t.scrollHeight - t.scrollTop - t.clientHeight,
            msgs: document.querySelectorAll('#log .msg-user, #log .msg-ai').length,
          });
        })()""", returnByValue=True)
        d = json.loads(r["result"]["result"]["value"])
        ok2 = d["bottom"] <= 2 and d["msgs"] > 0
        print(f"enterTask bottomGap={d['bottom']}px msgs={d['msgs']} -> {'PASS' if ok2 else 'FAIL'}")

        # 模拟用户向上翻再点另一条任务：贴底必须覆盖「回看残留」
        cdp(ws, "Runtime.evaluate", expression="""
          document.getElementById('thread').scrollTop = 0; 'ok'""", returnByValue=True)
        time.sleep(0.2)
        cdp(ws, "Runtime.evaluate", expression=f"openTask('{TASK}'); 'ok'", returnByValue=True)
        time.sleep(1.2)
        r = cdp(ws, "Runtime.evaluate", expression="""(() => {
          const t = document.getElementById('thread');
          return JSON.stringify({ bottom: t.scrollHeight - t.scrollTop - t.clientHeight });
        })()""", returnByValue=True)
        d = json.loads(r["result"]["result"]["value"])
        ok3 = d["bottom"] <= 2
        print(f"re-enter after scroll-up bottomGap={d['bottom']}px -> {'PASS' if ok3 else 'FAIL'}")

        ws.close()
        return 0 if (ok1 and ok2 and ok3) else 1
    finally:
        proc.kill()

sys.exit(main())
