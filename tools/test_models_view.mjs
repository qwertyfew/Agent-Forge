/**
 * 模型管理栏目（第 42 步）的回归测试：jsdom 冒烟，验证规则接线。
 *
 * 锁的是这几个坑：
 *   规则3  全选 → 待选方案 → 「应用」一次性落盘（enable POST 带全量 ids）
 *   规则5  付费模型进入页面时自动实测一次连接（第 45 步：退出重进才重测）；
 *          测不通的带 ⊘ 标记、勾选框禁用，实测中「测试中」且禁勾，
 *          之前勾上的实测不过自动取消勾选
 *   规则7  勾选的模型条目带 m-on 橙色填充类
 *   规则9  勾选变化弹「已新增模型 / 已减少模型」提示
 *   结构   设置页三个旧 pane 已移除、侧栏新栏目存在
 *
 * 运行：NODE_PATH=<jsdom 所在 node_modules> node tools/test_models_view.mjs
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
const sleep = ms => new Promise(r => setTimeout(r, ms));

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

/* 模型目录：1 个已启用的本地模型 + 1 个付费（将实测失败）+ 1 个付费（实测通过）
   enabled 跟随最近一次 enable POST（服务端真相），否则「应用/取消勾选」之后
   快照还回到初始状态，后续断言会拿到陈旧的勾选状态。 */
let curEnabled = ['local:smart-mini'];
const snapshot = () => ({
  dimensions: [
    {key:'coding',label:'编码'},{key:'reasoning',label:'推理'},{key:'writing',label:'写作'},
    {key:'chinese',label:'中文'},{key:'math',label:'数学'},{key:'context',label:'上下文'},
  ],
  models: [
    { id:'local:smart-mini', key:'smart-mini', name:'qwen3.5:4b', kind:'local',
      provider:'ollama', desc:'高效小模型', enabled:curEnabled.includes('local:smart-mini'), available:true,
      scores:{coding:66,reasoning:72,writing:66,chinese:78,math:74,context:80},
      total:74, spec:{num_ctx:65536} },
    { id:'paid:deepseek-x', key:'deepseek-x', name:'deepseek-v4-pro', kind:'paid',
      provider_label:'DeepSeek', desc:'旗舰', enabled:curEnabled.includes('paid:deepseek-x'), available:true,
      scores:{coding:90,reasoning:88,writing:85,chinese:88,math:90,context:95},
      total:90, spec:{num_ctx:262144} },
    { id:'paid:siliconflow-glm', key:'siliconflow-glm', name:'GLM-5.3', kind:'paid',
      provider_label:'SiliconFlow', desc:'智谱旗舰', enabled:curEnabled.includes('paid:siliconflow-glm'), available:true,
      scores:{coding:88,reasoning:86,writing:84,chinese:90,math:82,context:90},
      total:88, spec:{num_ctx:262144} },
  ],
  enabled: [...curEnabled],
  providers: [{id:'deepseek', label:'DeepSeek', base_url:'https://api.deepseek.com/v1', hint:''}],
  routing: {mode:'off', local_serial:true},
  dispatcher: {},
});

const calls = [];
window.fetch = (url, opts) => {
  const u = String(url);
  if (u === '/api/models' && (!opts || !opts.method || opts.method === 'GET')) {
    return Promise.resolve({ ok:true, json: () => Promise.resolve(snapshot()) });
  }
  if (u === '/api/models' && opts && opts.method === 'POST') {
    const body = JSON.parse(opts.body || '{}');
    calls.push({ url:u, body });
    if (body.action === 'enable') curEnabled = [...body.ids];   // 服务端真相跟随请求
    if (body.action === 'test') {
      // siliconflow-glm 模拟连接失败（HTTP 402），deepseek-x 通过
      const ok = body.id !== 'paid:siliconflow-glm';
      return Promise.resolve({ ok:true, json: () => Promise.resolve(
        ok ? {ok:true, message:'连接成功'} : {ok:false, message:'HTTP 402: 余额不足'}) });
    }
    return Promise.resolve({ ok:true, json: () => Promise.resolve(snapshot()) });
  }
  return Promise.resolve({ ok:true, json: () => Promise.resolve({}) });
};
window.EventSource = class { constructor() { this.close = () => {}; } addEventListener() {} };
window.matchMedia = window.matchMedia || (() => ({ matches:false, addListener(){}, removeListener(){} }));
window.speechSynthesis = window.speechSynthesis || { speak(){}, cancel(){} };
window.SpeechSynthesisUtterance = window.SpeechSynthesisUtterance || class {};

