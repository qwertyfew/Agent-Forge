"""评测台：Web UI 侧的评测集运行、报告生成与任务集管理。

为什么单独一个模块：评测运行器（tools/bench_tasks.py）是命令行工具，
跑一次几分钟到几十分钟，Web UI 不能在 HTTP 请求线程里等它。这里做的事：

1. **后台运行**：start_run() 起一个线程跑评测，状态进 self._state，
   前端轮询 /api/evals/status 拿进度；同一时刻只允许一个评测在跑。
   执行核心直接复用 bench_tasks.run_case_once（importlib 按文件加载，
   不复制代码——评测口径只能有一份）。
2. **报告**：跑完生成两份产物——JSON（与 bench_tasks 同 schema，
   bench_compare 可以直接吃）和 Markdown 报告（时间、版本、模型、
   逐条表现、失败归因分布、分类通过率），路径 evals/results/。
3. **任务集管理**：list_tasks / save_tasks 直接读写 evals/tasks.yaml。
   保存前三道校验：单条结构（id/prompt/expect）、受保护任务不许删
   （test_evals.py 钉死的诚实性簇与 policy 用例）、七大类不许清空。
   每次保存先把旧文件备份到 evals/.run/，写完再 load_cases 回读验证，
   验证失败自动回滚——评测集坏了好过静默漏测。
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import yaml

from .evals import load_cases

ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = ROOT / "evals" / "tasks.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
BACKUP_DIR = ROOT / "evals" / ".run"

# test_evals.py 用断言钉死的核心任务：删除/改名一律拒绝。
# 这是「删掉它测试会不会红」的清单——评测集的锚，不是 UI 的摆设。
PROTECTED_IDS = {"h1-send-email", "h2-bank-balance", "h3-npm-publish", "s1-system32"}

# test_evals.py 同时要求七大类齐全：清空任何一类都会让常驻回归变红
REQUIRED_CATEGORIES = {"coding", "fileops", "writing", "analysis",
                       "ops", "honesty", "policy"}

_CATEGORY_TITLES = {
    "coding": "编码", "fileops": "文件操作", "writing": "写作",
    "analysis": "分析", "ops": "运维", "honesty": "诚实性", "policy": "安全策略",
}


def _load_bench():
    """按文件路径加载 tools/bench_tasks.py（tools 不是包，不能常规 import）。"""
    spec = importlib.util.spec_from_file_location("af_bench_tasks",
                                                  ROOT / "tools" / "bench_tasks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_version() -> dict:
    """当前代码版本：commit + 工作区是否干净。评测报告用它回答
    「这是哪个版本的 harness 跑出来的」。git 不在/不是仓库时如实降级。"""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return {"commit": commit or "unknown", "dirty": bool(dirty)}
    except Exception:  # noqa: BLE001
        return {"commit": "unknown", "dirty": False}


class EvalsDesk:
    """单实例挂在 serve() 上，被所有 HTTP 请求线程共享。"""

    def __init__(self, cfg, hub=None):
        self.cfg = cfg
        # LLM 一键生成任务字段用（suggest_case）；测试/未注入时为 None，
        # 生成功能如实报「决策模型不可用」，不影响任务集管理与运行。
        self.hub = hub
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state: dict = {"running": False}
        # 启动即从最近一份结果恢复「上次评测」行：内存态随进程消失，
        # 但磁盘上的 JSON 是上一轮的真实成绩——重启不该让历史蒸发。
        self._hydrate_last()
        # 可注入的执行核心（tests 用替身替换，不碰真模型）
        self._runner = None

    _STD_JSON = re.compile(r"^[A-Za-z0-9_-]+-\d{8}-\d{6}\.json$")

    def _hydrate_last(self) -> None:
        """从 evals/results/ 最近一份标准命名 JSON 恢复上次评测的展示态。
        只填展示字段（results/finished_at/report/last_cases），
        running 恒为 False——重启前的运行早随旧进程结束了。"""
        if not RESULTS_DIR.is_dir():
            return
        try:
            jsons = [p for p in RESULTS_DIR.glob("*.json")
                     if self._STD_JSON.fullmatch(p.name)]
            jsons.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for p in jsons:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001  坏文件跳过，看更早的一份
                continue
            cases = data.get("cases") or []
            if not cases:
                continue
            meta = data.get("meta") or {}
            # 优先 meta.last_cases（完整计划批次，含中途停止未跑的）；
            # 旧 JSON 没有该字段，退回从 results 反推（跑完的子集）
            ids = [str(i) for i in (meta.get("last_cases")
                                    or [c.get("id") for c in cases])]
            self._state = {
                "running": False,
                "results": cases,
                "finished_at": meta.get("finished_at"),
                "report": p.stem + ".md",
                "json": p.name,
                "last_cases": [i for i in ids if i],
                "error": None,
            }
            return

    # ---------- 任务集 ----------
    def list_tasks(self) -> dict:
        raw = yaml.safe_load(TASKS_PATH.read_text(encoding="utf-8")) or {}
        cases = raw.get("cases") or []
        cats: dict[str, int] = {}
        for c in cases:
            k = str(c.get("category") or "general")
            cats[k] = cats.get(k, 0) + 1
        return {"cases": cases, "categories": cats,
                "protected": sorted(PROTECTED_IDS),
                "path": str(TASKS_PATH)}

    def save_tasks(self, cases: list[dict]) -> dict:
        """整体保存任务清单。校验 → 备份 → 写入 → 回读验证（失败回滚）。"""
        if not isinstance(cases, list) or not cases:
            return {"error": "任务清单为空：评测集至少要有一条任务"}
        seen: set[str] = set()
        cats: set[str] = set()
        for i, c in enumerate(cases):
            if not isinstance(c, dict):
                return {"error": f"第 {i + 1} 条不是对象"}
            cid = str(c.get("id") or "").strip()
            if not cid:
                return {"error": f"第 {i + 1} 条缺少 id"}
            if not str(c.get("prompt") or "").strip():
                return {"error": f"任务 {cid} 缺少 prompt"}
            if not (c.get("expect") or {}):
                return {"error": f"任务 {cid} 没有判定条件（expect 为空）——"
                                 "没有客观判定的任务不进评测集"}
            if not isinstance(c.get("expect"), dict):
                return {"error": f"任务 {cid} 的 expect 不是对象（JSON）"}
            if cid in seen:
                return {"error": f"任务 id 重复：{cid}"}
            seen.add(cid)
            cats.add(str(c.get("category") or "general"))
        lost = PROTECTED_IDS - seen
        if lost:
            return {"error": f"这些任务被常驻回归钉死，不能删除：{sorted(lost)}"}
        empty_cats = REQUIRED_CATEGORIES - cats
        if empty_cats:
            return {"error": f"这些分类被清空了（回归要求七大类齐全）：{sorted(empty_cats)}"}

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"tasks-backup-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
        backup.write_text(TASKS_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        new_text = _dump_tasks(cases)
        TASKS_PATH.write_text(new_text, encoding="utf-8")
        try:
            load_cases(TASKS_PATH)          # 回读验证：判定器吃得下才算数
        except Exception as e:  # noqa: BLE001
            backup_text = backup.read_text(encoding="utf-8")
            TASKS_PATH.write_text(backup_text, encoding="utf-8")
            return {"error": f"保存后校验失败，已回滚：{type(e).__name__}: {e}"}
        return {"ok": True, "count": len(cases), "backup": str(backup)}

    # ---------- 一键生成（LLM 出题助手） ----------
    def suggest_case(self, prompt: str) -> dict:
        """用户只填任务指令，其余字段（id/分类/seed/expect）由模型生成。

        模型产出必须过 _sanitize_suggestion 的收口：分类白名单、id 规范化、
        空判定拦截、核验命令危险动作过滤——生成是辅助，安全与「无判定不进
        评测集」的底线不跟着让步。没有可用模型/产出不可解析时如实报错，
        前端退回手动填写。
        """
        prompt = str(prompt or "").strip()
        if not prompt:
            return {"error": "任务指令为空：先填任务再生成"}
        spec = self._pick_suggest_model()
        if spec is None:
            return {"error": "没有可用模型：请先在设置里配置可用模型"}
        try:
            res = self.hub.chat(
                spec,
                [{"role": "system", "content": EVAL_SUGGEST_SYSTEM},
                 {"role": "user", "content": prompt}],
                tools=None,
                options={"temperature": 0.2, "num_predict": 2048},
                think=True,
            )
            raw = (res.content or "").strip()
        except Exception as e:  # noqa: BLE001
            return {"error": f"模型调用失败：{type(e).__name__}: {e}"}
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            return {"error": "模型没有产出可解析的 JSON，可再试一次或改用手动填写"}
        case = _sanitize_suggestion(obj, prompt)
        if case.get("error"):
            return case
        return {"case": case}

    def _pick_suggest_model(self):
        """选一个可用模型来做「任务建议生成」（原决策模型职责的替代）。"""
        if not self.hub:
            return None
        for spec in self.cfg.models.values():
            if getattr(spec, "available", False):
                return spec
        return None

    # ---------- 运行 ----------
    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def stop(self) -> dict:
        if not self._state.get("running"):
            return {"error": "当前没有评测在跑"}
        self._stop.set()
        return {"ok": True, "note": "当前任务跑完后停止，剩余任务跳过"}

    def start_run(self, *, category: str = "", cases: str = "",
                  repeat: int = 1, model: str = "", label: str = "",
                  timeout: int = 900, check_timeout: int = 60) -> dict:
        with self._lock:
            if self._state.get("running"):
                return {"error": "已有评测在跑，等它结束或先停止"}
        try:
            all_cases = load_cases(TASKS_PATH)
        except Exception as e:  # noqa: BLE001
            return {"error": f"任务集加载失败：{type(e).__name__}: {e}"}
        run_cases = all_cases
        if category:
            run_cases = [c for c in run_cases if c.category == category]
        want = {s.strip() for s in str(cases or "").split(",") if s.strip()}
        if want:
            run_cases = [c for c in run_cases if c.id in want]
        if not run_cases:
            return {"error": "没有匹配的任务"}
        reps = max(1, min(int(repeat or 1), 5))
        run_label = (label or "web").strip() or "web"

        self._stop.clear()
        with self._lock:
            self._state = {
                "running": True, "started_at": time.time(),
                "label": run_label, "model": model or "auto",
                "repeat": reps, "total": len(run_cases), "done": 0,
                "current": run_cases[0].id, "results": [],
                "error": None, "report": None, "json": None,
                "stop_requested": False, "skipped": 0,
                "last_cases": [c.id for c in run_cases],
            }
        threading.Thread(target=self._run, daemon=True, kwargs={
            "run_cases": run_cases, "reps": reps, "model": model,
            "label": run_label, "timeout": timeout,
            "check_timeout": check_timeout,
        }).start()
        return {"ok": True, "total": len(run_cases)}

    def _runner_fn(self):
        if self._runner is None:
            self._runner = _load_bench().run_case_once
        return self._runner

    def _run(self, *, run_cases, reps: int, model: str, label: str,
             timeout: int, check_timeout: int) -> None:
        st = self._state
        runner = self._runner_fn()
        results: list[dict] = []
        t_all = time.time()
        skipped = 0
        try:
            for case in run_cases:
                if self._stop.is_set():
                    skipped += 1
                    st["skipped"] = skipped
                    continue
                st["current"] = case.id
                recs = []
                for _ in range(reps):
                    if self._stop.is_set() and recs:
                        break
                    recs.append(runner(case, label, model, timeout, check_timeout))
                p = sum(1 for r in recs if r["verdict"] == "pass")
                final = dict(recs[-1])
                if reps > 1:
                    first_bad = next((r for r in recs if r["verdict"] == "fail"), None)
                    if first_bad is not None:
                        final.update({"verdict": "fail",
                                      "layer": first_bad.get("layer", "?"),
                                      "detail": first_bad.get("detail", "")})
                    final["pass_runs"] = p
                    final["runs"] = reps
                    final["passk"] = f"{p}/{reps}"
                results.append(final)
                st["done"] = len(results)
                st["results"] = [dict(r) for r in results]
                # 失败入缺口台账（与命令行 runner 同口径）
                if final["verdict"] != "pass":
                    from . import gaps
                    gaps.record("eval_fail", f"{case.category}/{case.id}",
                                f"失败层 {final.get('layer', '?')}: "
                                f"{str(final.get('detail', ''))[:120]}"
                                + (f"（pass^{reps}={p}/{reps}）" if reps > 1 else ""),
                                self.cfg, step=getattr(case, "prompt", ""))
        except Exception as e:  # noqa: BLE001
            st["error"] = f"{type(e).__name__}: {e}"

        finished = time.time()
        meta = {
            "label": label, "model": model or "auto",
            "config": str(ROOT / "config.yaml"), "tasks": str(TASKS_PATH),
            "repeat": reps, "cases_planned": len(run_cases),
            "skipped": skipped,
            # 完整计划批次：中途停止的运行，results 只是子集，
            # 「重跑上一次」要还原的是计划批次而不是跑完的残部
            "last_cases": [c.id for c in run_cases],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(st["started_at"])),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(finished)),
            "duration": round(finished - t_all, 1),
            "version": git_version(),
        }
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(finished))
        json_name = f"{label}-{stamp}.json"
        md_name = f"{label}-{stamp}.md"
        try:
            (RESULTS_DIR / json_name).write_text(
                json.dumps({"meta": meta, "cases": results},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            (RESULTS_DIR / md_name).write_text(
                build_report(meta, results), encoding="utf-8")
            st["json"] = json_name
            st["report"] = md_name
        except Exception as e:  # noqa: BLE001
            st["error"] = st["error"] or f"报告写入失败：{type(e).__name__}: {e}"

        st["running"] = False
        st["finished_at"] = meta["finished_at"]
        st["duration"] = meta["duration"]

    # ---------- 报告 ----------
    def list_reports(self) -> list[dict]:
        if not RESULTS_DIR.is_dir():
            return []
        out = []
        # 从新到旧：按修改时间倒序（文件名倒序是历史等价实现，报告名内嵌时间戳；
        # 改为 mtime 后手动放进来的非标准命名报告也保证新出的排第一条）
        mds = list(RESULTS_DIR.glob("*.md"))
        mds.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for p in mds:
            out.append({"name": p.name, "size": p.stat().st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(p.stat().st_mtime))})
        return out

    def read_report(self, name: str) -> dict:
        # 名字白名单：只认 results 目录下、不带路径分隔符的 .md
        safe = Path(name).name
        if safe != name or not name.endswith(".md"):
            return {"error": "非法的报告文件名"}
        p = RESULTS_DIR / safe
        if not p.is_file():
            return {"error": "报告不存在"}
        return {"name": safe, "markdown": p.read_text(encoding="utf-8")}


# ---------- 报告渲染 ----------
def _failure_signature(r: dict) -> str:
    """同因失败签名：check 层看核验输出（命令各不相同、输出往往相同），
    其余层看 detail 前缀。用于把「12 条各自失败」聚成「1 个环境问题」。"""
    data = r.get("data") or {}
    if r.get("layer") == "check" and data.get("check"):
        return "check :: " + str(data["check"].get("output") or "")[:100]
    return f"{r.get('layer', '?')} :: {str(r.get('detail', ''))[:100]}"


def failure_clusters(results: list[dict], min_size: int = 3) -> list[dict]:
    """把失败按签名聚合，返回条数 >= min_size 的簇（降序）。

    一批 check 失败共用同一句报错（如 exit=9009 Python 不在）时，那是
    环境问题不是模型问题——报告必须把它从 N 条独立失败里拎出来。
    """
    groups: dict[str, list[str]] = {}
    for r in results:
        if r.get("verdict") == "pass":
            continue
        groups.setdefault(_failure_signature(r), []).append(str(r.get("id", "?")))
    out = [{"signature": sig, "ids": ids, "count": len(ids)}
           for sig, ids in groups.items() if len(ids) >= min_size]
    out.sort(key=lambda c: -c["count"])
    return out


def _judge_lines(r: dict) -> list[str]:
    """判定层的原始数据（事实陈列，不做解读）：核验命令/输出、缺失片段等。"""
    lines: list[str] = []
    data = r.get("data") or {}
    layer = r.get("layer")
    if layer == "check" and data.get("check"):
        c = data["check"]
        lines.append(f"- 核验命令：`{c.get('cmd', '')}`（exit={c.get('code')}）")
        out = str(c.get("output") or "").strip()
        if out:
            lines.append(f"- 核验输出：{out}")
    elif layer in ("exact", "norm") and data.get(layer):
        missing = data[layer].get("missing") or []
        if missing:
            lines.append(f"- 缺少片段：{missing}")
    elif layer == "numeric" and data.get("numeric"):
        d = data["numeric"]
        lines.append(f"- 提取到 {d.get('found', [])[:10]}　缺失 {d.get('missing', [])}")
    elif layer == "fuzzy" and data.get("fuzzy"):
        d = data["fuzzy"]
        base = f"- 关键词组命中 {d.get('hit')}/{d.get('total')}（ratio {d.get('ratio')}）"
        if d.get("miss"):
            base += f"，未命中组：{d.get('miss')}"
        lines.append(base)
    elif layer == "must_not" and data.get("must_not"):
        lines.append(f"- 出现禁止内容：{data['must_not'].get('hit')}")
    return lines


def _process_lines(r: dict) -> list[str]:
    """过程记录：harness 处理这条任务的完整事实序列（所有用例都录，
    通过与否无关）。只按发生顺序陈列，不解读。"""
    lines: list[str] = []
    proc = r.get("process") or []
    if not proc:
        return lines
    lines.append(f"- 过程记录（按发生顺序，共 {len(proc)} 项）：")
    for p in proc[:200]:
        s = p.get("s", "?")
        k = p.get("k")
        if k == "tool":
            args = p.get("args")
            args_s = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args or "")
            L = f"  - 轮 {s} · {p.get('name', '?')} `{args_s}`"
            inner = p.get("inner") or []
            if inner:
                # PTC：真正的工具名在脚本里，报告里要露出来，否则读起来像「只调了个 ops_batch」
                L += "（脚本内：" + "、".join(dict.fromkeys(str(x) for x in inner)) + "）"
            out = str(p.get("out") or "").strip()
            if out:
                L += "\n    → " + out.replace("\n", "\n      ")
            lines.append(L)
        elif k == "skill":
            lines.append(f"  - 轮 {s} · skill 注入：{p.get('name', '')}（{p.get('detail', '')}）")
        elif k == "verify":
            lines.append(f"  - 轮 {s} · 系统核验")
        elif k == "gate":
            tag = "；打满放行" if p.get("exhausted") else ""
            lines.append(f"  - 轮 {s} · 核查闸门（第 {p.get('round', '?')} 轮{tag}）："
                         + "；".join(str(x) for x in (p.get("issues") or []))[:300])
        elif k == "error":
            lines.append(f"  - 轮 {s} · 事件流报错：{p.get('msg', '')}")
        elif k == "stopped":
            lines.append(f"  - 轮 {s} · 被停止")
    if len(proc) > 200:
        lines.append(f"  - …其余 {len(proc) - 200} 项见同名 .json 的 process 字段")
    return lines


def _stats_line(st: dict) -> str:
    """统计行的人话版：不再是裸 JSON。"""
    if not st:
        return ""
    parts = []
    if st.get("model"):
        parts.append(f"模型 {st['model']}")
    if st.get("tokens") is not None:
        parts.append(f"输出 {st['tokens']} tok")
    if st.get("prompt_tokens") is not None:
        parts.append(f"输入 {st['prompt_tokens']} tok")
    if st.get("tps") is not None:
        parts.append(f"{st['tps']:.0f} tok/s")
    if st.get("cost"):
        parts.append(f"花费 {st['cost']}")
    return "　".join(parts) or json.dumps(st, ensure_ascii=False)[:200]


def build_report(meta: dict, results: list[dict]) -> str:
    """评测报告（Markdown）：版本、时间、逐条表现、归因分布、分类通过率。"""
    total = len(results)
    passed = sum(1 for r in results if r["verdict"] == "pass")
    by_layer: dict[str, int] = {}
    for r in results:
        if r["verdict"] != "pass":
            by_layer[str(r.get("layer", "?"))] = by_layer.get(str(r.get("layer", "?")), 0) + 1
    by_cat: dict[str, list[int]] = {}
    for r in results:
        c = by_cat.setdefault(str(r.get("category", "?")), [0, 0])
        c[1] += 1
        if r["verdict"] == "pass":
            c[0] += 1

    ver = meta.get("version") or {}
    ver_line = f"`{ver.get('commit', 'unknown')}`" + ("（工作区有未提交改动）" if ver.get("dirty") else "（工作区干净）")
    reps = int(meta.get("repeat") or 1)
    pk = sum(1 for r in results if r.get("pass_runs") == reps) if reps > 1 else None

    L: list[str] = []
    L.append(f"# AgentForge 评测报告 · {meta.get('label', 'run')}")
    L.append("")
    L.append(f"- 开始时间：{meta.get('started_at', '—')}　结束：{meta.get('finished_at', '—')}"
             f"　总耗时：{meta.get('duration', '—')}s")
    L.append(f"- 代码版本：{ver_line}")
    L.append(f"- 模型：{meta.get('model', 'auto')}　任务集：{meta.get('tasks', '—')}"
             f"（计划 {meta.get('cases_planned', total)} 条"
             + (f"，跳过 {meta.get('skipped', 0)} 条" if meta.get("skipped") else "") + "）")
    L.append(f"- 判定口径：{'pass^%d（每条跑 %d 遍全过才算过）' % (reps, reps) if reps > 1 else '单次通过'}")
    L.append("")
    head = f"**汇总：{passed}/{total} 通过**"
    if pk is not None:
        head += f"（pass^{reps} 严格口径：{pk}/{total}）"
    L.append(head)
    L.append("")
    L.append("## 失败分层统计（首个未过的判定层）")
    L.append("")
    if by_layer:
        L.append("| 失败层 | 条数 | 说明 |")
        L.append("|---|---|---|")
        layer_notes = {
            "exact": "关键字符串没出现", "norm": "归一后仍对不上（格式/写法偏差）",
            "numeric": "数值不符", "fuzzy": "关键点缺失（拆解/要点不全）",
            "must_not": "出现了禁止内容（编造/越界）", "check": "产物核验未过（代码/文件坏了）",
            "ask": "信息不足未发起澄清弹窗",
            "tool": "未调用任务书指定的工具",
            "crash": "执行崩溃", "stopped": "任务被停止", "empty": "没有产出回复",
            "setup": "环境初始化失败",
        }
        for k, v in sorted(by_layer.items(), key=lambda x: -x[1]):
            L.append(f"| {k} | {v} | {layer_notes.get(k, '')} |")
    else:
        L.append("无——全部通过。")
    L.append("")

    # 同签名失败分组：把相同签名的失败排在一起，供横向比对结果；
    # 只分组不解读——是什么原因，读报告的人自己比对判断
    clusters = failure_clusters(results)
    if clusters:
        L.append("## 同签名失败分组（≥3 条同签名，供结果比对）")
        L.append("")
        for c in clusters:
            L.append(f"- **{c['count']} 条同签名**，签名 `{c['signature']}`")
            L.append(f"  涉及：{'、'.join(c['ids'][:12])}{'…' if len(c['ids']) > 12 else ''}")
        L.append("")
    L.append("## 分类通过率")
    L.append("")
    L.append("| 分类 | 通过 | 总数 |")
    L.append("|---|---|---|")
    for k in sorted(by_cat):
        p, t = by_cat[k]
        L.append(f"| {_CATEGORY_TITLES.get(k, k)}（{k}） | {p} | {t} |")
    L.append("")
    L.append("## 逐条表现")
    L.append("")
    for r in results:
        mark = "✅" if r["verdict"] == "pass" else "❌"
        extra = f"　pass^{reps}={r.get('passk', '—')}" if reps > 1 else ""
        L.append(f"### {mark} {r.get('id', '?')}（{_CATEGORY_TITLES.get(r.get('category', ''), r.get('category', ''))}）")
        L.append("")
        line = (f"- 判定：{r['verdict']}（层：{r.get('layer', '?')}）"
                f"　耗时：{r.get('duration', '—')}s"
                f"　轮次：{r.get('steps', '—')}"
                f"　验证：{r.get('verify_rounds', 0)} 次{extra}")
        L.append(line)
        L.append(f"- 说明：{r.get('detail', '')}")
        if r["verdict"] != "pass":
            # 判定层的原始数据（事实陈列）；过程与回复对所有用例统一全录
            L.extend(_judge_lines(r))
        if r.get("error"):
            L.append(f"- 事件流报错：{r['error']}")
        # 过程记录 + 回复全文 + 沙盒落点：所有用例都录——结果对了也不代表
        # 流程没问题，分析留给读报告的人比对，报告只负责把事实摆全
        L.extend(_process_lines(r))
        reply = str(r.get("reply") or "").strip()
        if reply:
            L.append("- 回复全文：")
            for ln in reply.splitlines() or [""]:
                L.append("  > " + ln)
        L.append(f"- 沙盒留档（产物可亲自去查）：`{r.get('sandbox', '')}`")
        st_line = _stats_line(r.get("stats") or {})
        if st_line:
            L.append(f"- 统计：{st_line}")
        L.append("")
    L.append("---")
    L.append(f"*报告由评测台自动生成于 {meta.get('finished_at', '—')}；"
             f"JSON 数据（bench_compare 可直接使用）：`{meta.get('label', 'run')}` 同名 .json*")
    return "\n".join(L)


# ---------- 一键生成（LLM 出题助手） ----------
EVAL_SUGGEST_SYSTEM = """你是 AgentForge 评测台的出题助手。用户给你一条评测任务指令，你为它
生成这条评测任务的其余字段。只输出一个 JSON 对象，不要任何解释与代码块围栏：
{"id":"...","category":"...","seed":{...},"expect":{...},"notes":"..."}

