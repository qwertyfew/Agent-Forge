from __future__ import annotations

from typing import Any, Callable


SAFE = "safe"      # 只读，自动执行
WRITE = "write"    # 会改动文件，默认需确认
EXEC = "exec"      # 会执行命令，默认需确认


class Tool:
    """一个可被模型调用的工具。

    子类只需声明 name / description / parameters / risk，并实现 run()。
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    risk: str = SAFE

    def run(self, **kwargs) -> str:
        raise NotImplementedError

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def make_tool(
    name: str,
    description: str,
    parameters: dict,
    fn: Callable[..., str],
    risk: str = SAFE,
) -> Tool:
    """用闭包快速构造一个工具，省掉一堆样板子类。"""
    cls = type(
        name.title().replace("_", ""),
        (Tool,),
        {
            "name": name,
            "description": description,
            "parameters": parameters,
            "risk": risk,
            "run": staticmethod(fn),
        },
    )
    return cls()
