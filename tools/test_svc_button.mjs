/**
 * 侧栏服务按钮（重启本地服务 / 云端常绿）的回归测试（web/index.html）。
 *
 * 锁四个坑：
 * 1. 本地部署（health.local=true）：Ollama 正常 → 绿「本地服务运行中」；
 * 2. 点击 → 确认弹窗 → 发出 POST /api/service/restart → 红褐色「正在重启」
 *    → 轮询恢复后变绿（含「重启完成」提示）；
 * 3. Ollama 挂了（auto_start 关闭）→ 红「Ollama 未连接」；
 * 4. 云端（local=false）→ 一律绿、不可点，且工作区路径整行隐藏。
 *
 * 运行：node tools/test_svc_button.mjs
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
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* 建一个页面实例：healthPayload 可变（每实例独立），fetch 按 URL 分发 */
function makePage(healthPayload) {
  const dom = new JSDOM(html.replace(/<script[\s\S]*?<\/script>/g, ''), {
    runScripts: 'outside-only', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8787/',
  });
  const { window } = dom;
  const { document } = window;
  const calls = [];
  const emptyJson = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  window.fetch = (url, opts) => {
    const u = String(url);
    calls.push({ url: u, opts: opts || {} });
    if (u.startsWith('/api/health')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...healthPayload }) });
    }
    if (u.startsWith('/api/service/restart')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, note: '已发出' }) });
    }
    return emptyJson();
  };
  window.EventSource = class { constructor() { this.close = () => {}; } addEventListener() {} };
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
  window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
  window.SpeechSynthesisUtterance = window.speechSynthesis || class {};
  window.eval(appJs);
  return { window, document, calls };
}

/* ---- 实例 A：本地 + Ollama 正常 ---- */
{
  const { window, document, calls } = makePage({ ollama: true, local: true });
  await sleep(120);
  const dot = document.getElementById('ollamaDot');
  check('S1 本地 + Ollama 正常 → 绿「本地服务运行中」',
        dot.classList.contains('ok')
        && document.getElementById('ollamaTxt').textContent === '本地服务运行中',
        document.getElementById('ollamaTxt').textContent);

  /* S2 点击 → 确认 → POST restart → 红褐色 → 轮询恢复变绿 */
  document.getElementById('ollamaMeta').dispatchEvent(
    new window.MouseEvent('click', { bubbles: true }));
  await sleep(50);
  const okBtn = document.querySelector('.modal-foot .btn.primary');
  check('S2a 点击弹出重启确认框', !!okBtn, '');
  okBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(100);
  check('S2b 确认后发出 POST /api/service/restart',
        calls.some(c => c.url === '/api/service/restart' && c.opts.method === 'POST'), '');
  check('S2c 重启期间红褐色「正在重启服务…」',
        dot.classList.contains('busy')
        && document.getElementById('ollamaTxt').textContent === '正在重启服务…',
        document.getElementById('ollamaTxt').textContent);
  await sleep(3000);   // 轮询周期 2s：health 恢复 ollama:true → 变绿
  check('S2d 轮询到恢复 → 变绿「本地服务运行中」',
        dot.classList.contains('ok')
        && document.getElementById('ollamaTxt').textContent === '本地服务运行中',
        document.getElementById('ollamaTxt').textContent);
  check('S2e 工作区行仍在（本地显示）',
        !document.getElementById('wsMeta').classList.contains('hide'), '');
}

/* ---- 实例 B：本地 + Ollama 挂了（auto_start 关闭）---- */
{
  const { document } = makePage({ ollama: false, ollama_auto_start: false, local: true });
  await sleep(120);
  check('S3 Ollama 未连接 → 红',
        document.getElementById('ollamaDot').classList.contains('bad')
        && document.getElementById('ollamaTxt').textContent === 'Ollama 未连接',
        document.getElementById('ollamaTxt').textContent);
}

/* ---- 实例 C：云端（local=false）→ 常绿不可点 + 工作区行隐藏 ---- */
{
  const { window, document, calls } = makePage({ ollama: false, local: false });
  await sleep(120);
  const dot = document.getElementById('ollamaDot');
  check('S4 云端 → 绿「云端部署 · 服务运行中」',
        dot.classList.contains('ok')
        && document.getElementById('ollamaTxt').textContent === '云端部署 · 服务运行中',
        document.getElementById('ollamaTxt').textContent);
  check('S5 云端工作区路径整行隐藏',
        document.getElementById('wsMeta').classList.contains('hide'), '');
  document.getElementById('ollamaMeta').dispatchEvent(
    new window.MouseEvent('click', { bubbles: true }));
  await sleep(80);
  check('S6 云端点击不弹确认框、不发重启请求',
        !document.querySelector('.modal-foot')
        && !calls.some(c => c.url === '/api/service/restart'), '');
}

/* ---- 实例 D：本地 + 服务无应答（fetch 失败）→ 红「服务未响应」---- */
{
  const dom = new JSDOM(html.replace(/<script[\s\S]*?<\/script>/g, ''), {
    runScripts: 'outside-only', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8787/',
  });
  const { window } = dom;
  const { document } = window;
  window.fetch = () => Promise.reject(new Error('connection refused'));
  window.EventSource = class { constructor() { this.close = () => {}; } addEventListener() {} };
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
  window.speechSynthesis = window.speechSynthesis || { speak() {}, cancel() {} };
  window.SpeechSynthesisUtterance = window.speechSynthesis || class {};
  window.eval(appJs);
  await sleep(120);
  check('S7 服务无应答 → 红「服务未响应」',
        document.getElementById('ollamaDot').classList.contains('bad')
        && document.getElementById('ollamaTxt').textContent === '服务未响应',
        document.getElementById('ollamaTxt').textContent);
}

console.log(failed === 0 ? '\n全部断言通过。' : `\n失败断言 ${failed} 条。`);
process.exit(failed === 0 ? 0 : 1);
