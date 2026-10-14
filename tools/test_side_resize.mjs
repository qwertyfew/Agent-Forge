/**
 * 侧栏拖拽调宽的回归测试（web/index.html 的 #sideResize 逻辑）。
 *
 * 左侧栏（装「新建任务」的大框）与右侧主区（装对话/发送框的大框）由同一条
 * 分界线分开：拖把手改侧栏宽度，主区 flex:1 自动跟随。锁四个坑：
 * 1. 把手存在且挂在 aside 内部（position:absolute 相对侧栏定位）；
 * 2. 上次的宽度从 localStorage 恢复；
 * 3. 拖动管线生效（pointerdown → pointermove → style.width 更新）；
 * 4. 范围钳制：低于 200 拉到 200，高于 420 封顶 420，松手写入 localStorage。
 *
 * 注：jsdom 没有真实布局（getBoundingClientRect 恒 0），起点宽度按 0 算，
 * 正好用来验证钳制逻辑的两端。
 *
 * 运行：node tools/test_side_resize.mjs
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

/* 预置上次的宽度，验证恢复逻辑 */
window.localStorage.setItem('af_side_w', '350');

const emptyJson = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
window.fetch = emptyJson;
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

const aside = document.getElementById('sidebar');
const handle = document.getElementById('sideResize');
const fire = (type, x) =>
  handle.dispatchEvent(new window.MouseEvent(type, { bubbles: true, clientX: x }));

/* T1 把手存在且在侧栏内部 */
check('T1 把手 #sideResize 存在且挂在 aside 内部（相对侧栏定位）',
      !!handle && !!handle.closest('aside') && handle.closest('aside') === aside, '');

/* T2 上次的宽度恢复 */
check('T2 localStorage 里的上次宽度（350px）被恢复',
      aside.style.width === '350px', aside.style.width);

/* T3 拖动管线：down → move 更新 style.width（jsdom 起点 0，钳到 MIN=200） */
fire('pointerdown', 100);
fire('pointermove', 160);   // 0 + 60 → 低于下限，钳到 200
check('T3 按住拖动更新宽度（起点 0 + 60px，钳到下限 200px）',
      aside.style.width === '200px', aside.style.width);

/* T4 上限钳制 + 松手落盘 */
fire('pointermove', 1200);  // 远超上限，钳到 420
check('T4a 宽度钳到上限 420px（不会把主区挤没）',
      aside.style.width === '420px', aside.style.width);
fire('pointerup', 1200);
check('T4b 松手后宽度写入 localStorage（420）',
      window.localStorage.getItem('af_side_w') === '420',
      window.localStorage.getItem('af_side_w'));

/* T5 松手后再 move 不再跟随 */
fire('pointermove', 10);
check('T5 松手后拖动停止（宽度保持 420px）', aside.style.width === '420px', aside.style.width);

/* T6-T8 侧栏图标竖直对齐：底部三行的图标都包在 15px 居中的 .ic 里，
   与上方导航列（图标左缘 22px、15px 宽 → 中线 29.5px）同一几何 */
const footSel = (sel) => aside.querySelector(`.side-foot ${sel}`);
check('T6 「设置」图标包在 .ic 居中盒里',
      !!footSel('#btnSettings > .ic > svg'), '');
check('T7 「本地」状态点包在 .ic 居中盒里（且不再被内联 padding 压过）',
      !!footSel('#ollamaMeta > .ic > .dot')
      && !(footSel('#ollamaMeta').getAttribute('style') || '').includes('padding:0'), '');
check('T8 工作区文件夹图标包在 .ic 居中盒里',
      !!footSel('#wsMeta > .ic > svg'), '');

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
