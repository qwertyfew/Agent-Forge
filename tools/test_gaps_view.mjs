/**
 * 「能力缺口」独立栏目的回归测试（web/index.html）。
 *
 * 锁三个坑：
 * 1. 缺口管理必须从「技能管理」里独立出来：侧栏有 navGaps、有独立的
 *    viewGaps 视图，技能管理视图里不再内嵌 sk-sec 缺口块；
 * 2. showView('gaps') 行为完整：视图显隐互斥、顶栏标题、导航高亮、
 *    输入区隐藏、返回键出现——和技能管理同一待遇，漏一条就是布局 bug；
 * 3. 「全选」按钮与确认区存在（刷新按钮已移除——清理完立即重拉显示）；
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
// 有状态的假后端：删除真实生效（GET 返回当前剩余），测试里可手动补回数据
const gapItems = [
  { kind: 'tool_missing', key: 'send_sms', count: 2, kind_label: '工具缺失',
    samples: ['调用 send_sms 失败', '再次调用'], first: '2026-10-14 10:00', last: '2026-10-14 11:00' },
];
const calls = [];
window.fetch = (url, opts) => {
  calls.push({ url: String(url), method: (opts && opts.method) || 'GET', body: (opts && opts.body) || '' });
  const u = String(url);
  if (u === '/api/gaps') {
    const j = (obj) => Promise.resolve({ ok: true, json: () => Promise.resolve(obj) });
    if ((opts && opts.method) === 'POST') {
      let req = {}; try { req = JSON.parse(opts.body || '{}'); } catch { /* 忽略 */ }
      if (req.action === 'update') {
        const i = gapItems.findIndex(g => g.kind === req.kind && g.key === req.key);
        if (i < 0) return j({ ok: false, error: '记录不存在（可能已被删除），刷新后重试' });
        const nk = String(req.new_key || '').trim() || req.key;
        if (nk !== req.key && gapItems.some(g => g.kind === req.kind && g.key === nk))
          return j({ ok: false, error: '同名记录已存在：' + nk });
        const it = gapItems[i];
        if (Array.isArray(req.samples)) it.samples = req.samples.filter(Boolean);
        it.key = nk;
        return j({ ok: true, items: [...gapItems], total: gapItems.length });
      }
      const i = gapItems.findIndex(g => g.kind === req.kind && g.key === req.key);
      const removed = i >= 0 ? gapItems.splice(i, 1).length : 0;
      return j({ removed, items: [...gapItems], total: gapItems.length });
    }
    return j({ items: [...gapItems], total: gapItems.length });
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
check('V4 viewGaps 含「全选」按钮与确认区（刷新按钮已移除：清理完立即重拉显示）',
      !!viewGaps.querySelector('#btnGapSel')
      && !!viewGaps.querySelector('#selbarGap')
      && !viewGaps.querySelector('#btnGapRefresh'));

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

/* V11-V14 缺失记录的「删除」按钮（行头 .sk-actions 里现在是 编辑+删除 两个） */
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
navGaps.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(60);   // 等 loadGaps 渲染完
const gapItemEl = document.querySelector('#gapList .sk-item');
const gapHeadBtns = gapItemEl ? [...gapItemEl.querySelectorAll('.sk-head .sk-actions .mini-btn')] : [];
const gapDelBtn = gapHeadBtns.find(b => b.textContent === '删除');
check('V11 缺失记录渲染，行头 .sk-actions 里有 编辑+删除 两个按钮（靠右对齐结构）',
      !!gapItemEl && gapHeadBtns.length === 2
      && !!gapHeadBtns.find(b => b.textContent === '编辑'),
      gapItemEl ? gapItemEl.className : 'gapList 为空');
check('V12 删除按钮文案为「删除」且位于行头（不跟在描述文案后面）',
      !!gapDelBtn
      && gapItemEl.querySelector('.sk-desc') && !gapItemEl.querySelector('.sk-desc .mini-btn'), '');
gapDelBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
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

/* V15-V18 行内全选清理（清空按钮的替代品） */
gapItems.push({ kind: 'tool_missing', key: 'send_sms', count: 2, kind_label: '工具缺失',
  samples: ['调用 send_sms 失败'], first: '2026-10-14 10:00', last: '2026-10-14 11:00' });
