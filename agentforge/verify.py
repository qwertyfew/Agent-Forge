"""收尾验证闸门：任务结束前由系统亲自运行任务书里声明的验证命令。

背景（第八轮测试的教训）：修复任务的 3 次 edit_file 全部实际失败，agent 没读
工具返回的错误信息、也没重跑测试，就在总结里谎报「所有断言通过」。结论是——
**收尾验证必须系统侧强制**：验证命令由程序自己跑，真实结果注入总结流程；
模型只有「解读权」，没有「裁定权」。它怎么说都不影响最终答复里附带的那份
系统侧验证报告。

三件事由这一层负责：

    1. 提取   从任务书里找出声明的验证命令（「验证：python tools/x.py」这类行，
              或任务书里出现的、指向工作区内真实脚本文件的命令行）
    2. 执行   程序自己跑（subprocess，工作区根目录，限时，输出截取），
              不经过模型的工具调用——模型谎报不影响这里的结果
    3. 核查   模型总结声称「验证通过」但任务里从没跑过 run_command 时，
              在答复后追加「未经系统确认」的声明，不给谎报留活口
    4. 产物   任务书声明了交付文件（「报告写入 docs/x.md」「产出文件：x.md」）、
              或总结声称「已写入 X」时，系统逐个 os.path.exists 核查，
              缺失就在答复后追加系统侧声明（REA 测试抓到的盲区：
              全程零 write_file 却谎报「报告已写入」，验证命令闸门管不到文件类交付）
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

# 「验证：xxx」这类声明行。容忍 markdown 列表符、编号、「验证脚本/命令/方式」等变体。
_DECLARE = re.compile(
    r"^\s*(?:[-*•>\d.、\s]*)"
    r"(?:验证|验收|自检|verify)(?:脚本|命令|方式|步骤|用例)?\s*[:：]\s*(.+)$",
    re.I | re.M,
)
# 行中变体：「修好它，验收：python x.py 全过」——验收不总在行首
_DECLARE_INLINE = re.compile(
    r"(?:验证|验收|自检|verify)(?:脚本|命令|方式|步骤|用例)?\s*[:：]\s*([^\n]+)",
    re.I,
)

# 看起来像命令开头的行（兜底扫描用）
_CMD_START = re.compile(
    r"^(?:python3?|py|node|npx|npm|pytest|bash|sh|pwsh|powershell|\.)\b", re.I
)

# 明确禁止系统侧代跑的东西。验证命令来自任务书，正常是「python xxx.py」；
# 带破坏性关键词的一律拒绝执行，宁可跳过闸门也不冒险。
_DANGEROUS = (
    "rm ", "rm -", "del ", "rmdir", "format ", "shutdown", "mkfs",
    "taskkill", "reg add", "reg delete", "> /dev/", "dd if=",
    "git push", "git reset", "git clean",
)

# 模型总结里的「验证通过」类措辞。谎报的经典句式都在这里。
_CLAIM_VERIFIED = re.compile(
    r"验证通过|验证已通过|测试通过|测试全部?通过|全部通过|断言通过|全部断言通过"
    r"|自检通过|护栏(?:全部)?(?:生效|通过)|all (?:tests |assertions )?passed"
    r"|tests? pass(?:ed)?",
    re.I,
)

# ---------------------------------------------------------------------------
# 声明产物存在性核查
# ---------------------------------------------------------------------------
# 路径 token：字母数字开头、可含 ./\_ -、以点扩展名结尾。中文/空格/括号一律不在
# 字符集里，保证抓出来的是纯路径，不带「全过」「（含来源链接）」这类人话尾巴。
_PATH_TOK = r"([A-Za-z0-9_][A-Za-z0-9._/\\-]*\.[A-Za-z0-9]{1,8})"

# 任务书里的产物声明·动词式：「（报告）写入 docs/x.md」「保存到 x.json」「输出到 out/r.md」。
# 前面紧邻「禁止/不要/勿/别/不」的不算（那是禁令，不是交付要求）。
_ARTIFACT_WRITE = re.compile(
    r"(?<!禁止)(?<!不要)(?<!勿)(?<!别)(?<!不)"
    r"(?:写入|写至|保存(?:到|为|在)|存(?:到|为|入)|输出到|输出为|落盘(?:到|为)?)"
    r"\s*[:：`]*\s*" + _PATH_TOK,
)

# 任务书里的产物声明·冒号式：「产出文件：docs/x.md」「输出路径：out/x.md」「交付物：x.md」。
# 故意不收裸「报告：」「结果：」——那两类后面跟文件名时歧义太大，容易误伤。
_ARTIFACT_COLON = re.compile(
    r"(?:产出|输出|交付)(?:文件|路径|物)?\s*[:：]\s*`?" + _PATH_TOK,
)

# 总结里的「已写入 X」完成式声明。必须带完成语气（已/成功/完成/了/好），
# 「接下来可写入 X」「将报告写入 X」这类将来时/转述不误伤。
_ARTIFACT_CLAIM = re.compile(
    r"(?:已(?:经)?|成功|完成)[^。\n；;]{0,12}?(?:写入|创建|保存到|保存在|生成|落盘|产出)(?:了|好)?"
    r"\s*[:：`]*\s*" + _PATH_TOK +
    r"|(?:写入|创建|保存到|保存在|生成|落盘|产出)(?:了|好)(?:一?份)?\s*[:：`]*\s*" + _PATH_TOK +
    r"|(?:has been |have been )?(?:written to|saved to|saved as|created|generated)\s+`?"
    + _PATH_TOK,
    re.I,
)

VERIFY_OUTPUT_LIMIT = 1500
VERIFY_TIMEOUT = 180


def _clip(text: str, limit: int = VERIFY_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，共 {len(text)} 字符]"


def _trim_command(candidate: str) -> str:
    """从声明行的剩余部分里抠出纯命令（去掉「全过」「必须通过」这类人话尾巴）。"""
    toks = candidate.strip().strip("`").strip().split()
    keep: list[str] = []
    for i, t in enumerate(toks):
        if i == 0:
            if not re.fullmatch(r"[\w./\\:-]+", t):
                break
            keep.append(t)
            continue
        if re.search(r"[\u4e00-\u9fff]", t):
            break                      # 从第一个中文词开始都是人话，不是命令
        keep.append(t)
    return " ".join(keep).strip()


def _script_exists(cmd: str, root: Path) -> bool:
    """命令引用的脚本文件是否真实存在于工作区（防止凭空捏造的路径被拿去跑）。"""
    root = Path(root).resolve()
    for tok in cmd.split():
        low = tok.lower().strip("\"'")
        if low.endswith((".py", ".sh", ".js", ".bat", ".ps1", ".cmd")):
            p = Path(low)
            if not p.is_absolute():
                p = root / p
            try:
                if p.resolve().is_file():
                    return True
            except OSError:
                continue
    return False


def extract_verify_cmd(text: str, root: str | Path) -> str:
    """从任务书提取声明的验证命令；找不到声明行时兜底扫描裸命令行。

    只认「指向工作区内真实存在的脚本」的命令——提取不出来或脚本不存在
    都返回空串（此时收尾验证退化为「声称核查」，见 claimed_verified）。
    """
    text = str(text or "")
    root = Path(root)
    candidates: list[str] = []
    for m in _DECLARE.finditer(text):
        candidates.append(m.group(1))
    if not candidates:
        for m in _DECLARE_INLINE.finditer(text):
            candidates.append(m.group(1))
    if not candidates:
        # 兜底 1：任务书里出现的、以解释器开头的行（常包在反引号里）
        for line in text.splitlines():
            s = line.strip().strip("`").strip()
            if _CMD_START.match(s):
                candidates.append(s)
    if not candidates:
        # 兜底 2：行中反引号包住的命令片段（「跑一下 `python x.py` 确认」）
        for m in re.finditer(r"`([^`\n]+)`", text):
            s = m.group(1).strip()
            if _CMD_START.match(s):
                candidates.append(s)
    for cand in candidates[:6]:
        cmd = _trim_command(cand)
        if not cmd or len(cmd) > 300:
            continue
        low = " " + cmd.lower() + " "
        if any(d in low for d in _DANGEROUS):
            continue
        if not _script_exists(cmd, root):
            continue
        return cmd
    return ""


def run_verify_cmd(cmd: str, root: str | Path,
                   timeout: int = VERIFY_TIMEOUT) -> dict:
    """程序自己运行验证命令——不经过模型的工具调用，结果不受模型影响。"""
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(root), shell=True,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "output": _clip(out),
            "duration": round(time.time() - t0, 1),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1,
                "output": f"验证命令超时（>{timeout}s）",
                "duration": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": -1,
                "output": f"验证命令执行失败：{type(e).__name__}: {e}",
                "duration": round(time.time() - t0, 1)}


def claimed_verified(summary: str) -> bool:
    """模型总结里是否声称了「验证/测试通过」这类结论。"""
    return bool(_CLAIM_VERIFIED.search(str(summary or "")))


def ran_verification(convo: list[dict] | None) -> bool:
    """任务过程中模型是否真的调用过 run_command（哪怕跑的不是验证脚本，
    至少说明它有「跑命令验证」的动作；一个命令都没跑过的「验证通过」
    才是最纯粹的谎报）。"""
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            name = ((tc.get("function") or {}).get("name")
                    or tc.get("name") or "")
            if name == "run_command":
                return True
    return False


def _dedupe_cap(paths: list[str], cap: int = 8) -> list[str]:
    """去重保序（大小写不敏感），限量防极端输入。"""
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        k = p.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(p)
    return out[:cap]


def extract_declared_artifacts(text: str) -> list[str]:
    """从任务书提取声明的交付文件路径（「报告写入 docs/x.md」「产出文件：x.md」等）。

    只认带扩展名的路径样 token；提取不出来就返回空表（此时退化为只查总结声明）。
    """
    text = str(text or "")
    out = [m.group(1) for m in _ARTIFACT_WRITE.finditer(text)]
    out += [m.group(1) for m in _ARTIFACT_COLON.finditer(text)]
    return _dedupe_cap(out)


def claimed_artifacts(summary: str) -> list[str]:
    """模型总结里声称「已写入/已创建/已生成」的文件路径（完成语气才算）。

    注意 _ARTIFACT_CLAIM 的三个分支各引用一次 _PATH_TOK，捕获组编号随分支
    变化，所以取「第一个非空捕获组」而不是固定 group(1)。
    """
    out = []
    for m in _ARTIFACT_CLAIM.finditer(str(summary or "")):
        p = next((g for g in m.groups() if g), "")
        if p:
            out.append(p)
    return _dedupe_cap(out)


def find_missing_artifacts(entries: list[tuple[str, bool]],
                           root: str | Path) -> list[dict]:
    """逐个 os.path.exists 核查交付文件，返回缺失清单。

    entries 是 (路径, 是否任务书声明) 对。相对路径按工作区根解析；
    指向工作区之外的绝对路径不核查（不是本任务的交付物，无从判定）。
    """
    root = Path(root).resolve()
    missing: list[dict] = []
    seen: set[str] = set()
    for raw, declared in entries:
        p = str(raw).strip().strip("`'\"")
        k = p.lower()
        if not p or k in seen:
            continue
        seen.add(k)
        f = Path(p)
        try:
            if f.is_absolute():
                target = f.resolve()
            else:
                # 相对路径按工作区根解析；带 .. 逃出工作区的一律不核查
                target = (root / f).resolve()
        except (OSError, ValueError):
            continue
        if not target.is_relative_to(root):
            continue
        try:
            exists = target.exists()
        except OSError:
            exists = False
        if not exists:
            missing.append({"path": p, "declared": bool(declared)})
    return missing


def artifact_missing_block(missing: list[dict]) -> str:
    """附在最终答复末尾的产物核查声明。程序写的，模型只有解读权。"""
    lines = ["【系统侧核查】以下产出文件经系统收尾核查**不存在**于工作区，应视为「未产出」："]
    for c in missing:
        tag = "任务书明确要求的交付物" if c.get("declared") else "总结声称已写入"
        lines.append(f"- `{c['path']}`（{tag}）")
    lines.append("此核查由程序完成，不依赖模型自报；若与本回复正文有出入，以本清单为准。")
    return "\n".join(lines)


def report_block(cmd: str, res: dict) -> str:
    """附在最终答复末尾的系统侧验证报告。模型只有解读权，这一段是程序写的。"""
    verdict = "通过" if res["ok"] else "失败"
    head = (f"【系统侧验证】任务收尾时由系统亲自运行了 `{cmd}` → "
            f"**{verdict}**（退出码 {res['code']}，耗时 {res['duration']}s）。"
            "此结果由程序产生，不依赖模型自报；若与本回复正文有出入，以本结果为准。")
    if res["ok"]:
        return head
    return head + "\n验证输出（节选）：\n```\n" + _clip(res["output"], 800) + "\n```"


UNVERIFIED_NOTE = (
    "【系统侧核查】本回复声称验证通过，但整个任务过程中模型没有执行过任何命令验证，"
    "该结论未经系统确认，应视为「未验证」。"
)

CORRECTIVE_PROMPT = """【系统侧验证结果（权威，不可推翻）】
任务收尾时系统已亲自运行验证命令：{cmd}
真实结果：{verdict}（退出码 {code}，耗时 {duration}s）
输出：
{output}

【你此前的总结】
{summary}

【要求】
验证失败时：如果你有能力修复，先修复问题，然后重新运行上面这条验证命令并看到通过结果；
确实修不了的，就在最终总结里如实说明失败原因与现状。禁止在未修复的情况下声称通过。
只输出修正后的最终总结（或修复动作的开始），不要寒暄。"""

ARTIFACT_CORRECTIVE_PROMPT = """【系统侧核查结果（权威，不可推翻）】
以下产出文件在收尾核查时不存在于工作区：
{missing}

【你此前的总结】
{summary}

【要求】
如果这些文件确实应该产出：现在真实地创建并写入它们（write_file），写完再说完成；
如果本就无需产出（例如核查误判或路径笔误），就在最终总结里如实说明原因。
禁止再次声称已写入而不实际创建文件。只输出修正动作的开始或修正后的最终总结，不要寒暄。"""
