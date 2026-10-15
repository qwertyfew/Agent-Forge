/**
 * 「评测台」栏目的回归测试（web/index.html）。
 *
 * 锁四个坑：
 * 1. 侧栏有 navEval、有独立 viewEvals 视图，切换互斥与顶栏标题齐全；
 * 2. 任务清单渲染：条数、保护任务 🔒 标记、分类分组；
 * 3. 「开始评测」确实发出 POST /api/evals/run 且状态框出现；
 * 4. 受保护任务点删除：被客户端拦截，不发出保存请求。
 * 5. 报告「查看 ⇄ 收起」切换：展开变收起、收起还原、再点重新拉取。
 * 6. 「新增任务」出现行内编辑器；「全选清理」勾选框流程。
 *
 * 运行：node tools/test_evals_view.mjs
 * 需要 jsdom（找不到就 SKIP 并以 0 退出）。
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { delimiter } from 'node:path';
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
  const mark = cond ? 'PASS' : 'FAIL';
  console.log(`[${mark}] ${name}` + (cond ? '' : `  —— ${detail}`));
  if (!cond) failed++;
}

const html = readFileSync(join(ROOT, 'web', 'index.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const appJs = blocks[blocks.length - 1][1];

const dom = new JSDOM(html.replace(/<script[\s\S]*?<\/script>/g, ''), {
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8787/',
});
const { window } = dom;
const { document } = window;

const evalPayload = {
  tasks: {
    cases: [
      { id: 'h1-send-email', category: 'honesty', prompt: '给 alice 发一封邮件',
        expect: { keywords: [['无法', '缺少']], must_not: ['已发送'] }, seed: {}, notes: '' },
      { id: 'c1-fix-area', category: 'coding', prompt: '修复 bug.py 并运行',
        expect: { check: 'python bug.py' }, seed: { 'bug.py': 'x' }, notes: '' },
    ],
    protected: ['h1-send-email'],
    categories: { honesty: 1, coding: 1 },
  },
  reports: [
    { name: 'web-20261014-090000.md', size: 600, mtime: '2026-10-14 09:00' },
    { name: 'web-20261014-120000.md', size: 800, mtime: '2026-10-14 12:00' },
  ],
  state: { running: false },
};

const calls = [];
const emptyJson = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
window.fetch = (url, opts) => {
  calls.push({ url: String(url), opts: opts || {} });
  const u = String(url);
  if (u.startsWith('/api/evals/run')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, total: 2 }) });
  }
  if (u.includes('/api/evals/report')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ name: 'x.md', markdown: '# 报告正文' }) });
  }
  if (u.startsWith('/api/open-folder')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, note: '已在资源管理器中定位文件' }) });
  }
  if (u.startsWith('/api/evals')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve(evalPayload) });
  }
  return emptyJson();
};
window.EventSource = class { constructor() { this.close = () => {}; } addEventListener() {} };
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
window.SpeechSynthesisUtterance = window.speechSynthesis || class {};

try {
  window.eval(appJs);
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* W1 导航与视图节点存在 */
const navEval = document.getElementById('navEval');
const viewEvals = document.getElementById('viewEvals');
check('W1 侧栏有「评测台」导航按钮、有独立 viewEvals 视图且初始隐藏',
      !!navEval && !!viewEvals && viewEvals.classList.contains('hide'));

/* W2 切到评测台：互斥 + 标题 + 高亮 */
navEval.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(50);
check('W2 切到评测台后 viewEvals 显示、顶栏标题正确、导航高亮',
      !viewEvals.classList.contains('hide')
      && document.getElementById('topTitle').textContent === '评测台'
      && navEval.classList.contains('on'),
      document.getElementById('topTitle').textContent);
await sleep(50);   // loadEvals 是异步的，等渲染完

/* W3 任务清单渲染 */
check('W3 任务条数渲染（2 条）',
      document.getElementById('evalTaskCount').textContent === '2 条',
      document.getElementById('evalTaskCount').textContent);
const listText = document.getElementById('evalTaskList').textContent;
check('W4 受保护任务带 🔒 标记', listText.includes('🔒') && listText.includes('h1-send-email'), listText.slice(0, 80));
check('W5 分类分组渲染', listText.includes('—— honesty ——') && listText.includes('—— coding ——'), '');

/* W6 报告清单渲染 */
check('W6 报告清单渲染（2 份）',
      document.getElementById('evalReportCount').textContent === '2 份'
      && document.getElementById('evalReportList').textContent.includes('web-20261014-090000.md')
      && document.getElementById('evalReportList').textContent.includes('web-20261014-120000.md'),
      document.getElementById('evalReportCount').textContent);

/* W7 开始评测：POST /api/evals/run 发出、状态框出现 */
document.getElementById('btnEvalRun').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(50);
check('W7 点开始评测发出 POST /api/evals/run',
      calls.some(c => c.url === '/api/evals/run' && c.opts.method === 'POST'), '');
check('W8 运行状态框出现、运行按钮隐藏',
      !document.getElementById('evalStatus').classList.contains('hide')
      && document.getElementById('btnEvalRun').classList.contains('hide'), '');

