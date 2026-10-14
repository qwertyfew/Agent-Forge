/**
 * 「一条回复的开场与收尾」的时序契约（web/index.html 的 beginReply / settleReply /
 * unsettleReply / attachMsgFoot）。
 *
 * 锁的是这一类坑：**元素的出现时机被交给了「模型什么时候吐东西」**。
 *   ① 头像行与计时原本是 lazy 的——`takeAIHead` 挂在 8 个渲染函数里，谁先出现谁
 *      负责插。于是模型首轮跑得慢 / 只吐胶囊 / 一个字都不吐时，头像行与计时就缺席
 *      或迟到。现在改成提问落盘的这一刻就建，与任何后端事件无关。
 *   ② 「复制 / 重新回答 / 朗读」工具条只属于完整回复。它曾在 history 事件上挂
 *      （回答中途就冒出来）；而 error 那条路又什么都不做——出错的回复永远停在
 *      「执行中」，done 那条路则一视同仁写「完成」，把出错的也写成完成。
 *      现在收尾收口到 settleReply(status) 一处，按**结局**决定，不按「最后收到
 *      哪个事件」决定；收尾之后流程若又动起来，unsettleReply 立刻撤回工具条。
 *
 * 运行：node tools/test_reply_flow.mjs
 * 需要 jsdom（任一 node_modules 里能找到即可，找不到就 SKIP 并以 0 退出）。
 * 不需要模型、不需要联网——事件是直接喂进去的。
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve, delimiter } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');
const require = createRequire(import.meta.url);

function loadJsdom() {
  try { return require('jsdom').JSDOM; } catch { /* 继续找 */ }
  const bases = [ROOT, process.cwd(),
    ...(process.env.NODE_PATH ? process.env.NODE_PATH.split(delimiter) : [])];
  for (const b of bases) {
    if (!b) continue;
    try { return require(join(b, 'node_modules', 'jsdom')).JSDOM; } catch { /* 继续找 */ }
  }
  return null;
}

const JSDOM = loadJsdom();
if (!JSDOM) {
  console.log('SKIP: 没找到 jsdom（npm i jsdom 或用 NODE_PATH 指到有它的 node_modules）');
  process.exit(0);
}

let failed = 0;
function check(name, cond, detail = '') {
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (cond ? '' : `  —— ${detail}`));
  if (!cond) failed++;
}

/* ---------------- 起页面 ---------------- */
const html = readFileSync(join(ROOT, 'web', 'index.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const appJs = blocks[blocks.length - 1][1];

const dom = new JSDOM(html.replace(/<script[\s\S]*?<\/script>/g, ''), {
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8787/',
});
const { window } = dom;
const doc = window.document;

/* /api/chat 的替身：把 chatChunks 里的 SSE 片段依次喂给 streamChat。
   用来测「流读完了但没等到 done 事件」时的兜底收尾。 */
let chatChunks = [];
const sse = (events) =>
  events.map(([ev, data]) => `event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`).join('');

window.fetch = (url) => {
  const u = String(url);
  if (u.startsWith('/api/chat') || u.startsWith('/api/combo') || u.startsWith('/api/models')) {
    const enc = new TextEncoder();
    const parts = chatChunks.map((s) => enc.encode(s));
    let i = 0;
    return Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve({}),
      body: {
        getReader: () => ({
          read: () => (i < parts.length
            ? Promise.resolve({ done: false, value: parts[i++] })
            : Promise.resolve({ done: true, value: undefined })),
        }),
      },
    });
  }
  let body = {};
  if (u.startsWith('/api/tasks?')) body = { task: null };
  else if (u.startsWith('/api/tasks')) body = { tasks: [], running: 0 };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
};
window.EventSource = class {
  constructor(url) { this.url = String(url); this.closed = false; }
  addEventListener() {}
  close() { this.closed = true; }
};
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };

const SHIM = `
;window.__af = {
  addUser, handleTaskEvent, resetChat, enterTaskView, streamChat, taskPrompt,
  addAssistant, dropStaleFeet,
  setBusy: (b) => { busy = b; },
  setOpenTaskId: (v) => { openTaskId = v; },
  setTaskData: (v) => { taskData = v; },
  setView: (v) => { view = v; },
  setViewingHistory: (v) => { viewingHistory = v; },
  getReplySettled: () => replySettled,
  headText: () => aiHeadTimeEl ? aiHeadTimeEl.textContent : '',
  headBase: () => aiHeadTimeEl ? aiHeadTimeEl.dataset.base : '',
};`;

