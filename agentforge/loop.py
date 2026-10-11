from __future__ import annotations

import platform
from pathlib import Path
from typing import Any, Iterator

from .config import Config
from .artifact import classify_tool_write
from .context import Compressor, measure
from .llm import OllamaClient, parse_text_tool_calls
from .memory import MemoryStore
from .permissions import Guard, PermissionDenied
from .pricing import cost_of, is_free
from .providers import ClientHub
from .router import Router
from .skills import SkillRegistry
from .tools import ToolRegistry

SYSTEM_TEMPLATE = """你是 AgentForge，一个运行在本机上的智能体。你的职责是**真正动手完成任务**，而不是描述该怎么做。

## 工作环境
- 操作系统：{os}
- 工作区根目录：{root}
- 相对路径一律相对工作区根目录解析；越出工作区的路径会被拒绝
- 当前使用的模型：{model}（{model_desc}）

## 工作方式
1. **先看清现状**：不确定就先用 list_dir / find_files / search_code / read_file 获取真实信息，绝不凭猜测回答。
2. **最小改动**：优先用 edit_file 做精确替换，不要用 write_file 整文件重写已有内容。
3. **改完要验证**：代码改动后用 run_command 跑测试或脚本，确认真的能跑通。
4. **一次只做一步**：可以连续调用多个互不依赖的只读工具，但有依赖时必须等上一步结果。
5. **记住该记的**：如果这轮对话里出现了「以后还用得上的结论」（用户偏好、项目约定、踩过的坑），
   用 remember 把它写进长期记忆；下次任务会自动读到。只写结论，不要写过程。

## 输出要求
- 用中文回复，简洁直接，不复述工具返回的原文。
- 调用工具时不要向用户解释你要调什么；直接调用。
- 任务完成后用一小段话总结：改了哪些文件、验证了什么。

## 安全约束
- 当前权限模式：{permission}。默认权限下写文件与执行命令需要用户确认；完全访问权限下直接执行。
- 被拒绝时不要反复重试同一操作，换一种方式或向用户说明。
- 禁止删除工作区之外的任何东西。
"""

# 长期记忆注入时的标题。写成指令而不是资料，模型才会真的照做
MEMORY_HEADER = """## 长期记忆（来自以往任务，继续遵守）
以下是以前的任务记下来的结论与偏好，请在本轮工作中遵守：
"""

COMPACT_THRESHOLD = 60_000