// 刷新按钮已移除：走栏目入口重新拉数据（navGaps = showView + loadGaps）
document.getElementById('navGaps').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(40);   // 重新拉一份有数据的清单（V14 已把唯一的记录删了）
document.getElementById('btnGapSel').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(40);
let cbs = document.querySelectorAll('#gapList .sel-cb');
check('V15 全选后条目出现勾选框且全选、出现「取消」与「确认清理(1 项)」',
      cbs.length === 1 && cbs[0].checked
      && document.getElementById('selbarGap').textContent.includes('确认清理(1 项)')
      && document.getElementById('selbarGap').textContent.includes('取消')
      && document.getElementById('btnGapSel').classList.contains('hide'),
      document.getElementById('selbarGap').textContent);
cbs[0].click();
await sleep(20);
check('V16 单独取消勾选后确认按钮变 0 项',
      document.getElementById('selbarGap').textContent.includes('确认清理(0 项)'),
      document.getElementById('selbarGap').textContent);
document.querySelector('#selbarGap .btn.ghost').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
check('V17 取消后退回本栏：勾选框消失、全选按钮回来',
      document.querySelectorAll('#gapList .sel-cb').length === 0
      && !document.getElementById('btnGapSel').classList.contains('hide')
      && document.getElementById('selbarGap').classList.contains('hide'), '');
document.getElementById('btnGapSel').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(40);
const selPostsBefore = calls.filter(c => c.method === 'POST' && c.url === '/api/gaps').length;
document.querySelector('#selbarGap .btn.danger').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(60);
check('V18 确认清理逐条发删除请求、勾选框消失、退回普通列表',
      calls.filter(c => c.method === 'POST' && c.url === '/api/gaps').length === selPostsBefore + 1
      && document.querySelectorAll('#gapList .sel-cb').length === 0
      && document.getElementById('gapCount').textContent === '0 条',
      JSON.stringify(calls.filter(c => c.method === 'POST' && c.url === '/api/gaps')));

/* V19-V22 行内编辑（参考评测台编辑器：取消/保存，保存后弹提示） */
gapItems.push({ kind: 'tool_missing', key: 'send_sms', count: 2, kind_label: '工具缺失',
  samples: ['调用 send_sms 失败'], first: '2026-10-14 10:00', last: '2026-10-14 11:00' });
document.getElementById('navGaps').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(40);
const item2 = document.querySelector('#gapList .sk-item');
const edtBtn = [...item2.querySelectorAll('.sk-head .sk-actions .mini-btn')]
  .find(b => b.textContent === '编辑');
edtBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
/* 点击后 renderGaps 会重建整个清单，必须从 document 取新渲染的编辑器 */
let editor = document.querySelector('#gapList .eval-edit');
check('V19 点编辑展开行内编辑器：名称输入框与样例文本框预填当前值',
      !!editor
      && editor.querySelector('input').value === 'send_sms'
      && editor.querySelector('textarea').value === '调用 send_sms 失败',
      editor ? editor.textContent : '编辑器未出现');
const cancelBtn = editor.querySelector('.ee-foot .mini-btn');
cancelBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
check('V20 取消后编辑器收起、条目原样保留',
      !document.querySelector('#gapList .eval-edit')
      && document.querySelector('#gapList .sk-item .sk-name').textContent === 'send_sms', '');
[...document.querySelectorAll('#gapList .sk-head .sk-actions .mini-btn')]
  .find(b => b.textContent === '编辑')
  .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
editor = document.querySelector('#gapList .eval-edit');
editor.querySelector('input').value = 'send_sms_v2';
editor.querySelector('textarea').value = '新版调用失败';
const saveBtn = [...editor.querySelectorAll('.ee-foot .mini-btn')].find(b => b.textContent === '保存');
const updCallsBefore = calls.filter(c => c.body.includes('"action":"update"')).length;
saveBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(50);
const updCall = calls.filter(c => c.body.includes('"action":"update"'))[updCallsBefore];
check('V21 保存发出 POST update（带 kind/key/new_key/samples），编辑器收起、清单显示新名称',
      !!updCall && updCall.url === '/api/gaps'
      && updCall.body.includes('"new_key":"send_sms_v2"')
      && updCall.body.includes('"samples":["新版调用失败"]')
      && !document.querySelector('#gapList .eval-edit')
      && document.querySelector('#gapList .sk-item .sk-name').textContent === 'send_sms_v2',
      updCall ? updCall.body : '没有 update 请求');
check('V22 保存后有弹窗提示（含保存结果说明）',
      document.body.textContent.includes('记录已保存'), '');

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
