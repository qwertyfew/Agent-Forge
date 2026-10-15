/**
 * 「记忆管理 / 技能管理」条目行内编辑的回归测试（web/index.html）。
 *
 * 参考评测台的编辑器形态：点「编辑」展开行内编辑器，含「取消」与「保存」，
 * 保存后按栏目弹出对应提示。锁的行为：
 * 1. 记忆条目：编辑器预填正文，保存发 POST /api/memory {action:'update',
 *    index,text}，成功后清单刷新、编辑器收起、弹「已更新这条记忆」提示；
 *    空内容不发请求；全选模式下点编辑被拦下；
 * 2. 技能条目（skills/ 下受管的）：编辑器预填 作用/适用/不适用 三字段，
 *    保存发 POST /api/skills {action:'update',...}，成功后弹
 *    「自动匹配立即按新描述生效」；外部目录技能不出现编辑按钮。
 *
 * 运行：NODE_PATH=<有 jsdom 的 node_modules> node tools/test_edit_view.mjs
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
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* ---- 有状态假后端 ---- */
const memEntries = [
  { index: 3, ts: '2026-10-14 10:00', task: 't1011', text: '用户偏好：回复先给结论' },
  { index: 5, ts: '2026-10-14 11:00', task: 't1022', text: '用户偏好：代码注释用中文' },
];
const memSnapshot = (ok) => ({
  ...(ok ? { ok: true } : {}),
  entries: memEntries.map(e => ({ ...e })),
  count: memEntries.length,
  path: 'X:/ws/.agentforge/memory.md',
});
const skillInstalled = [{
  name: 'pdf-export', description: '把 HTML 导出为 PDF', path: 'X:/skills/pdf-export',
  resources: [], version: '1.2', when_to_use: '用户要生成 PDF 时', when_not_to_use: '',
  source: 'installed', managed: true, when: 100, when_text: 'x', note: '',
}];
const skillExternal = [{
  name: 'ext-skill', description: '外部目录的技能', path: 'D:/elsewhere/ext-skill',
  resources: [], version: '', when_to_use: '', when_not_to_use: '',
  source: 'installed', managed: false, when: 50, when_text: 'x', note: '',
}];
const skillSnapshot = (ok) => ({
  ...(ok ? { ok: true } : {}),
  extracted: [], installed: [...skillInstalled, ...skillExternal],
  extracted_count: 0, installed_count: skillInstalled.length + skillExternal.length,
  dirs: ['X:/skills'], learned_dir: 'X:/skills/learned', managed_root: 'X:/skills',
  auto_load: true, min_score: 2,
});

const calls = [];
window.fetch = (url, opts) => {
  calls.push({ url: String(url), method: (opts && opts.method) || 'GET', body: (opts && opts.body) || '' });
  const u = String(url);
  const j = (obj) => Promise.resolve({ ok: true, json: () => Promise.resolve(obj) });
  if (u === '/api/memory' && (opts && opts.method) === 'POST') {
    let req = {}; try { req = JSON.parse(opts.body || '{}'); } catch { /* 忽略 */ }
    if (req.action === 'update') {
      const e = memEntries.find(x => x.index === req.index);
      if (!e || !String(req.text || '').trim())
        return j({ ok: false, error: '该条记忆不存在（可能已被删除），刷新后重试' });
      e.text = String(req.text).trim();
      return j(memSnapshot(true));
    }
    return j(memSnapshot(true));
  }
  if (u === '/api/memory') return j(memSnapshot(false));
  if (u === '/api/skills' && (opts && opts.method) === 'POST') {
    let req = {}; try { req = JSON.parse(opts.body || '{}'); } catch { /* 忽略 */ }
    if (req.action === 'update') {
      const s = skillInstalled.find(x => x.name === req.name);
      if (!s) return j({ ok: false, error: '未找到该技能' });
      s.description = String(req.description || '').trim();
      s.when_to_use = String(req.when_to_use || '').trim();
      s.when_not_to_use = String(req.when_not_to_use || '').trim();
      return j(skillSnapshot(true));
    }
    return j(skillSnapshot(true));
  }
  if (u === '/api/skills') return j(skillSnapshot(false));
  return emptyJson();
};
window.EventSource = class { constructor() { this.close = () => {}; } addEventListener() {} };
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
window.SpeechSynthesisUtterance = window.SpeechSynthesisUtterance || class {};

try {
  window.eval(appJs);
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}

const click = (elm) => elm.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const findBtn = (root, text) =>
  [...root.querySelectorAll('.mini-btn')].find(b => b.textContent === text);

/* ================= 记忆编辑 ================= */
click(document.getElementById('navMemory'));
await sleep(60);
const memItem = document.querySelector('#memList .mem-item');
check('M1 记忆条目带「编辑」按钮（与删除并排）',
      !!memItem && !!findBtn(memItem, '编辑') && !!findBtn(memItem, '删除'),
      memItem ? memItem.textContent : 'memList 为空');
