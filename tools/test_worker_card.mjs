/* 前端重构冒烟测试：用 jsdom 载入 web/index.html，模拟 leader 模式完整事件序列，
   断言：①员工过程收进折叠进度卡；②员工收尾话术不出现在主对话流；
   ③complete 后 leader 最终答复作为唯一正文渲染；④计划卡步骤打勾。 */
import { createRequire } from 'module';
import { dirname } from 'path';
import { fileURLToPath } from 'url';
const HERE = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const fs = require('fs');
const path = require('path');
const { JSDOM } = require(path.join('C:/Users/admin/.workbuddy/binaries/node/workspace/node_modules', 'jsdom'));

const html = fs.readFileSync(path.join(HERE, '..', 'web', 'index.html'), 'utf8');
const dom = new JSDOM(html, {
  url: 'http://127.0.0.1:8787/',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  beforeParse(window){
    // jsdom 没有的浏览器 API，在页面脚本执行前补齐
    window.matchMedia = q => ({ matches: false, media: q, addListener(){}, removeListener(){},
      addEventListener(){}, removeEventListener(){}, onchange: null, dispatchEvent(){ return false; } });
    window.scrollTo = () => {};
    window.fetch = () => Promise.resolve({ json: () => Promise.resolve({}), ok: true });
    window.HTMLElement.prototype.scrollIntoView = function(){};
    window.HTMLElement.prototype.scrollTo = function(){};
    if(!window.requestAnimationFrame) window.requestAnimationFrame = cb => setTimeout(cb, 16);
  },
});
const { window } = dom;
const { document } = window;

const errors = [];
window.addEventListener('error', e => errors.push(e.message));

function fire(ev, data){
  try { window.handleTaskEvent(ev, data); }
  catch(e){ errors.push(`${ev}: ${e.message}`); }
}
function q(sel){ return document.querySelector(sel); }
function qa(sel){ return [...document.querySelectorAll(sel)]; }
let pass = 0, fail = 0;
function ok(cond, msg){
  if(cond){ pass++; console.log('  ok  ' + msg); }
  else { fail++; console.log('  FAIL ' + msg); }
}

setTimeout(() => {
  console.log('== 模拟 leader 模式事件序列 ==');
  fire('task', { id: 't1', status: 'running', title: '对比两仓库' });
  fire('plan', { subtasks: [
    { index: 1, id: 'lf1', title: '获取 learn-claude-code 目录结构', kind_label: '检索', model_name: 'gemma4:12b' },
  ], summary: 'Leader 第 1 批' });
  fire('assign', { index: 1, total: 1, task_id: 'lf1', model_name: 'gemma4:12b', multi: true });

  // 指派即开卡
  ok(qa('.wcard').length === 1, '指派后立即出现 1 张员工进度卡');
  ok(q('.wcard-title').textContent === '获取 learn-claude-code 目录结构', '卡标题取自计划卡');
  ok(q('.wcard-body').classList.contains('hide'), '卡过程默认收起');

  // 员工过程全部进卡
  fire('thinking', { task_id: 'lf1', text: '决定调用工具 list_dir' });
  fire('tool_start', { task_id: 'lf1', name: 'list_dir', args: { path: 'external/learn-claude-code' } });
  fire('tool_result', { task_id: 'lf1', name: 'list_dir', output: '(...目录清单...)' });
  fire('tool_start', { task_id: 'lf1', name: 'run_command', args: { command: 'ls -R' } });
  fire('tool_result', { task_id: 'lf1', name: 'run_command', output: '...' });
  fire('delta', { task_id: 'lf1', text: '任务已完成。\n详细说明如下…' });
  fire('progress', { task_id: 'lf1', note: '阶段成果（获取目录结构）: evals/structure.txt', files: ['evals/structure.txt'] });

  ok(qa('.wcard .toolrun').length >= 1, '工具调用卡片收进进度卡');
  ok(q('.wcard-body').textContent.includes('任务已完成'), '员工收尾话术在卡片内部');
  ok(q('.wcard-live').textContent.includes('阶段成果'), 'progress 事件上卡：' + q('.wcard-live').textContent);

  // 主对话流不应有员工正文/工具/思考
  const mainFlow = qa('#log > *').filter(n => !n.classList.contains('wcard'));
  const leaked = mainFlow.some(n => n.textContent.includes('任务已完成') || n.classList.contains('toolrun'));
  ok(!leaked, '主对话流无员工正文/工具泄漏');

  // 员工完成 → 卡封口
  fire('task_done', { task_id: 'lf1', index: 1, total: 1, status: 'done', title: '获取目录结构', summary: '已列出结构' });
  ok(q('.wcard').classList.contains('done'), 'task_done 后卡封口（done 态）');
  ok(q('.wcard-live').textContent.includes('已完成'), '卡摘要行写结局：' + q('.wcard-live').textContent);
  ok(q('.wcard-body').classList.contains('hide'), '完成后过程自动收起');
  ok(qa('.pstep.done').length === 1, '计划卡该步打勾');

  // leader 最终答复：history 整段到达，complete 渲染
  fire('history', { messages: [
    { role: 'user', content: '对比 learn-claude-code 与 mini-swe-agent' },
    { role: 'assistant', content: '## 对比结论\n\n两个仓库共提取 6 个对比点，详见交付文件。' },
  ] });
  fire('complete', { steps: 1, multi: true });

  ok(qa('.divider').some(d => d.textContent === '结论'), 'complete 后出现「结论」分隔');
  const finals = qa('#log > .msg-ai');
  ok(finals.length === 1 && finals[0].textContent.includes('对比结论'), '最终答复作为唯一正文渲染');
  ok(!finals[0].textContent.includes('任务已完成'), '最终答复不混员工话术');
  ok(!q('.wcard-body').classList.contains('hide') === false, '进度卡仍保持收起');

  fire('done', {});
  ok(errors.length === 0, '全程无 JS 异常' + (errors.length ? '：' + errors.join(' | ') : ''));

  console.log('\n== 回归：普通聊天（无计划卡）行为不变 ==');
  window.resetLiveRender();   // 与真实发送路径同一复位（streamChat 内调用的就是它）
  fire('task', { id: 't2', status: 'running', title: '纯问答' });
  fire('thinking', { text: '用户在问概念，直接回答' });
  fire('delta', { text: 'Agent Loop 是指…' });
  fire('tool_start', { name: 'list_dir', args: { path: '.' } });
  fire('tool_result', { name: 'list_dir', output: '...' });
  fire('delta', { text: '补充说明…' });
  fire('history', { messages: [
    { role: 'user', content: '什么是 agent loop' },
    { role: 'assistant', content: 'Agent Loop 是指…' },
  ] });
  fire('complete', { steps: 1, multi: true });
  ok(qa('.wcard').length === 1, '无计划不开进度卡（仍是首场景那 1 张）');
  const plainSegs = qa('#log > .msg-ai');
  ok(plainSegs.length === 3
     && plainSegs[1].textContent.includes('Agent Loop 是指')
     && plainSegs[2].textContent.includes('补充说明'), '普通聊天正文照旧直出主对话流（两段独立正文）');
  ok(qa('#log > .divider').filter(d => d.textContent === '结论').length === 1, '无计划不渲染「结论」分隔（不重复渲染正文）');
  ok(errors.length === 0, '回归场景无 JS 异常');

  console.log(`\n结果：${pass} 过 / ${fail} 挂`);
  process.exit(fail ? 1 : 0);
}, 300);
