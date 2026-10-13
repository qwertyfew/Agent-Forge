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
  setBusy: (b) => { busy = b; },
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

console.log('');
try { dom.window.close(); } catch { /* 忽略 */ }
if (failed) {
  console.log(`${failed} 项失败。`);
  process.exit(1);
}
console.log('全部通过。');
process.exit(0);
