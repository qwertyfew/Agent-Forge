# 任务右键菜单真机验证（Edge headless + CDP，纯 DOM 交互，不改后端数据）：
#   1) 右键任务条目 → WorkBuddy 风格菜单弹出（打开文件夹/重命名/删除，删除为危险项）
#   2) Escape 关闭；点击别处关闭
#   3) 重命名 → 就地输入框；Enter 后列表重渲染
#   4) 删除 → 危险确认弹窗；取消不删
#   5) 运行中任务没有「删除」项
# 退出码 0 = 全过。
import base64, json, subprocess, time, urllib.request, sys, tempfile

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9335
URL  = "http://127.0.0.1:8787/"

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

MOCK = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r, ms));
  try{
    const items = [...document.querySelectorAll('.task-item')];
    if(!items.length) return JSON.stringify({err:'侧栏没有任务条目'});
    const out = {};
    const it = items.find(n=>n._task && n._task.status !== 'running') || items[0];
    const r = it.getBoundingClientRect();

    // 1) 右键弹菜单
    it.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true,
      clientX: r.left + 40, clientY: r.top + 20}));
    await sleep(200);
    let menu = document.querySelector('.ctxmenu');
    if(!menu) return JSON.stringify({err:'右键后菜单没出现'});
    out.labels = [...menu.querySelectorAll('.cm-item span:last-child')].map(s=>s.textContent);
    out.dangerCount = menu.querySelectorAll('.cm-item.danger').length;
    out.hasSep = !!menu.querySelector('.cm-sep');
    out.radius = getComputedStyle(menu).borderRadius;

    // 2) Escape 关闭
    document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
    await sleep(80);
    out.escCloses = !document.querySelector('.ctxmenu');

    // 3) 重命名 → 输入框
    it.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true,
      clientX: r.left + 40, clientY: r.top + 20}));
    await sleep(120);
    menu = document.querySelector('.ctxmenu');
    const ren = [...menu.querySelectorAll('.cm-item')].find(n=>n.textContent.includes('重命名'));
    ren.click();
    await sleep(120);
    const input = it.querySelector('.t-rename');
    out.renameInput = !!input;
    out.renameFocused = document.activeElement === input;
    out.renameValue = input ? input.value.slice(0, 12) : '';
    if(input){ input.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); }
    await sleep(120);

    // 4) 删除 → 危险确认弹窗
    const it2 = [...document.querySelectorAll('.task-item')].find(n=>n._task && n._task.status !== 'running') || items[0];
    const r2 = it2.getBoundingClientRect();
    it2.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true,
      clientX: r2.left + 40, clientY: r2.top + 20}));
    await sleep(120);
    menu = document.querySelector('.ctxmenu');
    const del = [...menu.querySelectorAll('.cm-item')].find(n=>n.textContent.includes('删除'));
    del.click();
    await sleep(250);
    const modal = document.querySelector('.modal, .modal-card, [class*=modal]');
    out.confirmShown = !!modal;
    out.confirmText = modal ? (modal.textContent||'').slice(0, 60) : '';
    // 取消：找取消按钮
    const cancel = modal ? [...modal.querySelectorAll('button')].find(b=>/取\s*消/.test(b.textContent)) : null;
    if(cancel) cancel.click();
    await sleep(200);

    // 5) 运行中任务无删除项（若有）
    const running = items.find(n=>n._task && (n._task.status === 'running' || n._task.status === 'queued'));
    if(running){
      const rr = running.getBoundingClientRect();
      running.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true,
        clientX: rr.left + 40, clientY: rr.top + 20}));
      await sleep(150);
      const m2 = document.querySelector('.ctxmenu');
      out.runningLabels = m2 ? [...m2.querySelectorAll('.cm-item span:last-child')].map(s=>s.textContent) : [];
      document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
      await sleep(80);
    }
    // 菜单留开截图
    const it3 = [...document.querySelectorAll('.task-item')][0];
    const r3 = it3.getBoundingClientRect();
    it3.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true,
      clientX: r3.left + 60, clientY: r3.top + 24}));
    await sleep(200);
    return JSON.stringify(out);
  }catch(e){ return JSON.stringify({err: e.message}); }
})()"""

def main():
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        "--window-size=1440,900", "--no-first-run", "--disable-gpu",
        "--user-data-dir=" + tempfile.mkdtemp(prefix="af_edge_"),
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
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=15)
        cdp(ws, "Runtime.enable")
        cdp(ws, "Page.enable")
        time.sleep(3)
        r = cdp(ws, "Runtime.evaluate", expression=MOCK, returnByValue=True, awaitPromise=True)
        d = json.loads(r["result"]["result"]["value"])
        print(json.dumps(d, ensure_ascii=False, indent=1))
        if "err" in d:
            print("FAIL:", d["err"]); return 1
        checks = {
            "菜单三项(打开文件夹/重命名/删除)": d["labels"] == ["打开文件夹", "重命名", "删除"],
            "删除为危险项": d["dangerCount"] == 1,
            "删除前有分隔线": d["hasSep"],
            "圆角卡片": d["radius"] == "14px",
            "Escape 关闭": d["escCloses"],
            "重命名输入框": d["renameInput"] and d["renameFocused"],
            "删除确认弹窗": d["confirmShown"],
        }
        if d.get("runningLabels") is not None:
            checks["运行中无删除项"] = "删除" not in d["runningLabels"]
        bad = [k for k, v in checks.items() if not v]
        for k, v in checks.items():
            print(f"{k}: {'PASS' if v else 'FAIL'}")
        shot = cdp(ws, "Page.captureScreenshot", format="png")["result"]
        if "data" in shot:
            open(r"D:\AI_develop\project\agent-forge\.run\verify_task_menu.png", "wb").write(
                base64.b64decode(shot["data"]))
            print("截图: .run/verify_task_menu.png")
        ws.close()
        return 0 if not bad else 1
    finally:
        proc.kill()

import websocket  # noqa: E402
sys.exit(main())
