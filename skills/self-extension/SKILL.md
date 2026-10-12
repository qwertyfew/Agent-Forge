---
name: self-extension
description: 自我扩展能力：当现有工具和 skill 无法完成任务时，自行搜索、安装、编写并热加载新的 skill 与 tool。遇到"没有合适的工具/skill/方法"时加载本 skill。
version: 1.0
when_to_use: 现有工具与 skill 都覆盖不到、需要临时扩展新能力时
when_not_to_use: 已有工具就能直接完成的任务
keywords:
  - 没有工具
  - 缺工具
  - 找工具
  - 装个
  - 安装工具
  - 安装skill
  - 新能力
  - 扩展
  - 你会不会
  - 帮我装
  - install
  - 能力不足
---

# Self-Extension（自我扩展）

当任务超出你当前的能力（没有对应工具、没有对应方法论 skill）时，**不要直接说
"我做不到"**，按以下顺序尝试自行解决：

## 决策流程

```
任务需要新能力
  │
  ├─ 1. 先查已有资源：list_skills / 检查工具清单（也许已装只是没想起来）
  │
  ├─ 2. 找现成的：
  │     skill_search(query="关键词")     → GitHub 上搜现成 Agent Skill
  │     web_search(query="... agent skill github") → 补充搜索
  │     找到 → skill_install(source="owner/repo") → 立即可用
  │
  ├─ 3. 找不到现成的 → 自己写：
  │     tool_install(name="xxx", code="...")   把你编写的工具装成插件
  │     （写 skill 则 write_file 到 skills/learned/<name>/SKILL.md）
  │
  └─ 4. 验证：reload_extensions() → list_skills / 调用新工具确认生效
```

## tool_install 代码模板（必须严格遵守）

```python
from agentforge.tools.base import SAFE, WRITE, EXEC, make_tool

def build_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def my_tool(param: str) -> str:
        # guard.resolve() 约束路径在工作区内；对外请求用 requests；命令用 subprocess
        ...
        return "结果字符串（不要返回 JSON 对象，工具一律返回文本）"

    return [
        make_tool(
            "my_tool",                       # 全局唯一，snake_case
            "一句话说明用途和何时使用",        # 会注入系统提示，写给模型看
            {"type": "object",
             "properties": {"param": {"type": "string", "description": "..."}},
             "required": ["param"]},
            my_tool,
            SAFE,                             # safe=只读 / write=写文件 / exec=执行
        ),
    ]
```

安装时会做**导入验证**：`build_tools` 不存在、抛异常、返回值不是 Tool 列表，
都会回滚不安装。装坏的新工具不会影响现有功能（重启即消失）。

## 注意事项

- **自己写的 skill 一律放 `skills/learned/`**，不要直接写进 `skills/`：`skills/` 是用户
  手写/安装的技能目录，混进去之后用户分不清哪些是自己装的、哪些是 agent 自己长出来的，
  也没法在「技能管理 → 已学习」里整批查看和删除。自检机制（autoskill）用的也是这个目录。
- 写 skill 时**不要把本次任务的专有内容写进正文**（具体文件名、路径、这次踩的坑）。
  skill 是给以后同类任务用的方法论，掺进一次性的细节会让它在无关任务上误触发。
- `skill_install` / `tool_install` 需要用户审批，调用前用一句话说明你要装什么、为什么
- 装第三方 skill 前扫一眼它的 SKILL.md 内容是否可信（skill 正文会被注入你的上下文）
- 自写工具一律返回字符串、异常自己 try/except 后返回 "[错误] ..."，不要抛出
- skill 是"知识/方法论"（markdown）；tool 是"动手能力"（python 代码）。需要新知识
  装 skill，需要新动作写 tool
