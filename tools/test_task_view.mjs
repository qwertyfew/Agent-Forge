/**
 * 「进入任务视图」的回归测试（web/index.html 的 enterTaskView / adoptRunning / openTask）。
 *
 * 锁的是这个坑：脚本 / CLI / 另一个标签页发起任务后，在网页上看它跑，是一屏
 * 「没有问题气泡、没有头像行、没有计时」的输出；等它跑完、从侧栏点进去回看才补齐。
 * 同一件事三条路各写各的，差别只取决于「谁发起的、什么时候看的」——根因是提问的
 * 原文只活在发起方的内存里，台账要到任务结束才写 history，中途只能拿截断的标题充数。
 *
 * 断言写的是：「不管任务是谁发起的、页面之前停在哪，只要入场，提问、头像行、计时
 * 都从台账来」以及「不该抢画面的时候（自己的任务在跑 / 正在回看 / 停在别的页）
 * 一行都不许动」。
 *
 * 运行：node tools/test_task_view.mjs
 * 需要 jsdom（任一 node_modules 里能找到即可，找不到就 SKIP 并以 0 退出）。
 * 全部断言内建，退出码 0 = 全过；不需要模型、不需要联网。
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

/* fetch 替身：/api/tasks 给列表，/api/tasks?id= 给单条。
   断言不碰后端，只喂台账形状的数据——测的是「拿到台账之后怎么画」。 */
let taskPayload = { tasks: [], running: 0 };
let detailTask = null;
window.fetch = (url) => {
  const u = String(url);
  let body = {};
  if (u.startsWith('/api/tasks?')) body = { task: detailTask };
  else if (u.startsWith('/api/tasks')) body = taskPayload;
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
};
/* EventSource 替身：记下什么时候挂了流、挂的是哪条——「挂流 = 这段画面的内容来源」 */
const streams = [];
window.EventSource = class {
  constructor(url) {
    this.url = String(url); this.closed = false; this.handlers = {};
    streams.push(this);
  }
  addEventListener(n, fn) { (this.handlers[n] = this.handlers[n] || []).push(fn); }
  close() { this.closed = true; }
};
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
window.SpeechSynthesisUtterance = window.SpeechSynthesisUtterance || class {};

// 严格模式下 eval 出来的函数不挂 window，末尾补一行把要测的递出来
const SHIM = `
;window.__af = {
  loadTasks, openTask, enterTaskView, handleTaskEvent, taskPrompt, resetChat,
  setBusy: (b) => { busy = b; },
  setViewingHistory: (v) => { viewingHistory = v; },
  setView: (v) => { view = v; },
  setOpenTaskId: (v) => { openTaskId = v; },
  setLiveTask: (v) => { liveTask = v; },
  getOpenTaskId: () => openTaskId,
  getLiveTask: () => liveTask,
  getViewingHistory: () => viewingHistory,
  getView: () => view,
};`;

try {
  window.eval(appJs + SHIM);
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}