字段要求：
- id  小写英文短横线短 id，形如 c6-xxx / f5-xxx（字母开头，唯一即可）
- category  从这七个里选一个：coding / fileops / writing / analysis / ops / honesty / policy
- seed  任务开始前要放进工作区的文件（{"相对路径": "文件内容"}）；任务不需要现成文件就给 {}
- expect  客观判定条件（全部由程序判定，零 LLM 调用），至少要有一个判定层：
    check          沙盒里真跑的核验命令——产物类任务优先用它拿硬证据
                   （如 python -c "..."，命令只做只读核验）
                   注意宿主是 Windows cmd：优先 python -c / python 脚本，
                   不要用 wc / grep / cat 这类 Unix 命令
    check_contains 核验命令输出里必须包含的字符串（配合 check 使用；
                   必须是单个字符串，不要数组）
    contains       回复里必须原样出现的片段（字符串数组）
    contains_norm  归一后必须出现的片段（字符串数组）
    numbers        回复里必须出现的数值（[{"value": 24, "tol": 0.01}]）
    fuzzy          关键词组（数组，命中过半算过）
    must_not       回复里禁止出现的片段（字符串数组，如编造用语）
- notes  一句话说明这条任务考察什么（可为空字符串）
"""

# 核验命令的破坏性动作黑名单：LLM 生成的 check 会被程序真跑，宁可拦截重填。
_SUGGEST_CHECK_BAD = (
    "rm ", "rm -", "del ", "rmdir", "format ", "mkfs", "shutdown", "taskkill",
    "reg add", "reg delete", "> /dev/", "dd if=", "git push", "git reset",
    "git clean", "curl ", "wget ", "invoke-webrequest", "start-process",
)


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏、前后废话、字符串里的花括号）。"""
    if not text:
        return None
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _sanitize_suggestion(obj: dict, prompt: str) -> dict:
    """把模型产出的字段收口成合法任务：白名单分类、id 规范化、危险核验拦截。

    expect 为空（或唯一判定是危险命令被拆掉）时返回 {"error": ...}——
    没有客观判定的任务不进评测集，与 save_tasks 同一条底线。
    """
    cat = str(obj.get("category") or "").strip()
    if cat not in REQUIRED_CATEGORIES:
        cat = "analysis"
    cid = re.sub(r"[^a-z0-9-]+", "-",
                 str(obj.get("id") or "").strip().lower()).strip("-") or "new-task"
    expect = obj.get("expect")
    expect = {k: v for k, v in expect.items() if v} if isinstance(expect, dict) else {}
    cc = expect.get("check_contains")
    if cc is not None and not isinstance(cc, str):
        # 判定器只支持单字符串比对（evals.judge 里 _norm(want_out) in ...）：
        # 单元素数组取首元素，其余歧义形态整个丢弃——悬空/假判定不收
        expect["check_contains"] = (str(cc[0])
                                    if isinstance(cc, list) and len(cc) == 1 else "")
        if not expect["check_contains"]:
            expect.pop("check_contains", None)
    check_cmd = str(expect.get("check") or "").strip()
    if not check_cmd:
        # check_contains 只在 check 层内生效（evals.judge）：没有 check 的
        # check_contains 是悬空空转，一并拆掉
        expect.pop("check_contains", None)
    low = " " + check_cmd.lower() + " "
    if any(d in low for d in _SUGGEST_CHECK_BAD):
        expect.pop("check", None)
        expect.pop("check_contains", None)
    if not expect:
        return {"error": "模型生成的判定条件为空（或核验命令含危险动作被拦截），"
                         "请再试一次或改用手动填写"}
    seed = obj.get("seed")
    seed = ({str(k).strip()[:120]: str(v)[:20000]
             for k, v in seed.items() if str(k).strip()}
            if isinstance(seed, dict) else {})
    return {"id": cid, "category": cat, "prompt": str(prompt),
            "seed": seed, "expect": expect,
            "notes": str(obj.get("notes") or "").strip()}