class Agent:
    def __init__(
        self,
        cfg: Config,
        client: OllamaClient,
        registry: ToolRegistry,
        guard: Guard,
        router: Router,
        model_key: str | None = None,
        skill_registry: SkillRegistry | None = None,
        hub: ClientHub | None = None,
    ):
        self.cfg = cfg
        self.client = client
        # hub 负责把请求分发到本地 Ollama 或付费云端 provider；
        # 不传就退化成"只有本地 Ollama"的单 provider 模式
        self.hub = hub or ClientHub(cfg, client)
        self.registry = registry
        self.guard = guard
        self.router = router
        self.skills = skill_registry
        self.compressor = Compressor(cfg, client, hub=self.hub)
        self.model_key = model_key or cfg.routing_default
        self.system_prompt = ""
        self.last_compact: str = ""
        self._skill_hint: str = ""
        # 长期记忆：任务开始前读进来注入系统提示，任务结束后由 remember 工具追加
        self.memory = MemoryStore(Path(cfg.workspace_root) / ".agentforge" / "memory.md")
        # 权限模式的中文名，只用于系统提示里的一句话说明
        self.permission_label = "完全访问权限"

    # ---------- 模型 ----------
    def set_model(self, key: str) -> None:
        if key not in self.cfg.models:
            raise KeyError(f"未定义的模型 key: {key}")
        self.model_key = key
        self.system_prompt = ""

    @property
    def model_name(self) -> str:
        return self.cfg.model(self.model_key).display

    @property
    def model_spec(self):
        return self.cfg.model(self.model_key)

    def _system(self) -> str:
        spec = self.cfg.model(self.model_key)
        base = SYSTEM_TEMPLATE.format(
            os=f"{platform.system()} {platform.release()}",
            root=self.cfg.workspace_root,
            model=spec.display,
            model_desc=spec.desc,
            permission=self.permission_label,
        )
        if self.skills and self.skills.skills:
            base += "\n" + self.skills.catalog() + "\n"
        # 长期记忆放在最后：它是「上一批任务留下的结论」，越靠后模型越容易当约束看
        block = self.memory.prompt_block()
        if block:
            base += "\n" + MEMORY_HEADER + block + "\n"
        return base

    def messages_with_system(self, history: list[dict]) -> list[dict]:
        msgs = [{"role": "system", "content": self._system()}]
        if self._skill_hint:
            msgs.append({"role": "system", "content": self._skill_hint})
        return [*msgs, *history]

    # ---------- 上下文压缩 ----------
    def _compact(self, messages: list[dict]) -> list[dict]:
        """每轮调用前的自动瘦身，只处理历史部分，系统提示不参与。"""
        if not self.cfg.compact_auto:
            return messages
        system, *rest = messages
        res = self.compressor.compact(rest, self.model_key, force=False)
        if res.changed:
            self.last_compact = res.note
        return [system, *res.messages]

    def compress_now(self, history: list[dict]) -> tuple[list[dict], str]:
        """手动压缩入口（CLI 的 /compact 与 Web 界面按钮都走这里）。"""
        res = self.compressor.compact(
            history, self.model_key, force=True, allow_summarize=True
        )
        self.last_compact = res.note
        return res.messages, res.note or "上下文仍很紧凑，无需压缩。"

    def context_size(self, history: list[dict]) -> int:
        return measure(history)

    # ---------- 主循环 ----------
    def run(
        self,
        history: list[dict],
        user_text: str,
        auto_route: bool = True,
        stop_check=None,
        auto_approve: bool | None = None,
        file_log: list | None = None,
    ) -> Iterator[dict[str, Any]]:
        """跑一轮完整任务。

        stop_check 是个无参可调用对象，返回 True 表示用户请求停止。每个检查点
        （每轮推理开始前、每次调工具前）问一次，命中就正常收尾退出。

        auto_approve  True = 完全访问权限（写文件与命令直接放行）
                      False/None = 走 Guard 的审批逻辑
        每轮运行单独传入而不是改 Guard 的全局开关——同一个 Agent 实例可能被
        多个请求共用，改全局状态会串味。

        file_log 传进来时，写类工具动过的文件会被追加进去，供任务台账记录产出。
        """
        stopped = stop_check or (lambda: False)
        self.permission_label = "完全访问权限" if auto_approve else "默认权限"

        if auto_route and self.model_key == self.cfg.routing_default:
            picked = self.router.classify(user_text)
            if picked != self.model_key:
                self.model_key = picked
                yield {"type": "route", "model_key": picked, "detail": self.router.explain(user_text)}

        history = list(history)
        history.append({"role": "user", "content": user_text})
        yield {"type": "user", "content": user_text}

        # Skill 自动激活：命中就把正文注入本轮上下文（Tier 2）
        self._skill_hint = ""
        if self.skills and self.cfg.skill_auto_load:
            hint = self.skills.auto_hint(user_text, self.cfg.skill_min_score)
            if hint:
                hits = self.skills.match(user_text, 1)
                self._skill_hint = hint
                yield {
                    "type": "skill",
                    "name": hits[0][0].name if hits else "",
                    "detail": f"已自动加载 skill：{hits[0][0].name}" if hits else "",
                }

        spec = self.cfg.model(self.model_key)
        tools = self.registry.schemas()

        for step in range(1, self.cfg.max_steps + 1):
            if stopped():
                # 把已经累积的上下文一起带出去，调用方可以据此保存/回看
                yield {"type": "stopped", "step": step, "reason": "用户停止",
                       "history": history}
                return
            yield {"type": "step", "step": step, "model_key": self.model_key, "model": spec.display}
            messages = self._compact(self.messages_with_system(history))

            result = None
            try:
                for kind, payload in self.hub.chat_stream(spec, messages, tools):
                    if kind == "delta":
                        yield {"type": "delta", "text": payload}
                    elif kind == "thinking":
                        yield {"type": "thinking", "text": payload}
                    elif kind == "result":
                        result = payload
            except Exception as e:  # noqa: BLE001
                yield {"type": "error", "message": f"模型调用失败：{e}"}
                return

            if result is None:
                yield {"type": "error", "message": "模型未返回任何内容"}
                return

            # stats 里把用量与费用一并带上：界面靠它实时累加「已消耗预估费用」
            yield {
                "type": "stats",
                "tps": result.tokens_per_second,
                "tokens": result.eval_count,
                "prompt_tokens": result.prompt_eval_count,
                "model_key": self.model_key,
                "model": spec.display,
                "kind": spec.kind,
                "cost": cost_of(
                    result.prompt_eval_count, result.eval_count,
                    spec.provider, spec.remote_model, spec.kind,
                ),
                "free": is_free(spec.provider, spec.kind),
            }

            # 部分模型（实测 qwen2.5-coder:14b）不走 tool_calls 字段，而是把调用
            # 写成正文 JSON。这里按已知工具名白名单把它抢救回来，否则 Agent 会把
            # 这段 JSON 当成最终答案念给用户，工具永远不执行。
            if not result.tool_calls and result.content:
                rescued = parse_text_tool_calls(result.content, self.registry.names())
                if rescued:
                    result.tool_calls = rescued
                    result.content = ""

            if not result.tool_calls:
                assistant_msg = {"role": "assistant", "content": result.content}
                history.append(assistant_msg)
                yield {"type": "assistant", "content": result.content, "history": history}
                return

            # 带工具调用的 assistant 消息
            tc_payload = [
                {"function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in result.tool_calls
            ]
            history.append(
                {"role": "assistant", "content": result.content or "", "tool_calls": tc_payload}
            )

            for tc in result.tool_calls:
                if stopped():
                    # 这次工具没执行，把已追加的 assistant(tool_calls) 摘掉：
                    # 否则历史里会留下一个没有 tool 应答的悬空调用，下次续聊会报错
                    if history and history[-1].get("tool_calls"):
                        history.pop()
                    yield {"type": "stopped", "step": step,
                           "reason": "用户停止（工具尚未执行）", "history": history}
                    return
                yield {"type": "tool_start", "name": tc.name, "args": tc.arguments}
                out = self._execute(tc.name, tc.arguments, auto_approve, file_log)
                yield {"type": "tool_result", "name": tc.name, "output": out}
                history.append({"role": "tool", "content": out})

        yield {"type": "error", "message": f"已达到最大步数 {self.cfg.max_steps}，任务未收敛，请拆小任务或提高上限。"}

    # ---------- 工具执行 ----------
    def _execute(
        self,
        name: str,
        args: dict,
        auto_approve: bool | None = None,
        file_log: list | None = None,
    ) -> str:
        tool = self.registry.get(name)
        if tool is None:
            return f"[错误] 未知工具 {name}。可用工具：{', '.join(self.registry.names())}"

        if (
            auto_approve is not True
            and self.guard.needs_approval(name)
            and not self.guard.approve(name, args)
        ):
            return f"[已拒绝] 用户拒绝了 {name} 的执行。请不要重试，改换方案或向用户说明。"

        try:
            out = str(tool.run(**(args or {})))
        except PermissionDenied as e:
            return f"[已拒绝] {e}"
        except TypeError as e:
            return f"[错误] 工具 {name} 参数不对：{e}"
        except Exception as e:  # noqa: BLE001
            return f"[错误] 工具 {name} 执行异常：{type(e).__name__}: {e}"

        # 记下这次调用改动的文件（任务台账据此展示「任务产出」，并在清空时可选删除）
        if file_log is not None:
            rec = classify_tool_write(name, args or {}, out)
            if rec:
                file_log.append(rec)
        return out
