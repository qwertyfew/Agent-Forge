# Markdown 渲染 + 过程流孤儿行修复 真机验证（Edge headless + CDP）：
#   1) md()：管道表格→真表格、字面 <br>→换行、列表、分隔线、段落间距、代码保护
#   2) 泳道短思考块在 tool_start 后不再留下「思考过程·决定调用工具 X」孤儿行
#   3) 泳道正文按 md() 渲染进自己那一列
# 退出码 0 = 全过。
import base64, json, subprocess, time, urllib.request, sys, tempfile

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9334
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

MOCK = r"""(() => {
  try{
    document.getElementById('mainPane').classList.remove('land');
    document.getElementById('hero').classList.add('hide');
    document.getElementById('thread').classList.remove('hide');

    // ===== A. md() 纯函数测试 =====
    const src = [
      '## 对比',
      '',
      '| 维度 | learn-claude-code | mini-swe-agent |',
      '| --- | :---: | --- |',
      '| Agent Loop | agent_loop() 第 163-176 行 | run() 第 78 行 |',
      '| 工具列表 | TOOLS 在 138-149 行 **注意** | TOOLS `dict` 映射 |',
      '',
      '- 第一点',
      '- 第二点',
      '  - 子点 A',
      '  - 子点 B',
      '1. 有序一',
      '2. 有序二',
      '',
      '段落一说明文字。',
      '段内第二行带<br>字面换行。',
      '',
      '---',
      '',
      '```python',
      'print("| 不被当表格 |")',
      '```',
    ].join('\n');
    const html = md(src);
    const div = document.createElement('div');
    div.innerHTML = html;

    const a = {};
    a.tableOk = !!div.querySelector('.mdtable table');
    a.thCount  = div.querySelectorAll('.mdtable th').length;
    a.tdCount  = div.querySelectorAll('.mdtable td').length;
    a.tdBold   = !!div.querySelector('.mdtable td b');
    a.tdCode   = !!div.querySelector('.mdtable td code');
    a.ulCount  = div.querySelectorAll('ul').length;
    a.liCount  = div.querySelectorAll('li').length;
    a.nested   = div.querySelectorAll('ul li ul li').length;
    a.olLi     = div.querySelectorAll('ol > li').length;
    a.pCount   = div.querySelectorAll('p').length;
    a.noLiteralBr = !/<br>/i.test(div.textContent) === false;   // 文本里没有字面 <br>
    a.brConverted = div.innerHTML.includes('<br>');
    a.hrOk     = !!div.querySelector('hr');
    a.preOk    = !!div.querySelector('pre code');
    a.preRawPipe = (div.querySelector('pre code') || {textContent:''}).textContent.includes('| 不被当表格 |');
    a.h2 = !!div.querySelector('h2');
    a.compact = !/\n\s*\n/.test(html.replace(/>\s*</g,'><'));   // 标签间无空白文本节点

    // ===== B. 泳道孤儿行修复 =====
    handleTaskEvent('dispatch', {multi:true, parallel:true, tasks:[
      {id:'l1', title:'任务一'}, {id:'l2', title:'任务二'}]});
    // l1：短思考 → 工具调用（修后不该留折叠行）
    handleTaskEvent('thinking', {task_id:'l1', text:'查一下'});
    handleTaskEvent('tool_start', {task_id:'l1', name:'read_file',
      args:{path:'external/learn-claude-code/s01_agent_loop/code.py'}});
    handleTaskEvent('tool_result', {task_id:'l1', output:'1| import os'});
    const lanesWrap = document.querySelector('.lanes');
    const l1body = lanesWrap.children[0].querySelector('.lane-body');
    const b = {};
    b.lanesOk = !!lanesWrap;
    b.noOrphanLine = !l1body.querySelector('.think-plain');
    b.toolGroupInLane = !!l1body.querySelector('.toolrun');

    // l2：有实质思考（>=20 字）→ 折叠行保留，但摘要被改写为动作
    handleTaskEvent('thinking', {task_id:'l2',
      text:'这个文件是 agent 主循环，需要确认 tool_use 的分发结构，再决定怎么对比。'});
    handleTaskEvent('tool_start', {task_id:'l2', name:'search_code', args:{pattern:'agent_loop'}});
    const l2body = lanesWrap.children[1].querySelector('.lane-body');
    const l2line = l2body.querySelector('.think-plain');
    b.keepRealThinking = !!l2line;
    b.actionSummary = l2line ? l2line.querySelector('.think-sum').textContent : '';

    // ===== C. 泳道正文 md 渲染 =====
    handleTaskEvent('delta', {task_id:'l1',
      text:'| 机制 | 位置 |\n| --- | --- |\n| 循环体 | 163-176 行 |\n\n结论一段。'});
    const c = {};
    c.laneTable = !!l1body.querySelector('.mdtable table');
    c.lanePara  = l1body.querySelectorAll('p').length >= 1;
    return JSON.stringify({a, b, c});
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
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10)
        cdp(ws, "Runtime.enable")
        cdp(ws, "Page.enable")
        time.sleep(2.5)
        r = cdp(ws, "Runtime.evaluate", expression=MOCK, returnByValue=True)
        d = json.loads(r["result"]["result"]["value"])
        print(json.dumps(d, ensure_ascii=False, indent=1))
        if "err" in d:
            print("FAIL:", d["err"]); return 1
        a, b, c = d["a"], d["b"], d["c"]
        checks = {
            "表格成真表格": a["tableOk"] and a["thCount"] == 3 and a["tdCount"] == 6,
            "单元格加粗/行内码": a["tdBold"] and a["tdCode"],
            "无序列表(含子级)": a["ulCount"] >= 1 and a["nested"] == 2,
            "有序列表": a["olLi"] == 2,
            "段落分段": a["pCount"] == 1,
            "<br>转真换行": a["brConverted"] and not a["noLiteralBr"],
            "分隔线": a["hrOk"],
            "代码块保护(管道不误判)": a["preOk"] and a["preRawPipe"],
            "标题": a["h2"],
            "输出紧凑(pre-wrap安全)": a["compact"],
            "泳道建立": b["lanesOk"],
            "短思考不留孤儿行": b["noOrphanLine"],
            "工具组落在泳道内": b["toolGroupInLane"],
            "实质思考保留": b["keepRealThinking"],
            "思考行改写为动作": b["actionSummary"] == "决定调用工具 search_code",
            "泳道正文渲染表格": c["laneTable"] and c["lanePara"],
        }
        bad = [k for k, v in checks.items() if not v]
        for k, v in checks.items():
            print(f"{k}: {'PASS' if v else 'FAIL'}")
        shot = cdp(ws, "Page.captureScreenshot", format="png")["result"]
        if "data" in shot:
            open(r"D:\AI_develop\project\agent-forge\.run\verify_md_render.png", "wb").write(
                base64.b64decode(shot["data"]))
            print("截图: .run/verify_md_render.png")
        ws.close()
        return 0 if not bad else 1
    finally:
        proc.kill()

import websocket  # noqa: E402
sys.exit(main())
