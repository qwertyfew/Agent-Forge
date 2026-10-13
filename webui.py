"""零依赖 Web 界面（仅用标准库 http.server）。

启动：python run.py --web --port 8787
"""

from __future__ import annotations

import json
import re
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agentforge import artifact, autonote, autoskill, runcontrol
from agentforge.cli import CLI
from agentforge.config import Config
from agentforge.memory import LAYER_LABEL, MemoryStore
from agentforge.llm import ollama_probe, set_local_serial
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


def _path_under(p: Path, base: Path) -> bool:
    """p 是否在 base 目录之下（is_relative_to 的宽松版，异常时退到字符串前缀）。"""
    try:
        return p.is_relative_to(base)
    except (ValueError, OSError):
        return str(p).startswith(str(base))


def _safe_resolve(p: Path) -> bool:
    """resolve 是否成功（失败返回 False，供过滤用）。"""
    try:
        p.resolve()
        return True
    except OSError:
        return False


class TaskBus:
    """任务事件广播器：让多个 SSE 订阅者看到同一条任务流。

    为什么需要：任务的事件流原本只有一个消费者——发起请求的那个 HTTP 连接。
    谁先连上谁独享，别人（典型：用户浏览器「接管」了 CLI/脚本发起的任务）
    只能靠 5 秒一次的轮询看进度条，看不到流式的思考与回复。

    现在每个运行中的任务挂一个 bus：
    - publish 把事件同时写进缓冲（供后来的订阅者重放）和每个订阅者的队列；
    - 发起方自己不经过 bus（直接写自己的连接），它断了也不会误伤别的订阅者；
    - 订阅者断开只是摘掉自己，不影响任务本身。
    """

    def __init__(self, max_buffer: int = 8000):
        self._max = max_buffer
        self._lock = threading.Lock()
        self._buffer: list[tuple[str, str]] = []       # (event, 已序列化的 data)
        self._subs: list[queue.Queue] = []

    def publish(self, event: str, data_json: str) -> None:
        with self._lock:
            self._buffer.append((event, data_json))
            if len(self._buffer) > self._max:
                # 留后一半：重放时丢的是最早的过程，task/combo/plan 等关键帧
                # 可能被截掉，前端反正有轮询兜底，可接受
                self._buffer = self._buffer[-(self._max // 2):]
            subs = list(self._subs)
        for q in subs:
            q.put((event, data_json))

    def subscribe(self) -> tuple[queue.Queue, list[tuple[str, str]]]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            backlog = list(self._buffer)
            self._subs.append(q)
        return q, backlog

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class _Handler(BaseHTTPRequestHandler):
    cli: "CLI" = None  # type: ignore[assignment]
    cfg: Config = None  # type: ignore[assignment]
    store: ModelStore = None  # type: ignore[assignment]
    tasks: TaskStore = None  # type: ignore[assignment]
    prefs: PrefsStore = None  # type: ignore[assignment]
    mem: MemoryStore = None  # type: ignore[assignment]
    # 运行中任务的事件总线（task_id -> TaskBus）；刚结束的挪进 done_buses 供
    # 迟到的接管者重放，数量封顶防止常驻内存
    buses: dict = {}
    done_buses: dict = {}
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
        elif path.startswith("/api/tasks/") and path.endswith("/stream"):
            # 接管者的事件流：先重放缓冲，再续播实时事件，直到 done
            tid = path[len("/api/tasks/"):-len("/stream")]
            self._handle_task_stream(tid)
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
    def _skills_root(self) -> Path:
        """受管理的技能根目录（项目 skills/）。

        只有这个目录下的技能才允许在网页上删除——用户级 ~/.agentforge/skills
        和额外挂进来的目录不归这里管，免得点一下清空把别处的东西也带走。
        """
        if self.cfg.skill_dirs:
            return Path(self.cfg.skill_dirs[0])
        return Path(self.cfg.workspace_root) / "skills"

    @staticmethod
    def _fmt_when(ts: float) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except (OSError, ValueError, OverflowError):
            return ""

    @staticmethod
    def _skill_when(sk) -> float:
        """技能的时间。

        提取得到的技能优先用 frontmatter 里的 learned_at（提取那一刻），
        没有就退回 SKILL.md 的 mtime；安装得到的技能就用 mtime（放进目录的时刻）。
        """
        for key in ("learned_at", "installed_at"):
            raw = getattr(sk, key, "") or ""
            if not raw:
                continue
            for pat in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return time.mktime(time.strptime(raw.strip(), pat))
                except ValueError:
                    continue
        try:
            return (sk.dirpath / "SKILL.md").stat().st_mtime
        except OSError:
            try:
                return sk.dirpath.stat().st_mtime
            except OSError:
                return 0.0

    def _skill_note(self, sk) -> str:
        """自动沉淀的技能里写着「自动沉淀于任务 XXX」，提出来给页面显示。"""
        try:
            raw = (sk.dirpath / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        m = re.search(r"自动沉淀于任务\s*([A-Za-z0-9_-]+)", raw)
        return f"来自任务 {m.group(1)}" if m else ""

    def _skills_snapshot(self) -> dict:
        reg = self.cli.skills
        # 每次都重扫：自动沉淀的 skill 是后台线程写盘后即时注册的，
        # 不重扫的话管理页要等重启才看得到新技能。
        try:
            reg.scan()
        except Exception:  # noqa: BLE001
            pass
        learned = autoskill.learned_dir(self.cfg)
        managed = self._skills_root().resolve()
        items = []
        for name, sk in reg.skills.items():
            path = Path(sk.dirpath).resolve()
            # 「提取得到」= 自检机制写出来的（落在 skills/learned/ 或 frontmatter
            # 标了 auto_learned）；其余都算「安装得到」——用户放进 skills/ 的、
            # 或从别处装进来的。
            try:
                in_learned = path.is_relative_to(Path(learned).resolve())
            except (ValueError, OSError):
                in_learned = str(path).startswith(str(learned))
            is_extracted = bool(in_learned or getattr(sk, "auto_learned", False))
            ts = self._skill_when(sk)
            items.append(
                {
                    "name": name,
                    "description": sk.description,
                    "path": str(sk.dirpath),
                    "resources": sk.resources(),
                    # 资产元数据：版本 + 触发边界（借鉴 TencentDB 的 Skill 定义模型）
                    "version": getattr(sk, "version", "") or "",
                    "when_to_use": getattr(sk, "when_to_use", "") or "",
                    "when_not_to_use": getattr(sk, "when_not_to_use", "") or "",
                    "auto": True,  # 当前实现里 skill 全部参与自动匹配
                    "source": "extracted" if is_extracted else "installed",
                    "learned": is_extracted,  # 兼容旧前端字段
                    "when": ts,
                    "when_text": self._fmt_when(ts),
                    "note": self._skill_note(sk) if is_extracted else "",
                    "managed": str(path).startswith(str(managed)),
                }
            )
        extracted = sorted(
            (x for x in items if x["source"] == "extracted"),
            key=lambda x: -x["when"],
        )
        installed = sorted(
            (x for x in items if x["source"] == "installed"),
            key=lambda x: -x["when"],
        )
        return {
            "skills": extracted + installed,
            "extracted": extracted,
            "installed": installed,
            "extracted_count": len(extracted),
            "installed_count": len(installed),
            "learned_count": len(extracted),  # 兼容旧前端字段
            "dirs": [str(d) for d in reg.dirs],
            "learned_dir": str(learned),
            "managed_root": str(self._skills_root()),
            "auto_load": self.cfg.skill_auto_load,
            "min_score": self.cfg.skill_min_score,
        }

    def _backup_skills(self, dirs: list[Path]) -> str:
        """删除前把整个技能目录复制到 .run/skills-backup-<时间戳>/。

        技能目录是用户手写和 Agent 沉淀混在一起的，网页面板点一下「全部清空」
        就没了；留一份本地备份，误删了能捞回来（.run/ 不入库，不污染仓库）。
        """
        import shutil

        dest = Path(self.cfg.workspace_root) / ".run" / time.strftime(
            "skills-backup-%Y%m%d-%H%M%S"
        )
        for d in dirs:
            try:
                shutil.copytree(d, dest / d.name, dirs_exist_ok=True)
            except OSError:
                pass
        return str(dest)

    def _handle_skills(self):
        """技能管理：删除单个技能 / 清空全部技能。

        只动项目 skills/ 目录下的技能；每次删除前先整目录备份到 .run/，
        再移除并重扫注册表，让它立刻退出后续任务的自动匹配。
        """
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        action = (req.get("action") or "").strip()
        reg = self.cli.skills
        learned = Path(autoskill.learned_dir(self.cfg)).resolve()
        root = self._skills_root().resolve()
        import shutil

        def _under(p: Path, base: Path) -> bool:
            try:
                return p.is_relative_to(base)
            except (ValueError, OSError):
                return str(p).startswith(str(base))

        if action == "delete":
            name = (req.get("name") or "").strip()
            try:  # 技能可能刚落盘（或刚被手动放进目录），先重扫再找
                reg.scan()
            except Exception:  # noqa: BLE001
                pass
            sk = reg.get(name)
            if sk is None:
                self._json({"ok": False, "error": "未找到该技能"}, 400)
                return
            if not _under(Path(sk.dirpath).resolve(), root):
                self._json({"ok": False, "error": "该技能不在项目 skills/ 下，不能在这里删"}, 400)
                return
            backup = self._backup_skills([Path(sk.dirpath)])
            try:
                shutil.rmtree(sk.dirpath, ignore_errors=True)
                reg.scan()
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
                return
            snap = self._skills_snapshot()
            snap.update({"ok": True, "backup": backup})
            self._json(snap)
            return

        if action in ("clear_all", "clear"):
            targets = [
                Path(sk.dirpath)
                for sk in list(reg.skills.values())
                if _under(Path(sk.dirpath).resolve(), root)
                and (action == "clear_all" or _under(Path(sk.dirpath).resolve(), learned))
            ]
            if not targets:
                snap = self._skills_snapshot()
                snap.update({"ok": True, "removed": 0, "backup": ""})
                self._json(snap)
                return
            backup = self._backup_skills(targets)
            removed = 0
            for d in targets:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
            try:
                reg.scan()
            except Exception:  # noqa: BLE001
                pass
            snap = self._skills_snapshot()
            snap.update({"ok": True, "removed": removed, "backup": backup})
            self._json(snap)
            return

        self._json({"ok": False, "error": f"未知操作 {action}"}, 400)

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
        # skills/ 目录也不进清理清单：技能是项目资产而不是任务产出，删它
        # 该走技能管理面板（那里有整目录备份）。实测教训：借鉴任务新建了
        # skills/<名字>/SKILL.md，被默认勾选的「任务新建的文件」顺手删掉，
        # 技能面板里就凭空消失了。所有挂进来的技能目录（含用户级）一并保护。
        skills_roots: list[Path] = []
        for d in [self._skills_root(), *(Path(x) for x in (self.cfg.skill_dirs or []))]:
            try:
                rp = Path(d).resolve()
            except OSError:
                continue
            if all(rp != s for s in skills_roots):
                skills_roots.append(rp)

        def _in_skills(p: dict) -> bool:
            try:
                rp = Path(p["path"]).resolve()
            except OSError:
                return False
            return any(_path_under(rp, sr) for sr in skills_roots)

        files = [f for f in files if str(Path(f["path"]).resolve()).lower() != mem_file]
        files = [f for f in files if not _in_skills(f)]
        created_all = [f for f in files if f["action"] == "created" and f["exists"]]
        modified = [f for f in files if f["action"] == "modified" and f["exists"]]
        # 任务新建的文件分两类：过渡/中间文件（tmp/草稿/命令副作用…，默认清理）
        # 与最终产出（报告/代码/文档…，默认保留）。用户说清空时别再把结果一起带走。
        created = [f for f in created_all if artifact.is_intermediate(f)]
        final = [f for f in created_all if not artifact.is_intermediate(f)]

        mem_items = [e for e in self.mem.entries() if e.get("task") in set(ids)]

        return {
            "tasks": len(finished),
            "running": self.tasks.running_count(),
            # 「created」键沿用旧名，但语义收窄为「过渡/中间文件」；最终产出单列
            "created": {"count": len(created), "items": created[:200]},
            "final": {"count": len(final), "items": final[:200]},
            "modified": {"count": len(modified), "items": modified[:200]},
            "memory": {
                "count": len(mem_items),
                "items": mem_items[:200],
                "path": str(self.mem.path),
            },
        }
    def _handle_task_stream(self, task_id: str):
        """接管者的 SSE：重放 + 续播一个任务的事件流。

        任务的发起连接是独占的，别人接不上；但每个事件都进了 TaskBus 的缓冲，
        这里先吐缓冲、再跟随订阅队列，网页就能看到和发起者一样的流式过程。
        订阅者断开只影响自己，不碰任务；任务结束后总线挪进 done_buses，
        迟到一会儿的接管者仍能重放，只是收不到「以后」的事件了。
        """
        bus = type(self).buses.get(task_id) or type(self).done_buses.get(task_id)
        if bus is None:
            self._json({"error": "该任务没有可重放的事件流"}, 404)
            return
        self._sse_headers()
        q, backlog = bus.subscribe()
        try:
            for event, data_json in backlog:
                self.wfile.write(f"event: {event}\ndata: {data_json}\n\n".encode("utf-8"))
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            # 缓冲里已经带着 done（任务在订阅前就结束了）：重放完直接收摊，
            # 否则下面的队列等待永远等不到第二个 done，连接只能挂到客户端超时
            had_done = any(e == "done" for e, _ in backlog)
            while not had_done:
                try:
                    event, data_json = q.get(timeout=15)
                except queue.Empty:
                    # 心跳：让浏览器与中间层知道连接还活着
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {event}\ndata: {data_json}\n\n".encode("utf-8"))
                self.wfile.flush()
                if event == "done":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 接管者关页/刷新，正常
        finally:
            bus.unsubscribe(q)

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
            # 补充产出文件的体积：前端回复结尾的文件卡片要显示大小。
            # 台账里的 path 可能是相对工作区的，先落到工作区再 stat。
            root = Path(self.cfg.workspace_root).resolve()
            for f in task.get("files") or []:
                try:
                    p = Path(str(f.get("path") or ""))
                    if not p.is_absolute():
                        p = root / p
                    f["size"] = p.stat().st_size
                except OSError:
                    f["size"] = None
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
            # 第二道防线：技能目录里的文件永远不从这里删（哪怕台账里混进来了）。
            # 技能的删除入口是技能管理面板，那里删除前有整目录备份。
            skill_roots = [self._skills_root(), *(Path(x) for x in (self.cfg.skill_dirs or []))]
            if any(_path_under(resolved, sr.resolve()) for sr in skill_roots
                   if _safe_resolve(sr)):
                skipped.append({"path": str(raw), "why": "属于技能目录，不在这里删（去技能管理面板）"})
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
                # 只清过渡/中间文件（「created」键已收窄为这一类），最终产出保留
                targets += [f["path"] for f in snap["created"]["items"]]
            if wipe_modified:
                targets += [f["path"] for f in snap["modified"]["items"]]
            removed_files = self._purge_files(targets)
            # 只把已删除文件的记录从台账摘掉；保留的最终产出继续跟着任务走
            self.tasks.drop_file_paths(removed_files["removed"])

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

    def _models_snapshot(self) -> dict:
        # probe 与 CLI 路径共用 llm.ollama_probe：新模型的原生窗口只问 Ollama 一次，
        # 结果落进 models.local.json，之后不再重复问。
        snap = self.store.snapshot(self._discover(), probe=ollama_probe(self.cli.client))
        snap["pool"] = self.store.summary_line()
        snap["routing"] = {
            "capability": bool(self.cfg.routing_capability),
            "allow_upgrade": bool(self.cfg.routing_allow_upgrade),
            "allow_paid_upgrade": bool(self.cfg.routing_allow_paid_upgrade),
            "local_serial": bool(getattr(self.cfg, "local_serial", True)),
            "mode": ("best" if (self.cfg.routing_capability
                                and self.cfg.routing_allow_upgrade
                                and self.cfg.routing_allow_paid_upgrade)
                     else "local" if (self.cfg.routing_capability
                                      and self.cfg.routing_allow_upgrade)
                     else "off"),
        }
        # 决策/调度模型（多 agent 的 Dispatcher）当前指向谁、能不能用
        snap["dispatcher"] = self._dispatcher_snapshot()
        snap.update(self._prompt_budget())
        return snap

    def _dispatcher_snapshot(self) -> dict:
        dkey = (getattr(self.cfg, "dispatcher_model", "") or "").strip()
        spec = None
        if dkey:
            spec = self.cfg.models.get(dkey) or next(
                (s for s in self.cfg.models.values() if s.name == dkey), None)
        kind = getattr(spec, "kind", "") if spec is not None else ""
        return {
            "enabled": bool(getattr(self.cfg, "dispatcher_enabled", False)),
            "model": dkey,
            "name": spec.display if spec is not None else "",
            "source": ("paid" if kind == "paid" else "local") if spec is not None else "none",
            "available": bool(getattr(spec, "available", False)) if spec is not None else False,
            "any_available": any(getattr(e, "available", False) for e in getattr(self.store, "_catalog", [])),
        }

    def _write_dispatcher_model(self, model: str) -> bool:
        """把决策/调度模型写回配置文件（agent.dispatcher.model）。已有则替换，没有则补块。

        用逐行扫描而不是整体 dump，避免把 config 里的注释和其它节全部重排。
        """
        p = getattr(self.cfg, "source_path", None)
        if not p:
            return False
        p = Path(p)
        if not p.exists():
            return False
        val = f'{model}'
        quoted = f'"{val}"'
        lines = p.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        in_disp = False
        replaced = False
        for ln in lines:
            if re.match(r'^(\s*)dispatcher\s*:\s*(.*)$', ln):
                in_disp = True
                out.append(ln)
                continue
            if in_disp and not replaced:
                if ln.strip() and not ln.startswith("    "):
                    in_disp = False                 # 遇到更浅的非空行 → 离开该块
                else:
                    mm = re.match(r'^(\s+)model\s*:\s*(.*)$', ln)
                    if mm:
                        out.append(f"{mm.group(1)}model: {quoted}")
                        replaced = True
                        continue
            out.append(ln)
        if not replaced:
            final: list[str] = []
            inserted = False
            for ln in out:
                final.append(ln)
                if not inserted and re.match(r'^agent\s*:\s*$', ln):
                    final.append("  dispatcher:")
                    final.append(f"    model: {quoted}")
                    inserted = True
            if not inserted:
                return False
            out = final
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True

    def _write_routing(self, capability: bool, allow_upgrade: bool,
                        allow_paid_upgrade: bool,
                        quality_threshold: int | None = None) -> bool:
        """把路由开关写回加载时用的配置文件（config.yaml 或 config.lanrun.yaml）。

        已有则替换、没有则插入到 routing: 块下，尽量不动其它内容与注释。
        返回是否成功写入。
        """
        p = getattr(self.cfg, "source_path", None)
        if not p:
            return False
        p = Path(p)
        if not p.exists():
            return False
        keys: dict[str, object] = {
            "capability": capability,
            "allow_upgrade": allow_upgrade,
            "allow_paid_upgrade": allow_paid_upgrade,
        }
        if quality_threshold is not None:
            keys["quality_threshold"] = int(quality_threshold)

        def _fmt(v: object) -> str:
            return str(v).lower() if isinstance(v, bool) else str(v)

        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        missing = dict(keys)
        out: list[str] = []
        in_routing = False
        for ln in lines:
            m = re.match(r'^(\s*)routing\s*:\s*(.*)$', ln)
            if m:
                in_routing = True
                out.append(ln)
                continue
            if in_routing:
                km = re.match(r'^(\s{2})([\w-]+)\s*:\s*(.*)$', ln)
                if km and km.group(2) in keys:
                    key = km.group(2)
                    out.append(f"{km.group(1)}{key}: {_fmt(keys[key])}")
                    missing.pop(key, None)
                    continue
                # 遇到更浅的非空行即离开 routing 块
                if ln.strip() and not ln.startswith('  '):
                    in_routing = False
            out.append(ln)
        # 缺的 key 插到 routing: 行之后
        if missing:
            final: list[str] = []
            for ln in out:
                final.append(ln)
                if re.match(r'^routing\s*:\s*(.*)$', ln):
                    for key in ("capability", "allow_upgrade",
                               "allow_paid_upgrade", "quality_threshold"):
                        if key in missing:
                            final.append(f"  {key}: {_fmt(keys.get(key, missing[key]))}")
                            missing.pop(key, None)
            out = final
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True

    def _write_local_serial(self, on: bool) -> bool:
        """把「本地模型串行执行」开关写回配置文件（放进 ollama: 块）。

        已有 local_serial 就替换，没有就插到 ollama: 行之后。返回是否成功写入。
        """
        p = getattr(self.cfg, "source_path", None)
        if not p:
            return False
        p = Path(p)
        if not p.exists():
            return False
        text = p.read_text(encoding="utf-8")
        line = f"  local_serial: {'true' if on else 'false'}"
        if re.search(r'^\s*local_serial\s*:', text, re.M):
            text = re.sub(r'^\s*local_serial\s*:.*$', line, text, flags=re.M)
        else:
            text = re.sub(r'^(ollama\s*:\s*\n)', r'\1' + line + "\n", text, count=1, flags=re.M)
        p.write_text(text, encoding="utf-8")
        return True

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

            elif action == "remove_model":
                # 统一的「移除模型」：付费模型删配置，本地模型顺带删掉本地文件
                eid = str(req.get("id") or "")
                delete_file = bool(req.get("delete_file", True))
                res = self.store.remove_model(
                    eid, delete_file=delete_file, client=self.cli.client
                )
                if not res.get("ok"):
                    self._json({"error": res.get("error", "移除失败")}, 400)
                    return
                if res.get("kind") == "local":
                    note = "已移除该本地模型"
                    if res.get("deleted_file"):
                        note += "（本地文件已删除）"
                    if res.get("removed_config"):
                        note += "，配置声明一并清除"
                else:
                    note = "已移除该付费模型"

            elif action == "set_local_serial":
                on = bool(req.get("on", True))
                self.cfg.local_serial = on
                set_local_serial(on)
                self._write_local_serial(on)
                note = "本地模型已改为串行执行（同时只跑一个）" if on \
                    else "本地模型已允许并行执行"

            elif action == "set_dispatcher":
                # 配置多 agent 的决策/调度模型（本地或在线都行；空=关闭，回退规则拆解）
                model = str(req.get("model") or "").strip()
                if model:
                    found = self.store.find_any(model) or next(
                        (e for e in self.store._catalog if e.name == model), None)
                    if found is None:
                        self._json({"error": f"未知模型：{model}"}, 400)
                        return
                    model = found.key        # 统一存 cfg.models 里的 key，Dispatcher 好查
                self.cfg.dispatcher_model = model
                self._write_dispatcher_model(model)
                if model:
                    spec = self.cfg.models.get(model)
                    note = "已把决策模型设为 " + (spec.display if spec else model)
                else:
                    note = "已关闭多 agent 调度（回退到规则拆解）"

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

            elif action == "auto_config":
                scope = str(req.get("scope") or "local")
                if scope == "best":
                    # 自动配置最佳模型：能力路由开，自动升级到最强（含付费模型）
                    self.store.apply_best()
                    flags = {"capability": True, "allow_upgrade": True,
                             "allow_paid_upgrade": True}
                    note = "已开启「按任务类型自动选模型」· 含付费最佳模型"
                else:
                    # 自动配置本地模型：能力路由开，自动升级到最强本地模型（不用付费）
                    self.store.apply_best_local()
                    flags = {"capability": True, "allow_upgrade": True,
                             "allow_paid_upgrade": False}
                    note = "已开启「按任务类型自动选模型」· 仅本地模型"
                self._write_routing(**flags)
                # 同步到运行中的配置与路由实例，免得重启才生效
                self.cfg.routing_capability = flags["capability"]
                self.cfg.routing_allow_upgrade = flags["allow_upgrade"]
                self.cfg.routing_allow_paid_upgrade = flags["allow_paid_upgrade"]
                r = self.cli.router
                r.capability_on = flags["capability"]
                r.allow_upgrade = flags["allow_upgrade"]
                r.allow_paid_upgrade = flags["allow_paid_upgrade"]

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
        if path == "/api/skills":
            self._handle_skills()
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
        # 任务事件广播：发起连接直写自己，同时所有事件进总线——网页接管
        # （/api/tasks/<id>/stream）靠它重放+续播，不再只有干巴巴的进度条
        bus = TaskBus()
        self.buses[task_id] = bus
        # 发起连接可能中途掉线（浏览器刷新、curl 超时）；掉线后任务不能死，
        # 事件继续进总线，前端可用 /api/tasks/<id>/stream 接管续播
        direct_dead = [False]

        def emit(event: str, data: dict):
            bus.publish(event, json.dumps(data, ensure_ascii=False))
            if not direct_dead[0]:
                try:
                    self._sse(event, data)
                except OSError:
                    direct_dead[0] = True  # 发起方掉线：之后只进总线

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

        # 能力路由：auto 模式下按任务所需能力维度选/建议模型。
        # 不做全局硬切换——只在明确偏弱且存在更强可用模型时升级；否则把决策
        # 透明地播报给用户，让他知道「为什么是这个模型、还有没有更好的」。
        cap_route = None
        preferred_key = ""
        if model_key == "auto" and getattr(self.cfg, "routing_capability", True):
            try:
                cap_route = self.cli.router.capability(message, self.store)
                if cap_route.get("upgrade"):
                    # 只当作「优先模型」上报，不直接钉死 model_key——
                    # 否则会挡住多 agent 调度（调度模型要按任务重新指派模型）。
                    preferred_key = cap_route["best_key"]
            except Exception:  # noqa: BLE001
                cap_route = None

        self._sse_headers()
        try:
            emit(
                "task",
                {
                    "id": task_id,
                    "title": message[:90],
                    "status": "running",
                    "created_at": started_at,
                    "permission": permission,
                },
            )
            if cap_route:
                emit("route", {
                    "kind": cap_route.get("kind"),
                    "dimension": cap_route.get("dimension"),
                    "dimension_label": cap_route.get("dimension_label"),
                    "chosen_key": cap_route.get("chosen_key"),
                    "chosen_name": cap_route.get("chosen_name"),
                    "chosen_score": cap_route.get("chosen_score"),
                    "best_key": cap_route.get("best_key"),
                    "best_name": cap_route.get("best_name"),
                    "best_score": cap_route.get("best_score"),
                    "auto": cap_route.get("auto", False),
                    "reason": cap_route.get("reason", ""),
                    "notice": cap_route.get("notice", ""),
                })
            for ev in run_task(cli.agent, self.store, history, message,
                               auto_plan=auto_plan, model_key=model_key,
                               preferred_key=preferred_key,
                               stop_check=stop_ev.is_set,
                               auto_approve=auto_approve, file_log=file_log):
                kind = ev["type"]
                if kind == "delta":
                    emit("delta", {"text": ev["text"]})
                elif kind == "thinking":
                    emit("thinking", {"text": ev["text"]})
                elif kind == "step":
                    round_now = int(ev.get("step") or 0)
                    self.tasks.touch(task_id, model=ev.get("model") or "",
                                     progress=progress_text())
                    emit("step", {"step": round_now, "model": ev.get("model", ""),
                                       "progress": progress_text()})
                elif kind == "tool_start":
                    tool_calls += 1
                    self.tasks.touch(task_id, tool=True, progress=progress_text())
                    emit("tool_start", {"name": ev["name"], "args": ev["args"]})
                elif kind == "tool_result":
                    payload = {"name": ev["name"], "output": ev["output"]}
                    if ev.get("failed"):
                        payload["failed"] = True
                    emit("tool_result", payload)
                elif kind == "skill":
                    emit("skill", {"name": ev["name"], "detail": ev["detail"]})
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
                    emit("stats", {
                        "tps": ev.get("tps", 0), "tokens": ct, "prompt_tokens": pt,
                        "model": ev.get("model", ""), "model_key": ev.get("model_key", ""),
                        "free": bool(ev.get("free", True)),
                        "cost": round(call_cost, 6),
                    })
                    # 累计用量：前端「已消耗预估费用」直接读这里，不用自己算
                    emit("usage", {
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
                    emit("history", {"messages": ev["history"]})
                elif kind == "history":
                    # 中途停止时编排器主动回吐已完成的部分，让界面/台账不丢上下文
                    final_history = ev["messages"]
                    emit("history", {"messages": ev["messages"]})
                elif kind == "error":
                    err_msg = ev["message"]
                    emit("error", {"message": ev["message"]})
                elif kind == "combo":
                    # 这里要显式带上附注字段：前端靠 window_notice 提示窗口跨度、
                    # 靠 pool_notice 提示"已自动改用可用模型"。原先只转发 combo+models，
                    # 两条提示都收不到。
                    payload = {"combo": ev["combo"], "models": ev["models"]}
                    for extra in ("window_notice", "pool_notice"):
                        if ev.get(extra):
                            payload[extra] = ev[extra]
                    emit("combo", payload)
                elif kind == "plan":
                    step_total = len(ev["subtasks"])
                    emit("plan", {"subtasks": ev["subtasks"], "summary": ev["summary"]})
                elif kind == "assign":
                    name = ev.get("model_name") or ""
                    step_index = int(ev.get("index") or 0)
                    step_total = int(ev.get("total") or step_total)
                    if name and name not in models:
                        models.append(name)
                        self.tasks.touch(task_id, model=name)
                    emit("assign", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "subtask":
                    emit("subtask", {"index": ev["index"], "total": ev["total"], "text": ev["text"]})
                elif kind == "dispatch":
                    # 多 agent：决策模型给出的拆解与指派
                    emit("dispatch", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "task_done":
                    # 某个 agent 任务完成，前端据此把计划里的这一步标记为完成
                    emit("task_done", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "bridge":
                    emit("bridge", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "conflict":
                    # 并行任务写到了同一个文件：写入已排队，这里提醒用户核对结果
                    emit("conflict", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "verify":
                    # 收尾验证闸门：系统亲自运行验证命令的真实结果（模型只有解读权）
                    emit("verify", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "stopped":
                    was_stopped = True
                    emit("stopped", {
                        "steps": ev.get("steps", 0), "total": ev.get("total", step_total),
                        "reason": ev.get("reason", "用户停止"),
                    })
                elif kind == "complete":
                    steps = ev["steps"]
                    emit("complete", {"steps": ev["steps"], "multi": ev["multi"]})
        except Exception as e:  # noqa: BLE001
            # 常见情形：浏览器关页/刷新把 SSE 连接断了，继续生成只是白烧 token。
            # 但如果已有接管者挂在总线上（别人还在看），就别停，让它接着跑。
            if bus.subscriber_count() == 0:
                runcontrol.request_stop(task_id)
            err_msg = f"{type(e).__name__}: {e}"
            try:
                emit("error", {"message": err_msg})
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

            # 记忆自动沉淀（可用 memory_autonote: false 关闭）：规则抽取立刻做
            # （不花算力、用户明说要记的绝不漏），模型抽取放后台线程，不拖慢收尾。
            # 任务里模型自己调 remember 写记忆的通道不在这里，不受本开关影响。
            try:
                added = autonote.capture(self.mem, final_history or [], task_id) \
                    if self.cfg.memory_autonote else []
                # 任务里模型自己调用 remember 写进去的也算这次沉淀的（工具侧已判重）
                by_tool = any(
                    isinstance(m, dict) and m.get("role") == "tool"
                    and str(m.get("content", "")).startswith("已写入长期记忆")
                    for m in (final_history or [])
                )
                total = len(added) + (1 if by_tool else 0)
                if total:
                    emit("memory", {
                        "added": total,
                        "items": [e.get("text", "") for e in added],
                        "source": "规则" if added else "工具",
                    })
            except Exception:  # noqa: BLE001
                pass
            try:
                spec = autonote.pick_extractor(self.store, self.cfg) \
                    if self.cfg.memory_autonote else None
                if spec is not None and final_history:
                    hist = list(final_history)
                    # 记忆：后台提炼长期结论
                    threading.Thread(
                        target=autonote.capture_model,
                        args=(self.mem, self.cli.agent.hub, spec, hist, task_id),
                        daemon=True,
                    ).start()

                    # 技能：后台提炼可复用方法论；产出时发 skill 事件提示用户
                    def _run_autoskill():
                        try:
                            made = autoskill.capture_model(
                                self.cli.skills, self.cli.agent.hub, spec, hist,
                                task_id, self.cfg, autoskill.learned_dir(self.cfg),
                            )
                            for sk in made:
                                try:
                                    emit("skill", {
                                        "name": sk.get("name", ""),
                                        "detail": "自动沉淀技能：" + (sk.get("description", "")),
                                        "auto": True,
                                        "path": sk.get("path", ""),
                                    })
                                except Exception:  # noqa: BLE001
                                    pass
                        except Exception:  # noqa: BLE001
                            pass
                    threading.Thread(target=_run_autoskill, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass

            try:
                emit("done", {})
            except Exception:
                pass
            # 任务收摊：总线挪进 done_buses 供迟到的接管者重放，封顶防涨内存
            type(self).buses.pop(task_id, None)
            done = type(self).done_buses
            done[task_id] = bus
            if len(done) > 20:
                for k in list(done.keys())[:-20]:
                    done.pop(k, None)


    # ---------- 记忆管理 ----------
    def _memory_snapshot(self) -> dict:
        items = self.mem.entries()
        # 倒序：最近写进来的排前面，管理页第一眼看到的是新东西
        items = list(reversed(items))
        return {
            "entries": items,
            "count": len(items),
            # 分层统计：界面显示「偏好 N / 约定 N / 事实 N」，也便于核对分层是否生效
            "layers": self.mem.layer_counts(),
            "layer_labels": dict(LAYER_LABEL),
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
        folder = self.cfg.state() / "uploads"
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

        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            # 状态目录被配到了工作区之外。附件要能被智能体读到才算数，
            # 这里不崩、退回绝对路径，让用户从提示里看出问题。
            rel = str(target)
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
        # 安全：只允许打开工作区之内的文件/文件夹（工作区根目录也自然满足），
        # 越界路径一律 403。前端产出文件行点击后调这里。
        # 台账/前端传来的可能是相对工作区的路径，先落到工作区再解析。
        root = Path(self.cfg.workspace_root).resolve()
        wanted = str(req.get("path") or root)
        try:
            p = Path(wanted)
            if not p.is_absolute():
                p = root / p
            target = p.resolve()
            target.relative_to(root)          # 不在工作区内会抛 ValueError
        except (ValueError, OSError):
            self._json({"ok": False, "error": "只允许打开工作区内的文件"}, 403)
            return
        if not target.exists():
            self._json({"ok": False, "error": "文件不存在（可能已被清理）"}, 404)
            return
        try:
            import os
            import subprocess
            if hasattr(os, "startfile"):
                os.startfile(str(target))  # noqa: S606 - Windows 系统默认程序
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, str(target)])  # noqa: S603
            self._json({"ok": True})
        except OSError:
            # 没有关联程序等打不开的情形：退一步在资源管理器里把文件定位出来，
            # 至少让用户拿到文件位置，而不是点了没反应。
            try:
                subprocess.Popen(["explorer", "/select,", str(target)])  # noqa: S603
                self._json({"ok": True, "note": "没有关联的打开方式，已在资源管理器中定位文件"})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)})
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
    mem = MemoryStore(cfg.state() / "memory.md")
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
