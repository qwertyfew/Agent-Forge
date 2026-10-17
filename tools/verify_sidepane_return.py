# 回归验证：从侧栏栏目（模型管理/设置）退回对话页，线程显隐的正确性。
# 复现用户报的 bug：点进 402 任务（台账 history 空、靠 summary 渲染回看页）
# → 发送栏点「模型配置」→ 退回。修复前线程被 history.length===0 整块藏掉
# （记录消失 + flex:1 的线程没了，发送框被顶到页面最上方）。
#   1) 进入 402 任务回看：线程可见、画面有内容（提问气泡+摘要）
#   2) 模型配置 → 退回：线程仍可见、main.land 不在、发送框仍贴页面底部
#   3) 回归：新建空对话 → 设置 → 退回：线程隐藏、落地态亮（发送框居中）
# 退出码 0 = 全过。
import json, subprocess, tempfile, time, urllib.request, sys

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9336
URL  = "http://127.0.0.1:8787/"
TASK = "t1016175431e333"   # 402 任务：status=error, history=[], summary=True

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

def probe(ws):
    r = cdp(ws, "Runtime.evaluate", expression="""(() => {
      const t = document.getElementById('thread');
      const wrap = document.getElementById('composerWrap');
      const cr = wrap.getBoundingClientRect();
      return JSON.stringify({
        threadHidden: t.classList.contains('hide'),
        land: document.getElementById('mainPane').classList.contains('land'),
        logCards: [...document.getElementById('log').children]
                    .filter(n => !n.classList.contains('pillnote')).length,
        gapBottom: Math.round(innerHeight - cr.bottom),   // 发送框离页面底部的距离
      });
    })()""", returnByValue=True)
    return json.loads(r["result"]["result"]["value"])

def main():
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*", "--window-size=1440,900",
        "--no-first-run", "--disable-gpu",
        "--user-data-dir=" + tempfile.mkdtemp(prefix="af_edge3_"),
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

        # --- 1) 点进 402 任务的回看页 ---
        cdp(ws, "Runtime.evaluate", expression=f"openTask('{TASK}'); 'ok'", returnByValue=True)
        time.sleep(1.5)   # 等 /api/tasks?id= 回来 + 摘要渲染
        d = probe(ws)
        ok1 = (not d["threadHidden"]) and d["logCards"] >= 2
        print(f"enter402Task threadHidden={d['threadHidden']} logCards={d['logCards']} -> {'PASS' if ok1 else 'FAIL'}")

        # --- 2) 发送栏「模型配置」→ 退回：记录不消失、发送框贴底 ---
        cdp(ws, "Runtime.evaluate", expression="document.getElementById('btnModelConfig').click(); 'ok'", returnByValue=True)
        time.sleep(0.8)
        cdp(ws, "Runtime.evaluate", expression="document.getElementById('btnBack').click(); 'ok'", returnByValue=True)
        time.sleep(0.5)
        d = probe(ws)
        ok2 = (not d["threadHidden"]) and d["logCards"] >= 2 and (not d["land"]) and d["gapBottom"] <= 30
        print(f"backFromModels threadHidden={d['threadHidden']} logCards={d['logCards']} land={d['land']} gapBottom={d['gapBottom']}px -> {'PASS' if ok2 else 'FAIL'}")

        # --- 3) 回归：空对话进设置再退回，落地态必须还在 ---
        cdp(ws, "Runtime.evaluate", expression="resetChat(); 'ok'", returnByValue=True)
        time.sleep(0.4)
        cdp(ws, "Runtime.evaluate", expression="openSettings('general'); 'ok'", returnByValue=True)
        time.sleep(0.6)
        cdp(ws, "Runtime.evaluate", expression="document.getElementById('btnBack').click(); 'ok'", returnByValue=True)
        time.sleep(0.4)
        d = probe(ws)
        ok3 = d["threadHidden"] and d["land"] and d["logCards"] == 0
        print(f"emptyChatRoundtrip threadHidden={d['threadHidden']} land={d['land']} logCards={d['logCards']} -> {'PASS' if ok3 else 'FAIL'}")

        ws.close()
        return 0 if (ok1 and ok2 and ok3) else 1
    finally:
        proc.kill()

sys.exit(main())