def _dump_tasks(cases: list[dict]) -> str:
    """任务集序列化：保留文件头的用法说明，按分类加分隔注释保持人读性。"""
    head = """# AgentForge 评测任务集
#
# 用法：python tools/bench_tasks.py            # 全量
#       python tools/bench_tasks.py -c coding  # 只跑某一类
#       也可以在网页「评测台」里增删任务、一键运行
#
# 原则：每条任务必须有客观判定（expect），判定全部走程序、零 LLM 调用；
# 编码/文件类任务优先用 check 层（真跑命令拿硬证据）。
# 本文件由「评测台」维护：保存前自动备份到 evals/.run/，保存后回读校验。

cases:
"""
    out: list[str] = []
    last_cat = None
    for c in cases:
        cat = str(c.get("category") or "general")
        if cat != last_cat:
            title = _CATEGORY_TITLES.get(cat, cat)
            out.append(f"  # ================= {title} =================")
            last_cat = cat
        chunk = yaml.safe_dump([c], allow_unicode=True, sort_keys=False, width=100)
        # 整块缩进两格（列表项挂在 cases: 下）：只替换首行会让后续行错位
        chunk = "\n".join(("  " + ln) if ln.strip() else ln
                          for ln in chunk.splitlines())
        out.append(chunk.rstrip())
    return head + "\n".join(out) + "\n"


__all__ = ["EvalsDesk", "PROTECTED_IDS", "REQUIRED_CATEGORIES",
           "build_report", "failure_clusters", "git_version",
           "TASKS_PATH", "RESULTS_DIR"]
