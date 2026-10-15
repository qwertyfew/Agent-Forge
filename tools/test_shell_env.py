"""run_command 子进程环境的回归：PATH 必须把当前解释器目录放最前。

运行：python tools/test_shell_env.py
事故（第四轮评测 web-20261015-102732，c2/c5）：Windows 继承 PATH 里 `python`
解析到微软商店占位符（exit=9009 "Python was not found"）——模型的脚本本身
没错却跑不起来，f4 没了可用的 python 验证只能肉眼复制（丢换行判负）。
修法与 verify.run_verify_cmd 同一套：子进程 PATH 前插当前解释器目录。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.tools.shell import build_shell_tools    # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


class StubCfg:
    workspace_root = ""
    max_tool_output_chars = 4000


class StubGuard:
    cfg = StubCfg()

    def check_command(self, command: str) -> None:
        return None


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_shell_"))
    StubCfg.workspace_root = str(tmp)
    tools = {t.name: t for t in build_shell_tools(StubGuard(), StubCfg())}
    rc = tools["run_command"]

    out = rc.run(command='python -c "import sys;print(sys.executable)"')
    check("python 能跑通（exit 0，不再是 9009）", "[exit] 0" in out, repr(out))
    exe = str(Path(sys.executable).resolve())
    check("python 解析到跑 harness 的同一个解释器",
          exe.casefold() in out.casefold(), f"输出 {out!r} 里没有 {exe!r}")

    out2 = rc.run(command="python -c \"import sys;print(sys.stdout.encoding)\"")
    check("子进程 stdout 是 UTF-8", "utf-8" in out2.casefold(), repr(out2))

    # 相对路径脚本在工作区根目录能直接跑（评测任务的实际形态）
    (tmp / "hello.py").write_text("print('ok-hello')\n", encoding="utf-8")
    out3 = rc.run(command="python hello.py")
    check("工作区相对路径脚本照常可跑", "[exit] 0" in out3 and "ok-hello" in out3,
          repr(out3))

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
