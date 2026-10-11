from __future__ import annotations

import platform
from pathlib import Path
from typing import Any, Iterator

from .config import Config
from .artifact import classify_tool_write
from .context import Compressor, measure
from .llm import OllamaClient, parse_text_tool_calls
from .memory import MemoryStore
from .llm import GenerationStopped
from .permissions import OUTSIDE_OK, Guard, PermissionDenied
from .pricing import cost_of, is_free
from .providers import ClientHub
from .router import Router
from .skills import SkillRegistry
from .tools import ToolRegistry

SYSTEM_TEMPLATE = """你是 AgentForge，一个运行在本机上的智能体。你的职责是**真正动手完成任务**，而不是描述该怎么做。

## 工作环境
- 操作系统：{os}
- 工作区根目录：{root}
- 相对路径一律相对工作区根目录解析；{outside_rule}
- 当前使用的模型：{model}（{model_desc}）

## 工作方式
1. **先看清现状**：不确定就先用 list_dir / find_files / search_code / read_file 获取真实信息，绝不凭猜测回答。
2. **最小改动**：优先用 edit_file 做精确替换，不要用 write_file 整文件重写已有内容。
3. **改完要验证**：代码改动后用 run_command 跑测试或脚本，确认真的能跑通。
4. **一次只做一步**：可以连续调用多个互不依赖的只读工具，但有依赖时必须等上一步结果。
5. **记住该记的**：如果这轮对话里出现了「以后还用得上的结论」（用户偏好、项目约定、踩过的坑），
   用 remember 把它写进长期记忆；下次任务会自动读到。只写结论，不要写过程。
6. **长材料先清单后成文**：整理、总结、大纲类任务若素材较长（几千字以上），禁止读完直接写
   最终文档。先通读并产出一份信息清单（人物、设定、时间线、要点各占一节，每条注明出处），
   用 write_file 落盘成中间文件，清单确认后再基于它成文。判断材料整体性质（是多个零散笔记
   还是同一件事的记录）要在清单阶段完成。
7. **成文前自检**：写完关键产出后，对照原始素材核对三件事——核心主体（谁/什么）有没有认对、
   有没有沿用早已作废的旧结论、细节能否在原素材中找到出处。发现编造或残留立即修正，再交付。

## 输出要求
- 用中文回复，简洁直接，不复述工具返回的原文。
- 调用工具时不要向用户解释你要调什么；直接调用。
- 任务完成后用一小段话总结：改了哪些文件、验证了什么。

## 安全约束
- 当前权限模式：{permission}。默认权限下写文件与执行命令需要用户确认；完全访问权限下直接执行。
- 被拒绝时不要反复重试同一操作，换一种方式或向用户说明。
- 禁止删除工作区之外的任何东西。
"""

# 系统提示里的路径规则一句话，按权限模式二选一（渲染见 _system()）。
# 完全访问权限放开的是「读写」工作区之外；「禁止删除工作区之外的任何东西」
# 是安全底线，写在模板末尾，两种模式都保留。
OUTSIDE_RULE = {
    True: "完全访问权限下也允许读写工作区之外的文件夹（用绝对路径）",
    False: "越出工作区的路径会被拒绝",
}

# 长对话末端的语言提醒。系统提示开头明明写了「用中文回复」，但本地小模型
# 在几十轮工具调用之后经常把它「忘」了，回复漂移成英文（实测 gemma4:12b，
# 中文维度分只有 62）。把关键输出要求复述一份放在消息列表最末尾——
# 那是生成时注意力离得最近的位置。system 角色放末尾对三家 provider 都安全
# （Anthropic 的 _convert_messages 会把任意位置的 system 归拢进顶层参数）。
LANG_REMINDER = {"role": "system",
                 "content": "提醒：忽略过程中的语言，最终回复一律用简体中文。"}

# 长期记忆注入时的标题。写成指令而不是资料，模型才会真的照做
MEMORY_HEADER = """## 长期记忆（来自以往任务，继续遵守）
以下是以前的任务记下来的结论与偏好，请在本轮工作中遵守：
"""