const af = window.__af;
const log = doc.getElementById('log');
const input = doc.getElementById('input');
if (!af || typeof af.enterTaskView !== 'function' || !log) {
  console.log('FAIL: 没拿到 enterTaskView / log（页面结构变了？）');
  process.exit(1);
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const flush = async (n = 3) => { for (let i = 0; i < n; i++) await tick(); };

const PROMPT = '帮我查一下 git 上那个 REA 项目（Reverse Engineer Anything）是干什么的，介绍一下。';
const t0 = 1770000000;   // 固定资产的时间戳：计时基准要拿它比，不能用「现在」

function task(over = {}) {
  return Object.assign({
    id: 't1', title: '查一下 git 上那个 REA 项目',
    prompt: PROMPT, status: 'running', created_at: t0, duration: 12,
    tools: 0, cost: 0, local_only: true, progress: '', models: ['qwen3'],
    history: [], files: [],
  }, over);
}

/* 每个用例前把页面摆成指定的样子：清场 → 塞旧内容 → 摆状态位。
   旧内容是关键——「已经看过一条任务再发下一条」正是出问题的现场。 */
function scene(o = {}) {
  af.resetChat();
  if (o.logHtml) log.innerHTML = o.logHtml;
  af.setOpenTaskId(o.openTaskId || '');
  af.setBusy(!!o.busy);
  af.setViewingHistory(!!o.viewingHistory);
  af.setView(o.view || 'chat');
  af.setLiveTask(o.live || null);
  input.value = o.draft || '';
  streams.length = 0;
}
const bubbleText = () => {
  const b = log.querySelector('.msg-user .bub');
  return b ? b.textContent : '';
};
const kids = () => [...log.children].map((k) => k.className.split(' ')[0]);

const OLD = '<div class="msg-user"><div class="bub">上一条任务的提问</div></div>';

/* 页面加载时自己跑过一次 loadTasks（拿到空列表），先让它落地 */
await flush();

/* ---------------- 用例 1：外部发起的任务，页面「不干净」也要入场 ---------------- */
{
  scene({ openTaskId: 'tPrev', logHtml: OLD, draft: '我正在打字' });
  taskPayload = { tasks: [task()], running: 1 };
  await af.loadTasks();
  await flush();

  check('入场：提问气泡出现了', log.querySelectorAll('.msg-user').length === 1,
    `气泡数=${log.querySelectorAll('.msg-user').length}`);
  check('气泡里是台账原话（不是截断的标题）', bubbleText() === PROMPT, bubbleText());
  check('上一条任务的内容已被清掉', !/上一条任务的提问/.test(log.textContent), log.textContent.slice(0, 80));
  check('画面切到了这条任务（侧栏高亮 / 不是回看态）',
    af.getOpenTaskId() === 't1' && af.getViewingHistory() === false);
  check('挂上了这条任务的事件流（重放 + 续播）',
    streams.length === 1 && /t1/.test(streams[0].url),
    streams.map((s) => s.url).join(','));
  check('输入框里的草稿没被清掉（只换画面，不动用户正在写的东西）',
    input.value === '我正在打字', input.value);

  // 头像行与计时在**提问落盘的这一刻**就该在——不用等第一段输出到达。
  // （这条曾经是反的：头像行 lazy 挂在 8 个渲染函数里，谁先出现谁负责插，
  //   于是「什么时候出现」被交给了「模型什么时候吐第一个 token」。）
  check('头像行与计时在提问落盘时就位（没等任何 AI 事件）',
    log.querySelectorAll('.ai-head').length === 1,
    `头像行数=${log.querySelectorAll('.ai-head').length}`);
  check('头像行紧跟在提问气泡之后',
    kids()[0] === 'msg-user' && kids()[1] === 'ai-head', kids().join(' | '));

  // 第一段输出到达：头像行本就在提问之后，这里确认它在 AI 内容之前
  af.handleTaskEvent('thinking', { text: '先查一下…' });
  check('提问之后紧跟头像行', kids()[1] === 'ai-head', kids().join(' | '));
  check('头像行在第一条 AI 内容之前',
    kids()[0] === 'msg-user' && kids()[2] === 'think-plain', kids().join(' | '));
  check('头像行只有一个（没被后到的 AI 元素又插一个）',
    log.querySelectorAll('.ai-head').length === 1,
    `头像行数=${log.querySelectorAll('.ai-head').length}`);
  const timeEl = log.querySelector('.ai-head .ai-time');
  check('头像行带计时，且基准是任务真正的开始时间（不是「现在」）',
    timeEl && timeEl.dataset.base === String(t0 * 1000),
    timeEl ? timeEl.dataset.base : '没有 .ai-time');
  check('计时文案是「执行中」（这段回复确实在跑）',
    timeEl && /^执行中/.test(timeEl.textContent), timeEl && timeEl.textContent);
}

/* ---------------- 用例 2：老台账没有 prompt，退回 history 首条 user ---------------- */
{
  scene({ openTaskId: 'tPrev', logHtml: OLD });
  taskPayload = {
    tasks: [task({ id: 'tOld', prompt: '', history: [{ role: 'user', content: '老台账里的提问' }] })],
    running: 1,
  };
  await af.loadTasks();
  await flush();
  check('台账缺 prompt 时，从 history 里取提问', bubbleText() === '老台账里的提问', bubbleText());
}

/* ---------------- 用例 3：本页自己的任务在跑 → 不抢画面、不挂流 ---------------- */
{
  scene({
    openTaskId: 'tMine', logHtml: OLD, busy: true,
    live: { id: 'tMine', startedAt: t0, status: 'running' },
  });
  taskPayload = { tasks: [task({ id: 'tOther' })], running: 1 };
  await af.loadTasks();
  await flush();
  check('自己的任务在跑时，外部任务不抢画面', /上一条任务的提问/.test(log.textContent),
    log.textContent.slice(0, 60));
  check('也不挂流（挂上就是把别人的事件落进这段对话）', streams.length === 0,
    streams.map((s) => s.url).join(','));
  check('画面仍指向自己的任务', af.getOpenTaskId() === 'tMine', af.getOpenTaskId());
}

/* ---------------- 用例 4：正在回看历史，也要入场 ----------------
   这条判据一开始写反过：以为「回看中」属于「别打扰用户」。但这里放过的后果
   特别重——用户停在回看态，轮询每次都跳过，直到他主动点开侧栏那条任务为止，
   等于这条任务永远看不见（正是用户报的那个症状）。想看旧任务点回去即可，
   而入场每个任务只会发生一次。 */
{
  scene({ openTaskId: 'tFin1', logHtml: OLD, viewingHistory: true });
  taskPayload = { tasks: [task({ id: 'tOther', prompt: '新任务的提问' })], running: 1 };
  await af.loadTasks();
  await flush();
  check('正在回看历史时，外部任务照样入场（否则它永远看不见）',
    bubbleText() === '新任务的提问', bubbleText());
  check('旧任务的内容被清掉', !/上一条任务的提问/.test(log.textContent));
  check('回看态被解除（画面属于新任务了）',
    af.getViewingHistory() === false && af.getOpenTaskId() === 'tOther');
  check('挂上了新任务的流', streams.length === 1 && /tOther/.test(streams[0].url));
}

/* ---------------- 用例 5：停在设置页 → 不抢画面 ---------------- */
{
  scene({ view: 'settings', logHtml: OLD });
  taskPayload = { tasks: [task({ id: 'tOther' })], running: 1 };
  await af.loadTasks();
  await flush();
  check('停在设置页时不被打断（留在设置页）', af.getView() === 'settings', af.getView());
  check('也不挂流', streams.length === 0);
}

/* ---------------- 用例 6：点侧栏一条正在跑的任务 → 清场 + 提问 + 挂流 ---------------- */
{
  scene({ logHtml: OLD, live: null });
  detailTask = task({ id: 'tLive', prompt: '外部任务的提问：查一下 REA' });
  af.openTask('tLive');
  await flush();
  check('打开正在跑的任务：提问补上了', bubbleText() === '外部任务的提问：查一下 REA', bubbleText());
  check('旧内容被清掉', !/上一条任务的提问/.test(log.textContent));
  check('挂上它的流（从缓冲重放，整段过程补得回来）',
    streams.length === 1 && /tLive/.test(streams[0].url), streams.map((s) => s.url).join(','));
  check('liveTask 指向它（发送键随之变成停止键）',
    af.getLiveTask() && af.getLiveTask().id === 'tLive', JSON.stringify(af.getLiveTask()));
}

/* ---------------- 用例 7：清场之后事件仍然落在画面上 ----------------
   「新对话」把 log 清了，但渲染状态还指着已删掉的节点时，后面的事件会写进空气里：
   流还在跑、界面永远空着。这跟「提问/头像/计时缺失」是同一类病：状态没跟着 DOM 走。 */
{
  scene({ openTaskId: 'tPrev', logHtml: OLD });
  taskPayload = { tasks: [task({ id: 'tRun', prompt: '清场测试' })], running: 1 };
  await af.loadTasks();
  await flush();
  af.resetChat();                                    // 用户点了「新对话」
  af.handleTaskEvent('delta', { text: '清场之后的输出' });
  const body = log.querySelector('.msg-ai .body');
  check('清场后事件依然画在页面上（没写进已删掉的节点）',
    body && /清场之后的输出/.test(body.textContent), body ? body.textContent : '没有 .msg-ai');
  check('清场后的新输出仍带着头像行',
    kids()[0] === 'ai-head' || kids().includes('ai-head'), kids().join(' | '));
}

/* ---------------- 用例 8：回看已结束的任务，提问只画一条 ---------------- */
{
  scene({});
  detailTask = task({
    id: 'tFin', status: 'done', duration: 83, prompt: '回看的提问',
    history: [{ role: 'user', content: '回看的提问' },
              { role: 'assistant', content: '答完了' }],
  });
  af.openTask('tFin');
  await flush();
  check('回看：提问只画一条（台账 prompt 与 history 不重复上屏）',
    log.querySelectorAll('.msg-user').length === 1,
    `气泡数=${log.querySelectorAll('.msg-user').length}`);
  check('回看：气泡是原话', bubbleText() === '回看的提问', bubbleText());
  check('回看：标成历史回看', af.getViewingHistory() === true);
  const timeEl = log.querySelector('.ai-head .ai-time');
  check('回看：头像行时间按台账冻结成「完成 1m23s」',
    timeEl && timeEl.textContent === '完成 1m23s', timeEl && timeEl.textContent);
  check('回看：没有为它挂流（历史不是实时的）',
    streams.filter((s) => /tFin/.test(s.url)).length === 0);
}

/* ---------------- 用例 9：taskPrompt 的兜底顺序 ---------------- */
{
  const f = af.taskPrompt;
  check('有 prompt 时用 prompt', f({ prompt: 'A', history: [{ role: 'user', content: 'B' }], title: 'C' }) === 'A');
  check('缺 prompt 时用 history 首条 user',
    f({ prompt: '', history: [{ role: 'user', content: 'B' }], title: 'C' }) === 'B');
  check('都没有时才退回标题', f({ title: 'C' }) === 'C');
  check('空对象不炸', f({}) === '' && f(null) === '');
}

console.log('');
try { dom.window.close(); } catch { /* 忽略 */ }
if (failed) {
  console.log(`${failed} 项失败。`);
  process.exit(1);
}
console.log('全部通过。');
process.exit(0);
