"""澄清弹窗判定（试行版 v0）——**决定「要不要问用户」的唯一出口**。

设计规格见 `docs/clarify-policy.md` §12。这里只做两件事，且两件都不依赖模型
「想一下」：

- **R1 盲选不可逆目标**（必问）：检索回了 ≥2 个候选，模型没读过其中任何一个，
  紧接着就要删/覆盖它挑中的那个 → 问。弱模型最典型的破坏行为就是这一条。
- **R2 检索零命中 + 回复末尾有问号**（可问）：模型自己也不确定而停下来问，
  harness 接手把问题问完（问题由 harness 用模板生成，不用模型的原话）。

三条纪律（改这里之前先读一遍）：

1. **不许扫 `ops_batch` 的合并文本**。PTC 常驻后，一次脚本 = 一次 `ops_batch`
   调用 + N 次内层调用，合并输出里「哪段属于哪次调用」已经丢了。按合并文本反解
   会把 A 工具的空目录算到 B 工具头上，从「不弹」变成「乱弹」。所以本模块的
   `observe()` 必须由执行层的**单次调用**喂（`loop.Agent._execute`），那里
   `_execute` 是唯一咽喉点，直连与脚本内层调用都经过它。
2. **只看结构信号**（工具名 / 固定渲染格式 / 标点位置），不做语义相似度、不让
   模型自评置信度——本地量化模型这两样都不可信（见 §12.1）。
3. **每个判定都要能翻成一句人话**（`Decision.reason` + `evidence`）。弹窗上看
   不出「为什么问你」，误报就永远发现不了。

参数全部是占位值（§12.4），集中定义在 `PARAMS`，影子期后统一改。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 参数台账（docs/clarify-policy.md §12.4）——**没有一个是实测来的**，别当基准用
# ---------------------------------------------------------------------------
PARAMS = {
    # R1：最近一次检索返回多少个候选才算「多候选」
    "r1_min_candidates": 2,
    # R2：只在回复**末尾**多少字符里找问号（正文前半句的问号不算）
    "r2_tail_chars": 200,
    # 选项个数上限（对齐 Claude Code：2–4 个，含「其他」）
    "max_options": 4,
}
PARAMS_STATUS = "待实测（docs/clarify-policy.md §12.4）"

# ---------------------------------------------------------------------------
# 工具层固定渲染格式的识别（工具改格式，这里同步改）
# ---------------------------------------------------------------------------
_GH_ROW_RX = re.compile(r"^\s*\d+\.\s+(\S+)\s+star=", re.M)   # github_search 渲染行
_BRACKET_Q_RX = re.compile(r"「([^」]{1,60})」")
# 追问语气：有问号 + 追问惯用词。宁可宽松——触发还要同时满足「本场有检索信号」
# 和「每场至多一次」，误触发的代价只是一次正常澄清。
_CLARIFY_RX = re.compile(r"[^。！？!?\n]{4,80}[？?]")
_CLARIFY_HINTS = ("请问", "请告知", "请提供", "请说明", "请补充", "请确认",
                  "是指", "哪一个", "哪个", "哪些", "是什么", "指的是")
_EMPTY_MARKERS = (
    "没拿到结果",                 # github_search：仓库搜索没拿到结果
    "未找到匹配",                 # search_code / find_files
    "共 0 个目录 / 0 个文件",     # list_dir：空目录
    "没有返回结果",               # web_search
    "文件不存在",                 # read_file
)
_FETCH_MARKERS = ("请求失败", "未找到")   # fetch_url / fetch_json 拿不到来源
_RETRIEVAL_TOOLS = {"list_dir", "find_files", "search_code", "read_file",
                    "github_search", "web_search", "fetch_url", "fetch_json"}

# R2 的白名单：只认「检索」的零命中。**排除 read_file 与 fetch_\***
# ——「读一个不存在的文件」不是「检索没找到实体」，混进来会让读文件失败也弹窗。
_R2_ZERO_TOOLS = {"list_dir", "find_files", "search_code",
                  "github_search", "web_search"}
_LOCAL_RETRIEVAL = {"list_dir", "find_files", "search_code"}

# 通道名（用于「没在{工作区/GitHub/网页}找到」这句人话）
_CHANNEL = {"list_dir": "工作区", "find_files": "工作区", "search_code": "工作区",
            "github_search": "GitHub", "web_search": "网页"}
_EMPTY_OPTIONS = ["本地项目", "git上的项目", "其他"]

# 不可逆动作：删 / 覆盖已有文件 / 含删除语义的命令
_DELETE_CMD_RX = re.compile(
    r"(^|[\s;&|(])(rm|rmdir|del|erase|shred|truncate|unlink)\s"
    r"|Remove-Item|git\s+clean|git\s+rm", re.I)
_OVERWRITE_TOOLS = {"write_file", "write_skeleton", "fill_section"}
_DELETE_TOOLS = {"delete_file"}

_READ_NOISE = re.compile(r"[\s\"']+")


# ---------------------------------------------------------------------------
# 信号识别（从 loop.py 迁入，行为不变——loop 侧仍以同名重新导出，测试不受影响）
# ---------------------------------------------------------------------------
def _lookup_signal(name: str, out: str) -> dict | None:
    """按工具层固定输出格式识别检索信号。返回
    {"kind": "multi"|"empty", "query": str, "candidates": [..]} 或 None。
    只看检索类工具——写类工具报「文件不存在」是另一回事，不算检索未命中。"""
    out = str(out or "")
    if not out or name not in _RETRIEVAL_TOOLS:
        return None
    rows = _GH_ROW_RX.findall(out)
    if len(rows) >= 2:                       # ≥2 个编号候选 =「多种可能不确定」
        q = _BRACKET_Q_RX.search(out)
        return {"kind": "multi", "query": q.group(1) if q else "",
                "candidates": rows[:3]}
    markers = list(_EMPTY_MARKERS) + (
        list(_FETCH_MARKERS) if name.startswith("fetch") else [])
    if any(m in out for m in markers):
        q = _BRACKET_Q_RX.search(out)
        if q:
            query = q.group(1)
        else:                                # list_dir 等无「」格式：首行就是路径
            query = out.splitlines()[0].strip()[:60] if out.splitlines() else ""
        return {"kind": "empty", "query": query, "candidates": []}
    return None


def _has_clarify_intent(text: str) -> bool:
    """最终回复是否在向用户追问（问号 + 追问惯用词，双条件同时满足）。"""
    if "？" not in text and "?" not in text:
        return False
    return any(w in text for w in _CLARIFY_HINTS)


def _tail_has_question(text: str, window: int) -> bool:
    """R2 的唯一模型相关信号：**末尾** window 字符内有问号。

    只看标点位置，不判语义——弱模型「会不会打问号」是稳定的，「想问什么」
    不可靠（所以问题由 harness 生成，见 §12.3）。"""
    tail = (text or "")[-int(window):]
    return "？" in tail or "?" in tail


# ---------------------------------------------------------------------------
# 候选提取：把「检索返回了什么」变成可比较的名字集合
# ---------------------------------------------------------------------------
_LIST_ENTRY_RX = re.compile(r"^\s*\[(?:DIR |FILE)\]\s*(.+?)(?:/\s*$|\s{2,}\()", re.M)
_FIND_HEAD_RX = re.compile(r"共\s*(\d+)\s*项")
_SEARCH_LINE_RX = re.compile(r"^(.*?):(\d+):", re.M)


def candidates_of(tool: str, out: str) -> list[str]:
    """从检索输出里取候选名（确定性解析，工具改格式这里同步改）。

    - `list_dir`  → 目录/文件条目名
    - `find_files`→ 每条命中的路径
    - `search_code`→ 命中的文件路径（去重）
    其他工具返回空列表（R1 只认「列/找/搜」这三种）。
    """
    out = str(out or "")
    if not out or out.lstrip().startswith(("[错误]", "[已拒绝]")):
        return []
    if tool == "list_dir":
        return [m.group(1).strip().rstrip("/") for m in _LIST_ENTRY_RX.finditer(out)]
    if tool == "find_files":
        head = _FIND_HEAD_RX.search(out)
        if not head:
            return []
        body = out.split("\n", 1)[1] if "\n" in out else ""
        return [ln.strip() for ln in body.splitlines() if ln.strip()][:50]
    if tool == "search_code":
        seen: list[str] = []
        for m in _SEARCH_LINE_RX.finditer(out):
            p = m.group(1).strip()
            if p and p not in seen:
                seen.append(p)
        return seen[:50]
    return []


def _norm(s: str) -> str:
    return _READ_NOISE.sub("", str(s or "")).replace("\\", "/").lower()


def _matches(cand: str, target: str) -> bool:
    """候选名与目标路径是否指同一个东西（归一化 + 后缀包含，双向）。

    刻意保守：宁可判「不是同一个」而少问，也不要因为把 A 当成 B 而多问。"""
    c, t = _norm(cand), _norm(target)
    if not c or not t:
        return False
    if c == t:
        return True
    cs = c.split("/")
    ts = t.split("/")
    return t.endswith("/" + c) or c.endswith("/" + t) or bool(set(cs) & set(ts))


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    rule: str                       # "R1" | "R2"
    action: str = "ask"             # v0 只有 ask；proceed 由调用方默认执行
    slot: str = ""
    reason: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)   # 供记忆检索/回灌用的原始信号

    def event(self) -> dict:
        """写进事件流的 `clarify_decision`——影子期靠它统计误报。"""
        return {"type": "clarify_decision", "rule": self.rule,
                "action": self.action, "slot": self.slot,
                "reason": self.reason, "question": self.question,
                "options": list(self.options), "evidence": list(self.evidence)}


def _clip_options(opts: list[str]) -> list[str]:
    out = [str(o)[:24] for o in opts if str(o).strip()]
    return out[: int(PARAMS["max_options"])]


# ---------------------------------------------------------------------------
# Watcher：一场 run 一个实例，按调用顺序喂，按需给出判定
# ---------------------------------------------------------------------------
class Watcher:
    """R1/R2 的状态机。

    `observe()` 在**每次工具调用执行完之后**喂（单次调用的真实工具名 + 真实输出，
    不是 ops_batch 的合并文本）；`precheck()` 在**不可逆调用执行之前**问一句；
    `on_reply()` 在**最终回复产生之后**判 R2。

    `mode`：
      - "off"    全关（回到 PTC 之前那种「什么都不问」）
      - "shadow" 只算不弹（事件照记，动作照做）——上线第一阶段的默认姿势
      - "on"     算到就弹
    """

    def __init__(self, mode: str = "on", workspace_root: str | Path | None = None):
        self.mode = (mode or "on").lower()
        self._ws = Path(workspace_root) if workspace_root else None
        self._lock = threading.RLock()
        self._retrieval: dict | None = None    # 最近一次「≥2 候选」的检索
        self._reads: list[str] = []            # 那次检索之后读过的路径
        self._zero: dict | None = None         # 最近一次检索零命中
        self._hit_after_zero = False           # 零命中之后又有命中 → 答案在证据里
        self._asked: set[str] = set()          # 同一槽位只问一次

    # -- 模式 ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def shadow(self) -> bool:
        return self.mode == "shadow"

    # -- 目标存在性（R1 判「覆盖」用） --------------------------------------
    def _exists(self, path: str) -> bool:
        if not path:
            return False
        p = Path(str(path))
        try:
            if not p.is_absolute() and self._ws is not None:
                p = self._ws / p
            return p.exists()
        except OSError:
            return False

    # -- 喂数据 -------------------------------------------------------------
    def observe(self, tool: str, args: dict, out: str) -> None:
        """执行完之后登记一次调用的「工具名 + 输出」。合成文本不进来。"""
        if not self.enabled:
            return
        out_s = str(out or "")
        with self._lock:
            if tool == "read_file":
                p = str((args or {}).get("path") or "")
                if p and not out_s.lstrip().startswith(("[错误]", "[已拒绝]")):
                    self._reads.append(p)
                return
            if tool in _LOCAL_RETRIEVAL:
                cands = candidates_of(tool, out_s)
                if len(cands) >= int(PARAMS["r1_min_candidates"]):
                    # 新的多候选：之前读过的路径不再作数（是另一批候选了）
                    self._retrieval = {"tool": tool, "candidates": cands}
                    self._reads = []
                elif cands:
                    # 有命中且唯一/少量 → 这条线已经收敛，不再算「盲选」
                    self._retrieval = None
                if not cands and _lookup_signal(tool, out_s):
                    self._set_zero(tool, out_s)
                elif cands:
                    self._hit_after_zero = True
                    self._zero = None
                return
            if tool in _R2_ZERO_TOOLS:
                sig = _lookup_signal(tool, out_s)
                if sig and sig.get("kind") == "empty":
                    self._set_zero(tool, out_s)
                elif sig or out_s.strip():
                    self._hit_after_zero = True
                    self._zero = None
                return

    def _set_zero(self, tool: str, out: str) -> None:
        q = ""
        m = _BRACKET_Q_RX.search(out)
        if m:
            q = m.group(1)
        elif out.splitlines():
            q = out.splitlines()[0].strip()[:60]
        self._zero = {"tool": tool, "query": q,
                      "channel": _CHANNEL.get(tool, "工作区")}
        self._hit_after_zero = False

    # -- 执行前：R1 ---------------------------------------------------------
    def precheck(self, tool: str, args: dict) -> Decision | None:
        """不可逆调用执行**之前**调用。返回 Decision 表示「先问，别动手」。"""
        if not self.enabled:
            return None
        args = args or {}
        verb = self._irreversible_verb(tool, args)
        if not verb:
            return None
        target = str(args.get("path") or args.get("file_path") or "")
        with self._lock:
            ret = self._retrieval
            if not ret or len(ret.get("candidates") or []) < int(PARAMS["r1_min_candidates"]):
                return None
            cands = list(ret["candidates"])
            if not target:
                # run_command 没有 path 参数：从命令文本里找它点的是哪个候选。
                # 找不到 = 没点这批候选中的任何一个 → 不归 R1 管。
                cmd = str(args.get("command") or "")
                target = next((c for c in cands if _matches(c, cmd)), "")
                if not target:
                    return None
            # 目标不是这批候选里的东西 → 不是「盲选」，不管
            if not any(_matches(c, target) for c in cands):
                return None
            # 已经读过其中任何一个 → 不是盲选（读过就能自证）
            if any(_matches(c, r) for c in cands for r in self._reads):
                self._retrieval = None
                return None
            key = f"R1|{_norm(target)}"
            if key in self._asked:
                return None
            self._asked.add(key)
            shown = cands[:3]
            opt = _clip_options([*shown, "其他"])
            question = (f"我要{verb}「{target}」，但检索到 {len(cands)} 个候选："
                        f"{'、'.join(shown)}，是哪一个？（没读内容就动手，怕删错）")
            return Decision(
                rule="R1", slot="irreversible_target",
                reason="BLIND_IRREVERSIBLE_TARGET",
                question=question[:200], options=opt,
                evidence=[f"{ret['tool']} 返回 {len(cands)} 个候选：{'、'.join(shown)}",
                          f"此后没有任何一次 read_file 命中它们",
                          f"紧接着要{verb}：{target}"],
                extra={"kind": "multi", "query": target,
                       "candidates": shown, "verb": verb},
            )

    def _irreversible_verb(self, tool: str, args: dict) -> str:
        """这次调用是不是「猜错就回不来」的。返回动作词（删除/覆盖/执行命令）或 ""。"""
        if tool in _DELETE_TOOLS:
            return "删除"
        if tool in _OVERWRITE_TOOLS:
            # 只有目标已存在才算覆盖（新建是可逆的）
            p = str(args.get("path") or args.get("file_path") or "")
            return "覆盖" if self._exists(p) else ""
        if tool == "run_command":
            cmd = str(args.get("command") or "")
            return "执行删除命令" if _DELETE_CMD_RX.search(cmd) else ""
        return ""

    # -- 回复后：R2 ---------------------------------------------------------
    def on_reply(self, text: str) -> Decision | None:
        if not self.enabled:
            return None
        with self._lock:
            z = self._zero
            if not z:
                return None
            if self._hit_after_zero:
                # 零命中之后又检索到东西 → 答案已在证据里，别拿它打扰用户
                return None
            if not _tail_has_question(text or "", PARAMS["r2_tail_chars"]):
                return None
            key = f"R2|{z['tool']}|{_norm(z['query'])}"
            if key in self._asked:
                return None
            self._asked.add(key)
            self._zero = None
            q = z["query"]
            base = (f"没在{z['channel']}找到「{q}」，你要的是？"
                    if q else f"没在{z['channel']}检索到需要的信息，你要的是？")
            # 模板由 harness 定（弱模型的问题常常跑偏），但模型原话里往往带着它猜的
            # 实体名（如「您说的 REA」）——把它作为补充附上，用户才看得懂在问什么。
            m = _CLARIFY_RX.search(text or "")
            tail_q = m.group(0).strip() if m else ""
            question = base if not tail_q else f"{base}\n（模型原话：{tail_q}）"
            return Decision(
                rule="R2", slot="entity", reason="ZERO_HIT_AND_MODEL_ASKED",
                question=question[:200], options=_clip_options(list(_EMPTY_OPTIONS)),
                evidence=[f"{z['tool']} 返回零命中：{q or '(无查询词)'}",
                          "模型回复末尾带问号（它自己也没把握）"],
                extra={"kind": "empty", "query": q, "candidates": [],
                       "channel": z["channel"]},
            )


__all__ = ["PARAMS", "PARAMS_STATUS", "Decision", "Watcher", "candidates_of",
           "_lookup_signal", "_has_clarify_intent", "_tail_has_question",
           "_EMPTY_OPTIONS", "_CLARIFY_RX", "_RETRIEVAL_TOOLS"]