try {
  window.eval(appJs + '\n;window.__af = { renderModelList, selState, paidTest, noteModelCtx, ensurePaidTests, resetCtx: () => { modelCtx = {}; } };');
} catch (e) {
  console.log('FAIL: 页面脚本在 jsdom 里跑不起来 —— ' + (e && e.message));
  process.exit(1);
}

const $ = id => window.document.getElementById(id);
const click = elm => elm.dispatchEvent(new window.MouseEvent('click', { bubbles:true }));

/* ================= 结构断言 ================= */
check('S1 设置页三个旧 pane 已移除',
      !$('paneModels') && !$('paneSkills') && !$('paneTools'));
check('S2 侧栏「模型管理」栏目存在且在记忆之下、技能工具之上',
      !!$('navModels') && !!$('viewModels')
      && $('navModels').compareDocumentPosition($('navSkill')) & window.Node.DOCUMENT_POSITION_FOLLOWING
      && $('navMemory').compareDocumentPosition($('navModels')) & window.Node.DOCUMENT_POSITION_FOLLOWING);
check('S3 技能栏目改名「技能工具」且含工具条目集',
      $('skillNavLabel').textContent === '技能工具'
      && $('skillNavLabel').textContent.includes('技能') && !!$('toolAdminList') && !!$('toolCount'));
check('S4 串行执行开关已移除（硬规则）', !$('chkLocalSerial'));

/* ================= 渲染与规则 5/7 ================= */
click($('navModels'));
await sleep(80);

const rows = [...window.document.querySelectorAll('#modelItemList .it-row')];
check('R1 三个模型条目都渲染', rows.length === 3, `got ${rows.length}`);
check('R2 已启用模型带 m-on 橙色填充类（规则7）',
      rows[0].classList.contains('m-on') && !rows[1].classList.contains('m-on'));
await sleep(60);
const glmRow = rows[2];
check('R3 付费模型实测失败带 ⊘ 无法勾选标记（规则5）',
      glmRow.textContent.includes('无法勾选'));
const glmCb = glmRow.querySelector('.sel-cb');
check('R4 实测失败的模型勾选框被禁用（规则5）', !!glmCb && glmCb.disabled);
const dsCb = rows[1].querySelector('.sel-cb');
check('R5 实测通过的付费模型勾选框可用', !!dsCb && !dsCb.disabled);
check('R6 测试连接按钮已移除（规则5）', !rows[0].textContent.includes('测试连接'));

/* ================= 规则9：勾选变化弹提示 ================= */
dsCb.checked = true;
dsCb.dispatchEvent(new window.Event('change', { bubbles:true }));
await sleep(60);
const addCall = calls.filter(c => c.body.action === 'enable').pop();
check('R7 勾选付费模型立即发出 enable POST（含全量 ids）',
      !!addCall && addCall.body.ids.includes('paid:deepseek-x')
      && addCall.body.ids.includes('local:smart-mini'),
      JSON.stringify(addCall && addCall.body.ids));
check('R8 弹出「已新增模型」提示', window.document.body.textContent.includes('已新增模型'));

/* ================= 规则3：全选 → 应用 ================= */
const before = calls.filter(c => c.body.action === 'enable').length;
click($('btnModelsSel'));
await sleep(40);
check('A1 全选后按钮变身「退出」且「应用」出现',
      $('btnModelsSel').textContent === '退出' && !$('btnModelApply').classList.contains('hide'));
check('A2 全选勾上全部（含测不通的被排除在外）',
      window.__af.selState.models.checked.has('paid:deepseek-x')
      && !window.__af.selState.models.checked.has('paid:siliconflow-glm'));
click($('btnModelApply'));
await sleep(60);
const applyCall = calls.filter(c => c.body.action === 'enable').pop();
check('A3 「应用」一次性落盘勾选方案',
      calls.filter(c => c.body.action === 'enable').length === before + 1
      && applyCall.body.ids.includes('paid:deepseek-x')
      && !applyCall.body.ids.includes('paid:siliconflow-glm'),
      JSON.stringify(applyCall && applyCall.body.ids));
check('A4 应用后弹出绿色提示并退出全选模式',
      window.document.body.textContent.includes('已应用勾选方案')
      && $('btnModelsSel').textContent === '全选');

/* ================= 规则12：每模型上下文占用圈 ================= */
window.__af.resetCtx();
window.__af.noteModelCtx({model_key:'smart-mini', model:'qwen3.5:4b', prompt_tokens:12000});
window.__af.noteModelCtx({model_key:'deepseek-x', model:'deepseek-v4-pro', prompt_tokens:90000});
const rings = [...window.document.querySelectorAll('#ctxRings .ctx-ring-wrap')];
check('C1 两个 LLM 两个占用圈（规则12）', rings.length === 2, `got ${rings.length}`);
check('C2 圈本体不显示百分比文字', !$('ctxLine').textContent.includes('%'));
check('C3 悬停说明是哪个模型的圈（data-tip，无原生 title）',
      rings.length === 2 && rings[0].dataset.tip.includes('qwen3.5:4b')
      && rings[1].dataset.tip.includes('deepseek-v4-pro')
      && rings.every(r => !r.hasAttribute('title')),
      rings.map(r=>r.dataset.tip).join(' | '));
