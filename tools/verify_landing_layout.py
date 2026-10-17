# 落地页真机验证（Edge headless + CDP）：
#   1) 发送框左侧加号（#btnAttach）中心 = 页面水平中线（innerHeight/2）
#   2) 发送框（.c-card）宽向几乎占满页面（main 宽减去左右 24px 边距）
#   3) hero 标题水平居中、字号 30px 不变
# 退出码 0 = 全过。
import base64, json, subprocess, time, urllib.request, sys

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
URL  = "http://127.0.0.1:8787/"

ws_lib_ok = True
try:
    import websocket   # noqa
except ImportError:
    ws_lib_ok = False

def http_json(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode())

def cdp(ws, method, **params):
    ws.send(json.dumps({"id": cdp.seq, "method": method, "params": params}))
    cdp.seq += 1
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == cdp.seq - 1:
            return msg

cdp.seq = 1

def main():
    if not ws_lib_ok:
        print("SKIP: 没有 websocket-client"); return 2
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        "--window-size=1440,900", "--no-first-run", "--disable-gpu",
        "--user-data-dir=" + __import__("tempfile").mkdtemp(prefix="af_edge_"),
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
        time.sleep(2.5)   # 等 fetch /api/models 等首屏请求落地，落地态稳定
        expr = """(() => {
          const plus = document.getElementById('btnAttach').getBoundingClientRect();
          const card = document.querySelector('#composerWrap .c-card').getBoundingClientRect();
          const h1 = document.querySelector('#hero h1').getBoundingClientRect();
          const main = document.getElementById('mainPane').getBoundingClientRect();
          const wrap = document.getElementById('composerWrap').getBoundingClientRect();
          const comp = document.querySelector('#composerWrap .composer').getBoundingClientRect();
          const hero = document.getElementById('hero').getBoundingClientRect();
          return JSON.stringify({
            innerW: innerWidth, vh: innerHeight,
            plusCenterY: plus.top + plus.height/2,
            mainLeft: main.left, mainWidth: main.width,
            wrapLeft: wrap.left, wrapWidth: wrap.width,
            compLeft: comp.left, compWidth: comp.width,
            compMaxW: getComputedStyle(document.querySelector('#composerWrap .composer')).maxWidth,
            cardWidth: card.width, cardLeft: card.left,
            heroLeft: hero.left, heroWidth: hero.width, heroH: hero.height,
            h1CenterX: h1.left + h1.width/2,
            h1Font: getComputedStyle(document.querySelector('#hero h1')).fontSize,
            land: document.getElementById('mainPane').classList.contains('land'),
          });
        })()"""
        r = cdp(ws, "Runtime.evaluate", expression=expr, returnByValue=True)
        d = json.loads(r["result"]["result"]["value"])
        print(json.dumps(d, indent=1))
        mid = d["vh"] / 2
        ok1 = abs(d["plusCenterY"] - mid) <= 2
        gap = (d["mainLeft"] + d["mainWidth"]) - (d["cardLeft"] + d["cardWidth"])
        left_gap = d["cardLeft"] - d["mainLeft"]
        ok2 = gap <= 52 and left_gap <= 52   # 左右各 24px 边距，留渲染余量
        main_mid = d["mainLeft"] + d["mainWidth"] / 2
        ok3 = abs(d["h1CenterX"] - main_mid) <= 2 and d["h1Font"] == "30px"
        print(f"midline={mid:.1f} plusCenterY={d['plusCenterY']:.1f} -> {'PASS' if ok1 else 'FAIL'}")
        print(f"cardLeftGap={left_gap:.0f} cardRightGap={gap:.0f} -> {'PASS' if ok2 else 'FAIL'}")
        print(f"h1CenterX={d['h1CenterX']:.1f} mainMid={main_mid:.1f} font={d['h1Font']} -> {'PASS' if ok3 else 'FAIL'}")
        ws.close()
        return 0 if (ok1 and ok2 and ok3) else 1
    finally:
        proc.kill()

sys.exit(main())
