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

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state: dict = {"running": False}
        # 可注入的执行核心（tests 用替身替换，不碰真模型）
        self._runner = None

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
                                self.cfg)
        except Exception as e:  # noqa: BLE001
            st["error"] = f"{type(e).__name__}: {e}"

        finished = time.time()
        meta = {
            "label": label, "model": model or "auto",
            "config": str(ROOT / "config.yaml"), "tasks": str(TASKS_PATH),
            "repeat": reps, "cases_planned": len(run_cases),
            "skipped": skipped,
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
        for p in sorted(RESULTS_DIR.glob("*.md"), reverse=True):
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
    L.append("## 失败归因分布")
    L.append("")
    if by_layer:
        L.append("| 失败层 | 条数 | 说明 |")
        L.append("|---|---|---|")
        layer_notes = {
            "exact": "关键字符串没出现", "norm": "归一后仍对不上（格式/写法偏差）",
            "numeric": "数值不符", "fuzzy": "关键点缺失（拆解/要点不全）",
            "must_not": "出现了禁止内容（编造/越界）", "check": "产物核验未过（代码/文件坏了）",
            "crash": "执行崩溃", "stopped": "任务被停止", "empty": "没有产出回复",
            "setup": "环境初始化失败",
        }
        for k, v in sorted(by_layer.items(), key=lambda x: -x[1]):
            L.append(f"| {k} | {v} | {layer_notes.get(k, '')} |")
    else:
        L.append("无——全部通过。")
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
        L.append(f"- 判定：{r['verdict']}（层：{r.get('layer', '?')}）　耗时：{r.get('duration', '—')}s{extra}")
        L.append(f"- 说明：{r.get('detail', '')}")
        if r.get("stats"):
            st = r["stats"]
            L.append(f"- 统计：token {st.get('prompt_tokens', '?')}+{st.get('completion_tokens', '?')}"
                     f"　工具调用 {st.get('tool_calls', '?')} 次"
                     if "tool_calls" in st else f"- 统计：{json.dumps(st, ensure_ascii=False)[:200]}")
        L.append("")
    L.append("---")
    L.append(f"*报告由评测台自动生成于 {meta.get('finished_at', '—')}；"
             f"JSON 数据（bench_compare 可直接使用）：`{meta.get('label', 'run')}` 同名 .json*")
    return "\n".join(L)


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
           "build_report", "git_version", "TASKS_PATH", "RESULTS_DIR"]
