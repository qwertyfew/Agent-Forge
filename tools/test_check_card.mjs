/**
 * 程序侧核查卡的渲染测试（web/index.html 的 splitCheckTrailer / mountCheckCard）。
 *
 * 事故（2026-10-21 用户原话）：「有答案文案，然后又会有文案，说上面这些信息没有
 * 核实，推荐我核实。」—— 闸门的声明被程序拼在正文末尾，于是读者先读完结论、
 * 再被告知「别信」，而且看起来像「答案之后又冒出第二段答案」。
 *
 * 修法：句尾的声明段剥出来渲染成独立卡片，正文前面加一句状态。
 * 这个测试钉的是 DOM 层的三条不变量：
 *   ① 声明不再留在正文里（正文只有模型说的话）
 *   ② 卡片、顶部状态条都真的画出来了
 *   ③ 幂等 + 反例（正文中间的分隔线不许吞）
 *
 * 运行：node tools/test_check_card.mjs
 * 需要 jsdom（任一 node_modules 里能找到即可，找不到就 SKIP 并以 0 退出）。
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

window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
window.EventSource = class { constructor() { this.closed = false; } addEventListener() {} close() {} };
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
window.SpeechSynthesisUtterance = window.SpeechSynthesisUtterance || class {};

const SHIM = `
;window.__af = { mountCheckCard, splitCheckTrailer, checkFails };`;

try {
  window.eval(appJs + SHIM);
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}
const af = window.__af;
if (!af || typeof af.mountCheckCard !== 'function') {
  console.log('FAIL: 没拿到 mountCheckCard（页面结构变了？）');
  process.exit(1);
}

/* 造一段「已经流完的答复」：结构与 addAssistant 返回的 seg 一致 */
function makeSeg(text) {
  const m = doc.createElement('div');
  m.className = 'msg-ai';
  const body = doc.createElement('div');
  body.className = 'body';
  body.innerHTML = window.md ? window.md(text) : text;
  m.appendChild(body);
  doc.getElementById('log').appendChild(m);
  return { m, body, text };
}

const NOTE = '【系统侧核查】本回复描述了当前工作区里的文件 / 目录，但整个任务没有调用过'
  + '任何读取工作区的工具，下列内容未经核实，请勿直接采信：\n- agent-forge.json';
const FAILED_TEXT = '任务完成情况总结：\n- 完成情况：全部要求已满足，交付物已落盘。\n\n---\n' + NOTE;
const PASS_TEXT = '已修复并跑过回归。\n\n---\n'
  + '【系统侧验证】任务收尾时由系统亲自运行了 `pytest -q` → **通过**（退出码 0，耗时 3s）。';
const MID_TEXT = '第一段结论。\n\n---\n第二段：这是正文自己的分隔线，不是声明。';
const PLAIN_TEXT = '就是一段普通答复，没有任何核查。';

// C1 声明离开正文、进卡片，并在正文最前面留一句状态
{
  const seg = makeSeg(FAILED_TEXT);
  af.mountCheckCard(seg);
  const cards = seg.body.querySelectorAll('.check-card');
  const flag = seg.body.querySelector('.check-flag');
  check('C1 生成且只生成一张核查卡', cards.length === 1, `cards=${cards.length}`);
  check('C1 顶部有「未通过」状态条', !!flag, '没画状态条');
  check('C1 正文里不再残留声明',
    !String(seg.body.textContent).includes('【系统侧核查】'), '声明还在正文里');
  check('C1 模型正文完整保留',
    String(seg.body.textContent).includes('全部要求已满足'), String(seg.body.textContent).slice(0, 80));
  check('C1 复制/朗读只拿模型正文（seg.text 不含声明）',
    !seg.text.includes('未经核实') && seg.text.includes('全部要求已满足'), seg.text.slice(-40));
  check('C1 卡片里保留未核实的名字（信息不丢）',
    String(cards[0].textContent).includes('agent-forge.json'), String(cards[0].textContent));
}

// C2 幂等：收尾与回看都会调一次，不能出两张卡
{
  const seg = makeSeg(FAILED_TEXT);
  af.mountCheckCard(seg);
  af.mountCheckCard(seg);
  check('C2 重复调用只有一张卡', seg.body.querySelectorAll('.check-card').length === 1);
  check('C2 重复调用不重复插状态条', seg.body.querySelectorAll('.check-flag').length === 1);
}

// C3 验证「通过」的收尾报告：也成卡，但不加「未通过」状态条
{
  const seg = makeSeg(PASS_TEXT);
  af.mountCheckCard(seg);
  check('C3 通过的验证报告单独成卡', seg.body.querySelectorAll('.check-card').length === 1);
  check('C3 通过时不加告警状态条', !seg.body.querySelector('.check-flag'),
    String(seg.body.textContent).slice(0, 60));
}

// C4 反例：正文中间的分隔线不是声明，不许剥
{
  const seg = makeSeg(MID_TEXT);
  af.mountCheckCard(seg);
  check('C4 正文中间的分隔线不拆卡', !seg.body.querySelector('.check-card'));
  check('C4 两段正文都还在', String(seg.body.textContent).includes('第二段'), seg.text);
}

// C5 普通答复一个字不动
{
  const seg = makeSeg(PLAIN_TEXT);
  af.mountCheckCard(seg);
  check('C5 无声明时不改正文', seg.text === PLAIN_TEXT && !seg.body.querySelector('.check-card'));
}

// C6 回看与实时同一版：同一段文字喂两次（模拟 done 与 renderHistory），产物一致
{
  const a = makeSeg(FAILED_TEXT);
  const b = makeSeg(FAILED_TEXT);
  af.mountCheckCard(a);
  af.mountCheckCard(b);
  check('C6 两条路径渲染结果逐字相同',
    a.body.innerHTML === b.body.innerHTML && a.text === b.text);
}

console.log('');
try { window.close(); } catch { /* ignore */ }
if (failed) {
  console.log(`${failed} 条未过`);
  process.exit(1);
}
console.log('全部通过');
process.exit(0);
