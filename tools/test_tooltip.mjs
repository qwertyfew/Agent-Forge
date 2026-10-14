/**
 * 统一悬停卡片（tip-card）的回归测试（web/index.html 的 bindTipCard）。
 *
 * 锁五个坑：
 * 1. 悬停卡片必须存在且接管 [title]：悬停时原生 title 暂存到 data-tip、
 *    卡片显示同一文本；离开时 title 还原；
 * 2. 1.5s 延迟：刚悬停不弹（扫过一排按钮不连成一串卡片），到点才弹；
 *    title 必须在悬停瞬间就被接管——否则浏览器自带的 title 提示会先冒出来；
 * 3. 消失规则：脱离触发元素**且**脱离卡片才消失，卡片里的文字可选中；
 * 4. 定位贴着当前鼠标（而不是元素外框）：悬停区大的元素不再把卡片甩远；
 * 5. 「完全访问权限」的说明段落已删 + 卡片样式齐全（圆角/单行优先/
 *    可见开关/pointer-events 跟随 show）。
 *
 * 运行：node tools/test_tooltip.mjs
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

const calls = [];
const emptyJson = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
window.fetch = (url, opts) => {
  calls.push({ url: String(url), opts: opts || {} });
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
const DELAY = 1500;

/* T1 卡片节点存在且样式已定义 */
const card = document.querySelector('body > .tip-card');
check('T1 悬停卡片节点已挂到 body', !!card);
check('T2 卡片样式已定义（圆角 + 单行优先的 max-width + 可见开关 + 显示时可悬停）',
      /\.tip-card\{[^}]*border-radius:11px/.test(html)
      && /\.tip-card\{[^}]*max-width:min\(280px/.test(html)
      && /\.tip-card\.show\{opacity:1;pointer-events:auto\}/.test(html));
/* 隐藏态必须 pointer-events:none：透明卡片绝不能挡住底下元素的点击 */
check('T2b 隐藏态卡片不拦截鼠标（pointer-events:none 在基础规则里）',
      /\.tip-card\{[^}]*pointer-events:none/.test(html));

/* T3 「模型配置」按钮不得再挂 title：它的悬停说明由专属 modelPop 承担 */
const modelBtn = document.getElementById('btnModelConfig');
check('T3 模型配置按钮已无 title（不再与 modelPop 叠出两张卡）',
      !modelBtn.hasAttribute('title') && modelBtn.dataset.tip === undefined,
      String(modelBtn.getAttribute('title')));

/* T4–T6 悬停接管 + 1.5s 延迟（用「记忆管理」按钮验证） */
const navBtn = document.getElementById('navMemory');
const titleBefore = navBtn.getAttribute('title');
check('T4 记忆管理按钮有 title', !!titleBefore, String(titleBefore));
navBtn.dispatchEvent(new window.MouseEvent('mouseover',
  { bubbles: true, clientX: 100, clientY: 200 }));
/* T5a 刚悬停不弹：1.5s 延迟在位 */
check('T5a 悬停瞬间卡片不弹（1.5s 延迟生效）', !card.classList.contains('show'));
/* title 在悬停瞬间就被接管：否则浏览器自带的 title 会先冒出来 */
check('T5b 悬停瞬间原生 title 已暂存 data-tip（双提示预防）',
      navBtn.dataset.tip === titleBefore && !navBtn.hasAttribute('title'));
await sleep(DELAY + 200);
check('T6 悬停 1.5s 后卡片弹出、显示同一文本',
      card.classList.contains('show') && card.textContent === titleBefore,
      `card="${(card.textContent || '').slice(0, 40)}"`);
/* 定位贴着鼠标：clientX=100/clientY=200 → 卡片左上角应在 (112, 216) 附近 */
check('T6b 卡片贴着鼠标出现（mx+12, my+16）',
      card.style.left === '112px' && card.style.top === '216px',
      `left=${card.style.left} top=${card.style.top}`);

/* T7 新消失规则：离开触发元素但移入卡片 → 不消失；卡片里的文字可选中 */
navBtn.dispatchEvent(new window.MouseEvent('mouseout',
  { bubbles: true, relatedTarget: card }));
check('T7 离开按钮但移入卡片：卡片保持显示且 title 未还原',
      card.classList.contains('show') && !navBtn.hasAttribute('title'));
/* 在卡片内部移动 / 选字：同样不消失 */
card.dispatchEvent(new window.MouseEvent('mouseover',
  { bubbles: true, relatedTarget: card }));
check('T7b 在卡片内部悬停：卡片仍显示', card.classList.contains('show'));

/* T8 双向脱离（卡片 → 空白处）：title 还原，卡片隐藏 */
card.dispatchEvent(new window.MouseEvent('mouseout',
  { bubbles: true, relatedTarget: document.body }));
await sleep(150);
check('T8 离开卡片后 title 还原、卡片隐藏',
      navBtn.getAttribute('title') === titleBefore
      && navBtn.dataset.tip === undefined
      && !card.classList.contains('show'));

/* T9 「完全访问权限」：悬停只出卡片，说明段落不存在（对话框不再被顶起） */
const permBtn = document.getElementById('permBtn');
check('T9 composer 里已无说明段落（.composer-wrap .hint 已删）',
      !document.querySelector('.composer-wrap .hint')
      && !/composer-wrap\.show-hint/.test(html));
const permTitle = permBtn.getAttribute('title');
permBtn.dispatchEvent(new window.MouseEvent('mouseover',
  { bubbles: true, clientX: 50, clientY: 300 }));
await sleep(DELAY + 200);
check('T10 完全访问权限悬停出卡片', card.classList.contains('show')
      && card.textContent === permTitle);
permBtn.dispatchEvent(new window.MouseEvent('mouseout',
  { bubbles: true, relatedTarget: document.body }));
await sleep(150);

/* T11 长文本按钮（多路径尝试的动态 title）同样接管 */
const pathBtn = document.getElementById('pathBtn');
const pathTitle = pathBtn.getAttribute('title');
pathBtn.dispatchEvent(new window.MouseEvent('mouseover',
  { bubbles: true, clientX: 60, clientY: 320 }));
await sleep(DELAY + 200);
check('T11 多路径尝试悬停出卡片（动态 title 同样接管）',
      card.classList.contains('show') && card.textContent === pathTitle,
      `card="${(card.textContent || '').slice(0, 40)}"`);
pathBtn.dispatchEvent(new window.MouseEvent('mouseout',
  { bubbles: true, relatedTarget: document.body }));
await sleep(150);

/* T12 悬停不足 1.5s 就离开：不弹，title 还原（扫过一排按钮不弹一串） */
navBtn.dispatchEvent(new window.MouseEvent('mouseover',
  { bubbles: true, clientX: 100, clientY: 200 }));
await sleep(200);
navBtn.dispatchEvent(new window.MouseEvent('mouseout',
  { bubbles: true, relatedTarget: document.body }));
await sleep(DELAY + 300);
check('T12 快扫而过：卡片不弹、title 已还原',
      !card.classList.contains('show')
      && navBtn.getAttribute('title') === titleBefore, '');

/* T13 通用设置里的延迟选择器存在，默认 1.5s */
const genSel = document.getElementById('genTipDelay');
check('T13 通用面板有「悬停提示延迟」选择器（默认 1.5 秒）',
      !!genSel && genSel.value === '1500' && genSel.options.length >= 6,
      genSel ? String(genSel.value) : 'null');

/* T14 改成 0.5s：发出 POST /api/prefs 且延迟立即生效（700ms 内弹出） */
genSel.value = '500';
genSel.dispatchEvent(new window.Event('change', { bubbles: true }));
await sleep(50);
check('T14a 改延迟发出 POST /api/prefs（tip_delay=500）',
      calls.some(c => c.url === '/api/prefs' && c.opts.method === 'POST'
                  && String(c.opts.body).includes('"tip_delay":500')), '');
navBtn.dispatchEvent(new window.MouseEvent('mouseover',
  { bubbles: true, clientX: 100, clientY: 200 }));
await sleep(700);
check('T14b 新延迟立即生效（0.5s 后卡片已弹出）',
      card.classList.contains('show')
      && card.textContent === titleBefore
      && card.style.left === '112px', `left=${card.style.left}`);
navBtn.dispatchEvent(new window.MouseEvent('mouseout',
  { bubbles: true, relatedTarget: document.body }));
await sleep(150);

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