try {
  window.eval(appJs + SHIM);
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}

const af = window.__af;
const log = doc.getElementById('log');
if (!af || typeof af.addUser !== 'function' || !log) {
  console.log('FAIL: 没拿到 addUser / log（页面结构变了？）');
  process.exit(1);
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const flush = async (n = 3) => { for (let i = 0; i < n; i++) await tick(); };

const heads = () => log.querySelectorAll('.ai-head');
const times = () => [...log.querySelectorAll('.ai-head .ai-time')];
const foot = () => log.querySelector('.msg-foot');
const footCount = () => log.querySelectorAll('.msg-foot').length;
const kids = () => [...log.children].map((k) => k.className.split(' ')[0]);
const thinkLines = () => [...log.querySelectorAll('.think-plain')];
const thinkSum = (el) => {
  const s = el && el.querySelector('.think-head .think-sum');
  return s ? s.textContent : null;
};

function scene() {
  chatChunks = [];
  af.resetChat();
  af.setBusy(false);      // 不设 false 的话 send()/streamChat 会被 taskActive 拦
}

await flush();
console.log('');

/* ---------------- A. 开场：提问落盘那一刻就位，与后端事件无关 ---------------- */
{
  scene();
  af.addUser('帮我查一下这个项目是干什么的');
  check('A1 头像行在提问落盘时就出现（一个后端事件都还没喂）',
    heads().length === 1, `头像行数=${heads().length}`);
  check('A2 计时也在，文案是「执行中」', /^执行中/.test(af.headText()), af.headText());
  const base = Number(af.headBase()) || 0;
  check('A3 计时基准取「提问落盘那一刻」（不是 0，也不是上一轮的残留）',
    base > 0 && Math.abs(base - Date.now()) < 3000, String(base));
  check('A4 头像行紧跟在提问气泡之后',
    kids()[0] === 'msg-user' && kids()[1] === 'ai-head', kids().join(' | '));
  check('A5 这一轮还没有工具条（回复没结束）', !foot());

  af.addUser('第二问');
  check('A6 每一轮各有一个头像行（不是全局一个）',
    heads().length === 2, `头像行数=${heads().length}`);
}

/* ---------------- B. 工具条只在收尾之后 ---------------- */
{
  scene();
  af.addUser('Q');
  af.handleTaskEvent('delta', { text: '正在写正文' });
  check('B1 正文在写了，仍没有工具条', !foot());
  check('B2 计时在跑，文案还是「执行中」', /^执行中/.test(af.headText()), af.headText());
  check('B3 这一轮还没标记为收尾', af.getReplySettled() === false);

  af.handleTaskEvent('done', {});
  const f = foot();
  check('B4 收尾后工具条出现', !!f);
  check('B5 三个按钮齐全（复制 / 重新回答 / 朗读）',
    !!f && ['复制', '重新回答', '朗读'].every((t) => f.textContent.includes(t)),
    f && f.textContent);
  check('B6 头像行冻结成「完成」', /^完成/.test(af.headText()), af.headText());
  check('B7 收尾标记已落定', af.getReplySettled() === true);
  check('B8 只挂一条工具条', footCount() === 1, `工具条数=${footCount()}`);
}

/* ---------------- C. 收尾之后流程又动起来 → 撤回工具条 ---------------- */
{
  scene();
  af.addUser('Q');
  af.handleTaskEvent('delta', { text: '第一段' });
  af.handleTaskEvent('done', {});
  check('C1 先有工具条', !!foot());

  af.handleTaskEvent('delta', { text: '又接着写第二段' });
  check('C2 流程又动起来了 → 工具条被撤回（不在回答中途挂着）', !foot(),
    foot() && foot().textContent);
  check('C3 头像行回到「执行中」（这一轮还没结束）',
    /^执行中/.test(af.headText()), af.headText());

  af.handleTaskEvent('done', {});
  check('C4 再次收尾 → 工具条回来', !!foot());
  check('C5 而且只有一条（不许叠）', footCount() === 1, `工具条数=${footCount()}`);
}

/* ---------------- D. error 也是收尾，且按结局写标签 ---------------- */
{
  scene();
  af.addUser('Q');
  af.handleTaskEvent('delta', { text: '半截回复' });
  af.handleTaskEvent('error', { message: '模型调用失败' });
  check('D1 出错也冻结头像行（不再永远停在「执行中」）',
    /^已中断/.test(af.headText()), af.headText());
  check('D2 出错也有工具条（半截回复仍是完整回复）', !!foot());
  check('D3 错误框也在', !!doc.querySelector('.errbox'));

  scene();
  af.addUser('Q');
  af.handleTaskEvent('error', { message: '一上来就连不上' });
  check('D4 一个字都没吐时，空头像行被收掉（不留孤零零的「已中断」）',
    heads().length === 0, `头像行数=${heads().length}`);
}

/* ---------------- E. 流读完就收尾：不依赖 done 事件必须到达 ---------------- */
{
  scene();
  af.addUser('Q');
  chatChunks = [sse([['delta', { text: '流里的一段正文' }]])];   // 故意不发 done
  await af.streamChat('Q', []);
  await flush();
  check('E1 流读完就收尾：头像行不再是「执行中」',
    !/^执行中/.test(af.headText()), af.headText());
  check('E2 没等到终态事件也挂上了工具条', !!foot());
  check('E3 收尾标记已落定', af.getReplySettled() === true);
}

/* ---------------- F. 回看多轮：每轮都有头像行，且都冻结 ---------------- */
{
  scene();
  af.enterTaskView({
    id: 'h1', title: '历史任务', status: 'done', duration: 83, prompt: '问题一',
    history: [
      { role: 'user', content: '问题一' }, { role: 'assistant', content: '回答一' },
      { role: 'user', content: '问题二' }, { role: 'assistant', content: '回答二' },
    ],
    files: [],
  });
  check('F1 两轮各有一个头像行', heads().length === 2, `头像行数=${heads().length}`);
  check('F2 没有一行停在「执行中 0m00s」',
    times().every((t) => !/^执行中/.test(t.textContent)),
    times().map((t) => t.textContent).join(' / '));
  check('F3 都按台账冻结成「完成 1m23s」',
    times().every((t) => t.textContent === '完成 1m23s'),
    times().map((t) => t.textContent).join(' / '));
  check('F4 回看也给最后一条回复挂工具条（历史本来就是完整的）', !!foot());
}

/* ---------------- G. 清场换任务：状态一起复位 ---------------- */
{
  scene();
  af.addUser('Q');
  af.handleTaskEvent('delta', { text: '正文' });
  af.handleTaskEvent('done', {});
  af.resetChat();
  check('G1 清场后没有残留工具条', !foot());
  check('G2 清场后没有残留头像行', heads().length === 0, `头像行数=${heads().length}`);

  af.addUser('新的问题');
  check('G3 新回复有自己的头像行', heads().length === 1);
  check('G4 新回复没有被上一条的收尾状态污染（不许继承「已收尾」）',
    af.getReplySettled() === false && !foot(),
    `settled=${af.getReplySettled()} foot=${!!foot()}`);
}

/* ---------------- H. 子任务失败 ≠ 整条回复失败 ----------------
   事故现场：并行跑的某一批子任务里，一个崩了。设计上明确「单个崩了不拖垮同批
   其它任务」，但事件名沿用 error，外层（webui）就把它当整条回复的终态：
   界面立刻收尾，复制工具条挂在回答正中间，台账也把整条任务记成 error。
   修法两条：编排层改发 subtask_error；前端也只把**不带 task_id 的 error**
   当终态。这条用例锁的就是后者。 */
{
  scene();
  af.addUser('帮我做三件事');
  af.handleTaskEvent('delta', { text: '先做第一步……' });
  af.handleTaskEvent('subtask_error', { task_id: 't-b', message: '第二个子任务崩了' });
  check('H1 子任务失败后：整条回复没有收尾', af.getReplySettled() === false);
  check('H2 子任务失败后：没有冒出工具条（回答还在继续）', !foot());
  check('H3 子任务失败有提示行（不是静默吞掉）',
    /子任务失败/.test(log.textContent), log.textContent.slice(0, 120));

  // 老服务端仍可能带 task_id 发 error：同样不许当成整条回复的终点
  af.handleTaskEvent('error', { task_id: 't-c', message: '第三个子任务也崩了' });
  check('H4 带 task_id 的 error 也不收尾（兼容旧事件名）',
    af.getReplySettled() === false && !foot());

  // 真正的致命错误（不带 task_id）才收尾
  af.handleTaskEvent('error', { message: '模型不可用' });
  check('H5 不带 task_id 的 error 才收尾', af.getReplySettled() === true && !!foot());
}

/* ---------------- I. 工具条只能待在整条回复的末尾 ----------------
   事故现场：「一条完整回答的正中间卡着复制按钮」。根因是工具条挂上之后，
   下面又长出了本轮自己的内容（后续步骤的正文 / 新工具组 / 新思考块），
   而工具条留在原地——它就变成了「中途的按钮」。
   不变量：**回复内容出现在工具条下面，工具条立刻撤掉**；
   并且收尾时挂的是「本轮最后一段」，从 DOM 数，不靠 lastSeg 变量。 */
{
  scene();
  af.addUser('分两步做');
  af.handleTaskEvent('delta', { text: '第一步的正文。' });
  af.handleTaskEvent('done', {});
  check('I1 收尾后：工具条挂上了', footCount() === 1, `工具条数=${footCount()}`);

  // 流程又动起来：后续步骤的正文落在工具条下面 → 工具条必须立刻撤掉
  af.handleTaskEvent('delta', { text: '第二步的正文。' });
  check('I2 工具条下面长了新正文 → 立刻撤掉（不许卡在回答中途）', !foot(),
    `工具条数=${footCount()} DOM=${kids().join(' | ')}`);

  af.handleTaskEvent('done', {});
  check('I3 再次收尾：挂在本轮最后一段上', footCount() === 1, `工具条数=${footCount()}`);
  const lastAi = [...log.querySelectorAll('.msg-ai')].pop();
  check('I4 而且确实挂在最后那一段（不是上一段）',
    !!lastAi && !!lastAi.querySelector(':scope > .msg-foot'));

  // 系统侧提示条不算「回复内容」：它本来就排在回复之后，工具条不该因此被撤
  af.handleTaskEvent('verify', { cmd: 'pytest', ok: true, exit: 0, duration: 1.2 });
  check('I5 系统侧提示条在工具条之后，不影响工具条', footCount() === 1,
    `工具条数=${footCount()} DOM=${kids().join(' | ')}`);
}

/* ---------------- J. 提问气泡是入场的前提，缺了就自愈 ----------------
   事故现场：任务跑到一半去看它，是一屏「没有问题气泡、只有输出」的画面。
   凡绕过 enterTaskView 进入（或入场时台账字段还没到）的路径都会这样。
   不变量：**这条任务的视图里必须有它的提问气泡**——任务自己的事件到达时核对一次，
   缺了就按台账原话补，并且永远摆在最前面。（判据问 DOM，不问内存标志。） */
{
  scene();
  af.setOpenTaskId('t7');
  af.setView('chat');
  af.setViewingHistory(false);
  af.setTaskData([{ id: 't7', prompt: '台账里的提问原话', title: '截断过的标题' }]);
  af.handleTaskEvent('delta', { text: '输出已经开始写了' });
  check('J1 缺提问气泡时自愈补上', log.querySelectorAll('.msg-user').length === 1,
    `气泡数=${log.querySelectorAll('.msg-user').length} DOM=${kids().join(' | ')}`);
  check('J2 补的是台账原话（不是截断的标题）',
    (log.querySelector('.msg-user .bub') || {}).textContent === '台账里的提问原话',
    (log.querySelector('.msg-user .bub') || {}).textContent);
  check('J3 提问永远在最前', kids()[0] === 'msg-user', kids().join(' | '));
  check('J4 已有气泡时不重复补（幂等）', (() => {
    af.handleTaskEvent('delta', { text: '继续写' });
    return log.querySelectorAll('.msg-user').length === 1;
  })(), `气泡数=${log.querySelectorAll('.msg-user').length}`);
  check('J5 回看态不补（历史回看由台账整条重绘）', (() => {
    scene();
    af.setOpenTaskId('t8'); af.setView('chat'); af.setViewingHistory(true);
    af.setTaskData([{ id: 't8', prompt: '回看的提问' }]);
    af.handleTaskEvent('delta', { text: 'x' });
    return log.querySelectorAll('.msg-user').length === 0;
  })());
}

/* ---------------- K. dropStaleFeet 的判据（兜底的那张网）----------------
   实时路径上，「工具条下面又长内容」一般会先被 unsettleReply 撤掉，所以这张网
   平时看不见。它防的是**将来新增一条把内容写到工具条下面的路径、却忘了撤销**——
   于是不需要每个调用点都记得这件事。既然它是「忘了也不会错」的保证，就得把判据
   自己钉死：**下面还有本轮回复的内容就撤，只有系统侧注记就留。** */
{
  scene();
  const mkFoot = () => {
    const a = af.addAssistant();
    a.text = '一段正文';
    const f = doc.createElement('div');
    f.className = 'msg-foot';
    a.m.appendChild(f);
    return { a, f };
  };
  const sysNote = () => { const d = doc.createElement('div'); d.className = 'pillnote'; log.appendChild(d); return d; };

  // K1 下面还有正文段
  scene();
  let { f } = mkFoot();
  af.addAssistant();                                  // 工具条下面又来了一段回复正文
  af.dropStaleFeet();
  check('K1 工具条下面还有正文段 → 撤掉', !f.isConnected);

  // K2 下面还有工具组
  scene();
  ({ f } = mkFoot());
  const tg = doc.createElement('div'); tg.className = 'tool-group'; log.appendChild(tg);
  af.dropStaleFeet();
  check('K2 工具条下面还有工具组 → 撤掉', !f.isConnected);

  // K3 下面只有系统侧提示条 / 错误框 / 产出卡片：保留
  scene();
  ({ f } = mkFoot());
  sysNote();
  const eb = doc.createElement('div'); eb.className = 'errbox'; log.appendChild(eb);
  const fc = doc.createElement('div'); fc.className = 'file-cards'; log.appendChild(fc);
  af.dropStaleFeet();
  check('K3 下面只有系统侧注记（提示条/错误框/产出卡片）→ 保留', f.isConnected);

  // K4 工具条本来就是整条的结尾：保留
  scene();
  ({ f } = mkFoot());
  af.dropStaleFeet();
  check('K4 工具条已在末尾 → 保留', f.isConnected);
}

/* ---------------- L. 思维链：在流时可见，收束后只剩一行 ----------------
   用户的原话（逐字）：「每当你的思维链在完全打印出来以后，直接将思维链隐藏，并根据
   用户的文字偏好（会在记忆里面），将思维链的结果总结成简短的一句话，比如：『决定
   调用工具 fetch_json』；『将任务拆解为若干步骤，执行第一步：先列目录』，在执行完
   第一步以后，『执行第二步：再读文件』」。他说这两条没被执行——因为它们当时只写在
   提示词里。这里是架构保证：由**事件**（调了哪个工具 / 走到第几步 / 拆成几步）生成，
   不经过模型，所以既不依赖模型自觉，也不会被模型复述错。
   锁三件事：① 正文收起来；② 那一行写的是**动作**而不是「思考过程」四个字；
   ③ 那一行跟着事情推进**就地改写**，不堆成一片。 */
{
  scene();
  af.addUser('给这个项目加一个 README');

  af.handleTaskEvent('thinking', { text: '先看看目录里有什么…' });
  let tl = thinkLines();
  check('L1 思考块在流时是展开的（推理看得见）',
    tl.length === 1 && tl[0].classList.contains('open'), `思考块数=${tl.length}`);

  af.handleTaskEvent('tool_start', { name: 'fetch_json', args: {} });
  tl = thinkLines();
  check('L2 开始动手 → 思维链收起（正文不再可见）',
    tl.length === 1 && !tl[0].classList.contains('open'), tl[0] && tl[0].className);
  check('L3 收起那一行写的是「决定调用工具 fetch_json」（工具名来自事件）',
    thinkSum(tl[0]) === '决定调用工具 fetch_json', JSON.stringify(thinkSum(tl[0])));

  af.handleTaskEvent('plan', {
    subtasks: [
      { index: 1, title: '读一下现有目录结构', kind_label: '检视', model_name: 'gemma4:12b', reason: 'x' },
      { index: 2, title: '写 README', kind_label: '写作', model_name: 'gemma4:12b', reason: 'x' },
    ],
    summary: '2 个任务 → 1 个 agent（gemma4:12b）',
  });
  check('L4 拆解不新起一块——还是那一行', thinkLines().length === 1,
    `思考块数=${thinkLines().length}`);
  check('L5 那一行写的是拆解动作「将任务拆解为 2 步，先做：…」',
    thinkSum(thinkLines()[0]) === '将任务拆解为 2 步，先做：读一下现有目录结构',
    JSON.stringify(thinkSum(thinkLines()[0])));

  af.handleTaskEvent('subtask', { index: 2, total: 2, text: '写 README', task_id: 't2' });
  check('L6 走到第 2 步 → 同一行就地改写（不另起一行）',
    thinkLines().length === 1 && thinkSum(thinkLines()[0]) === '执行第 2/2 步：写 README',
    JSON.stringify(thinkSum(thinkLines()[0])));

  af.handleTaskEvent('delta', { text: '正文来了' });
  check('L7 开始动笔 → 那一行收敛成「整理结论并作答」',
    thinkSum(thinkLines()[0]) === '整理结论并作答', JSON.stringify(thinkSum(thinkLines()[0])));
  check('L8 全程只有一行', thinkLines().length === 1, `思考块数=${thinkLines().length}`);
  check('L9 那一行在正文之上（收起的思考不挤在回答中间）',
    !!(log.querySelector('.think-plain').compareDocumentPosition(log.querySelector('.msg-ai'))
      & 4) /* DOCUMENT_POSITION_FOLLOWING */, '顺序不对');

  // 模型压根不吐思维链时，这一行仍然要有——否则「在干什么」只在会推理的模型上看得见
  scene();
  af.addUser('查一下 git 上的 REA');
  af.handleTaskEvent('plan', {
    subtasks: [{ index: 1, title: '检索 REA', kind_label: '检视', model_name: 'gemma4:12b', reason: 'x' }],
    summary: '1 个任务 → 1 个 agent（gemma4:12b）',
  });
  check('L10 没有思维链也有一行（否则动作信息只在会推理的模型上可见）',
    thinkLines().length === 1 && thinkSum(thinkLines()[0]) === '将任务拆解为 1 步，先做：检索 REA',
    `${thinkLines().length} 行 / ${JSON.stringify(thinkSum(thinkLines()[0]))}`);

  // 跨轮隔离：上一轮的思考行不许被这一轮改写
  scene();
  af.addUser('第一轮');
  af.handleTaskEvent('plan', {
    subtasks: [{ index: 1, title: '甲', kind_label: '检视', model_name: 'm', reason: 'x' }], summary: '' });
  const first = thinkSum(thinkLines()[0]);
  af.addUser('第二轮');
  af.handleTaskEvent('tool_start', { name: 'list_dir', args: {} });
  check('L11 上一轮的思考行不被这一轮的进度改写',
    thinkSum(thinkLines()[0]) === first,
    `${JSON.stringify(thinkSum(thinkLines()[0]))} vs ${JSON.stringify(first)}`);
  check('L12 这一轮自己的行写的是自己的动作',
    thinkLines().length === 2 && thinkSum(thinkLines()[1]) === '决定调用工具 list_dir',
    `思考块数=${thinkLines().length}`);
}

/* ---------------- M. 泳道只承载输出，不再有列头 ----------------
   用户的原话：「我不需要知道各任务的流式输出是靠谁的，直接删掉」＋「从计时往下到
   『任务拆解』之间的内容都没有用」。列头那三样（长标题 / 运行中徽标 / 模型名）与下方
   计划卡完全重复：序号、标题、性质、交给谁、走到第几步，计划卡全有。而窄列里 26 字的
   标题必然折行、截断——就是用户截图里那一片糊字。保留分列的唯一理由是别让两路输出
   串成一段，所以只留 body、不留列头。 */
{
  scene();
  af.addUser('做两件互不相关的事');
  af.handleTaskEvent('dispatch', {
    multi: true, parallel: true, source: 'rules',
    tasks: [{ id: 't1', index: 1, title: '查 git 上的 REA' },
            { id: 't2', index: 2, title: '写一份评估报告' }],
  });
  const wrap = log.querySelector('.lanes');
  check('M1 并行仍然分列（两路输出不能串成一段）', !!wrap);
  check('M2 列头整块没了：标题 / 徽标 / 模型名一个都不留',
    !log.querySelector('.lane-head') && !log.querySelector('.lane-title')
    && !log.querySelector('.lane-badge') && !log.querySelector('.lane-model'),
    log.querySelector('.lanes') && log.querySelector('.lanes').innerHTML.slice(0, 120));
  check('M3 每一列都留着承载输出的 body',
    !!wrap && wrap.querySelectorAll('.lane-body').length === 2,
    wrap && String(wrap.querySelectorAll('.lane-body').length));

  af.handleTaskEvent('delta', { text: 'A 路输出', task_id: 't1' });
  af.handleTaskEvent('delta', { text: 'B 路输出', task_id: 't2' });
  const bodies = [...wrap.querySelectorAll('.lane-body')].map((b) => b.textContent);
  check('M4 两路输出各自留在自己那一列（不互相穿插）',
    bodies[0].includes('A 路输出') && !bodies[0].includes('B 路输出') && bodies[1].includes('B 路输出'),
    JSON.stringify(bodies));
}

console.log('');
try { dom.window.close(); } catch { /* 忽略 */ }
if (failed) {
  console.log(`${failed} 项失败。`);
  process.exit(1);
}
console.log('全部通过。');
process.exit(0);