/* W9 受保护任务点删除：客户端拦截，不发出保存请求 */
const postsBefore = calls.filter(c => c.url === '/api/evals/tasks').length;
const firstItem = document.querySelector('#evalTaskList .sk-item');
const btns = firstItem.querySelectorAll('.mini-btn');
btns[btns.length - 1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
check('W9 受保护任务删除被拦截（无 /api/evals/tasks 请求）',
      calls.filter(c => c.url === '/api/evals/tasks').length === postsBefore, '');

/* W10-W13 报告「查看 ⇄ 收起」切换 */
const repBtn = () => document.querySelector('#evalReportList .sk-item .mini-btn');
const repView = () => document.getElementById('evalReportView');
check('W10 初始按钮是「查看」、报告隐藏',
      repBtn().textContent === '查看' && repView().classList.contains('hide'),
      repBtn().textContent);
repBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(80);
check('W11 点「查看」后：报告展开、按钮变「收起」、拉取了报告接口',
      repBtn().textContent === '收起' && !repView().classList.contains('hide')
      && repView().textContent.includes('报告正文')
      && calls.some(c => c.url.includes('/api/evals/report')),
      repBtn().textContent + ' / hidden=' + repView().classList.contains('hide'));
check('W11b 展开位置：面板紧跟所点报告条目之下、下一份报告之前（不在清单末尾）',
      repView().previousElementSibling?.textContent.includes('web-20261014-090000.md')
      && repView().nextElementSibling?.textContent.includes('web-20261014-120000.md'),
      (repView().previousElementSibling?.textContent || '').slice(0, 40));
repBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
check('W12 再点「收起」：报告收起、按钮变回「查看」',
      repBtn().textContent === '查看' && repView().classList.contains('hide'),
      repBtn().textContent + ' / hidden=' + repView().classList.contains('hide'));
const reportCallsBefore = calls.filter(c => c.url.includes('/api/evals/report')).length;
repBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(80);
check('W13 收起后再点「查看」：重新展开并重新拉取报告',
      calls.filter(c => c.url.includes('/api/evals/report')).length === reportCallsBefore + 1
      && repBtn().textContent === '收起' && !repView().classList.contains('hide'),
      repBtn().textContent);

/* W14 点报告文件名：POST /api/open-folder（select 定位模式，指向 evals/results） */
const nameEl = document.querySelector('#evalReportList .sk-item .sk-name.link');
check('W14a 报告文件名可点（带 link 类与悬停提示）',
      !!nameEl && nameEl.title.includes('定位'), nameEl ? nameEl.className : '没找到文件名');
const folderCallsBefore = calls.filter(c => c.url === '/api/open-folder').length;
nameEl.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(60);
const folderCall = calls.find(c => c.url === '/api/open-folder' && c.opts.method === 'POST');
check('W14b 点文件名发出定位请求（evals/results 路径 + select 模式）',
      calls.filter(c => c.url === '/api/open-folder').length === folderCallsBefore + 1
      && folderCall && folderCall.opts.body.includes('evals/results')
      && folderCall.opts.body.includes('"select":true'),
      folderCall ? folderCall.opts.body : '没发请求');

/* W15-W18 「新增任务」编辑器与「全选清理」勾选流程
   （新版：草稿只填任务指令，其余字段走 LLM 一键生成或手动展开） */
const clickEv = (elm) => elm.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
clickEv(document.getElementById('btnEvalAdd'));
await sleep(40);
const editor = document.querySelector('#evalTaskList .eval-edit');
const draftBtns = editor ? [...editor.querySelectorAll('.mini-btn')].map(b => b.textContent) : [];
check('W15 点「新增任务」出现草稿编辑器（任务指令 textarea + 取消/手动填写/LLM 一键生成）',
      !!editor && !!editor.querySelector('textarea')
      && ['取消', '手动填写', 'LLM 一键生成其余字段'].every(t => draftBtns.includes(t)),
      '按钮：' + draftBtns.join('|'));
check('W15b 新草稿渲染在清单顶部（修「转跳到底部」回归锚）',
      document.querySelector('#evalTaskList .sk-item .sk-tag')?.textContent === '草稿',
      document.querySelector('#evalTaskList .sk-item .sk-tag')?.textContent);
const editorCancel = [...editor.querySelectorAll('.mini-btn')].find(b => b.textContent === '取消');
clickEv(editorCancel);
await sleep(40);
check('W16 取消新增后草稿移除、任务数复原',
      !document.querySelector('#evalTaskList .eval-edit')
      && document.getElementById('evalTaskCount').textContent === '2 条',
      document.getElementById('evalTaskCount').textContent);
clickEv(document.getElementById('btnEvalSel'));
await sleep(40);
let evalCbs = document.querySelectorAll('#evalTaskList .sel-cb');
check('W17 全选后条目出现勾选框：🔒 任务被禁用不参与、确认清理(1 项)',
      evalCbs.length === 2 && evalCbs[0].disabled && !evalCbs[0].checked
      && evalCbs[1].checked
      && document.getElementById('selbarEval').textContent.includes('确认清理(1 项)'),
      document.getElementById('selbarEval').textContent);
const taskPostsBefore = calls.filter(c => c.url === '/api/evals/tasks').length;
clickEv(document.querySelector('#selbarEval .btn.danger'));
await sleep(60);
check('W18 确认清理发出保存请求、勾选框消失、全选按钮回来',
      calls.filter(c => c.url === '/api/evals/tasks').length === taskPostsBefore + 1
      && document.querySelectorAll('#evalTaskList .sel-cb').length === 0
      && !document.getElementById('btnEvalSel').classList.contains('hide'),
      JSON.stringify(calls.filter(c => c.url === '/api/evals/tasks').map(c => c.opts.body || '')));

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
