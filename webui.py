"""零依赖 Web 界面（仅用标准库 http.server）。

启动：python run.py --web --port 8787
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agentforge import artifact, autonote, autoskill, gaps, interact, ollamaguard, reveal, runcontrol
from agentforge.cli import CLI
from agentforge.config import Config
from agentforge.evalsdesk import EvalsDesk
from agentforge.memory import MemoryStore
from agentforge.llm import ollama_probe, set_local_serial
from agentforge.modelstore import ModelStore
from agentforge.orchestrator import run_task
from agentforge.planner import short_title
from agentforge.prefs import PERMISSION_LABEL, PrefsStore
from agentforge.resume import repair_history, resume_brief
from agentforge.skills import write_meta
from agentforge.taskstore import TaskStore

HERE = Path(__file__).resolve().parent
INDEX = HERE / "web" / "index.html"

# 算力闸门（任务级）：同时「执行中」的任务上限，配置 compute.max_running（默认 1）。
# serve() 启动时按配置重建；超出上限的任务登记为「排队中」，有空位自动开跑。
# 注意：调大上限需要重启服务（信号量在启动时定容）。
_COMPUTE_GATE = threading.Semaphore(1)


class _QueuedCancelled(Exception):
    """排队等待算力期间被用户取消：事件已发、台账由 finally 落「已停止」。"""


def _is_local_deployment(base: Path | None = None) -> bool:
    """本地部署判定：项目根目录同时有 start.bat / stop.bat 即本地。

    这两个 Windows 脚本只在本机有用，云端镜像不会带（.dockerignore 排除了
    *.bat 与 service.py），所以「文件在不在」本身就是部署位置的权威信号——
    据此决定侧栏服务按钮是可点击的重启入口，还是云端常绿的只读指示。
    """
    root = Path(base) if base else HERE
    return (root / "start.bat").is_file() and (root / "stop.bat").is_file()


def _venv_interpreters() -> tuple[str, str]:
    """与 service.python_exe() 同一口径：优先 .venv，缺了退回当前解释器。

    返回 (python.exe, pythonw.exe)。重启链里直接点名这两个解释器，
    不再借道 start.bat 的 venv 探测——少一层间接，就少一类意外。
    """
    venv = HERE / ".venv"
    py = venv / "Scripts" / "python.exe"
    pyw = venv / "Scripts" / "pythonw.exe"
    if py.exists():
        return str(py), str(pyw if pyw.exists() else py)
    return sys.executable, sys.executable


def _restart_command(py: str | None = None, pyw: str | None = None) -> str:
    """重启命令行：先等 HTTP 响应发出去，再停旧的、等端口释放、拉起新的。

    为什么不再 call stop.bat / start.bat（上一版事故的三个根因）：
    1. 分离 cmd 用了 DETACHED_PROCESS，它会给每个控制台子进程开【可见】
       窗口——stop.bat 里的 `python service.py --stop` 每次都弹窗喊
       「服务没在运行」，用户关掉窗口杀掉的是子进程，保姆又拉起服务，
       「关了又弹」（service.py 顶部有同样的血泪注释）；
    2. `timeout /t /nobreak` 在无控制台输入时立即失败，响应先行的等待
       形同虚设 → 换 ping -n 做纯网络栈延时，不依赖任何控制台；
    3. stop.bat 末尾的 pause 靠 %cmdcmdline% 判断是否被双击启动，被
       cmd 链 call 进来时会误判而卡住 → 绕开 bat，直接调 service.py。
    """
    if not (py and pyw):
        dpy, dpyw = _venv_interpreters()
        py, pyw = py or dpy, pyw or dpyw
    return (
        'ping -n 3 127.0.0.1 >nul'          # ≈2s：先让这条 HTTP 响应发出去（stop 连本服务一起停）
        f' & "{py}" service.py --stop'
        ' & ping -n 4 127.0.0.1 >nul'       # ≈3s：等端口释放
        f' & start "" "{pyw}" service.py'   # start "" 的空标题参数防止路径被当成窗口名
    )


# 服务端重启单飞锁：重启是「会停掉本服务」的操作，HTTP 响应发出后前后端
# 都没有兜底。同一瞬间连点两次（或多开一个标签页再点）会让两轮 stop/start
# 链叠在一起互相残杀——上一轮弹窗风暴的放大器之一。60s 内的重复请求直接拒绝。
_RESTART_LOCK_TTL = 60.0


def _restart_locked(run_dir: Path | None = None) -> str | None:
    """写入重启锁；返回拒绝原因，None 表示放行。锁文件时间戳即发令时刻。"""
    run = Path(run_dir) if run_dir else (HERE / ".run")
    lock = run / "restart.lock"
    try:
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < _RESTART_LOCK_TTL:
                return f"上一次重启刚发起（{age:.0f} 秒前），请等它跑完（约 1 分钟）再试"
            lock.unlink(missing_ok=True)
        run.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()), encoding="ascii")
    except OSError:
        pass  # 锁失效（磁盘只读等）不应堵死重启，顶多退化成可叠加
    return None

# 从 Ollama 发现本地模型的动作不快也不慢，缓存几秒避免每次刷新都打一次
_DISCOVER_TTL = 5.0

# ⚠️ 临时关闭：任务收尾的「自动提取记忆」（规则抽取 + 模型提炼）整段停用。
#
# 为什么放在代码里而不是配置里：`agent.memory_autonote` 只在 webui 收尾那一处生效，
# 一旦换配置（config.yaml / config.lanrun.yaml）、换入口就悄悄恢复默认 True，
# 「关了又没关」正是它反复出现的原因。这里做成代码级短路——配置写 true 也不生效。
#
# 恢复办法：把下面的 True 改回 False（或删掉这个常量与两处 `and not _AUTONOTE_OFF`），
# 配置开关即恢复原有行为。`remember` 工具与侧栏「记忆管理」手工增删**不受影响**。
_AUTONOTE_OFF = True


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
        elif path == "/api/gaps":
            self._json(gaps.snapshot(self.cfg))
        elif path == "/api/evals":
            # 评测台：任务清单 + 历史报告 + 运行状态，一次拿全
            self._json({"tasks": self.evals.list_tasks(),
                        "reports": self.evals.list_reports(),
                        "state": self.evals.snapshot()})
        elif path == "/api/evals/status":
            self._json(self.evals.snapshot())
        elif path == "/api/evals/report":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            self._json(self.evals.read_report(name))
        elif path == "/api/tools":
            self._json(self._tools_snapshot())
        elif path == "/api/health":
            self._json({
                "ollama": self.cli.client.ping(),
                # 界面发现 ollama=false 时会去 /api/ollama/ensure 拉一把；
                # auto_start 关掉时就不发这个请求，别做无谓动作
                "ollama_auto_start": bool(self.cfg.ollama_auto_start),
                "ollama_host": self.cfg.ollama_host,
                "ollama_launcher": ollamaguard.launcher_label(),
                "ollama_last": ollamaguard.last_result(),
                # 本地部署才有重启一说：云端一律绿 + 不可点（见侧栏服务按钮）
                "local": _is_local_deployment(),
            })
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

    def _handle_gaps(self):
        """能力缺口台账：清空（全部或按 kind/key）、手工修订一条的名称与样例。

        记录侧由架构自动完成（loop 的未知工具、honesty_guard 的诚实缺口、
        bench_tasks 的评测失败归因），这里只负责管理动作。
        """
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        if (req.get("action") or "").strip() == "update":
            err = gaps.update(self.cfg,
                              kind=str(req.get("kind") or ""),
                              key=str(req.get("key") or ""),
                              new_key=str(req.get("new_key") or ""),
                              samples=req.get("samples"))
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            snap = gaps.snapshot(self.cfg)
            snap["ok"] = True
            self._json(snap)
            return
        removed = gaps.clear(self.cfg,
                             kind=str(req.get("kind") or ""),
                             key=str(req.get("key") or ""))
        self._json({"removed": removed, **gaps.snapshot(self.cfg)})

    # ---------- 评测台 ----------
    def _evals_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {"__error__": "invalid json"}

    def _handle_evals_run(self):
        """启动一轮评测：后台线程跑，前端轮询 /api/evals/status 看进度。"""
        req = self._evals_body()
        if "__error__" in req:
            self._json({"error": req["__error__"]}, 400)
            return
        self._json(self.evals.start_run(
            category=str(req.get("category") or ""),
            cases=str(req.get("cases") or ""),
            repeat=req.get("repeat") or 1,
            model=str(req.get("model") or ""),
            label=str(req.get("label") or ""),
        ))

    def _handle_evals_tasks(self):
        """整体保存评测任务清单（新增/修改/删除都在前端改好整体提交）。"""
        req = self._evals_body()
        if "__error__" in req:
            self._json({"error": req["__error__"]}, 400)
            return
        if str(req.get("action") or "") != "save":
            self._json({"error": "action 只支持 save"}, 400)
            return
        self._json(self.evals.save_tasks(req.get("cases") or []))

    def _handle_evals_suggest(self):
        """一键生成评测任务字段：用户只填任务指令，其余由决策模型产出。"""
        req = self._evals_body()
        if "__error__" in req:
            self._json({"error": req["__error__"]}, 400)
            return
        self._json(self.evals.suggest_case(str(req.get("prompt") or "")))

    def _handle_skills(self):
        """技能管理：删除单个技能 / 清空全部技能 / 编辑作用与触发边界。

        只动项目 skills/ 目录下的技能；删除前先整目录备份到 .run/，
        再移除并重扫注册表，让它立刻退出后续任务的自动匹配。
        编辑同样只对 skills/ 下的技能开放，改前也备份一份。
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

        if action == "update":
            name = (req.get("name") or "").strip()
            try:  # 技能可能刚落盘，先重扫再找
                reg.scan()
            except Exception:  # noqa: BLE001
                pass
            sk = reg.get(name)
            if sk is None:
                self._json({"ok": False, "error": "未找到该技能"}, 400)
                return
            if not _under(Path(sk.dirpath).resolve(), root):
                self._json({"ok": False, "error": "该技能不在项目 skills/ 下，不能在这里改"}, 400)
                return
            err = write_meta(Path(sk.dirpath),
                             description=str(req.get("description") or ""),
                             when_to_use=str(req.get("when_to_use") or ""),
                             when_not_to_use=str(req.get("when_not_to_use") or ""))
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            try:
                reg.scan()
            except Exception:  # noqa: BLE001
                pass
            snap = self._skills_snapshot()
            snap.update({"ok": True})
            self._json(snap)
            return

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
            # 算力闸门容量：前端据此判断「还有没有空闲算力」
            "max_running": self.max_running,
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
        """按勾选项清理：对话记录 / 磁盘文件 / 记忆条目。

        设计成「各勾各的」而不是一键全清：多数时候用户只想清对话，
        而文件是他真正的工作产出，不该被顺手删掉。

        前端把勾选结果摊平成两份清单（files / mem_indices）传进来，这里**逐条与
        清理快照比对**：只认快照里出现过的路径与记忆行号，认不出来的一律跳过。
        也就是说，删不删由用户在弹窗里勾出来的东西决定，接口本身不接受任意路径——
        即使有人直接调这个接口，也删不掉本次可清理范围之外的任何文件。
        旧前端只传布尔开关（created/modified/memory），仍按类别整片处理。
        """
        history = bool(req.get("history", True))
        raw_files = req.get("files")
        raw_mem = req.get("mem_indices")
        legacy = raw_files is None and raw_mem is None

        finished = self.tasks.finished()
        ids = [t.id for t in finished]
        removed_files = {"removed": [], "failed": [], "skipped": []}
        dropped_mem = 0

        root = Path(self.cfg.workspace_root).resolve()

        def _as_key(raw) -> str:
            """归一到「绝对路径 + 小写」，用于和快照比对、顺手去重。"""
            p = Path(str(raw))
            if not p.is_absolute():
                p = root / p
            try:
                return str(p.resolve()).lower()
            except OSError:
                return str(p).lower()

        if legacy:
            # 兼容旧前端：只给了类别开关，就按类别整片清（仍然只清过渡/中间文件）
            snap = self._cleanup_snapshot()
            targets = []
            if req.get("created"):
                targets += [f["path"] for f in snap["created"]["items"]]
            if req.get("modified"):
                targets += [f["path"] for f in snap["modified"]["items"]]
            if targets:
                removed_files = self._purge_files(targets)
                self.tasks.drop_file_paths(removed_files["removed"])
            if req.get("memory"):
                dropped_mem = self.mem.drop_tasks(ids)
        else:
            snap = self._cleanup_snapshot()
            # 允许删的路径 = 本次快照里的三类文件（过渡/中间、最终产出、被改动）
            allowed = {}
            for group in ("created", "final", "modified"):
                for f in snap[group]["items"]:
                    if f.get("path"):
                        allowed[_as_key(f["path"])] = f["path"]
            targets, rejected = [], []
            seen = set()
            for raw in (raw_files or []):
                key = _as_key(raw)
                real = allowed.get(key)
                if real is None:
                    rejected.append({"path": str(raw), "why": "不在本次可清理清单内，已跳过"})
                    continue
                if key in seen:      # 同一文件可能同时落在两个类别里
                    continue
                seen.add(key)
                targets.append(real)
            if targets:
                removed_files = self._purge_files(targets)
                # 只把已删除文件的记录从台账摘掉；没删的继续跟着任务走
                self.tasks.drop_file_paths(removed_files["removed"])
            removed_files["skipped"] += rejected
            if raw_mem:
                # 记忆按行号删，但只认「属于本次这些已结束任务」的行号：行号是前端
                # 打开弹窗那一刻文件里的位置，这里再跟当前文件对一次，免得删到别的
                # 任务写的记忆。
                allowed_mem = {e["index"] for e in self.mem.entries()
                               if e.get("task") in set(ids)}
                wanted = []
                for raw in raw_mem:
                    try:
                        n = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if n in allowed_mem:
                        wanted.append(n)
                dropped_mem = self.mem.drop_indices(wanted)

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
        # 一项都没勾时别说「已没有可清理的内容」——那听着像活儿全干完了
        note = "已" + "，".join(parts) if parts else "没有勾选任何要清理的内容，什么都没动"

        self._json(
            {
                "ok": True,
                "note": note,
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
        # Leader 双环的轮次配置（第 47 步：评估环上限 / 语义验收重试上限）
        snap["leader"] = {
            "max_rounds": int(getattr(self.cfg, "leader_max_rounds", 12)),
            "max_retries": int(getattr(self.cfg, "leader_max_retries", 2)),
        }
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

    def _write_leader(self, max_rounds: int, max_retries: int) -> bool:
        """把 Leader 双环轮次写回配置文件（leader: 块），尽量不动其它内容。"""
        p = getattr(self.cfg, "source_path", None)
        if not p:
            return False
        p = Path(p)
        keys = {"max_rounds": int(max_rounds), "max_retries": int(max_retries)}
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        lines = text.splitlines()
        missing = dict(keys)
        out: list[str] = []
        in_block = False
        for ln in lines:
            m = re.match(r'^(\s*)leader\s*:\s*(.*)$', ln)
            if m:
                in_block = True
                out.append(ln)
                continue
            if in_block:
                km = re.match(r'^(\s{2})([\w-]+)\s*:\s*(.*)$', ln)
                if km and km.group(2) in keys:
                    out.append(f"{km.group(1)}{km.group(2)}: {keys[km.group(2)]}")
                    missing.pop(km.group(2), None)
                    continue
                if ln.strip() and not ln.startswith('  '):
                    in_block = False
            out.append(ln)
        if missing:
            if any(re.match(r'^leader\s*:', ln) for ln in out):
                final = []
                for ln in out:
                    final.append(ln)
                    if re.match(r'^leader\s*:', ln):
                        for key in ("max_rounds", "max_retries"):
                            if key in missing:
                                final.append(f"  {key}: {missing[key]}")
                                missing.pop(key, None)
                out = final
            else:
                if out and out[-1].strip():
                    out.append("")
                out.append("leader:")
                for key in ("max_rounds", "max_retries"):
                    out.append(f"  {key}: {keys[key]}")
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

            elif action == "set_leader":
                # Leader 双环轮次配置：写回配置文件 + 热更新运行中的 cfg
                mr = int(req.get("max_rounds") or 0)
                mr = min(60, max(1, mr)) if mr else None
                xr = int(req.get("max_retries") or 0)
                xr = min(10, max(0, xr)) if req.get("max_retries") is not None else None
                if mr is not None:
                    self.cfg.leader_max_rounds = mr
                if xr is not None:
                    self.cfg.leader_max_retries = xr
                ok = self._write_leader(
                    self.cfg.leader_max_rounds, self.cfg.leader_max_retries)
                self._json({
                    "ok": ok,
                    "leader": {"max_rounds": self.cfg.leader_max_rounds,
                               "max_retries": self.cfg.leader_max_retries},
                    "snapshot": self._models_snapshot(),
                })
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
        if path == "/api/gaps":
            self._handle_gaps()
            return
        if path == "/api/evals/run":
            self._handle_evals_run()
            return
        if path == "/api/evals/stop":
            self._json(self.evals.stop())
            return
        if path == "/api/evals/tasks":
            self._handle_evals_tasks()
            return
        if path == "/api/evals/suggest":
            self._handle_evals_suggest()
            return
        if path == "/api/ollama/ensure":
            self._handle_ollama_ensure()
            return
        if path == "/api/service/restart":
            self._handle_service_restart()
            return
        if path == "/api/open-folder":
            self._handle_open_folder()
            return
        if path == "/api/ask-answer":
            # 前端澄清弹窗回传：唤醒挂起中的 ask_user 工具调用
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                req = {}
            n = interact.user_reply(str((req or {}).get("answer", "")))
            self._json({"ok": n > 0, "resolved": n})
            return
        if path == "/api/file-content":
            self._handle_file_content()
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
        history = req.get("history") or []

        # 会话恢复（断点续跑）：带 resume_of=<旧任务 id> 且不带正文时，从台账
        # 取回旧任务的对话历史，修复中断缝隙（悬空 tool_calls 补合成应答、
        # 孤儿 tool 消息丢弃）后作为初始上下文，正文换成「从中断处继续」的
        # 续跑指令。新任务单独登记一条台账，旧任务记录原样保留。
        display_prompt = ""       # 台账/界面展示用的任务原文（恢复时 = 原任务指令）
        resume_of = str(req.get("resume_of") or "").strip()
        if resume_of:
            old = self.tasks.get(resume_of)
            if not old:
                self._json({"error": "没有找到该任务，无法恢复"}, 404)
                return
            if old.get("status") in ("running", "queued"):
                self._json({"error": "该任务仍在执行中，不能恢复"}, 409)
                return
            old_hist = old.get("history") or []
            if len(old_hist) < 2:
                self._json({"error": "该任务没有可恢复的执行记录"}, 400)
                return
            history = repair_history(old_hist)
            old_prompt = str(old.get("prompt") or old.get("title") or "").strip()
            message = resume_brief(old_prompt)
            display_prompt = (old_prompt + "\n\n（恢复执行：上一次运行被中断，"
                              "本次从中断处继续。）")
        elif not message:
            self._json({"error": "message 为空"}, 400)
            return

        auto_plan = bool(req.get("auto_plan", True))
        multi_path = bool(req.get("multi_path", False))
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
        # 台账/界面展示用 display_prompt（恢复任务时 = 原任务指令 + 恢复标记），
        # 模型实际收到的是 message——两者在恢复场景下刻意不同。
        started_at = time.time()
        task_id = self.tasks.begin(display_prompt or message, permission=permission)
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
        # 算力闸门：先试占一个执行位；占不到就登记「排队中」，等有空位再开跑。
        # 位藏在 finally 里归还——无论跑完、报错还是排队中取消，都不漏还。
        got_slot = _COMPUTE_GATE.acquire(blocking=False)
        if not got_slot:
            self.tasks.set_status(task_id, "queued", progress="排队等待算力…")
        file_log: list[dict] = []              # 写类工具动过的文件
        # 开工前拍一张工作区索引，收工时再拍一张相减，兜住 run_command 的副作用
        try:
            tree_before = artifact.snapshot(self.cfg.workspace_root)
        except Exception:  # noqa: BLE001
            tree_before = {}
        models: list[str] = []
        final_history: list[dict] = []
        tool_calls = 0
        touched: list[str] = []                # 本任务调用过的工具名（判「有没有可能写过盘」）
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
                    # 用 chosen_key（本档实际选中的）而不是 best_key（全局最强）：
                    # 后果路由的价值就在「够用且更省」，不在于永远上最强。
                    preferred_key = cap_route.get("chosen_key") or cap_route["best_key"]
            except Exception:  # noqa: BLE001
                cap_route = None

        self._sse_headers()
        # ask_user 澄清弹窗：本线程进入交互模式——工具挂起等前端回传答复
        # （POST /api/ask-answer）；收尾 finally 里退出，防串到评测线程同线程复用
        interact.begin(interactive=True)
        try:
            emit(
                "task",
                {
                    "id": task_id,
                    "title": short_title(display_prompt or message, 90),
                    "status": "running" if got_slot else "queued",
                    "created_at": started_at,
                    "permission": permission,
                },
            )
            if not got_slot:
                # 排队等位：每 0.5 秒醒一次看停止标志，有空位立刻占上开跑。
                emit("queued", {"note": "当前算力不足，任务已排队；算力足够时自动执行"})
                while True:
                    if stop_ev.wait(0.5):        # 排队中被用户取消
                        was_stopped = True
                        break
                    if _COMPUTE_GATE.acquire(blocking=False):
                        got_slot = True
                        break
                if was_stopped:
                    emit("stopped", {"steps": 0, "total": 0, "reason": "排队中取消"})
                    raise _QueuedCancelled()
                self.tasks.set_status(task_id, "running")
                emit("task", {                   # 状态翻转再播一次：侧栏翻「执行中」
                    "id": task_id,
                    "title": short_title(display_prompt or message, 90),
                    "status": "running",
                    "created_at": started_at,
                    "permission": permission,
                })
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
                               auto_approve=auto_approve, file_log=file_log,
                               multi_path=multi_path):
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
                    # 记下这次任务调过哪些工具：收尾判断「本任务有没有可能写过盘」
                    # 要用它（见 artifact.may_have_written）。绝不靠「模型说它写了」。
                    touched.append(ev["name"])
                    self.tasks.touch(task_id, tool=True, progress=progress_text())
                    emit("tool_start", {"name": ev["name"], "args": ev["args"]})
                elif kind == "tool_result":
                    payload = {"name": ev["name"], "output": ev["output"]}
                    if ev.get("failed"):
                        payload["failed"] = True
                    emit("tool_result", payload)
                elif kind == "notify":
                    # 系统侧提示（确定性架构动作的结果，如「已存入记忆」）：
                    # 前端渲染成绿色 toast，reload 指明要刷新哪个面板
                    emit("notify", {"text": ev.get("text") or "",
                                    "kind": ev.get("kind") or "ok",
                                    "reload": ev.get("reload") or ""})
                elif kind == "multipath":
                    # 多路径尝试：先播「正在拟路径」，再逐条播每条路径的尝试。
                    # 用 thinking 通道播报，界面渲染成思考行，不打断正常回答流。
                    if ev.get("phase") == "plan":
                        emit("thinking", {
                            "text": f"第 {ev.get('index', '?')} 步未完成，"
                                    "正在拟定不同的实现路径，逐条尝试…",
                        })
                    else:
                        emit("thinking", {
                            "text": f"第 {ev.get('index', '?')} 步 · 方案 "
                                    f"{ev.get('approach_index', '?')}"
                                    f"/{ev.get('approach_total', '?')}："
                                    f"{ev.get('approach', '')}",
                        })
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
                elif kind == "subtask_error":
                    # 子任务级失败：**只播报，不改变整条任务的结局**（不写 err_msg）。
                    # 它的语义是「这一路由子任务自己失败了，整条任务照常往下走」：
                    # 并行跑的那批任务本来就按「单个崩了不拖垮其它」设计。
                    # 早先它沿用 error 事件名，外层于是把整条任务记成 error、
                    # 界面在回答中途就收尾（复制工具条挂在回答正中间）——见
                    # agentforge/orchestrator.py 里同一处改动的说明。
                    emit("subtask_error", {
                        "task_id": ev.get("task_id", ""),
                        "message": ev.get("message", ""),
                    })
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
                elif kind == "leader_round":
                    # Leader 双环每轮的规划/评估结论：原先是这里没分支被静默丢弃，
                    # 前端渲染函数早就备好（plan 卡上方的胶囊播报）——滚动规划
                    # 对用户可见就差这一行
                    emit("leader_round", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "bridge":
                    emit("bridge", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "conflict":
                    # 并行任务写到了同一个文件：写入已排队，这里提醒用户核对结果
                    emit("conflict", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "verify":
                    # 收尾验证闸门：系统亲自运行验证命令的真实结果（模型只有解读权）
                    emit("verify", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "artifacts":
                    # 产出文件清单：随最终答复送给前端，渲染成可点击展开的文件卡片，
                    # 保证汇总结果与产出文件对上
                    emit("artifacts", {k: v for k, v in ev.items() if k != "type"})
                elif kind == "stopped":
                    was_stopped = True
                    emit("stopped", {
                        "steps": ev.get("steps", 0), "total": ev.get("total", step_total),
                        "reason": ev.get("reason", "用户停止"),
                    })
                elif kind == "complete":
                    steps = ev["steps"]
                    emit("complete", {"steps": ev["steps"], "multi": ev["multi"]})
        except _QueuedCancelled:
            pass    # 排队中取消：stopped 事件已发，台账由下方 finally 落「已停止」
        except Exception as e:  # noqa: BLE001
            # 常见情形：浏览器关页/刷新把 SSE 连接断了，继续生成只是白烧 token。
            # 但如果已有接管者挂在总线上（别人还在看），就别停，让它接着跑。
            if bus.subscriber_count() == 0:
                runcontrol.request_stop(task_id)
            # 完整堆栈进服务日志——str(e) 只有异常名，排障时定位不到行号
            import traceback
            traceback.print_exc(file=sys.stderr)
            err_msg = f"{type(e).__name__}: {e}"
            try:
                emit("error", {"message": err_msg})
            except Exception:
                pass
        finally:
            interact.end()
            if got_slot:
                _COMPUTE_GATE.release()   # 归还执行位，让排队的下一个任务开跑
            # 产出文件 = 工具明确写的 + 工作区索引两次相减（兜住命令副作用）
            #
            # 相减那一半是**推测**，必须先过 may_have_written：本任务一次工具都没调
            # （纯问答）或只调了只读工具时，直接不扫。否则任务时间窗里任何别的进程
            # 顺手改的文件都会被算成本任务产出——实测踩过：一个纯问答任务被记上了
            # `docs/evolution.md`，只因跑它的时候恰好有编辑器在写这个文件，界面上
            # 就变成「它返回了一份自己没生成的文档」。
            try:
                reg = getattr(self.cli.agent, "registry", None)

                def _risk(name: str):
                    tool_def = reg.get(name) if reg is not None else None
                    return getattr(tool_def, "risk", None) if tool_def is not None else None

                if artifact.may_have_written(touched, _risk):
                    after = artifact.snapshot(self.cfg.workspace_root)
                    scanned = artifact.diff_snapshot(tree_before, after)
                else:
                    scanned = []
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
                    if (self.cfg.memory_autonote and not _AUTONOTE_OFF) else []
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
                    if (self.cfg.memory_autonote and not _AUTONOTE_OFF) else None
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
            "path": str(self.mem.path),
        }

    def _handle_memory(self):
        """记忆管理：新增 / 编辑单条 / 删除单条 / 全部清空。"""
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
        elif action == "update":
            try:
                idx = int(req.get("index"))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "index 无效"}, 400)
                return
            if not str(req.get("text") or "").strip():
                self._json({"ok": False, "error": "内容为空"}, 400)
                return
            if not self.mem.update(idx, str(req.get("text"))):
                self._json({"ok": False, "error": "该条记忆不存在（可能已被删除），刷新后重试"}, 400)
                return
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
        # 悬停卡片延迟（毫秒）：通用设置里调，立即生效并持久化
        if req.get("tip_delay") is not None:
            saved_delay = self.prefs.set_tip_delay(req.get("tip_delay"))
            snap = self.prefs.snapshot()
            snap["ok"] = True
            snap["note"] = f"悬停卡片延迟已设为 {saved_delay / 1000:g} 秒"
            self._json(snap)
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

    # ---------- 拉起 Ollama（本地模型的上游） ----------
    def _handle_ollama_ensure(self):
        """界面发现 Ollama 无响应时来敲一下，把本地模型的上游拉回来。

        幂等 + 带冷却：已经在跑就直接返回 already_up；刚试过没多久返回 cooldown，
        免得界面反复轮询把 ollama 进程刷爆（见 ollamaguard.RETRY_COOLDOWN）。
        用户明确点「立即拉起」时传 force=true 跳过冷却。
        """
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            req = {}
        self._json(ollamaguard.ensure(self.cfg, force=bool(req.get("force"))))

    def _handle_service_restart(self):
        """重启本地常驻服务（service.py --stop → pythonw service.py）。仅本地可用。

        重启链会把本 Web 服务一起停掉，所以真正动手的是**分离的** cmd
        进程：先 ping 等约 2s 让这条 HTTP 响应发出去，再执行停与启。
        页面随后靠 /api/health 轮询感知「失联（红褐色）→ 恢复（绿色）」。

        细节（每一条都是上一次事故换来的）：
        - CREATE_NO_WINDOW 而非 DETACHED_PROCESS：后者会给 cmd 的控制台
          子进程开可见窗口，用户看到的就是「不停弹窗说服务没在运行」；
        - 60s 单飞锁：连点/多标签页同时点的重复请求直接 429，不让两轮
          stop/start 叠加互相残杀；
        - 不经 stop.bat / start.bat：pause 误判卡链、timeout 无控制台
          即失效、DETACHED 弹窗，三个坑都出自 bat 转发层。
        """
        if not _is_local_deployment():
            self._json({"error": "云端部署：本地服务重启不可用"}, 400)
            return
        if os.name != "nt":
            self._json({"error": "本地服务重启仅适用于 Windows 部署"}, 400)
            return
        locked = _restart_locked()
        if locked:
            self._json({"error": locked}, 429)
            return
        try:
            subprocess.Popen(
                _restart_command(), cwd=str(HERE), shell=True,
                # CREATE_NO_WINDOW：隐藏控制台，子进程继承后也不再弹窗
                # （service.py 同款教训）；NEW_PROCESS_GROUP 不被连坐。
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"重启指令发起失败：{type(e).__name__}: {e}"}, 500)
            return
        self._json({"ok": True,
                    "note": "重启指令已发出：停旧服务 → 拉起新服务，页面稍候自动恢复"})

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
            # 选中模式：不打开文件本身，而是在资源管理器里定位它——
            # 评测报告的文件名点击走这里：直接抵达报告所在的文件夹并选中报告
            if req.get("select") and target.is_file():
                reveal.select_in_explorer(target)
                self._json({"ok": True, "note": "已在资源管理器中定位文件"})
                return
            # 打开之后还要把窗口抬到前台：Windows 的前台锁会让后台进程开的窗口
            # 停在后面，用户得再点一次任务栏才看得到（见 agentforge/reveal.py）。
            front = reveal.open_path(target)
            self._json({"ok": True, "front": front})
        except OSError:
            # 没有关联程序等打不开的情形：退一步在资源管理器里把文件定位出来，
            # 至少让用户拿到文件位置，而不是点了没反应。
            try:
                reveal.select_in_explorer(target)
                self._json({"ok": True, "note": "没有关联的打开方式，已在资源管理器中定位文件"})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)})

    # ---------- 读取文件内容（前端文件卡片「展开」内联预览用） ----------
    def _handle_file_content(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        # 安全：只允许工作区之内的文件，越界一律 403（与 _handle_open_folder 同口径）
        root = Path(self.cfg.workspace_root).resolve()
        wanted = str(req.get("path") or root)
        try:
            p = Path(wanted)
            if not p.is_absolute():
                p = root / p
            target = p.resolve()
            target.relative_to(root)            # 不在工作区内会抛 ValueError
        except (ValueError, OSError):
            self._json({"ok": False, "error": "只允许读取工作区内的文件"}, 403)
            return
        if not target.is_file():
            self._json({"ok": False, "error": "文件不存在（可能已被清理）"}, 404)
            return
        try:
            size = target.stat().st_size
            if size > 200_000:
                self._json({"ok": False, "error": "文件过大，无法内联预览",
                            "size": size})
                return
            text = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        cap = 50_000
        truncated = len(text) > cap
        self._json({"ok": True, "size": size, "truncated": truncated,
                    "content": text[:cap]})

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
    # 任务台账与偏好的落盘目录：默认跟代码走（项目根），容器/云部署用
    # AGENTFORGE_DATA_DIR 指到持久卷——否则容器一重建，任务历史就没了。
    data_dir = Path(os.environ.get("AGENTFORGE_DATA_DIR", "") or HERE)
    data_dir.mkdir(parents=True, exist_ok=True)
    tasks = TaskStore(data_dir / ".tasks.json")
    prefs = PrefsStore(data_dir / ".prefs.json")
    mem = MemoryStore(cfg.state() / "memory.md")
    # 上次进程退出时没来得及收尾的任务：标成「已中断」，别让界面一直转圈
    orphans = tasks.mark_orphaned()
    handler = type(
        "Handler",
        (_Handler,),
        {"cli": cli, "cfg": cfg, "store": cli.store, "tasks": tasks,
         "prefs": prefs, "mem": mem,
         "evals": EvalsDesk(cfg, hub=cli.agent.hub),
         "max_running": max(1, int(getattr(cfg, "compute_max_running", 1)))},
    )
    global _COMPUTE_GATE
    _COMPUTE_GATE = threading.Semaphore(handler.max_running)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"AgentForge Web UI: {url}")
    print(f"工作区: {cfg.workspace_root}")
    # 本地模型的上游就是 Ollama：启动时先确保它在，否则界面一打开就是
    # 「Ollama 无响应」，而本地模型全废。已经在跑的话这里什么都不做。
    guard = ollamaguard.ensure(cfg)
    print(f"Ollama: {cfg.ollama_host}  可用模型: {len(cli.store.enabled())}/{len(cli.store.catalog())} 已启用")
    if guard["action"] != "already_up":
        print(f"本地模型: {guard['detail']}")
    print(f"当前组合: {cli.store.summary_line()}")
    print(f"新建任务默认权限: {PERMISSION_LABEL.get(prefs.permission, prefs.permission)}"
          f"（在界面上改权限会更新这份默认值）")
    print(f"任务台账: {data_dir / '.tasks.json'}（历史 {len(tasks.list())} 条）")
    if mem.exists():
        print(f"工作区记忆: {mem.path}（{mem.count()} 条，新任务会自动读取）")
    if orphans:
        print(f"已把 {orphans} 条上次未收尾的任务标记为「已中断」")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, url