# 工具索引：只给名字，不给说明。完整 schema 里 4000 tokens 大部分当轮用不上，
# 但模型得知道"有这么个能力"，否则会说"我做不到"。要用时用 find_tools 现取。
TOOL_INDEX_HEADER = """## 其他可用工具（本次未展开说明）
下面这些工具现在就能用，只是为了省上下文没有下发详细参数。需要哪个就先调用
find_tools("关键词") 取它的用法，然后再调用它；也可以直接按名字调用。
  {names}
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
        # 用户是否显式指定过模型。自动路由的开关看它，而不是看「当前是不是默认模型」——
        # 默认模型本机跑不起来时会被替换成别的 key，那种情况下不该从此不再路由。
        self.model_pinned = bool(model_key)
        self.system_prompt = ""
        self.last_compact: str = ""
        self._skill_hint: str = ""
        # 本轮实际下发的工具名集合：系统提示里的「工具索引」要把它们排除掉
        self._active_tool_names: set[str] = set()
        # 长期记忆：任务开始前读进来注入系统提示，任务结束后由 remember 工具追加
        self.memory = MemoryStore(cfg.state() / "memory.md")
        # 权限模式的中文名，只用于系统提示里的一句话说明
        self.permission_label = "完全访问权限"
        # 是否处于完全访问权限（决定路径边界放不放工作区之外，见 OUTSIDE_RULE）
        self.full_access = True

    # ---------- 模型 ----------
    def set_model(self, key: str) -> None:
        if key not in self.cfg.models:
            raise KeyError(f"未定义的模型 key: {key}")
        self.model_key = key
        # 调用方明确点名了模型（编排层给每一步分配、用户 /model 切换），
        # 后续这一轮就不该再被自动路由改掉。
        self.model_pinned = True
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
            outside_rule=OUTSIDE_RULE[self.full_access],
        )
        if self.skills and self.skills.skills:
            base += "\n" + self.skills.catalog() + "\n"
        # 工具索引：把没下发说明的工具名字列出来（只列名字，很便宜），
        # 让模型知道能力边界，而不是"没看到就当没有这个功能"
        sel = self.registry.selector
        if sel is not None and getattr(self.cfg, "tools_lazy", True):
            idx = sel.index_text(exclude=self._active_tool_names)
            if idx:
                base += "\n" + TOOL_INDEX_HEADER.format(names=idx)
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

    # ---------- 本轮该下发哪些工具 ----------
    def _active_tools(self, messages: list[dict]) -> list[dict]:
        """按需下发工具 schema：核心 + 关键词命中 + 本轮已用过的。

        顺便把结果记进 self._active_tool_names，供系统提示里的「工具索引」排重。
        """
        sel = self.registry.selector
        if sel is None:
            schemas = self.registry.schemas()
            self._active_tool_names = {s["function"]["name"] for s in schemas}
            return schemas

        texts: list[str] = []
        for m in messages[-6:]:
            if m.get("role") not in ("user", "assistant"):
                continue
            c = m.get("content")
            if isinstance(c, str) and c:
                texts.append(c[-1500:])       # 取尾部：结论与要求通常在后面
        sticky: set[str] = set(self.registry.pinned)
        for m in messages[-12:]:
            for tc in (m.get("tool_calls") or []):
                nm = (tc.get("function") or {}).get("name") or tc.get("name")
                if nm:
                    sticky.add(nm)

        schemas = sel.select(texts, sticky=sticky)
        self._active_tool_names = {s["function"]["name"] for s in schemas}
        return schemas

    # ---------- 主循环 ----------
    def run(
        self,
        history: list[dict],
        user_text: str,
        auto_route: bool = True,
        stop_check=None,
        auto_approve: bool | None = None,
        file_log: list | None = None,
        relay: list[dict] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """跑一轮完整任务。

        stop_check 是个无参可调用对象，返回 True 表示用户请求停止。每个检查点
        （每轮推理开始前、每次调工具前）问一次，命中就正常收尾退出。

        auto_approve  True = 完全访问权限（写文件与命令直接放行，工作区之外的
                      路径也允许读写）；False/None = 走 Guard 的审批逻辑，此时若
                      Guard.auto_approve_all 已打开（CLI 的 --yes / /yes），效果等同 True
        每轮运行单独传入而不是改 Guard 的全局开关——同一个 Agent 实例可能被
        多个请求共用，改全局状态会串味。

        file_log 传进来时，写类工具动过的文件会被追加进去，供任务台账记录产出。

        relay 给定时，用它**替代**全量历史作为发给模型的上下文（多步任务的接力
        省法：只带计划 + 摘要 + 文件路径，而不是把前面每一步的工具输出全带上）。
        注意它只影响"模型看到什么"：yield 出去的 history 仍是全量，界面回看与
        任务台账不受影响。
        """
        stopped = stop_check or (lambda: False)
        # 完全访问 = 本次调用显式传入 auto_approve=True，或 Guard 的全局自动批准
        # 已打开（CLI 的 --yes / /yes 走这条路）。两条通道必须给同一个答案，
        # 否则会出现「命令不用确认了、路径还是被拦」的分裂状态。
        full = auto_approve is True or bool(getattr(self.guard, "auto_approve_all", False))
        self.permission_label = "完全访问权限" if full else "默认权限"
        self.full_access = full

        if auto_route and not self.model_pinned:
            picked = self.router.classify(user_text)
            if picked != self.model_key:
                self.model_key = picked
                yield {"type": "route", "model_key": picked, "detail": self.router.explain(user_text)}

        history = list(history)
        this_turn = {"role": "user", "content": user_text}
        history.append(this_turn)
        # ctx = 真正发给模型的消息序列。没给 relay 时直接复用 history 这个列表
        # （同一个对象，后续 append 两边都生效）；给了 relay 就分道走。
        ctx = history if relay is None else [*relay, this_turn]

        def _push(msg: dict) -> None:
            history.append(msg)
            if ctx is not history:
                ctx.append(msg)

        def _pop() -> None:
            if history:
                history.pop()
            if ctx is not history and ctx:
                ctx.pop()

        yield {"type": "user", "content": user_text}

        # Skill 自动激活：命中就把正文摘要注入本轮上下文（Tier 2）
        self._skill_hint = ""
        if self.skills and self.cfg.skill_auto_load:
            hint = self.skills.auto_hint(
                user_text, self.cfg.skill_min_score,
                getattr(self.cfg, "skill_auto_max_chars", 1600),
            )
            if hint:
                hits = self.skills.match(user_text, 1)
                self._skill_hint = hint
                nm = hits[0][0].name if hits else ""
                yield {
                    "type": "skill",
                    "name": nm,
                    "detail": (f"已加载 skill 摘要：{nm}"
                               f"（正文过长时只注入开头，需要全文用 use_skill 取）") if nm else "",
                }

        spec = self.cfg.model(self.model_key)

        for step in range(1, self.cfg.max_steps + 1):
            if stopped():
                # 把已经累积的上下文一起带出去，调用方可以据此保存/回看
                yield {"type": "stopped", "step": step, "reason": "用户停止",
                       "history": history}
                return
            yield {"type": "step", "step": step, "model_key": self.model_key, "model": spec.display}
            # 每轮重算：这轮刚用过的工具、刚 find_tools 取过的工具，下一轮要常驻下发
            tools = self._active_tools(ctx)
            messages = self._compact(self.messages_with_system(ctx))
            messages.append(dict(LANG_REMINDER))

            result = None
            try:
                for kind, payload in self.hub.chat_stream(spec, messages, tools,
                                                          stop_check=stopped):
                    if kind == "delta":
                        yield {"type": "delta", "text": payload}
                    elif kind == "thinking":
                        yield {"type": "thinking", "text": payload}
                    elif kind == "result":
                        result = payload
            except GenerationStopped:
                # 用户在生成中途点了停止：此时 assistant 消息还没 push 进历史，
                # 半截内容直接丢弃，按「用户停止」收尾，不等下一个检查点。
                yield {"type": "stopped", "step": step,
                       "reason": "用户停止（生成中断）", "history": history}
                return
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
                _push(assistant_msg)
                yield {"type": "assistant", "content": result.content, "history": history}
                return

            # 带工具调用的 assistant 消息
            tc_payload = [
                {"function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in result.tool_calls
            ]
            _push({"role": "assistant", "content": result.content or "", "tool_calls": tc_payload})

            for tc in result.tool_calls:
                if stopped():
                    # 这次工具没执行，把已追加的 assistant(tool_calls) 摘掉：
                    # 否则历史里会留下一个没有 tool 应答的悬空调用，下次续聊会报错
                    if history and history[-1].get("tool_calls"):
                        _pop()
                    yield {"type": "stopped", "step": step,
                           "reason": "用户停止（工具尚未执行）", "history": history}
                    return
                yield {"type": "tool_start", "name": tc.name, "args": tc.arguments}
                out = self._execute(tc.name, tc.arguments, auto_approve, file_log)
                yield {"type": "tool_result", "name": tc.name, "output": out}
                _push({"role": "tool", "content": out})

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
            # 按需下发模式下，模型可能凭印象报了个名字（例如把 git_push 叫成 push）。
            # 这里替它找一次：名字沾边的直接把用法递过去并粘住，下一轮就带完整说明了。
            sel = self.registry.selector
            if sel is not None:
                probe = name.lower()
                hits = [n for n in self.registry.names()
                        if probe == n or probe in n or n in probe or
                        (probe.split("_")[0] and probe.split("_")[0] in n)]
                if hits:
                    self.registry.pinned.update(hits[:3])
                    return (f"[工具名不符] 没有名为 {name} 的工具，你要用的可能是下面这个，"
                            f"请用正确的名字重新调用：\n" + sel.describe_many(hits[:3]))
            return f"[错误] 未知工具 {name}。可用工具：{', '.join(self.registry.names())}"

        if (
            auto_approve is not True
            and self.guard.needs_approval(name)
            and not self.guard.approve(name, args)
        ):
            return f"[已拒绝] 用户拒绝了 {name} 的执行。请不要重试，改换方案或向用户说明。"

        # 完全访问权限的任务里，这一次工具调用允许读写工作区之外的路径。
        # 判据与 run() 里的 full 保持一致：显式 auto_approve，或 Guard 全局自动批准。
        # 只放开「读写」；命令黑名单不随之放开（见 Guard.check_command）。
        token = OUTSIDE_OK.set(auto_approve is True or bool(getattr(self.guard, "auto_approve_all", False)))
        try:
            out = str(tool.run(**(args or {})))
        except PermissionDenied as e:
            return f"[已拒绝] {e}"
        except TypeError as e:
            return f"[错误] 工具 {name} 参数不对：{e}"
        except Exception as e:  # noqa: BLE001
            return f"[错误] 工具 {name} 执行异常：{type(e).__name__}: {e}"
        finally:
            OUTSIDE_OK.reset(token)

        # 记下这次调用改动的文件（任务台账据此展示「任务产出」，并在清空时可选删除）
        if file_log is not None:
            rec = classify_tool_write(name, args or {}, out)
            if rec:
                file_log.append(rec)
        return out