findBtn(memItem, '编辑').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
let memEditor = document.querySelector('#memList .eval-edit');
check('M2 点编辑展开编辑器，正文预填当前值',
      !!memEditor && memEditor.querySelector('textarea').value === '用户偏好：回复先给结论',
      memEditor ? memEditor.querySelector('textarea').value : '编辑器未出现');
check('M3 编辑器含「取消」与「保存」两个按钮',
      !!memEditor.querySelector('.ee-foot .mini-btn')
      && [...memEditor.querySelectorAll('.ee-foot .mini-btn')].map(b => b.textContent).join(',') === '取消,保存',
      memEditor.querySelector('.ee-foot').textContent);
memEditor.querySelector('textarea').value = '   ';
[...memEditor.querySelectorAll('.ee-foot .mini-btn')].find(b => b.textContent === '保存')
  .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
const memPostBefore = calls.filter(c => c.url === '/api/memory' && c.method === 'POST').length;
check('M4 空内容点保存不发请求（本地拦截）',
      calls.filter(c => c.url === '/api/memory' && c.method === 'POST').length === memPostBefore
      && !!document.querySelector('#memList .eval-edit'), '');
memEditor = document.querySelector('#memList .eval-edit');
memEditor.querySelector('textarea').value = '用户偏好：回复永远先给结论';
[...memEditor.querySelectorAll('.ee-foot .mini-btn')].find(b => b.textContent === '保存')
  .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(50);
const memUpd = calls.find(c => c.url === '/api/memory' && c.method === 'POST'
      && c.body.includes('"action":"update"'));
check('M5 保存发 POST update（带 index/text），编辑器收起、清单显示新内容',
      !!memUpd && memUpd.body.includes('"index":3')
      && memUpd.body.includes('回复永远先给结论')
      && !document.querySelector('#memList .eval-edit')
      && document.querySelector('#memList .mem-item .mtxt').textContent === '用户偏好：回复永远先给结论',
      memUpd ? memUpd.body : '没有 update 请求');
check('M6 保存后有按栏目的提示（记忆生效说明）',
      document.body.textContent.includes('已更新这条记忆'), '');
/* 全选模式下编辑被拦 */
click(document.getElementById('btnMemSel'));
await sleep(30);
const memEditBtn2 = findBtn(document.querySelector('#memList .mem-item'), '编辑');
if (memEditBtn2) {
  memEditBtn2.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(20);
}
check('M7 全选模式下点编辑被拦下（不出编辑器，提示先退出全选）',
      !document.querySelector('#memList .eval-edit')
      && document.body.textContent.includes('先退出全选模式再编辑'), '');
click(document.querySelector('#selbarMem .btn.ghost'));
await sleep(30);

/* ================= 技能编辑 ================= */
click(document.getElementById('navSkill'));
await sleep(60);
const items = [...document.querySelectorAll('#skillInstalledList .sk-item')];
const managedItem = items.find(x => x.querySelector('.sk-name').textContent === 'pdf-export');
const externalItem = items.find(x => x.querySelector('.sk-name').textContent === 'ext-skill');
check('S1 受管技能带「编辑」按钮，外部目录技能没有',
      managedItem && findBtn(managedItem, '编辑') && externalItem && !findBtn(externalItem, '编辑'), '');
findBtn(managedItem, '编辑').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(30);
const skEditor = document.querySelector('#skillInstalledList .eval-edit');
const tas = skEditor ? [...skEditor.querySelectorAll('textarea')] : [];
check('S2 编辑器三个文本框预填 作用/适用/不适用',
      tas.length === 3
      && tas[0].value === '把 HTML 导出为 PDF'
      && tas[1].value === '用户要生成 PDF 时'
      && tas[2].value === '',
      tas.map(t => t.value).join(' | '));
tas[0].value = '把网页与 HTML 文件导出为 PDF';
tas[1].value = '用户要产出 PDF 文件时';
[...skEditor.querySelectorAll('.ee-foot .mini-btn')].find(b => b.textContent === '保存')
  .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await sleep(50);
const skUpd = calls.find(c => c.url === '/api/skills' && c.method === 'POST'
      && c.body.includes('"action":"update"'));
check('S3 保存发 POST update（带 name/description/边界），编辑器收起、新作用上屏',
      !!skUpd && skUpd.body.includes('"name":"pdf-export"')
      && skUpd.body.includes('把网页与 HTML 文件导出为 PDF')
      && !document.querySelector('#skillInstalledList .eval-edit')
      && [...document.querySelectorAll('#skillInstalledList .sk-item')]
        .some(x => x.querySelector('.sk-name').textContent === 'pdf-export'
              && x.textContent.includes('把网页与 HTML 文件导出为 PDF')),
      skUpd ? skUpd.body : '没有 update 请求');
check('S4 保存后有按栏目的提示（自动匹配立即生效）',
      document.body.textContent.includes('自动匹配立即按新描述生效'), '');

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