check('C4 各按自己的窗口算占用',
      rings[0].dataset.tip.includes('18.3%') && rings[1].dataset.tip.includes('34.3%'),
      rings.map(r=>r.dataset.tip).join(' | '));

/* ================= 第 45 步：付费实测时机收敛 ================= */
// 现状：进入页后两个付费模型都已实测完（deepseek-x 通过且被 R7 勾上，glm 失败）。
const testsAtEntry = calls.filter(c => c.body.action === 'test').length;
window.__af.ensurePaidTests();   // 同一轮进入内重复调用不应再发测试
check('T1 每轮进入只测一次（重复调用不发测试）',
      calls.filter(c => c.body.action === 'test').length === testsAtEntry);
check('T2 通过后「测试中」标签整体消失、失败仍带 ⊘',
      !rows[1].textContent.includes('测试中') && !rows[2].textContent.includes('测试中')
      && rows[2].textContent.includes('无法勾选'));

// 退出重进：旧结果作废、重新实测；这次把 deepseek-x 的测试挂起，捕捉「测试中」状态
click($('btnBack'));             // 回到对话 = 退出模型管理
await sleep(20);
const testsOnChat = calls.filter(c => c.body.action === 'test').length;
window.__af.ensurePaidTests();   // 不在模型管理页里不应发测试
check('T3 只在模型管理页内实测（对话页不发测试）',
      calls.filter(c => c.body.action === 'test').length === testsOnChat);

const prevFetch = window.fetch;
let releaseDsTest = null;
window.fetch = (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  if (String(url) === '/api/models' && body.action === 'test' && body.id === 'paid:deepseek-x') {
    calls.push({ url:String(url), body });
    return new Promise(res => { releaseDsTest = () => res(
      { ok:true, json: () => Promise.resolve({ok:false, message:'HTTP 401: 密钥失效'}) }); });
  }
  return prevFetch(url, opts);
};
click($('navModels'));           // 重进：epoch++，旧结果作废，两个付费模型都重测
await sleep(30);
const rows2 = [...window.document.querySelectorAll('#modelItemList .it-row')];
const dsRow2 = rows2[1], localRow2 = rows2[0];
const dsCb2 = dsRow2.querySelector('.sel-cb'), localCb2 = localRow2.querySelector('.sel-cb');
check('T4 实测中显示「测试中」徽标且勾选框禁用',
      dsRow2.querySelector('.badge.testing') && dsRow2.textContent.includes('测试中')
      && !!dsCb2 && dsCb2.disabled);
check('T5 实测不影响本地模型的勾选（本地勾选框可用）',
      !localRow2.textContent.includes('测试中') && !!localCb2 && !localCb2.disabled
      && localRow2.classList.contains('m-on'));
check('T6 退出重进会重新实测（重进即重发测试请求）',
      releaseDsTest !== null
      && calls.filter(c => c.body.action === 'test').length === testsOnChat + 2,
      `test calls ${calls.filter(c => c.body.action === 'test').length} vs ${testsOnChat}`);

releaseDsTest();                 // 深测返回失败：之前勾上的应被自动取消勾选
await sleep(40);
const rows3 = [...window.document.querySelectorAll('#modelItemList .it-row')];
const uncheckCall = calls.filter(c => c.body.action === 'enable').pop();
check('T7 实测不过自动取消勾选（enable POST 不含该模型）',
      !!uncheckCall && !uncheckCall.body.ids.includes('paid:deepseek-x')
      && uncheckCall.body.ids.includes('local:smart-mini'),
      JSON.stringify(uncheckCall && uncheckCall.body.ids));
check('T8 取消勾选后勾选框恢复未勾的正常表现（⊘、禁用、无橙色填充）',
      rows3[1].textContent.includes('无法勾选')
      && !rows3[1].classList.contains('m-on')
      && !rows3[1].textContent.includes('测试中')
      && !!rows3[1].querySelector('.sel-cb').disabled);
check('T9 弹出自动取消勾选的橙色提示',
      window.document.body.textContent.includes('已自动取消勾选'));

console.log('');
if (failed) { console.log(`FAILED: ${failed} 项断言未通过`); process.exit(1); }
console.log('ALL PASS');
process.exit(0);   // 页面脚本里的 toast 定时器会吊住事件循环，显式收尾
