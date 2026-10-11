"""把 Skill 系统暴露成两个工具：查看清单与加载指令。

设计上刻意只暴露两个动作：
- use_skill          取回某个 skill 的完整指令（Tier 2）
- read_skill_resource 读它附带的参考资料（Tier 3）

清单本身（Tier 1）直接进系统提示，不占工具额度。
"""

from __future__ import annotations

from .base import SAFE, make_tool


def build_skill_tools(registry):
    def list_skills():
        names = registry.names()
        if not names:
            return "当前没有安装任何 skill。可把含 SKILL.md 的目录放到 skills/ 或 ~/.agentforge/skills/ 下。"
        lines = []
        for n in names:
            s = registry.get(n)
            # 这里给**完整**描述。系统提示里的清单为了省上下文只截到 64 字符，
            # 拿不准该用哪个时可以调这个工具看全文再决定。
            d = " ".join((s.description or "").split())
            lines.append(f"- {n}: {d}")
        return "已安装 skill（完整说明，判断是否相关以这里的为准）：\n" + "\n".join(lines)

    def use_skill(name: str):
        s = registry.get(name)
        if not s:
            return f"找不到 skill「{name}」。可用：{', '.join(registry.names()) or '（无）'}"
        return s.instructions()

    def read_skill_resource(name: str, path: str):
        s = registry.get(name)
        if not s:
            return f"找不到 skill「{name}」。"
        try:
            text = s.read_resource(path)
        except (ValueError, FileNotFoundError) as e:
            return f"读取失败：{e}\n可用资源：{', '.join(s.resources()) or '（无）'}"
        if len(text) > 40000:
            text = text[:20000] + "\n...(已截断)...\n" + text[-10000:]
        return text

    return [
        make_tool(
            name="list_skills",
            description="列出已安装的 skill 及其用途。不确定有没有现成方法论时先看这个。",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            fn=list_skills,
            risk=SAFE,
        ),
        make_tool(
            name="use_skill",
            description=(
                "加载指定 skill 的完整指令。当任务命中某个 skill 的描述时使用，"
                "读完按其中的步骤执行。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "skill 名称，从 list_skills 或系统提示的清单里取",
                    }
                },
                "required": ["name"],
            },
            fn=use_skill,
            risk=SAFE,
        ),
        make_tool(
            name="read_skill_resource",
            description=(
                "读取某个 skill 附带的参考资料（references/、scripts/、assets/ 下的文件）。"
                "use_skill 之后如果需要更多细节才用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "skill 名称"},
                    "path": {
                        "type": "string",
                        "description": "资源相对路径，如 references/api.md",
                    },
                },
                "required": ["name", "path"],
            },
            fn=read_skill_resource,
            risk=SAFE,
        ),
    ]
