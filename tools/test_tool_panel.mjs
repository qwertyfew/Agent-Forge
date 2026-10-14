/**
 * 工具面板回看的回归测试（web/index.html 的 renderHistory）。
 *
 * 锁的是这个坑：任务结束后点开回看，工具组头一直显示「调用中」、图标一直转圈。
 * 根因是回看路径只改状态文本与样式，从没把 dataset.run 清成 '0'，而 toolHeadSummary
 * 判定「运行中」正是看这个标志。断言写的是「回看一个已完成的任务，组头不得是运行中」。
 *
 * 运行：node tools/test_tool_panel.mjs
 * 需要 jsdom（任一 node_modules 里能找到即可，找不到就 SKIP 并以 0 退出）。
 * 全部断言内建，退出码 0 = 全过；不需要模型、不需要联网。
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { delimiter } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');
const require = createRequire(import.meta.url);

/* ---------------- jsdom：找不到就跳过（保持可移植，别的机器没装也能跑） --------- */
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

// 页面脚本运行期会用到的东西，jsdom 没有的补上（只为把脚本跑起来）
const emptyJson = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
window.fetch = emptyJson;
window.EventSource = class { constructor() { this.close = () => {}; } addEventListener() {} };
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
window.SpeechSynthesisUtterance = window.SpeechSynthesisUtterance || class {};

// 页面脚本是严格模式（"use strict"），eval 出来的函数声明只活在 eval 作用域里、
// 不会挂到 window 上。所以在同一段 eval 末尾补一行导出，把要测的函数递出来。
const SHIM = '\n;window.__af = { renderHistory, setNeedAIHead: (v) => { needAIHead = v; } };';

try {
  window.eval(appJs + SHIM);
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}

const log = window.document.getElementById('log');
const renderHistory = window.__af && window.__af.renderHistory;
if (!log || typeof renderHistory !== 'function') {
  console.log('FAIL: 没拿到 log 容器或 renderHistory（页面结构变了？）  '
    + `log=${!!log} renderHistory=${typeof renderHistory}`);
  process.exit(1);
}

/* ---------------- 造历史 ---------------- */
/* pairs: [[工具名, 输出 | null], …]，null = 声明了但没执行（必须放最后——
   renderHistory 按 tool 消息顺序与 tool_calls 逐一配对，中间缺一条会整体错位）。 */
function hist(pairs) {
  const calls = pairs.map(([name]) => ({
    function: { name, arguments: '{"q":"x"}' },
  }));
  const h = [
    { role: 'user', content: '查一下这个项目' },
    { role: 'assistant', content: '', tool_calls: calls },
  ];
  for (const [, out] of pairs) if (out !== null) h.push({ role: 'tool', content: out });
  h.push({ role: 'assistant', content: '查完了，结论如下。' });
  return h;
}

function render(list, task) {
  log.innerHTML = '';
  need_head_reset();
  renderHistory(list, task);
  const groups = [...log.querySelectorAll('.toolrun')];
  return {
    groups,
    head: groups[0] ? groups[0].querySelector('.tool-head .tool-state') : null,
    items: groups[0] ? [...groups[0].querySelectorAll('.tool-item')] : [],
    headTime: log.querySelector('.ai-head .ai-time'),
  };
}
/* renderHistory 只在「一轮回复的第一个 AI 元素」前插一次头像行，靠 needAIHead 这个
   模块级标志控制。连续渲染多次时它已经是 false，得让下一次重新插，否则拿不到头像行。 */
function need_head_reset() {
  window.__af.setNeedAIHead(true);
}

const itemState = (it) => it.querySelector('.tool-state');
const itemIcon = (it) => it.querySelector('.tool-ico');

/* ---------------- 用例 1：全部成功 ---------------- */
{
  const r = render(hist([['web_search', '搜索结果摘要'],
                         ['fetch_json', '{"stargazers_count":171067}'],
                         ['read_file', '文件内容']]), { status: 'done', duration: 12 });
  check('工具调用被合并成一组', r.groups.length === 1, `组数=${r.groups.length}`);
  check('组内有 3 条调用明细', r.items.length === 3, `明细数=${r.items.length}`);
  check('回看已完成的回复，组头不是「调用中」',
    r.head && r.head.textContent === '已调用', r.head && r.head.textContent);
  check('组头 dataset.run 已清成 0（运行中判定的唯一依据）',
    r.head && r.head.dataset.run === '0', r.head && r.head.dataset.run);
  check('组头图标不是 spinner',
    !r.groups[0].querySelector('.tool-head .tool-ico').classList.contains('spin'));
  check('每条明细都标「完成」',
    r.items.every((it) => itemState(it).textContent === '完成'),
    r.items.map((it) => itemState(it).textContent).join(','));
  check('明细图标全部停止旋转',
    r.items.every((it) => !itemIcon(it).classList.contains('spin')));
  check('明细里的 dataset.run 也清了',
    r.items.every((it) => itemState(it).dataset.run === '0'));
  check('回看已完成的回复，头像行不是「执行中」',
    r.headTime && !/执行中/.test(r.headTime.textContent), r.headTime && r.headTime.textContent);
}
/* ---------------- 用例 2：一条失败、一条未执行 ---------------- */
{
  const r = render(hist([['web_search', '[错误] 请求失败: 超时'],
                         ['read_file', '文件内容'],
                         ['fetch_json', null]]), { status: 'stopped', duration: 40 });
  check('失败的工具标「失败」',
    r.items[0].querySelector('.tool-state').textContent === '失败',
    r.items[0].querySelector('.tool-state').textContent);
  check('失败的工具带 denied 样式', r.items[0].classList.contains('denied'));
  check('声明了但没执行的工具标「未执行」',
    r.items[2].querySelector('.tool-state').textContent === '未执行',
    r.items[2].querySelector('.tool-state').textContent);
  check('未执行的工具带 denied 样式', r.items[2].classList.contains('denied'));
  check('有异常时组头显示「有异常」而不是「调用中」',
    r.head && r.head.textContent === '有异常', r.head && r.head.textContent);
  check('异常组里没有残留的 spinner',
    [...r.groups[0].querySelectorAll('.tool-ico')].every((ic) => !ic.classList.contains('spin')));
}

