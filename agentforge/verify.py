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
              或总结声称「已写入 X」时，系统逐个核查——不只查存在，还查
              「本轮是否真的写过」：
                missing  文件不存在（REA 一测：零 write_file 谎报已写入）
                stale    文件存在但 mtime 与任务开始快照一致——本轮从未更新，
                         吃了上一轮的老本（REA 三测：报告是上轮旧文件）
                ghost    文件存在但全程零写操作——它不可能是本轮写出的
              任一命中先给一轮修正机会，复查仍不过就在答复后追加系统侧声明
    5. 自述   上面 3/4 治的都是「声称做了但没做」，它们的反面同样致命却一直没人管：
              模型**如实承认**「下一步我将…」「我无法修改…」，系统照样把这条回复当
              done 收下。见文件末尾的收尾自述核查。
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys
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

# 行中「运行 python x.py 确认输出 24」式声明（c1 测出的漏检语料）：命令没写在
# 「验证：」冒号行里，而是嵌在句子中间，后面直接跟期望输出。旧词表只认冒号式
# 声明，这条最强的自检线索整个被浪费——闸门本可以亲跑命令并比对输出。
_VERIFY_RUN = re.compile(
    r"(?:运行|执行|跑)(?:一下)?\s+"
    r"((?:python3?|py|node|npm|npx|pytest|bash|sh|pwsh|cmd)\s+[^\n。；;，]+?\."
    r"(?:py|sh|js|mjs|ts|cmd|bat))"
    r"\s*(?:[，,]\s*)?(?:来|以|再)?\s*(?:确认|检查|验证|看)"
    r"[^\n。；;]{0,10}?(?:输出|结果)(?:为|是|等于|应为|应该是)?\s*"
    r"([^\s，。；;、]+)",
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
# 第四轮评测补充（探针抓到的两种滑句式）：「验证状态：通过」被「状态：」隔开、
# 「实际输出：3 / 输出结果为：3」——零命令却报出"实测"输出，纯属编造。
# 「输出为」加负向断言排除「预期/期望输出为」（那是说明，不是谎报）；
# 真跑过 run_command 的任务根本不进这道分支（ran_verification 短路），无误伤面。
_CLAIM_VERIFIED = re.compile(
    r"验证通过|验证已通过|测试通过|测试全部?通过|全部通过|断言通过|全部断言通过"
    r"|自检通过|护栏(?:全部)?(?:生效|通过)|符合预期"
    r"|验证状态[:：]?\s*通过"
    r"|(?<![预期望该应])输出(?:结果)?为|实际输出"
    r"|all (?:tests |assertions )?passed"
    r"|tests? pass(?:ed)?",
    re.I,
)

# ---------------------------------------------------------------------------
# 声明产物存在性核查
# ---------------------------------------------------------------------------
# 路径 token：字母数字开头、可含 ./\_ -、以点扩展名结尾。中文/空格/括号一律不在
# 字符集里，保证抓出来的是纯路径，不带「全过」「（含来源链接）」这类人话尾巴。
_PATH_TOK = r"([A-Za-z0-9_][A-Za-z0-9._/\\-]*\.[A-Za-z0-9]{1,8})"

# 路径前的分隔符：冒号/反引号之外还要容忍 markdown 强调符号——REA 三测里总结写的是
# 「已写入：**`docs/x.md`**」，** 把声明提取挡死了，闸门因此整个漏判。
# 「到/为/至」是写入动词带的介词（「写入到 x.txt」「写进到」少见但「输出为」常见）。
_SEP = r"\s*[:：`*_~\s]*(?:到|为|至)?\s*"

# 动词与路径之间的填充语（第四轮评测 f1 实测两处漏检都死在这里）：
# 总结「新建了空的 `summary.md` 文件」、任务书「新建一个空的 summary.md」——
# 动词和路径之间夹着「一个/空的/的」，旧分隔符只认标点与介词，双双漏提取，
# declared 与 claimed 全空，产物核查无米下锅。
_FILLER = r"(?:一?\s*[个份]\s*)?(?:空的?|空白|新)?\s*(?:的)?\s*"

# 任务书里的产物声明·动词式：「（报告）写入 docs/x.md」「保存到 x.json」「输出到 out/r.md」。
# 「写进」是第三轮评测真实任务书的措辞（把文件名写进 largest.txt），漏了它 declared 全空。
# 「创建/新建」是第四轮评测 f1 任务书的措辞（新建一个空的 summary.md），同样曾全盲。
# 前面紧邻「禁止/不要/勿/别/不」的不算（那是禁令，不是交付要求）。
_ARTIFACT_WRITE = re.compile(
    r"(?<!禁止)(?<!不要)(?<!勿)(?<!别)(?<!不)"
    r"(?:写入|写至|写进|创建|新建|保存(?:到|为|在)|存(?:到|为|入)|输出到|输出为|落盘(?:到|为)?)"
    + _FILLER + _SEP + _PATH_TOK,
)

# 任务书里的产物声明·冒号式：「产出文件：docs/x.md」「输出路径：out/x.md」「交付物：x.md」。
# 故意不收裸「报告：」「结果：」——那两类后面跟文件名时歧义太大，容易误伤。
_ARTIFACT_COLON = re.compile(
    r"(?:产出|输出|交付)(?:文件|路径|物)?\s*[:：]" + _SEP + _PATH_TOK,
)

# 总结里的「已写入 X」完成式声明。必须带完成语气（已/成功/完成/了/好），
# 「接下来可写入 X」「将报告写入 X」这类将来时/转述不误伤。
# 动词表按真实漏检语料扩充（第三轮评测四条编造回复全部绕过了旧词表）：
# 「已修复 bug.py」「新建文件 x.py」「完成了 x.py 的编写」——修复/新建/编写都曾失明。
_ARTIFACT_CLAIM = re.compile(
    r"(?:已(?:经)?|成功|完成)[^。\n；;]{0,12}?"
    r"(?:写入|创建|新建|保存到|保存在|生成|落盘|产出|修复|修改|更新|编辑|重写|补全)"
    r"(?:了|好)?"
    + _SEP + _PATH_TOK +
    r"|(?:写入|创建|新建|保存到|保存在|生成|落盘|产出)(?:了|好|文件)(?:一?份)?"
    + _FILLER + _SEP + _PATH_TOK +
    # 「完成了 count_users.py 的编写」：动词在路径后面，非贪婪防回溯咬掉文件名尾巴
    r"|完成(?:了)?[^。\n；;]{0,16}?" + _PATH_TOK + r"\s*的?\s*(?:编写|实现|开发|制作|修复|设计|撰写|创建)"
    + r"|(?:has been |have been )?(?:written to|saved to|saved as|created|generated)"
    + _SEP + _PATH_TOK,
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
            # 中文不一定就是人话尾巴：脚本/路径名本身可以是中文（tools/校验.py）。
            # 带路径分隔符或以脚本扩展名结尾的照常保留；真正的纯中文词
            # （「全过」「必须通过」）才从这里开始断尾。之前一律 break，
            # 中文文件名的验证命令整条被弃，闸门静默退化成「声称核查」。
            if re.search(r"[./\\]", t) or re.search(
                    r"\.(?:py|pyw|sh|js|mjs|bat|cmd|ps1)$", t, re.I):
                keep.append(t)
                continue
            break
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
        # 「运行 python x.py 确认输出 24」式句子声明（无冒号）
        for m in _VERIFY_RUN.finditer(text):
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


def _decode_output(raw: bytes) -> str:
    """子进程输出解码：先按 UTF-8，失败退回本机区域编码（中文 Windows 是 GBK）。

    之前写死 encoding='utf-8' + errors='replace'：Windows 下脚本 print 中文走
    管道时默认是 GBK 字节，硬按 UTF-8 解出来整段乱码——修正轮拿到的「失败
    原因」不可读，验证闸门等于半瞎。
    区域编码要用 locale.getencoding()（3.11+）：UTF-8 模式下
    getpreferredencoding() 会谎报 utf-8，兜底就形同虚设；3.10 退回旧接口。"""
    try:
        fallback = locale.getencoding()               # noqa: ATTRS  3.11+
    except AttributeError:
        fallback = locale.getpreferredencoding(False)
    for enc in ("utf-8", fallback):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_verify_cmd(cmd: str, root: str | Path,
                   timeout: int = VERIFY_TIMEOUT) -> dict:
    """程序自己运行验证命令——不经过模型的工具调用，结果不受模型影响。"""
    t0 = time.time()
    try:
        # 给子进程强制 UTF-8 模式：Windows 下 Python 脚本 print 中文走管道
        # 默认是 GBK 字节，而解码端无法可靠区分 GBK 与 UTF-8（GBK 字节常常
        # 恰好是合法 UTF-8，解错了也不报错）。从源头统一成 UTF-8 才是确定性的；
        # _decode_output 的区域编码回退留给不认这个变量的子进程（bat 等）。
        #
        # PATH 把**当前解释器所在目录**放最前：check 命令里写的 `python` 必须
        # 解析到跑 harness 的同一个解释器（评测跑在 .venv 里就解析到 .venv 的
        # python），而不是继承 PATH 碰到的——Windows 上那个经常是微软商店的
        # 占位符（exit=9009 "Python was not found"），12 条 check 任务就这样
        # 全军覆没过，看报告还以为是模型的问题。
        exe_dir = str(Path(sys.executable).resolve().parent)
        env = {**os.environ, "PYTHONUTF8": "1",
               "PATH": exe_dir + os.pathsep
                       + str(Path(exe_dir) / "Scripts") + os.pathsep
                       + os.environ.get("PATH", "")}
        proc = subprocess.run(
            cmd, cwd=str(root), shell=True,
            capture_output=True, timeout=timeout, env=env,
        )
        out = _decode_output(
            (proc.stdout or b"") + b"\n" + (proc.stderr or b"")).strip()
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


# ---------------------------------------------------------------------------
# 产物核查·第二轮升级：不只查存在，还查「本轮是否真的写过」
# ---------------------------------------------------------------------------
# 会产出/修改文件的写类工具。总结声称「已写入」而任务里一个都没出现过，
# 就是幽灵声明的判定依据（REA 三测：零 write_file 却声称报告已写入）。
WRITE_TOOLS = ("write_file", "edit_file")

# mtime 比较容差：同一文件未修改时 stat 返回值应完全一致，留一点余量
# 防文件系统时间精度差异造成的误判。
_MTIME_EPS = 0.002


def _tool_names(convo: list[dict] | None):
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            name = ((tc.get("function") or {}).get("name")
                    or tc.get("name") or "")
            if name:
                yield name


def ran_writes(convo: list[dict] | None) -> bool:
    """任务过程中是否调用过写类工具（write_file / edit_file）。"""
    return any(n in WRITE_TOOLS for n in _tool_names(convo))


def produced_possible(convo: list[dict] | None) -> bool:
    """任务里是否出现过任何可能产出文件的操作（写类工具或 run_command——
    命令也可能合法地产出文件，比如跑脚本生成报告；两者都没有时，
    「已写入 X」才板上钉钉是假的）。"""
    return any(n in WRITE_TOOLS or n == "run_command" for n in _tool_names(convo))


def _resolve_inside(path: str, root: Path) -> Path | None:
    """把声明/声称的路径解析到工作区内；逃出工作区或解析失败返回 None。"""
    f = Path(path)
    try:
        target = f if f.is_absolute() else root / f
        target = target.resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(root):
        return None
    return target


def snapshot_artifacts(text: str, root: str | Path) -> dict[str, float]:
    """任务开始时调用：记录任务书声明交付文件的 mtime（不存在的不记录）。

    收尾核查用它判断「本轮是否真的更新过」——REA 三测里 agent 没重写
    声明的报告，上一轮的旧文件原样躺在那里，单纯的存在性核查被糊弄过去了。
    """
    root = Path(root).resolve()
    snap: dict[str, float] = {}
    for p in extract_declared_artifacts(text):
        target = _resolve_inside(p, root)
        if target is None:
            continue
        try:
            if target.is_file():
                snap[p.lower()] = target.stat().st_mtime
        except OSError:
            continue
    return snap


def resolve_claim_paths(entries: list[tuple[str, bool]],
                        root: str | Path) -> list[tuple[str, bool]]:
    """裸文件名声称对齐：总结声称「已写入 summary.md」而实际写在 reports/2026/
    之下时，按工作区内**唯一的同名文件**重解析——否则按根目录判「不存在」，
    附上的系统核查声明反而是假的（第四轮评测 f1 实测：文件真在、声明却冤枉它）。

    只对「没有目录成分的裸文件名 + 按原路径不存在」出手；同名命中不唯一
    （或超过查找上限）时维持原样——不确定的不猜。
    """
    rootp = Path(root).resolve()
    out: list[tuple[str, bool]] = []
    for p, declared in entries:
        clean = str(p).strip().strip("`'\"")
        target = _resolve_inside(clean, rootp)
        if target is not None and target.is_file():
            out.append((p, declared))
            continue
        bare = Path(clean).name
        if bare and bare == clean:
            hits: list[Path] = []
            for i, f in enumerate(rootp.rglob(bare)):
                if i >= 200:          # 大工作区兜底：够用即止，不拖垮收尾
                    break
                if f.is_file():
                    hits.append(f)
            if len(hits) == 1:
                out.append((hits[0].relative_to(rootp).as_posix(), declared))
                continue
        out.append((p, declared))
    return out


def path_mtime(path: str, root: str | Path) -> float | None:
    """单个文件的当前 mtime；不存在/不可读/工作区外返回 None（修正轮复查用）。"""
    target = _resolve_inside(str(path).strip().strip("`'\""), Path(root).resolve())
    if target is None:
        return None
    try:
        return target.stat().st_mtime if target.is_file() else None
    except OSError:
        return None


def find_artifact_issues(entries: list[tuple[str, bool]],
                         root: str | Path,
                         snapshot: dict[str, float] | None = None,
                         produced: bool | None = None) -> list[dict]:
    """产物核查的主判定：返回问题清单，每项 {path, declared, status}。

    - missing  文件不存在（原 find_missing_artifacts 的行为）
    - stale    文件存在、任务书声明为交付物、mtime 与任务开始快照一致——
               本轮从未更新，吃老本（REA 三测主形态）
    - ghost    文件存在、总结声称已写入、但任务全程没有任何可能产出文件的
               操作——现有内容不可能是本轮写的

    produced 三态：True/False = 任务里是否出现过写类工具或 run_command；
    None = 无法判定，跳过 ghost。snapshot 为 None 时跳过 stale（没做起点
    快照）；对声明文件只要给了快照就以 mtime 为准（变了/本轮新建都算数），
    不再看 produced——mtime 比工具调用清单更权威。
    """
    root = Path(root).resolve()
    issues: list[dict] = []
    seen: set[str] = set()
    for raw, declared in entries:
        p = str(raw).strip().strip("`'\"")
        k = p.lower()
        if not p or k in seen:
            continue
        seen.add(k)
        target = _resolve_inside(p, root)
        if target is None:
            continue
        try:
            exists = target.exists()
        except OSError:
            exists = False
        if not exists:
            issues.append({"path": p, "declared": bool(declared),
                           "status": "missing"})
            continue
        if declared and snapshot is not None:
            # 快照是权威：mtime 变了或任务开始时还不存在，都说明本轮动过
            m0 = snapshot.get(k)
            if m0 is not None:
                try:
                    now = target.stat().st_mtime
                except OSError:
                    now = None
                if now is not None and now <= m0 + _MTIME_EPS:
                    issues.append({"path": p, "declared": True,
                                   "status": "stale"})
            continue
        if produced is False:
            issues.append({"path": p, "declared": bool(declared),
                           "status": "ghost"})
    return issues


def artifact_fixed(issue: dict, pre: dict[str, float | None],
                   root: str | Path) -> bool:
    """修正轮之后复查单个问题是否已解决。

    missing → 文件现在存在即可；stale/ghost → mtime 比修正前记录变大了
    （pre 里是 None 说明核查时文件还不存在，现在存在就算修好）。
    """
    now = path_mtime(issue["path"], root)
    if now is None:
        return False
    if issue["status"] == "missing":
        return True
    m0 = pre.get(issue["path"].lower())
    if m0 is None:
        return True
    return now > m0 + _MTIME_EPS


_STATUS_TEXT = {
    "missing": "不存在于工作区",
    "stale": "存在，但修改时间与任务开始时的快照一致——本轮从未更新，现有内容不是本轮产出",
    "ghost": "存在，但整个任务没有任何写文件/执行命令的操作——它不是本轮写出的",
}


def issue_line(c: dict) -> str:
    """问题清单里的一行：修正轮提示与最终核查声明共用，保证口径一致。"""
    tag = "任务书明确要求的交付物" if c.get("declared") else "总结声称已写入"
    return f"`{c['path']}`（{tag}；{_STATUS_TEXT.get(c['status'], c['status'])}）"


def artifact_issue_block(issues: list[dict]) -> str:
    """附在最终答复末尾的产物核查声明。程序写的，模型只有解读权。"""
    lines = ["【系统侧核查】以下产出文件经系统收尾核查**不是本轮产出**："]
    for c in issues:
        lines.append(f"- {issue_line(c)}")
    lines.append("此核查由程序完成，不依赖模型自报；若与本回复正文有出入，以本清单为准。")
    return "\n".join(lines)


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


def inline_verify_note(user_text: str, root: str | Path,
                       timeout: int = 60) -> tuple[str, str]:
    """写类工具落盘后的即时复核（Aider --auto-test 同款思路）。

    任务书声明了验证命令（含行中「运行 X 确认输出 N」式）→ 程序亲自跑一遍：
    - 失败（含 exit=0 但输出不含期望值）：返回要回灌给模型的系统侧备注，
      带真实输出与期望值——错当场露馅，不等收尾闸门；
    - 通过：返回空串，绿了不打扰；
    - 没声明（提取不到、目标脚本还不存在）也返回空串：这类任务的把关交给
      收尾闸门与判定层。
    返回 (备注, 命令)；调用方负责限次（防「改挂→提醒→再改挂」死循环）。
    """
    cmd = extract_verify_cmd(user_text, root)
    if not cmd:
        return "", ""
    res = run_verify_cmd(cmd, root, timeout=timeout)
    expect = extract_expect_output(user_text, cmd)
    res = apply_expect(res, expect)
    if res["ok"]:
        return "", cmd
    out = _clip(str(res.get("output") or ""), 600)
    note = (f"[系统侧验证] 程序已亲自运行 `{cmd}` → 失败"
            + (f"（期望输出含 `{expect}`）" if expect else "")
            + f"，真实输出：\n{out}"
            + "\n请修复问题后再次确认；禁止在未修复的情况下声称完成。")
    return note, cmd


def extract_expect_output(text: str, cmd: str) -> str:
    """从「运行 <cmd> 确认输出 X」式声明里取期望输出 X，与 extract_verify_cmd 配对。

    cmd 与提取出的命令对不上、或声明里没写期望，都返回空串——此时验证退化为
    只看退出码。c1 的教训：任务书明说「确认输出 24」，旧闸门只看 exit=0，
    模型谎报「输出 24」而实际是 10，没有任何一层比对过这个数。
    """
    text = str(text or "")
    want = " ".join(str(cmd or "").strip().lower().split())
    if not want:
        return ""
    for m in _VERIFY_RUN.finditer(text):
        got = " ".join((m.group(1) or "").strip().lower().split())
        if got == want:
            return m.group(2).strip("`'\" ")
    return ""


def apply_expect(res: dict, expect: str) -> dict:
    """把「确认输出 X」的期望比对进验证结果：exit=0 但输出不含期望值也算失败。

    就地改写 res（ok/expect/mismatch），闸门与报告共用同一口径，避免
    「事件说通过、报告说失败」的分裂。没有期望声明时原样返回。
    """
    if not expect:
        return res
    res["expect"] = expect
    if res.get("ok") and expect not in str(res.get("output") or ""):
        res["ok"] = False
        res["mismatch"] = True
    return res


def report_block(cmd: str, res: dict) -> str:
    """附在最终答复末尾的系统侧验证报告。模型只有解读权，这一段是程序写的。"""
    verdict = "通过" if res["ok"] else "失败"
    head = (f"【系统侧验证】任务收尾时由系统亲自运行了 `{cmd}` → "
            f"**{verdict}**（退出码 {res['code']}，耗时 {res['duration']}s"
            + (f"，期望输出含 `{res['expect']}`" if res.get("expect") else "")
            + "）。"
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
以下产出文件经系统核查不是本轮产出：
{issues}

【你此前的总结】
{summary}

【要求】
如果这些文件确实应该由本任务产出：现在真实地创建/更新它们（write_file），写完再说完成；
如果本就无需产出（例如核查误判或路径笔误），就在最终总结里如实说明原因。
禁止再次声称已写入而不实际写文件——系统会复查文件是否真的在本轮被写过。
只输出修正动作的开始或修正后的最终总结，不要寒暄。"""


# ---------------------------------------------------------------------------
# 收尾自述核查（第五道）：**自己承认还没做 / 做不了**，也不许当成交付
# ---------------------------------------------------------------------------
# 上面三道治的是「声称做了但没做」；这是它的反面，同样致命却一直没人管：
# 模型**如实招了**「还没做」「做不了」，系统照样把这条回复当 done 收下。
#
# 事故现场（端到端实测，用户原话「其两个指令都没有执行」）：一条含两件事的消息被拆成
# 2 个子任务，各只调了一次只读工具就收尾——
#   子任务1：「…尚未发现…。**下一步：我将检索 docs/ 目录及 README.md 中的引用细节**」
#   子任务2：「**我无法直接修改自身的思维链显示机制，因为其可见性由平台底层的 UI 渲染
#             逻辑控制，而非 agent 可以通过修改代码或指令来干预的**」，随后「已记录您的
#             偏好要求」
# 两条都被判成功。用户拿到的是一句"下一步"和一句"我做不到"，而任务一步没动。
#
# 判据刻意分成两支、只认**模型自己招了**这句话：
#   defer        把「下一步 / 接下来 / 尚未完成」当交付
#   out_of_scope 把本项目的代码当成了外部平台（「无法修改…机制 / 由平台…控制」）
# 「做没做」不由这里判——那是 convo 里有没有真产生改动（ran_writes）。两支都只在
# 「任务本身就是要动手改东西」且「本轮零文件改动」时才有意义，组合逻辑见
# orchestrator._verify_gate：纯问答里一句「我无法联网核实」是如实回答，不是怠工。
_DEFER_RX = re.compile(
    r"(?:下一步|接下来|稍后|随后|后续)[：:，,\s]*(?:我|会|将|再|来|继续)"
    r"|(?:我|会|将)\s*(?:继续|再)\s*(?:检索|检查|查看|排查|分析|修改|处理|完善|补充|尝试)"
    r"|(?:尚未|还未|没能|未能)\s*(?:完成|开始|定位|实现|修改|落地|处理|交付|写|改)"
    r"|(?:暂未|待我|待稍后|待后续)\s*(?:完成|处理|实现|修改|落地)"
    r"|还将\s*(?:继续|需要)"
)
_OUT_OF_SCOPE_RX = re.compile(
    # 「无法直接修改…机制/代码/界面…」——注意中间可能夹着被引用的类名、括号注释
    r"(?:无法|不能|没法|难以)\s*(?:直接)?\s*(?:修改|改动|干预|触及|控制|调整|改变)"
    r"[^\n]{0,30}?(?:机制|逻辑|代码|界面|页面|前端|组件|渲染|显示|样式|行为|实现)"
    # 「由平台底层的 UI 渲染逻辑控制」——把宿主当成了平台
    r"|(?:由|属于)\s*(?:平台|宿主|宿主机|系统)\s*[^\n]{0,20}?(?:控制|决定|渲染|管理|负责)"
    r"|(?:不属于|不是)\s*(?:本)?项目\s*[^\n]{0,8}?(?:代码|逻辑|范围|职责)"
    r"|超出\s*(?:我|本任务|agent|本项目)\s*[^\n]{0,8}?范围"
)


def unfinished_claim(summary: str) -> str:
    """总结里是否**自己承认这件还没做、或做不了**。

    返回 ""（没有）/ "defer"（把"下一步"当交付）/ "out_of_scope"（把本项目的代码
    当成外部平台，自认改不了）。只认模型自己招的那句话——不看它到底做没做。
    """
    s = str(summary or "")
    if _OUT_OF_SCOPE_RX.search(s):
        return "out_of_scope"
    if _DEFER_RX.search(s):
        return "defer"
    return ""


def unfinished_quote(summary: str, kind: str = "") -> str:
    """把命中的**那一整句**摘出来，给修正提示当「原话」用（压成一行、限长）。

    只截匹配到的那几个字（如「下一步：我」）没有意义——修正提示里要能看见模型
    自己说的完整那句，它才知道被点的是哪一句。
    """
    s = str(summary or "")
    stops = "。！？\n"
    order = ((_OUT_OF_SCOPE_RX, _DEFER_RX) if kind == "out_of_scope"
             else (_DEFER_RX, _OUT_OF_SCOPE_RX))
    for rx in order:
        m = rx.search(s)
        if not m:
            continue
        a, b = m.start(), m.end()
        while a > 0 and s[a - 1] not in stops:      # 扩到句首
            a -= 1
        while b < len(s) and s[b] not in stops:     # 扩到句末
            b += 1
        line = " ".join(s[a:b].split())
        return line if len(line) <= 160 else line[:160] + "…"
    return ""


UNFINISHED_CORRECTIVE_PROMPT = """【系统侧核查结果（权威，不可推翻）】
系统发现：你在上一条总结里**自己承认这件事还没做 / 做不了**，而本任务全程没有产生任何文件改动。
命中原话：{quote}

【你此前的总结】
{summary}

【要求】
1. 如果是「下一步我要…」这类把计划当成交付的写法：**现在就把它做掉**——用工具真去读文件、
   改代码、跑命令。在真正做完之前，不要再写「下一步」。
2. 如果是一句「我无法修改…」这类自弃判断：你手里**就是**本项目的源码。用户口中的
   「你的界面 / 你的思维链 / 你的记忆 / 你的工具」，指的都是**这个工作区里的代码**，
   不是外部平台。先去 list_dir / search_code 找到渲染它的那个文件，能改就改。
3. 确实做不了（缺权限、缺依赖、缺信息）就在总结里如实说明**卡在哪**，不要用一句
   「下一步」把没做的事盖过去。
只输出动作的开始或修正后的最终总结，不要寒暄。"""

UNFINISHED_NOTE = (
    "【系统侧核查】本回复承认仍有未完成的工作（或自述做不到），而本任务全程没有产生任何"
    "文件改动。系统已给过一轮修正机会。此处如实标注：**这一步没有交付**。"
)
