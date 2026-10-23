from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .config import Config
from .llm import OllamaClient, ollama_probe, set_local_serial
from .loop import Agent
from .modelstore import ModelStore
from .permissions import Guard
from .providers import ClientHub
from .router import Router
from .session import Session
from .skills import SkillRegistry, default_dirs
from .tools import build_registry

HELP = """/help            显示帮助
/tools           列出可用工具
/skills          列出已安装的 skill
/compact         手动压缩上下文（超长对话时用）
/context         显示当前上下文体积与阈值
/model           显示当前模型与切换方式
/model <key>     切换模型（general / code / fast）
/route <文本>    只看路由判断，不执行
/new             开一个新会话（清空历史）
/save [名称]     保存当前会话
/load [名称]     载入会话，省略则载入最近一个
/sessions        列出已保存会话
/yes             本次会话内自动批准所有写操作与命令
/exit            退出
"""


class CLI:
    def __init__(self, cfg: Config, auto_approve: bool = False, model_key: str | None = None):
        self.cfg = cfg
        self.console = Console(highlight=False)
        self.client = OllamaClient(cfg.ollama_host, cfg.keep_alive, cfg.request_timeout,
                                   idle_timeout=getattr(cfg, "stream_idle_timeout", 180))
        # 本地模型串行执行开关（config.yaml 的 ollama.local_serial）：
        # 让所有本地模型推理排成一条队，避免并发任务同时压 Ollama 互相拖累。
        set_local_serial(getattr(cfg, "local_serial", True))
        self.hub = ClientHub(cfg, self.client)
        # 可用模型目录：本地模型 + 自动发现 + 用户添加的付费模型
        self.store = ModelStore(cfg)
        try:
            # 带上 probe：新发现的本地模型顺带探一次原生窗口，与 Web 路径行为一致
            self.store.catalog(self.client.list_models(), probe=ollama_probe(self.client))
        except Exception:  # noqa: BLE001
            self.store.catalog()
        self.guard = Guard(cfg, approver=self._approve)
        self.guard.auto_approve_all = auto_approve
        self.skills = SkillRegistry(default_dirs(Path(cfg.workspace_root), cfg.skill_dirs))
        self.registry = build_registry(self.guard, cfg, skill_registry=self.skills)
        self.router = Router(cfg)
        self.agent = Agent(
            cfg, self.client, self.registry, self.guard, self.router, model_key,
            self.skills, hub=self.hub,
        )
        self.session = Session(cfg.session_dir)
        self._thinking_shown = False

    # ---------- 审批 ----------
    def _approve(self, tool_name: str, args: dict) -> bool:
        print()
        self.console.print()
        if tool_name == "run_command":
            detail = Text(f"  $ {args.get('command','')}", style="bold yellow")
            self.console.print(Panel(detail, title="[bold red]需要批准：执行命令", border_style="red"))
        else:
            path = args.get("path", "")
            self.console.print(
                Panel(
                    Text(f"  {tool_name}  {path}", style="bold yellow"),
                    title=f"[bold red]需要批准：{tool_name}",
                    border_style="red",
                )
            )
        ans = Prompt.ask("允许执行?", choices=["y", "n", "a"], default="y")
        if ans == "a":
            self.guard.auto_approve_all = True
            return True
        return ans == "y"

    @staticmethod
    def _looks_like_tool_json(buf: str) -> bool:
        """判断累积的流式文本是否疑似“被写成正文的工具调用”。

        部分模型（实测 qwen2.5-coder:14b）不走 tool_calls 字段，而是直接输出
        {"name": ..., "arguments": ...}。这类内容要先扣住不显示：若随后确认是
        工具调用就丢弃，避免把一大段 JSON 当成答案念给用户。
        """
        import re

        s = buf.strip()
        if not s or len(s) > 6000:
            return False
        if s.startswith("{"):
            return True
        if not s.startswith("`"):
            return False
        # 注意 buf 是累积的完整前缀（流式逐字拼起来的）。
        # 三个反引号=代码块：看到换行后首字符是 "{" 判为工具调用，继续扣留；
        # 否则是普通代码块，立刻放行，保证流式显示不受影响。
        if s.startswith("```"):
            m = re.match(r"^```[a-zA-Z0-9]*[ \t]*\r?\n\s*(\S)", s, re.S)
            return True if not m else (m.group(1) == "{")
        # 不足三个反引号时先看几帧：攒够 4 个字符还形不成 ``` 就是行内代码，放行
        if s.startswith("`"):
            return len(s) < 4
        return False

    # ---------- 事件渲染 ----------
    def _render(self, events, history_out: list):
        final = None
        printed_live = False
        pending = ""
        for ev in events:
            t = ev["type"]
            if t == "route":
                self.console.print(f"[dim]{ev['detail']}[/dim]")
            elif t == "skill":
                self.console.print(f"[magenta]◆ {ev['detail']}[/magenta]")
            elif t == "step":
                if printed_live:
                    print()
                    printed_live = False
            elif t == "thinking":
                # 思考过程不展示，只在首次出现时给个提示
                if not getattr(self, "_thinking_shown", False):
                    self.console.print("[dim]  (模型推理中...)[/dim]")
                    self._thinking_shown = True
            elif t == "delta":
                pending += ev["text"]
                if self._looks_like_tool_json(pending):
                    continue  # 先扣住，等确认是正文还是工具调用
                print(pending, end="", flush=True)
                pending = ""
                printed_live = True
            elif t == "tool_start":
                pending = ""  # 确认是工具调用，丢弃扣住的 JSON
                if printed_live:
                    print()
                    printed_live = False
                args = ev["args"]
                brief = args.get("path") or args.get("command") or args.get("pattern") or args.get("url") or ""
                self.console.print(f"[cyan]▸ {ev['name']}[/cyan] [dim]{str(brief)[:110]}[/dim]")
            elif t == "tool_result":
                out = ev["output"]
                preview = out if len(out) <= 700 else out[:350] + "\n...\n" + out[-250:]
                self.console.print(
                    Panel(preview, title=f"{ev['name']} 结果", border_style="dim", style="dim")
                )
            elif t == "stats":
                if ev.get("tps"):
                    self.console.print(f"[dim]  ({ev['tps']:.1f} tok/s, {ev['tokens']} tokens)[/dim]")
            elif t == "assistant":
                if pending:  # 之前扣住的内容其实是正文，补打出来
                    print(pending, end="", flush=True)
                    pending = ""
                    printed_live = True
                if printed_live:
                    print()
                else:
                    # 非流式情况下正文没有逐字打印过，这里整体渲染一次
                    if ev["content"].strip():
                        self.console.print(Markdown(ev["content"]))
                final = ev["content"]
                history_out.clear()
                history_out.extend(ev["history"])
            elif t == "error":
                self.console.print(f"[bold red]{ev['message']}[/bold red]")
        return final

    # ---------- 命令 ----------
    def _cmd_model(self, arg: str) -> None:
        if not arg:
            table = Table(title="可用模型", box=None)
            table.add_column("key", style="cyan")
            table.add_column("模型", style="white")
            table.add_column("说明")
            for k, spec in self.cfg.models.items():
                mark = "*" if k == self.agent.model_key else " "
                table.add_row(f"{mark}{k}", spec.name, spec.desc)
            self.console.print(table)
            return
        if arg not in self.cfg.models:
            self.console.print(f"[red]未知模型 key: {arg}，可选 {list(self.cfg.models)}[/red]")
            return
        self.agent.set_model(arg)
        self.console.print(f"[green]已切换到 {arg}（{self.cfg.model(arg).name}）[/green]")

    def _cmd_sessions(self) -> None:
        names = Session.list_all(self.cfg.session_dir)
        self.console.print("\n".join(names) if names else "[dim]暂无会话[/dim]")

    # ---------- 主循环 ----------
    def repl(self) -> None:
        self.console.print(
            Panel(
                f"[bold]AgentForge[/bold]  模型 [cyan]{self.agent.model_name}[/cyan]\n"
                f"工作区 [dim]{self.cfg.workspace_root}[/dim]\n"
                f"输入 /help 查看命令，/exit 退出",
                border_style="cyan",
            )
        )
        history: list[dict] = []
        while True:
            try:
                raw = Prompt.ask("[bold green]你[/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not raw:
                continue

            if raw.startswith("/"):
                parts = raw.split(maxsplit=1)
                cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
                if cmd in ("/exit", "/quit"):
                    break
                elif cmd == "/help":
                    self.console.print(HELP)
                elif cmd == "/tools":
                    self.console.print(self.registry.describe())
                elif cmd == "/skills":
                    names = self.skills.names()
                    if not names:
                        self.console.print("[dim]尚未安装 skill。把含 SKILL.md 的目录放进 skills/ 即可。[/dim]")
                    else:
                        self.console.print("\n".join(f"  - {n}" for n in names))
                elif cmd == "/compact":
                    if not history:
                        self.console.print("[dim]当前没有可压缩的对话[/dim]")
                    else:
                        with self.console.status("[cyan]正在压缩上下文...[/cyan]"):
                            history[:] = self.agent.compress_now(history)[0]
                        self.console.print(f"[green]{self.agent.last_compact}[/green]")
                elif cmd == "/context":
                    n = self.agent.context_size(history)
                    th = self.cfg.compact_threshold
                    ratio = n / th if th else 0
                    bar = "#" * int(ratio * 20)
                    self.console.print(
                        f"上下文 [bold]{n}[/bold] 字 / 阈值 {th} 字  "
                        f"[{bar:<20}] {ratio:.0%}"
                    )
                elif cmd == "/model":
                    self._cmd_model(arg)
                elif cmd == "/route":
                    self.console.print(self.router.explain(arg))
                elif cmd == "/new":
                    history.clear()
                    self.console.print("[dim]已清空对话历史[/dim]")
                elif cmd == "/save":
                    self.session.name = arg or self.session.name
                    for m in history:
                        self.session.messages = []
                    for m in history:
                        self.session.append(m)
                    self.console.print(f"[dim]已保存到 {self.session.path}[/dim]")
                elif cmd == "/load":
                    s = Session(self.cfg.session_dir, arg) if arg else Session.latest(self.cfg.session_dir)
                    if s is None:
                        self.console.print("[yellow]没有可载入的会话[/yellow]")
                    else:
                        self.session = s
                        history = list(s.messages)
                        self.console.print(f"[dim]已载入 {s.path.name}，{len(history)} 条消息[/dim]")
                elif cmd == "/sessions":
                    self._cmd_sessions()
                elif cmd == "/yes":
                    self.guard.auto_approve_all = True
                    self.console.print("[yellow]本次会话内已开启自动批准[/yellow]")
                else:
                    self.console.print(f"[red]未知命令 {cmd}[/red]")
                continue

            self._render(self.agent.run(history, raw), history)
            print()

    def run_once(self, task: str) -> str:
        history: list[dict] = []
        out: list[dict] = []
        final = self._render(self.agent.run(history, task), out)
        return final or ""