/* ---------------- 用例 3：头像行时间按任务台账冻结 ---------------- */
{
  const r = render(hist([['web_search', 'a']]), { status: 'done', duration: 83 });
  check('回看已完成任务：头像行冻结为「完成」并带上台账里的真实耗时',
    r.headTime && r.headTime.textContent === '完成 1m23s', r.headTime && r.headTime.textContent);
  check('冻结后打上 done 标记（不再被实时刷新）',
    r.headTime && r.headTime.dataset.done === '1');

  const r2 = render(hist([['web_search', 'a']]), { status: 'stopped', duration: 5 });
  check('回看被停止的任务：头像行标「已停止」而不冒充完成',
    r2.headTime && /^已停止/.test(r2.headTime.textContent), r2.headTime && r2.headTime.textContent);

  // 兜底：万一将来有别的调用方没传任务，也不能退回「执行中」——
  // 能走到渲染历史这一步，就说明这段回复已经结束了。
  const r3 = render(hist([['web_search', 'a']]));
  check('没传任务信息时也按「已结束」处理，不显示执行中',
    r3.headTime && !/执行中/.test(r3.headTime.textContent), r3.headTime && r3.headTime.textContent);
}

/* ---------------- 用例 4：整页不得残留「运行中」 ---------------- */
{
  render(hist([['web_search', 'a'], ['fetch_json', 'b'], ['read_file', 'c']]),
         { status: 'done', duration: 1 });
  const stillRunning = [...log.querySelectorAll('.tool-state')]
    .filter((s) => s.dataset.run === '1');
  check('回看后不存在仍标记为运行中的工具',
    stillRunning.length === 0, `${stillRunning.length} 个仍在运行`);
  const toolText = [...log.querySelectorAll('.toolrun')].map((g) => g.textContent).join(' ');
  check('工具区里没有「调用中」/「执行中」字样',
    !/调用中|执行中/.test(toolText), toolText.slice(0, 120));
}

/* ---------------- 用例 5：单次调用的组，条目头不得与组头重复 ---------------- */
/* 锁的是这个坑：组里只有一次调用时，展开后组头行与条目头行内容一模一样
   （同名、同参数、同状态），两行重复。约定：单次调用的组打上 single 标记，
   CSS 藏掉条目头，组头状态文案沿用条目的（失败 / 已拒绝等细节不丢）。 */
{
  const r = render(hist([['github_search',
    '[实测 · API] GitHub 官方搜索接口 · 查询「whisper」· 命中 67639 个，取前 5']]),
    { status: 'done', duration: 3 });
  check('单次调用的组打上 single 标记',
    r.groups[0].classList.contains('single'));
  check('single 组的条目头仍在 DOM 里（隐藏靠 CSS，删除结构是另一回事）',
    r.items.length === 1 && r.items[0].querySelector('.tool-item-head'));
  check('藏条目头的 CSS 规则存在（删掉这条规则本测试就该红）',
    /\.toolrun\.single \.tool-item-head\{display:none\}/.test(html));
  check('单次失败调用的组头沿用条目文案「失败」，不笼统写「有异常」',
    (() => {
      const r2 = render(hist([['fetch_json', '[错误] 接口超时']]), { status: 'done', duration: 1 });
      return r2.head && r2.head.textContent === '失败' && r2.head.classList.contains('deny');
    })());
  check('多次调用的组不打 single 标记（条目头照常显示）',
    (() => {
      const r3 = render(hist([['web_search', 'a'], ['read_file', 'b']]), { status: 'done', duration: 1 });
      return r3.groups.length === 1 && !r3.groups[0].classList.contains('single');
    })());
}

console.log('');
// jsdom 起的页面里有轮询定时器（loadTasks 等），不主动收尾的话 node 的事件循环
// 会一直被吊着、进程不退出（表现是被外层超时杀掉，而不是自己结束）。
try { dom.window.close(); } catch { /* 忽略 */ }
if (failed) {
  console.log(`${failed} 项失败。`);
  process.exit(1);
}
console.log('全部通过。');
process.exit(0);
