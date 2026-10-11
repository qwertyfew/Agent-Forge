"""零依赖 Web 界面（仅用标准库 http.server）。

启动：python run.py --web --port 8787
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agentforge import artifact, autonote, runcontrol
from agentforge.cli import CLI
from agentforge.config import Config
from agentforge.memory import MemoryStore
from agentforge.llm import native_context
from agentforge.modelstore import ModelStore
from agentforge.orchestrator import run_task
from agentforge.prefs import PERMISSION_LABEL, PrefsStore
from agentforge.taskstore import TaskStore

HERE = Path(__file__).resolve().parent
INDEX = HERE / "web" / "index.html"

# 从 Ollama 发现本地模型的动作不快也不慢，缓存几秒避免每次刷新都打一次
_DISCOVER_TTL = 5.0


def est_tokens(text: str) -> float:
    """粗估 token 数：中日韩 ≈ 1 token/字，其余 ≈ 3.8 字符/token。

    与前端 web/index.html 的 estTokens 保持同一口径，两边的百分比才对得上。
    只是量级估算（真实 tokenizer 依赖具体模型），用来给用户一个占比参考足够。
    """
    cjk = 0
    for ch in text:
        c = ord(ch)
        if (0x3400 <= c <= 0x9FFF) or (0xF900 <= c <= 0xFAFF) or (0x3040 <= c <= 0x30FF):
            cjk += 1
    return cjk * 1.05 + (len(text) - cjk) / 3.8


class _Handler(BaseHTTPRequestHandler):
    cli: "CLI" = None  # type: ignore[assignment]
    cfg: Config = None  # type: ignore[assignment]
    store: ModelStore = None  # type: ignore[assignment]
    tasks: TaskStore = None  # type: ignore[assignment]
    prefs: PrefsStore = None  # type: ignore[assignment]
    mem: MemoryStore = None  # type: ignore[assignment]
    _discovered: list[str] = []
    _discovered_at: float = 0.0

    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # ---------- helpers ----------
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 动态接口一律禁缓存，避免浏览器拿到陈旧的模型/启用状态
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # 流式响应没有 Content-Length，客户端只能靠连接断开判断结束。
        # 若这里声明 keep-alive，客户端会一直等到超时才认为响应完成
        # （实测：内容早就发完了，客户端却干等 6 分钟）。
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not INDEX.exists():
                self._json({"error": "缺少 web/index.html"}, 500)
                return
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(
                {
                    "models": {k: {"name": s.display, "desc": s.desc} for k, s in self.cfg.models.items()},
                    "workspace": str(self.cfg.workspace_root),
                    "tools": self.cli.registry.describe(),
                    "ollama": self.cfg.ollama_host,
                    "skills": self.cli.skills.names(),
                    "compact_threshold": self.cfg.compact_threshold,
                    "pool": self.store.summary_line(),
                    "prefs": self.prefs.snapshot(),
                    "memory": {
                        "count": self.mem.count(),
                        "path": str(self.mem.path),
                    },
                }
            )
        elif path == "/api/models":
            self._json(self._models_snapshot())
        elif path == "/api/tasks":
            self._handle_tasks_get()
        elif path == "/api/prefs":
            self._json(self.prefs.snapshot())
        elif path == "/api/memory":
            self._json(self._memory_snapshot())
        elif path == "/api/skills":
            self._json(self._skills_snapshot())
        elif path == "/api/tools":
            self._json(self._tools_snapshot())
        elif path == "/api/health":
            self._json({"ollama": self.cli.client.ping()})
        else:
            self._json({"error": "not found"}, 404)

    # ---------- 技能 / 工具 ----------
    def _skills_snapshot(self) -> dict:
        reg = self.cli.skills
        items = []
        for name, sk in reg.skills.items():
            items.append(
                {
                    "name": name,
                    "description": sk.description,
                    "path": str(sk.dirpath),
                    "resources": sk.resources(),
                    "auto": True,  # 当前实现里 skill 全部参与自动匹配
                }
            )
        return {
            "skills": items,
            "dirs": [str(d) for d in reg.dirs],
            "auto_load": self.cfg.skill_auto_load,
            "min_score": self.cfg.skill_min_score,
        }

    def _tools_snapshot(self) -> dict:
        reg = self.cli.registry
        approval = set(self.cfg.require_approval or [])
        items = []
        for name in reg.names():
            tool = reg.get(name)
            if tool is None:
                continue
            items.append(
                {
                    "name": tool.name,
                    "risk": tool.risk,
                    "description": (tool.description or "").strip(),
                    "approval": name in approval,
                    "params": sorted((tool.parameters or {}).get("properties", {}).keys()),
                }
            )
        return {"tools": items, "count": len(items)}

    # ---------- 任务台账 ----------
    def _cleanup_snapshot(self) -> dict:
        """清空前给界面看的清单：能删几条任务、哪些文件、几条记忆。

        这里只做「统计 + 预览」，绝不删东西——删是用户在弹窗里勾完确认之后的事。
        """
        finished = self.tasks.finished()
        ids = [t.id for t in finished]

        files = artifact.describe(self.tasks.files_of(ids), self.cfg.workspace_root)
        # 记忆文件本身不进「文件」清单，它由独立的「记忆」勾选项管
        mem_file = str(self.mem.path.resolve()).lower()
        files = [f for f in files if str(Path(f["path"]).resolve()).lower() != mem_file]
        created = [f for f in files if f["action"] == "created" and f["exists"]]
        modified = [f for f in files if f["action"] == "modified" and f["exists"]]

        mem_items = [e for e in self.mem.entries() if e.get("task") in set(ids)]

        return {
            "tasks": len(finished),
            "running": self.tasks.running_count(),
            "created": {"count": len(created), "items": created[:200]},
            "modified": {"count": len(modified), "items": modified[:200]},
            "memory": {
                "count": len(mem_items),
                "items": mem_items[:200],
                "path": str(self.mem.path),
            },
        }
    def _handle_tasks_get(self):
        query = urlparse(self.path).query
        tid = ""
        cleanup = False
        for part in query.split("&"):
            if part.startswith("id="):
                tid = part[3:]
            elif part == "cleanup=1":
                cleanup = True
        if tid:
            task = self.tasks.get(tid)
            if task is None:
                self._json({"error": "没有找到该任务"}, 404)
                return
            self._json({"task": task})
            return
        payload = {
            "tasks": self.tasks.list(),
            "running": self.tasks.running_count(),
        }
        if cleanup:
            payload["cleanup"] = self._cleanup_snapshot()
        self._json(payload)

    # ---------- 清理（按用户在弹窗里勾的项执行） ----------
    def _purge_files(self, paths: list[str]) -> dict:
        """删除任务产出的文件。

        安全边界：只删工作区内的文件，越界的（理论上不该出现）直接跳过并报回来。
        这里不存在「删目录」——台账记的都是文件。
        """
        root = Path(self.cfg.workspace_root).resolve()
        removed, failed, skipped = [], [], []
        for raw in paths:
            p = Path(str(raw))
            try:
                resolved = p.resolve()
            except OSError:
                failed.append({"path": str(raw), "why": "路径无法解析"})
                continue
            try:
                resolved.relative_to(root)
            except ValueError:
                skipped.append({"path": str(raw), "why": "在工作区之外，已跳过"})
                continue
            if not resolved.exists():
                skipped.append({"path": str(raw), "why": "文件已不存在"})
                continue
            if resolved.is_dir():
                skipped.append({"path": str(raw), "why": "是目录，不删"})
                continue
            try:
                resolved.unlink()
                removed.append(str(resolved))
            except OSError as e:
                failed.append({"path": str(resolved), "why": str(e)})
        return {"removed": removed, "failed": failed, "skipped": skipped}

    def _handle_tasks_post(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        action = req.get("action") or "list"

        if action == "clear":
            return self._do_clear(req)

        if action == "stop":
            tid = str(req.get("id") or "")
            if not tid:
                # 没指定就停掉当前所有在跑的任务（界面上只有一个停止按钮）
                ids = runcontrol.running_ids()
                hit = [i for i in ids if runcontrol.request_stop(i)]
                self._json({"ok": bool(hit), "stopped": hit,
                            "note": f"已请求停止 {len(hit)} 个任务"})
                return
            ok = runcontrol.request_stop(tid)
            self._json({"ok": ok, "stopped": [tid] if ok else [],
                        "note": "已请求停止，当前这轮生成结束后即退出。"
                                if ok else "该任务不在执行中（可能已结束）"})
            return
        self._json({"error": f"未知动作 {action}"}, 400)

    def _do_clear(self, req: dict) -> None:
        """按勾选项清理：对话记录 / 任务新建的文件 / 任务改过的文件 / 任务记忆。

        设计成「各勾各的」而不是一键全清：多数时候用户只想清对话，
        而文件是他真正的工作产出，不该被顺手删掉。
        """
        history = bool(req.get("history", True))
        wipe_created = bool(req.get("created", False))
        wipe_modified = bool(req.get("modified", False))
        wipe_memory = bool(req.get("memory", False))

        finished = self.tasks.finished()
        ids = [t.id for t in finished]
        removed_files = {"removed": [], "failed": [], "skipped": []}

        if wipe_created or wipe_modified:
            snap = self._cleanup_snapshot()
            targets = []
            if wipe_created:
                targets += [f["path"] for f in snap["created"]["items"]]
            if wipe_modified:
                targets += [f["path"] for f in snap["modified"]["items"]]
            removed_files = self._purge_files(targets)
            # 文件已经删了，台账里的清单要跟着清，免得界面还列着幽灵文件
            self.tasks.drop_files(ids)

        dropped_mem = self.mem.drop_tasks(ids) if wipe_memory else 0
        removed_tasks = self.tasks.clear() if history else 0

        parts = []
        if history:
            parts.append(f"删除 {removed_tasks} 条任务记录")
        if removed_files["removed"]:
            parts.append(f"清理 {len(removed_files['removed'])} 个文件")
        if dropped_mem:
            parts.append(f"清理 {dropped_mem} 条记忆")
        if removed_files["failed"]:
            parts.append(f"{len(removed_files['failed'])} 个文件删除失败")
        if removed_files["skipped"]:
            parts.append(f"{len(removed_files['skipped'])} 个跳过")
        if not parts:
            parts.append("没有可清理的内容")

        self._json(
            {
                "ok": True,
                "note": "已" + "，".join(parts),
                "removed": removed_tasks,
                "files": {
                    "removed": removed_files["removed"][:200],
                    "failed": removed_files["failed"][:50],
                    "skipped": removed_files["skipped"][:50],
                },
                "memory_dropped": dropped_mem,
                "tasks": self.tasks.list(),
                "running": self.tasks.running_count(),
                "cleanup": self._cleanup_snapshot(),
            }
        )

    # ---------- 可用模型 ----------
    def _discover(self) -> list[str]:
        now = time.time()
        if now - _Handler._discovered_at > _DISCOVER_TTL:
            try:
                _Handler._discovered = self.cli.client.list_models()
            except Exception:  # noqa: BLE001
                _Handler._discovered = []
            _Handler._discovered_at = now
        return _Handler._discovered

    def _probe_model(self, name: str) -> dict:
        """问 Ollama 要某个本地模型的元信息（主要是原生上下文长度）。

        只有「Ollama 里有、config.yaml 里没声明」的模型会走到这里，而且每个模型
        只问一次（结果缓存进 models.local.json），所以这点耗时可以接受。这样用户 pull
        完新模型直接勾选就能用，不必自己去查文档填 num_ctx。
        """
        try:
            info = self.cli.client.model_info(name)
        except Exception:  # noqa: BLE001
            return {}
        return {"native_ctx": native_context(info)}

    def _models_snapshot(self) -> dict:
        snap = self.store.snapshot(self._discover(), probe=self._probe_model)
        snap["pool"] = self.store.summary_line()
        snap.update(self._prompt_budget())
        return snap

    def _prompt_budget(self) -> dict:
        """每轮请求的固定开销（系统提示 + 全部工具 schema）token 估算。

        这部分每轮都会随请求发出去，但它不属于对话历史，前端从 history 里
        看不到；前端以前写死 1200，而实测 35 个工具 + 系统提示约 5300 tokens，
        导致「上下文占比」被少报三四倍。所以由后端算好直接给前端。

        用浅拷贝而不是直接改 self.cli.agent：agent 是全局单例，正在跑的任务
        也用它，改它的 model_key 会把并发请求带偏。
        """
        import copy as _copy

        # 常规轮次实际下发的是「核心工具 + 按需命中」的那一小撮，外加一段工具
        # 名字索引。这里按这个基线算，而不是按 35 个工具的全量算——否则前端
        # 会高估固定开销（按需下发之后每轮省下来的那部分正是收益所在）。
        sel = getattr(self.cli.registry, "selector", None)
        if sel is not None:
            tool_tok = int(round(est_tokens(json.dumps(sel.core_schemas(), ensure_ascii=False))))
            index_tok = int(round(est_tokens(sel.index_text())))
        else:
            tool_tok = int(round(est_tokens(json.dumps(self.cli.registry.schemas(), ensure_ascii=False))))
            index_tok = 0
        all_tok = int(round(est_tokens(json.dumps(self.cli.registry.schemas(), ensure_ascii=False))))
        proto = _copy.copy(self.cli.agent)
        system: dict[str, int] = {}
        for key in self.cfg.models:
            proto.model_key = key
            proto.system_prompt = ""
            try:
                system[key] = int(round(est_tokens(proto._system())))
            except Exception:  # noqa: BLE001  某个模型配置坏了不该拖垮整个列表
                system[key] = 0
        base = tool_tok + index_tok
        return {
            "prompt_tools": base,          # 常规轮次的工具部分（核心 schema + 名字索引）
            "prompt_tools_core": tool_tok,
            "prompt_index": index_tok,
            "prompt_tools_all": all_tok,   # 全量下发时的大小，用来说明省了多少
            "prompt_system": system,
            "prompt_tokens": {k: v + base for k, v in system.items()},
        }

    def _handle_models(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        action = req.get("action") or "list"
        note = ""
        try:
            if action == "enable":
                ids = [str(i) for i in (req.get("ids") or [])]
                if not ids:
                    self._json({"error": "至少要勾选一个模型"}, 400)
                    return
                self.store.set_enabled(ids)
                note = f"已启用 {len(ids)} 个模型"

            elif action == "add_paid":
                provider = str(req.get("provider") or "custom")
                model_id = str(req.get("model_id") or "").strip()
                api_key = str(req.get("api_key") or "").strip()
                if not model_id:
                    self._json({"error": "请填写模型号"}, 400)
                    return
                if not api_key:
                    self._json({"error": "请填写 API Key"}, 400)
                    return
                entry = self.store.add_paid(
                    provider=provider,
                    model_id=model_id,
                    api_key=api_key,
                    base_url=str(req.get("base_url") or ""),
                    label=str(req.get("label") or ""),
                    num_ctx=int(req.get("num_ctx") or 32000),
                )
                note = f"已添加付费模型 {entry.display}，并加入当前组合"

            elif action == "remove_paid":
                eid = str(req.get("id") or "")
                ok = self.store.remove_paid(eid)
                note = "已删除该付费模型" if ok else "没有找到该模型"

            elif action == "apply_best":
                picks = self.store.apply_best(int(req.get("budget") or 0) or None)
                note = "已应用最优配置：" + "、".join(e.display for e in picks)

            elif action == "apply_best_local":
                picks = self.store.apply_best_local(int(req.get("budget") or 0) or None)
                if not picks:
                    self._json({"error": "没有可用的本地模型（Ollama 里没发现任何本地模型）"}, 400)
                    return
                note = ("已应用本地最佳配置（仅本地模型，付费模型已全部取消勾选）："
                        + "、".join(e.display for e in picks))

            elif action == "test":
                eid = str(req.get("id") or "")
                entry = self.store.find(eid)
                if entry is None:
                    self._json({"error": "没有找到该模型"}, 404)
                    return
                spec = self.cfg.models.get(entry.key)
                ok, msg = self.cli.hub.ping_spec(spec) if spec else (False, "模型配置缺失")
                self._json({"ok": ok, "message": msg, "snapshot": self._models_snapshot()})
                return

            elif action == "set_scores":
                eid = str(req.get("id") or "")
                self.store.set_scores(eid, req.get("scores") or {})
                note = "已更新该模型的维度得分"

            else:
                self._json({"error": f"未知动作 {action}"}, 400)
                return
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return

        payload = self._models_snapshot()
        payload["note"] = note
        self._json(payload)

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/compact":
            self._handle_compact()
            return
        if path == "/api/models":
            self._handle_models()
            return
        if path == "/api/tasks":
            self._handle_tasks_post()
            return
        if path == "/api/prefs":
            self._handle_prefs()
            return
        if path == "/api/memory":
            self._handle_memory()
            return
        if path == "/api/open-folder":
            self._handle_open_folder()
            return
        if path == "/api/upload":
            self._handle_upload()
            return
        if path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        message = (req.get("message") or "").strip()
        if not message:
            self._json({"error": "message 为空"}, 400)
            return

        history = req.get("history") or []
        auto_plan = bool(req.get("auto_plan", True))
        model_key = (req.get("model_key") or "auto").strip()

        # 权限：优先用请求里显式的 permission；老前端只传 allow_write 时兼容处理；
        # 两者都没有（后台脚本、直接打 API）就用持久化的默认值——默认是完全访问。
        raw_perm = req.get("permission")
        if raw_perm is None:
            if "allow_write" in req:
                permission = "full" if bool(req.get("allow_write")) else "default"
            else:
                permission = self.prefs.permission
        else:
            permission = "full" if str(raw_perm) == "full" else "default"
        auto_approve = permission == "full"

        cli = self.cli

        # 任务台账：这次对话先登记成 running，跑完再落最终状态。
        # 前端侧栏的「任务管理」靠它显示正在执行的任务，所以 SSE 第一帧就推 task。
        # 模型名交给后面的 assign 事件补（那里给的是给人看的展示名）。
        started_at = time.time()
        task_id = self.tasks.begin(message, permission=permission)
        runcontrol.set_current_task(task_id)   # 让 remember 工具知道记忆归属哪个任务
        stop_ev = runcontrol.register(task_id)
        file_log: list[dict] = []              # 写类工具动过的文件
        # 开工前拍一张工作区索引，收工时再拍一张相减，兜住 run_command 的副作用
        try:
            tree_before = artifact.snapshot(self.cfg.workspace_root)
        except Exception:  # noqa: BLE001
            tree_before = {}
        models: list[str] = []
        final_history: list[dict] = []
        tool_calls = 0
        steps = 0
        summary = ""
        err_msg = ""
        was_stopped = False
        # 用量累计：界面上的「已消耗预估费用」就是这几个变量
        prompt_tokens = 0
        completion_tokens = 0
        cost_total = 0.0
        free_only = True
        # 进度：round 是模型推理轮次，step_index/total 是编排步
        round_now = 0
        step_index = 0
        step_total = 0

        def progress_text() -> str:
            if step_total > 1:
                return f"第 {step_index or 1}/{step_total} 步 · 第 {round_now or 1} 轮推理 · {tool_calls} 次工具"
            return f"第 {round_now or 1} 轮推理 · {tool_calls} 次工具"

        self._sse_headers()
        try:
            self._sse(
                "task",
                {
                    "id": task_id,
                    "title": message[:90],
                    "status": "running",
                    "created_at": started_at,
                    "permission": permission,
                },
            )
            for ev in run_task(cli.agent, self.store, history, message,
                               auto_plan=auto_plan, model_key=model_key,
                               stop_check=stop_ev.is_set,
                               auto_approve=auto_approve, file_log=file_log):
                kind = ev["type"]
                if kind == "delta":
                    self._sse("delta", {"text": ev["text"]})
                elif kind == "thinking":
                    self._sse("thinking", {"text": ev["text"]})
                elif kind == "step":
                    round_now = int(ev.get("step") or 0)
                    self.tasks.touch(task_id, model=ev.get("model") or "",
                                     progress=progress_text())
                    self._sse("step", {"step": round_now, "model": ev.get("model", ""),
                                       "progress": progress_text()})
                elif kind == "tool_start":
                    tool_calls += 1
                    self.tasks.touch(task_id, tool=True, progress=progress_text())
                    self._sse("tool_start", {"name": ev["name"], "args": ev["args"]})
                elif kind == "tool_result":
                    self._sse("tool_result", {"name": ev["name"], "output": ev["output"]})
                elif kind == "skill":
                    self._sse("skill", {"name": ev["name"], "detail": ev["detail"]})
                elif kind == "stats":
                    pt = int(ev.get("prompt_tokens") or 0)
                    ct = int(ev.get("tokens") or 0)
                    call_cost = float(ev.get("cost") or 0)
                    prompt_tokens += pt
                    completion_tokens += ct
                    cost_total += call_cost
                    if ev.get("free") is False:
                        free_only = False
                    self.tasks.touch(
                        task_id,
                        prompt_tokens=pt, completion_tokens=ct, cost=call_cost,
                        free=bool(ev.get("free", True)),
                        progress=progress_text(),
                    )
                    self._sse("stats", {
                        "tps": ev.get("tps", 0), "tokens": ct, "prompt_tokens": pt,
                        "model": ev.get("model", ""), "model_key": ev.get("model_key", ""),
                        "free": bool(ev.get("free", True)),
                        "cost": round(call_cost, 6),
                    })
                    # 累计用量：前端「已消耗预估费用」直接读这里，不用自己算
                    self._sse("usage", {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "tokens": prompt_tokens + completion_tokens,
                        "cost": round(cost_total, 6),
                        "local_only": free_only,
                        "tool_calls": tool_calls,
                        "round": round_now,
                        "progress": progress_text(),
                    })
                elif kind == "assistant":
                    summary = (ev["content"] or "").strip()
                    final_history = ev["history"]
                    self._sse("history", {"messages": ev["history"]})
                elif kind == "history":
                    # 中途停止时编排器主动回吐已完成的部分，让界面/台账不丢上下文
                    final_history = ev["messages"]
                    self._sse("history", {"messages": ev["messages"]})
                elif kind == "error":
                    err_msg = ev["message"]
                    self._sse("error", {"message": ev["message"]})
                elif kind == "combo":
                    self._sse("combo", {"combo": ev["combo"], "models": ev["models"]})
                elif kind == "plan":
                    step_total = len(ev["subtasks"])
                    self._sse("plan", {"subtasks": ev["subtasks"], "summary": ev["summary"]})
                elif kind == "assign":
                    name = ev.get("model_name") or ""
                    step_index = int(ev.get("index") or 0)
                    step_total = int(ev.get("total") or step_total)
                    if name and name not in models:
                        models.append(name)
                        self.tasks.touch(task_id, model=name)
                    self._sse("assign", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "subtask":
                    self._sse("subtask", {"index": ev["index"], "total": ev["total"], "text": ev["text"]})
                elif kind == "stopped":
                    was_stopped = True
                    self._sse("stopped", {
                        "steps": ev.get("steps", 0), "total": ev.get("total", step_total),
                        "reason": ev.get("reason", "用户停止"),
                    })
                elif kind == "complete":
                    steps = ev["steps"]
                    self._sse("complete", {"steps": ev["steps"], "multi": ev["multi"]})
        except Exception as e:  # noqa: BLE001
            err_msg = f"{type(e).__name__}: {e}"
            try:
                self._sse("error", {"message": err_msg})
            except Exception:
                pass
        finally:
            # 产出文件 = 工具明确写的 + 工作区索引两次相减（兜住命令副作用）
            try:
                after = artifact.snapshot(self.cfg.workspace_root)
                scanned = artifact.diff_snapshot(tree_before, after)
                merged = artifact.merge([*file_log, *scanned])
            except Exception:  # noqa: BLE001
                merged = file_log
            self.tasks.finish(
                task_id,
                "stopped" if was_stopped else ("error" if err_msg else "done"),
                summary=summary[:800],
                error=err_msg[:500],
                history=final_history or None,
                steps=steps,
                files=merged,
            )
            runcontrol.release(task_id)   # 摘掉停止标志，避免越攒越多
            runcontrol.clear_current_task()

            # 记忆自动沉淀：规则抽取立刻做（不花算力、用户明说要记的绝不漏），
            # 模型抽取放后台线程，不拖慢任务的收尾反馈。
            try:
                added = autonote.capture(self.mem, final_history or [], task_id)
                # 任务里模型自己调用 remember 写进去的也算这次沉淀的（工具侧已判重）
                by_tool = any(
                    isinstance(m, dict) and m.get("role") == "tool"
                    and str(m.get("content", "")).startswith("已写入长期记忆")
                    for m in (final_history or [])
                )
                total = len(added) + (1 if by_tool else 0)
                if total:
                    self._sse("memory", {
                        "added": total,
                        "items": [e.get("text", "") for e in added],
                        "source": "规则" if added else "工具",
                    })
            except Exception:  # noqa: BLE001
                pass
            try:
                spec = autonote.pick_extractor(self.store, self.cfg)
                if spec is not None and final_history:
                    threading.Thread(
                        target=autonote.capture_model,
                        args=(self.mem, self.cli.agent.hub, spec,
                              list(final_history), task_id),
                        daemon=True,
                    ).start()
            except Exception:  # noqa: BLE001
                pass

            try:
                self._sse("done", {})
            except Exception:
                pass


    # ---------- 记忆管理 ----------
    def _memory_snapshot(self) -> dict:
        items = self.mem.entries()
        # 倒序：最近写进来的排前面，管理页第一眼看到的是新东西
        items = list(reversed(items))
        return {
            "entries": items,
            "count": len(items),
            "path": str(self.mem.path),
        }

    def _handle_memory(self):
        """记忆管理：新增 / 删除单条 / 全部清空。"""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        action = (req.get("action") or "").strip()
        if action == "add":
            text = (req.get("text") or "").strip()
            if not text:
                self._json({"ok": False, "error": "内容为空"}, 400)
                return
            self.mem.append(text, "手动")
        elif action == "delete":
            try:
                idx = int(req.get("index"))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "index 无效"}, 400)
                return
            self.mem.drop_index(idx)
        elif action == "clear":
            self.mem.clear()
        else:
            self._json({"ok": False, "error": f"未知操作 {action}"}, 400)
            return
        snap = self._memory_snapshot()
        snap["ok"] = True
        self._json(snap)

    # ---------- 用户偏好 ----------
    def _handle_prefs(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        value = req.get("permission")
        if value is None:
            self._json(self.prefs.snapshot())
            return
        saved = self.prefs.set_permission(value)
        snap = self.prefs.snapshot()
        snap["ok"] = True
        snap["note"] = (
            f"以后新建的任务默认使用「{PERMISSION_LABEL.get(saved, saved)}」"
        )
        self._json(snap)

    # ---------- 上传附件 ----------
    def _handle_upload(self):
        """接收前端 base64 附件，落到工作区 .agentforge/uploads/ 供 Agent 读取。

        放在隐藏目录里是有意的：产出文件追踪会跳过隐藏目录，
        用户上传的素材不该被算成「这个任务产出的文件」。
        """
        import base64

        length = int(self.headers.get("Content-Length") or 0)
        if length > 40 * 1024 * 1024:
            self._json({"ok": False, "error": "文件过大（超过 25MB）"}, 413)
            return
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "invalid json"}, 400)
            return
        raw_name = str(req.get("name") or "upload.bin")
        # 只取文件名，挡掉 ../ 之类的路径穿越
        safe = Path(raw_name).name.replace("\\", "_").replace("/", "_")
        safe = safe.strip().strip(".") or "upload.bin"
        try:
            blob = base64.b64decode(req.get("data") or "", validate=False)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"解码失败：{exc}"}, 400)
            return
        if not blob:
            self._json({"ok": False, "error": "空文件"}, 400)
            return
        if len(blob) > 25 * 1024 * 1024:
            self._json({"ok": False, "error": "文件过大（超过 25MB）"}, 413)
            return

        root = Path(self.cfg.workspace_root).resolve()
        folder = root / ".agentforge" / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe
        # 同名不覆盖，改名 a-1.png、a-2.png …
        if target.exists():
            stem, suffix = target.stem, target.suffix
            i = 1
            while (folder / f"{stem}-{i}{suffix}").exists():
                i += 1
            target = folder / f"{stem}-{i}{suffix}"
        target.write_bytes(blob)

        rel = target.relative_to(root).as_posix()
        self._json(
            {
                "ok": True,
                "name": target.name,
                "path": rel,
                "abs": str(target),
                "size": len(blob),
            }
        )

    # ---------- 打开工作区文件夹（资源管理器） ----------
    def _handle_open_folder(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        # 安全：只允许打开工作区根目录本身，别的路径一律拒绝
        root = str(Path(self.cfg.workspace_root).resolve())
        wanted = str(req.get("path") or root)
        if Path(wanted).resolve() != Path(root):
            self._json({"ok": False, "error": "只允许打开工作区根目录"}, 403)
            return
        try:
            import os

            os.startfile(root)  # noqa: S606 - Windows 资源管理器
            self._json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)})

    # ---------- 手动压缩 ----------
    def _handle_compact(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        history = req.get("history") or []
        if not history:
            self._json({"messages": [], "note": "当前没有可压缩的对话", "changed": False})
            return
        try:
            messages, note = self.cli.agent.compress_now(history)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        self._json(
            {
                "messages": messages,
                "note": note,
                "size": self.cli.agent.context_size(messages),
                "threshold": self.cfg.compact_threshold,
            }
        )


def serve(cfg: Config, auto_approve: bool, port: int, host: str = "127.0.0.1"):
    cli = CLI(cfg, auto_approve=auto_approve)
    tasks = TaskStore(HERE / ".tasks.json")
    prefs = PrefsStore(HERE / ".prefs.json")
    mem = MemoryStore(Path(cfg.workspace_root) / ".agentforge" / "memory.md")
    # 上次进程退出时没来得及收尾的任务：标成「已中断」，别让界面一直转圈
    orphans = tasks.mark_orphaned()
    handler = type(
        "Handler",
        (_Handler,),
        {"cli": cli, "cfg": cfg, "store": cli.store, "tasks": tasks,
         "prefs": prefs, "mem": mem},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"AgentForge Web UI: {url}")
    print(f"工作区: {cfg.workspace_root}")
    print(f"Ollama: {cfg.ollama_host}  可用模型: {len(cli.store.enabled())}/{len(cli.store.catalog())} 已启用")
    print(f"当前组合: {cli.store.summary_line()}")
    print(f"新建任务默认权限: {PERMISSION_LABEL.get(prefs.permission, prefs.permission)}"
          f"（在界面上改权限会更新这份默认值）")
    print(f"任务台账: {HERE / '.tasks.json'}（历史 {len(tasks.list())} 条）")
    if mem.exists():
        print(f"工作区记忆: {mem.path}（{mem.count()} 条，新任务会自动读取）")
    if orphans:
        print(f"已把 {orphans} 条上次未收尾的任务标记为「已中断」")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, url
