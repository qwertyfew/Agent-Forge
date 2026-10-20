# rerun25 评测分析：闸门被「换个写法」绕过

- 运行：16 次 LLM 调用，LLM 263.8s（纯生成 173.6s、prefill 70.7s、首轮载入 12.7s），prompt 峰值 21 357
- 交付物：`evals/external-harness-gap-list-rerun25.md`（4 522 字节，已落盘）
- 判定：**流程健康，输出不合格——9 条引用全是虚构路径，闸门一条都没拦**

## 1. 修复生效的部分

| 项 | 结果 |
| :-- | :-- |
| 位置参数宽容（`tools.list_dir("x")`） | ✅ 本轮 0 次 `TypeError: 只支持关键字参数` |
| 权威块（系统核对压末尾） | ✅ 每次写入后都附「已写入磁盘 / 写入失败」，模型不再自说自话 |
| 交付物落盘 | ✅ |
| 拼写类返工 | ✅ 0 次 `learn_claude_code` |

## 2. 新问题：引用全部是虚构的缩写路径

交付物里对方侧 10 条，引用长这样：

```
lc_loop/code.py:7-13        lc_tools/code.py:15-20
lc_compact/code.py:30-50    lc_todo/code.py:10-30
lc_subagent/code.py:40-60   lc_permission/code.py:10-40
lc_memory/code.py:20-45     lc_skill/code.py:30-70
```

真实路径是 `external/learn-claude-code/s01_agent_loop/code.py` 这类。
`lc_loop/`、`lc_tools/` 这些目录**根本不存在**，是模型自己编的缩写。
另外还有 `agentforge/evals.py`（不存在）与「会会儿序列化」这类错字。

**9 条虚构引用，全部随交付物落盘。**

## 3. 根因：核查入口只认反引号

`agentforge/tools/fs.py` 的 write_file 原来是这样：

```python
if content and "`" in content:          # ← 只有含反引号才进核查
    cit_audit = audit_citations(content, guard, cfg.workspace_root, ledger)
```

前面几轮（rerun22/23/24）模型都用反引号写引用，所以核查一直在跑。
rerun25 把引用改写成**圆括号**：`... 重新进入 LLM 调用 (lc_loop/code.py:7-13)`。
整份文件一个反引号都没有（已实测 `"`" in content == False`），
于是核查入口直接短路 —— **不是审计漏判，是审计没被调用**。

证据：对这份交付物手工跑审计，结果完全正确：

```
checked = 9   issues = 9（全部 level=error）
  - lc_loop/code.py:7-13  引用路径不存在: ...\lc_loop\code.py
  - lc_tools/code.py:15-20 引用路径不存在: ...
  ...
```

`audit_citations` 的提取器本来就同时支持反引号与中英文圆括号
（`_BT_RX` + `_PAREN_RX`），门槛 `checked>=2` 也在审计内部。
外面这层「含反引号」的形状判断纯属多余，反而成了绕过口。

## 4. 修复

```python
# 不再用「含反引号」当门槛：rerun25 把引用改写成圆括号，整份文件零反引号，
# 核查直接被跳过，9 条虚构路径全部落盘。审计内部已按 _extract_citations
# 提取（反引号 + 中英文圆括号），并由 checked>=2 控制「引用密集文档」门槛。
if content:
    cit_audit = audit_citations(content, guard, cfg.workspace_root, ledger)
```

验证（拿 rerun25 真实交付物回放）：

```
含反引号: False | 含圆括号引用: True
写入结果: [错误] 引用核查未通过，文件未写入。
  - lc_loop/code.py:7-13: 引用路径不存在: ...\lc_loop\code.py
  ...
文件是否被写: False
```

新增回归用例 2 项（`tools/test_claims_gate.py`）：样本断言不含反引号 + 圆括号形态被拦且不落盘。
全套 32 通过 / 4 预存失败。

## 5. 这条经验值得记住

同一个能力，模型会不断换写法试探：

| 轮次 | 绕过形态 | 拦住它的地方 |
| :-- | :-- | :-- |
| rerun22 | 反引号但路径错（`agentforge/core/`） | 引用核查 ✅ |
| rerun23 | 下划线仓库名 `learn_claude_code` | 引用核查 ✅（4 次） |
| rerun24 | 证据降级为 README（读了就算） | ❌ 拦不住（形式合规） |
| rerun25 | 圆括号引用 | ❌ 入口短路（已修） |

**闸门的形状假设越具体，越容易被绕开。** 校验条件应放在「语义」层（路径是否真实存在、
是否读过、结论是否与代码矛盾），而不是「文本形态」层（有没有反引号）。
rerun24 的 README 降级问题仍未解决——那需要「证据层级」判定，不能靠字符匹配。
