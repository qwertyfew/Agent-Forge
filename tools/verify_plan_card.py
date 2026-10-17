# 任务拆解卡片重渲染真机验证（Edge headless + CDP）：
#   1) addPlan 能在真实页面里渲染出新版卡片（头行/摘要行/chip/进度）
#   2) markPlanStep 按 task_id 精确命中（跨批次 index 全局递增也不串行）
#   3) 完成/失败后序号圆点换成 ✓/✗ 图标，进度计数正确
# 退出码 0 = 全过。
import base64, json, subprocess, time, urllib.request, sys

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
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

MOCK = """(() => {
  try{
    // 切出落地态，让对话流（含新卡片）可见，截图才有东西看
    document.getElementById('mainPane').classList.remove('land');
    document.getElementById('hero').classList.add('hide');
    document.getElementById('thread').classList.remove('hide');
    // 模拟 Leader 第 2 批（index 全局递增：4、5）——旧代码按 index 对位会错行
    addPlan([
      {index:4, id:'l4', title:'分析 mini-swe-agent 核心机制',
       kind_label:'检视', model_name:'gpt-oss:20b',
       reason:'决策模型不接这类要动手/要算力的任务，改派 gpt-oss:20b（检视难度 74 分）'},
      {index:5, id:'l5', title:'分析 learn-claude-code 核心机制',
       kind_label:'检视', model_name:'gpt-oss:20b',
       reason:'决策模型不接这类要动手/要算力的任务，改派 gpt-oss:20b（检视难度 74 分）'},
      {index:6, id:'l6', title:'分析 AgentForge 现有实现',
       kind_label:'分析', model_name:'gpt-oss:20b',
       reason:'分析维度最强（74 分）'},
    ], 'Leader 第 4 批：由于之前的尝试中 mini-swe-agent 和 AgentForge 本身的分析均失败，本批次优先并行补齐这两个关键仓库的维度分析，确保后续对比有据可依。');
    markPlanStep(4, 'done', 'l4');
    markPlanStep(5, 'error', 'l5');
    markPlanStep(6, 'running', 'l6');
    const plan = document.querySelector('.plan');
    if(!plan) return JSON.stringify({err:'no .plan'});
    const rows = [...plan.querySelectorAll('.pstep')];
    return JSON.stringify({
      hasHead: !!plan.querySelector('.plan-head'),
      headText: plan.querySelector('.plan-head').textContent.trim(),
      hasNote: !!plan.querySelector('.plan-note'),
      prog: plan.querySelector('.plan-prog').textContent,
      rowCount: rows.length,
      chipCount: plan.querySelectorAll('.pchip').length,
      kindText: plan.querySelector('.pchip.kind').textContent,
      modelText: plan.querySelector('.pchip.model').textContent,
      reasonTitle: plan.querySelector('.preason').title.slice(0, 7),
      doneRowHasSvg: !!rows[0].querySelector('.pidx svg'),
      errRowHasSvg: !!rows[1].querySelector('.pidx svg'),
      runRowIsNum: rows[2].querySelector('.pidx').textContent.trim() === '6',
      errRowClass: rows[1].className,
      reasonOverflow: getComputedStyle(plan.querySelector('.preason')).textOverflow,
    });
  }catch(e){ return JSON.stringify({err: e.message}); }
})()"""

def main():
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
        cdp(ws, "Page.enable")
        time.sleep(2.5)
        r = cdp(ws, "Runtime.evaluate", expression=MOCK, returnByValue=True)
        d = json.loads(r["result"]["result"]["value"])
        print(json.dumps(d, ensure_ascii=False, indent=1))
        if "err" in d:
            print("FAIL:", d["err"]); return 1
        checks = {
            "头行": d["hasHead"] and "任务拆解" in d["headText"] and "3 个步骤" in d["headText"],
            "摘要行": d["hasNote"],
            "进度计数": d["prog"] == "2/3",                       # done+error=2
            "chip": d["chipCount"] == 6 and d["kindText"] == "检视" and d["modelText"] == "gpt-oss:20b",
            "原因悬停全文": d["reasonTitle"].startswith("决策模型不接"),
            "完成图标": d["doneRowHasSvg"],
            "失败图标": d["errRowHasSvg"],
            "运行中保留序号": d["runRowIsNum"] and "running" not in d["errRowClass"],
            "原因单行截断": d["reasonOverflow"] == "ellipsis",
        }
        bad = [k for k, v in checks.items() if not v]
        for k, v in checks.items():
            print(f"{k}: {'PASS' if v else 'FAIL'}")
        # 截图留档（log 区域可能不可见，截整页）
        shot = cdp(ws, "Page.captureScreenshot", format="png")["result"]
        if "data" not in shot:
            print("截图失败:", json.dumps(shot)[:300])
        else:
            open(r"D:\AI_develop\project\agent-forge\.run\verify_plan_card.png", "wb").write(
                base64.b64decode(shot["data"]))
            print("截图: .run/verify_plan_card.png")
        ws.close()
        return 0 if not bad else 1
    finally:
        proc.kill()

import websocket  # noqa: E402
sys.exit(main())
