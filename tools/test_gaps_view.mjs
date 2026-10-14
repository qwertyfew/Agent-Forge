/**
 * 「能力缺口」独立栏目的回归测试（web/index.html）。
 *
 * 锁三个坑：
 * 1. 缺口管理必须从「技能管理」里独立出来：侧栏有 navGaps、有独立的
 *    viewGaps 视图，技能管理视图里不再内嵌 sk-sec 缺口块；
 * 2. showView('gaps') 行为完整：视图显隐互斥、顶栏标题、导航高亮、
 *    输入区隐藏、返回键出现——和技能管理同一待遇，漏一条就是布局 bug；
 * 3. 刷新按钮存在且技能/缺口两栏的刷新互不牵连；
 * 4. 缺失记录带「删除」按钮：在 .sk-actions 里（靠右对齐），确认后发
 *    POST /api/gaps {kind,key}，成功后清单刷新。
 *
 * 运行：node tools/test_gaps_view.mjs
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

const emptyJson = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
const gapPayload = { items: [
  { kind: 'tool_missing', key: 'send_sms', count: 2, kind_label: '工具缺失',
    samples: ['调用 send_sms 失败', '再次调用'], first: '2026-10-14 10:00', last: '2026-10-14 11:00' },
], total: 2 };
const calls = [];
window.fetch = (url, opts) => {
  calls.push({ url: String(url), method: (opts && opts.method) || 'GET', body: (opts && opts.body) || '' });
  const u = String(url);
  if (u === '/api/gaps') {
    if ((opts && opts.method) === 'POST') {
      let req = {}; try { req = JSON.parse(opts.body || '{}'); } catch { /* 忽略 */ }
      if (req.key === 'send_sms') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ removed: 1, items: [], total: 0 }) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ error: 'unknown key' }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve(gapPayload) });
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

/* V1 导航与视图节点存在，缺口块不再内嵌在技能管理里 */
const navGaps = document.getElementById('navGaps');
const viewGaps = document.getElementById('viewGaps');
const viewSkills = document.getElementById('viewSkills');
check('V1 侧栏有「能力缺口」导航按钮', !!navGaps);
check('V2 有独立的 viewGaps 视图', !!viewGaps);
check('V3 技能管理视图里已无内嵌缺口块（gapList 只在 viewGaps 里）',
      !!viewGaps.querySelector('#gapList') && !viewSkills.querySelector('#gapList'));
check('V4 viewGaps 含独立的刷新与清空按钮',
      !!viewGaps.querySelector('#btnGapRefresh') && !!viewGaps.querySelector('#btnGapClear'));

/* V5 初始互斥：viewGaps 默认隐藏 */
check('V5 viewGaps 初始隐藏', viewGaps.classList.contains('hide'));

/* V6 进入缺口栏目：视图显隐互斥 + 顶栏标题 + 导航高亮（点击导航触发） */
navGaps.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('V6 切到缺口栏目后 viewGaps 显示、viewSkills 隐藏',
      !viewGaps.classList.contains('hide') && viewSkills.classList.contains('hide'));
check('V7 顶栏标题为「能力缺口」',
      document.getElementById('topTitle').textContent === '能力缺口',
      document.getElementById('topTitle').textContent);
check('V8 navGaps 高亮、navSkill 不高亮',
      navGaps.classList.contains('on') && !document.getElementById('navSkill').classList.contains('on'));
check('V9 缺口栏目下输入区隐藏、返回键出现',
      document.getElementById('composerWrap').classList.contains('hide')
      && !document.getElementById('btnBack').classList.contains('hide'));

/* V10 切回技能管理：互斥关系反向成立 */
document.getElementById('navSkill').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('V10 切回技能管理后两视图互斥正确',
      viewGaps.classList.contains('hide') && !viewSkills.classList.contains('hide')
      && document.getElementById('topTitle').textContent === '技能管理');

/* V11-V14 缺失记录的「删除」按钮 */
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
navGaps.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(60);   // 等 loadGaps 渲染完
const gapItemEl = document.querySelector('#gapList .sk-item');
check('V11 缺失记录渲染，删除按钮在 .sk-actions 里（靠右对齐结构）',
      !!gapItemEl && !!gapItemEl.querySelector('.sk-head .sk-actions .mini-btn'),
      gapItemEl ? gapItemEl.className : 'gapList 为空');
check('V12 删除按钮文案为「删除」且位于行头（不跟在描述文案后面）',
      gapItemEl.querySelector('.sk-head .sk-actions .mini-btn').textContent === '删除'
      && gapItemEl.querySelector('.sk-desc') && !gapItemEl.querySelector('.sk-desc .mini-btn'), '');
gapItemEl.querySelector('.sk-head .sk-actions .mini-btn')
  .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(50);   // 等确认弹窗
const okBtn = document.querySelector('.modal-foot .btn.danger');   // danger 弹窗的确定键
check('V13 点删除先弹确认框（含标题与「删除」确认键）',
      !!okBtn && okBtn.textContent === '删除'
      && document.querySelector('.modal-head h3').textContent.includes('send_sms'),
      document.querySelector('.modal-foot')?.textContent || '无弹窗');
okBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(60);   // 等请求与重渲染
const delCall = calls.find(c => c.method === 'POST' && c.url === '/api/gaps'
      && c.body.includes('"key":"send_sms"'));
check('V14 确认后发出 POST /api/gaps（带 kind/key），清单刷新为空',
      !!delCall
      && document.getElementById('gapCount').textContent === '0 条'
      && document.getElementById('gapList').textContent.includes('还没有记录'),
      JSON.stringify(calls.filter(c => c.url === '/api/gaps')));

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
