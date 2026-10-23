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

import json
import locale
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from . import skeleton as skel
from . import evidence as evid

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

# python -c「内联比对」命令：没有脚本文件可比存在性，但声明行里明确写出的
# 内联代码照跑（f4 教训：复制类任务的逐字节比对声明全是 -c 内联代码，
# 旧口径要求「命令指向工作区内真实脚本」，这类声明整条被弃，闸门静默退化）。
_INLINE_PYC = re.compile(r"(?i)^python3?\s+-c\b")

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
    candidate = candidate.strip().strip("`").strip()
    # python -c「内联代码」：代码段整体保留（拆 token 会把 import 语句咬断），
    # 只断掉引号外的人话尾巴——「python -c "..." 必须通过」这种。
    m = re.match(r"(?i)^(python3?|py)\s+-c\s+(.*)$", candidate)
    if m:
        exe, rest = m.group(1), m.group(2).strip()
        q = rest[:1]
        if q in ('"', "'"):
            end = rest.find(q, 1)
            code = rest[1:end] if end > 0 else rest[1:]
            return f'{exe} -c "{code}"'
        return f"{exe} -c {rest}".strip()
    toks = candidate.split()
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

    只认「指向工作区内真实存在的脚本」的命令，或声明行里明确写出的
    python -c 内联比对（无脚本文件可比，已过 _DANGEROUS 过滤）；
    提取不出来或两类都不满足时返回空串（此时收尾验证退化为「声称核查」，
    见 claimed_verified）。
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
            # python -c 内联命令没有脚本文件可比存在性（见 _INLINE_PYC 注释）；
            # 声明行里明确写出的 -c 代码已过 _DANGEROUS 过滤，工作区内只读
            # 比对，风险可控——放行。
            if not _INLINE_PYC.match(cmd):
                continue
        return cmd
    return ""


# ---------------------------------------------------------------------------
# 复制/备份类任务的任务书补强（第四轮评测 f4 教训）
# ---------------------------------------------------------------------------
# f4-backup-copy 实测：模型把 draft.md 读出来再用 write_file 写回去——文本往返
# 丢了尾部换行符，两个文件肉眼看一模一样，逐字节比对才现形。任务书没写验证
# 声明，闸门无米下锅，错误直接放行。
#
# 补法由 harness 出手而不是指望模型自觉：复制/备份语义 + 恰好两个文件名 +
# 没夹带改内容的诉求 + 没有现成验证声明时，替用户往任务书追加一条 python -c
# 逐字节比对（read_bytes 原样比较 CRLF 与编码，不做文本往返）。声明进任务书后
# 三处同时生效：模型看得见（知道验收标准）、写盘后的即时验证跑它（干活时就被
# 拦住）、收尾闸门亲跑它（谎报现形）。
_COPY_SEMANTIC = re.compile(r"复制|拷贝|备份|\bcopy\b|\bbackup\b", re.I)

# 任务里夹带了「要改内容」的诉求时，逐字节一致本来就不成立——不补，宁可少拦。
_COPY_TRANSFORM = re.compile(
    r"修改|编辑|替换|删除|去掉|去除|清理|清除|转换|翻译|压缩|解压|改写|重写"
    r"|格式化|整理|追加|合并|拆分|提取|汇总|统计|重命名"
    r"|两份|多份|数份|几份|各一|互相|批量",
    re.I,
)


def declare_copy_check(message: str) -> str:
    """复制/备份类任务自动补一条「逐字节一致」的验证声明；不满足条件返回空串。

    只认保守形态：恰好出现两个不同的文件名 token（第一个为源、第二个为目标，
    与「把 A 复制一份命名为 B」的语序一致）。识别不了源/目标、任务里要顺带
    改内容、已有验证声明——一律不补，不可错拦。
    """
    text = str(message or "")
    if not _COPY_SEMANTIC.search(text):
        return ""
    if _COPY_TRANSFORM.search(text):
        return ""
    if (_DECLARE.search(text) or _DECLARE_INLINE.search(text)
            or _VERIFY_RUN.search(text)):
        return ""                      # 已有声明，不重复补
    toks: list[str] = []
    for m in re.compile(_PATH_TOK).finditer(text):
        p = m.group(1)
        if p.lower() not in {t.lower() for t in toks}:
            toks.append(p)
    if len(toks) != 2:
        return ""                      # 源/目标识别不了，不猜
    src, dst = toks
    cmd = (f'python -c "import sys,pathlib;'
           f"a=pathlib.Path('{src}').read_bytes();"
           f"b=pathlib.Path('{dst}').read_bytes();"
           'sys.exit(0 if a==b else 1)"')
    return f"\n验证：{cmd}"


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
        # 核验命令必须全自动：禁掉 git 的凭据弹窗（credential helper GUI /
        # 终端账号密码提示）——评测台跑 `git ls-remote` 类核验时曾在用户机器上
        # 弹出过凭据窗口，卡到人工输入才继续。置空 helper 后 git 退回终端提示，
        # 再用 GIT_TERMINAL_PROMPT=0 把终端提示也关死 → 需要凭据的核验会立刻
        # 失败（exit 128）而不是挂起等输入。判定回归到「命令是否通过」本身。
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
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
WRITE_TOOLS = ("write_file", "edit_file", "write_skeleton", "fill_section")

# mtime 比较容差：同一文件未修改时 stat 返回值应完全一致，留一点余量
# 防文件系统时间精度差异造成的误判。
_MTIME_EPS = 0.002


def _tool_names(convo: list[dict] | None):
    """轨迹里出现过的工具名（含 PTC 脚本内层调用，见 agentforge.tooltrace）。"""
    from . import tooltrace
    return tooltrace.tool_names(convo)


# PTC 常驻下，写盘发生在 ops_batch 的脚本里，顶层只看得到 name="ops_batch"。
# 只按工具名判定「有没有写过」会让轨迹侧证据整块消失：脚本里真写了文件，
# ran_writes 仍返回 False，于是 unfinished_gate 以为「全程零文件改动」，
# 对已经交付的答复多发一轮修正（rerun26 实测的误判形态）。这里补一条按
# 脚本源码判定的通道——只看 ops_batch 的 arguments 里有没有对写类工具的调用。
# 注意这是「尝试过写」而不是「写成功」，与 WRITE_TOOLS 的语义一致；真正
# 是否落盘仍由磁盘 mtime / file_log 台账裁定（loop 内契约闸门就是这么做的）。
_PTC_WRITE_RX = re.compile(
    # 引号可能被 JSON 转义成 \"，所以前后都允许一个可选的反斜杠
    r"""(?:['"]|\\['"])\s*(?:write_file|edit_file|write_skeleton|fill_section)"""
    r"""\s*(?:['"]|\\['"])"""
    r"""|tools\.(?:write_file|edit_file|write_skeleton|fill_section)\s*\(""")


def _ptc_write_attempted(convo: list[dict] | None) -> bool:
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
            if name != "ops_batch":
                continue
            args = tc.get("arguments", fn.get("arguments"))
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            if _PTC_WRITE_RX.search(str(args or "")):
                return True
    return False


def ran_writes(convo: list[dict] | None) -> bool:
    """任务过程中是否调用过写类工具（write_file / edit_file）。

    含 PTC 路径：ops_batch 脚本里对写类工具的调用同样算数。
    """
    return (any(n in WRITE_TOOLS for n in _tool_names(convo))
            or _ptc_write_attempted(convo))


# 写失败的回执特征：工具被拒（[错误]/[已拒绝]）、内容没落盘（未写入）、
# 或 PTC 权威块判定失败（写入失败）。带这些标记的调用不算「写过」。
_WRITE_FAIL_RX = re.compile(r"\[\s*错误\s*\]|\[\s*已拒绝\s*\]|未写入|写入失败|写入未通过")


def wrote_ok(convo: list[dict] | None) -> bool:
    """是否有一次写类调用**真的落了盘**（被核查拒绝 / 写入失败的不算）。

    rerun27 事故：模型两次 write_file 都被拦（引用路径错、写前门槛没取证），
    一个字没落盘，但「未交付硬闸」的豁免只看轨迹里出现过写类调用
    （produced_possible），于是硬闸被绕过——任务状态记成 done，磁盘上空的。
    豁免必须问「写成功过吗」，不能只问「试过吗」。

    判定材料是轨迹本身：assistant 的 tool_calls 与随后 role="tool" 的回执按
    顺序配对（回执消息不带工具名），写类工具 + 回执无失败标记 = 写成功。
    run_command 一律按「可能已产出文件」从宽（脚本生成类任务不该被误判）。
    """
    pending: list[str] = []
    for m in convo or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                if name in WRITE_TOOLS:
                    pending.append("write")
                elif name == "run_command":
                    pending.append("cmd")
                elif name == "ops_batch":
                    args = tc.get("arguments", fn.get("arguments"))
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    pending.append("ptc" if _PTC_WRITE_RX.search(str(args or "")) else "")
                else:
                    pending.append("")
        elif role == "tool":
            kind = pending.pop(0) if pending else ""
            if not kind:
                continue
            text = str(m.get("content") or "")
            if kind == "cmd":
                return True
            if kind == "write":
                if not _WRITE_FAIL_RX.search(text[:400]):
                    return True
            elif kind == "ptc":
                # 权威块明说已确认落盘；没有权威块（旧路径）时按「无失败标记」从宽
                if "已写入磁盘" in text:
                    return True
                if not _WRITE_FAIL_RX.search(text):
                    return True
    # 有调用但没等到回执（上下文被压缩 / 接力只带了过去的一部分）：
    # 不能说它没写成——拿不到回执就不许判死，宁可放过也不误伤真交付。
    if any(k in ("write", "ptc", "cmd") for k in pending):
        return True
    return False


def produced_possible(convo: list[dict] | None) -> bool:
    """任务里是否出现过任何可能产出文件的操作（写类工具或 run_command——
    命令也可能合法地产出文件，比如跑脚本生成报告；两者都没有时，
    「已写入 X」才板上钉钉是假的）。PTC 脚本内的写类调用同样算数。"""
    return (any(n in WRITE_TOOLS or n == "run_command" for n in _tool_names(convo))
            or _ptc_write_attempted(convo))


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
                         produced: bool | None = None,
                         produced_paths: list[str] | None = None,
                         task_start_ts: float | None = None,
                         required: list[str] | None = None) -> list[dict]:
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
    produced_paths 是「任务真实产出过的文件」权威清单（来自执行轨迹，按解析
    绝对路径匹配）：只要某个声称/声明文件在其中，即使 produced is False（典型
    如收尾闸门 convo 只有 [任务原文, 拟回答]、ran_writes 缺位）也不得误判为
    ghost——这是复测踩过的验证竞态：真实写出的交付物被当成「全程零写操作」。
    task_start_ts 是任务开始的墙上时钟（time.time()）：声明交付物只要【当前
    mtime 晚于任务开始】就认定本轮产出——比逐文件「开局快照 mtime」更稳，能
    正确处理「文件任务开始前就存在、但本轮被重写」的情况，且不受「闸门抢在最终
    写盘前检查」的竞态影响（只要写盘发生在任务开始后，闸门任意时刻复查都认）。
    """
    root = Path(root).resolve()
    produced_set: set[str] = set()
    if produced_paths:
        for pp in produced_paths:
            t2 = _resolve_inside(str(pp), root)
            if t2 is not None:
                produced_set.add(str(t2.resolve()))
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
        # 骨架未填完 = 没交付。必须放在 mtime / produced 那些分支**之前**：
        # write_skeleton 会真的落盘并刷新 mtime，若先走「mtime 比任务开始新」
        # 就被判成本轮产出——那等于写个空架子就能骗过存在性核查。
        if skel.unfinished_file(root, p):
            issues.append({"path": p, "declared": bool(declared),
                           "status": "skeleton"})
            continue
        # 覆盖核查（任务书必填项）：文件写全了、引用也对，但**该写的项没写**。
        # rerun29 就是这种：十项要求写了八项，引用核查一处没错，缺的两项谁也没管。
        # required 来自任务书里显式列举的项（skeleton.required_items），判据是
        # 「字面是否出现在交付物里」——不要求按这个标题分节，只要求真的写了。
        if required and declared:
            miss = skel.required_missing(root, p, required)
            if miss:
                issues.append({"path": p, "declared": True,
                               "status": "incomplete", "missing": miss})
                continue
        # 表格证据列核查（表头即契约）：不管文件是怎么落盘的（写闸、cp、手工改），
        # 收尾只看磁盘内容——证据列缺可定位 path:行号 = 证据无法回源。rerun39
        # 实证：run_command cp 把无锚点草稿盖过写闸，交付 0 引用还全绿。
        # ledger=None：收尾只验结构（锚点存在、路径真实、行号在文件内），
        # 「是否落在已读区间」由写入时的闸门负责。
        if declared:
            try:
                ev = evid.check_table_evidence(
                    target.read_text(encoding="utf-8", errors="replace"),
                    ledger=None, root=root, skip_placeholder_rows=True)
            except OSError:
                ev = []
            if ev:
                issues.append({"path": p, "declared": True,
                               "status": "evidence", "problems": ev[:6]})
                continue
        # 执行轨迹权威记录（来自全局 file_log 的本任务真实写操作）：只要本任务
        # 确实写出过该文件，无论 mtime 快照怎么判（stale/ghost 都源于「开局快照
        # 早于最终写盘 / convo 缺写工具记录」的竞态），一律认定本轮产出、跳过判定。
        # 必须放在 declared+snapshot 分支【之前】，否则声明交付物会被该分支直接
        # continue 掉，导致本检查对声明交付物永远不可达（复测踩过的死代码坑）。
        if target is not None and produced_set and str(target.resolve()) in produced_set:
            continue
        # 任务开始时间戳判定（最稳健，优先于逐文件快照）：声明交付物只要当前 mtime
        # 晚于任务开始，即认定本轮产出——正确处理「文件任务前就存在、但本轮被重写」
        # 的情况，且不受「闸门抢在最终写盘前检查」的竞态影响（写盘发生在任务开始后，
        # 闸门任意时刻复查都认）。task_start_ts 为 None 时退化为逐文件快照判定。
        if declared and task_start_ts is not None:
            try:
                now = target.stat().st_mtime
            except OSError:
                now = None
            if now is not None and now > task_start_ts + _MTIME_EPS:
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
            # 任务中途真实写出过的文件（执行轨迹权威记录），即便最终 convo 缺写工具
            # 调用也不许误判为 ghost——修复验证竞态（见函数 docstring）。
            if produced_set and target is not None and str(target.resolve()) in produced_set:
                continue
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
    if issue["status"] == "incomplete":
        # 缺项是内容判定，mtime 变了不算数：由调用方用 find_artifact_issues
        # 带上 required 重算（见 orchestrator 的产物核查修正轮）。
        return False
    if issue["status"] == "skeleton":
        # 骨架补齐后 mtime 当然也变了，但真正的判据是「还有没有空小节」——
        # 又填了一节、还是空着一半，都算没修好。
        return not skel.unfinished_file(root, issue["path"])
    if issue["status"] == "evidence":
        # 证据列是内容判定，mtime 变了不算数：由调用方用 find_artifact_issues
        # 重算（磁盘内容里证据列有可定位锚点才算修好）。
        return False
    m0 = pre.get(issue["path"].lower())
    if m0 is None:
        return True
    return now > m0 + _MTIME_EPS


_STATUS_TEXT = {
    "missing": "不存在于工作区",
    "stale": "存在，但修改时间与任务开始时的快照一致——本轮从未更新，现有内容不是本轮产出",
    "ghost": "存在，但整个任务没有任何写文件/执行命令的操作——它不是本轮写出的",
    "incomplete": "存在，但任务书要求覆盖的项没写全",
    "skeleton": "只有骨架：小节标题在，正文还是空的或只有占位符——不算交付，用 fill_section 逐节填完",
    "evidence": "已填完，但证据列缺可定位的 `path:行号`（或行号对不上真实文件）——证据无法回源",
}


def issue_line(c: dict) -> str:
    """问题清单里的一行：修正轮提示与最终核查声明共用，保证口径一致。"""
    tag = "任务书明确要求的交付物" if c.get("declared") else "总结声称已写入"
    if c.get("status") == "incomplete":
        miss = "、".join(str(x) for x in (c.get("missing") or [])[:10])
        return (f"`{c['path']}`（{tag}；缺任务书点名要覆盖的项：{miss}）"
                "——把这些项各写一行/一节，附上证据行号")
    if c.get("status") == "evidence":
        probs = "；".join(str(x) for x in (c.get("problems") or [])[:4])
        return (f"`{c['path']}`（{tag}；{probs}）"
                "——证据列用 `path:起-止` 引用你 read_file 读过的行")
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


def report_block(cmd: str, res: dict, write_failed: list[str] | None = None) -> str:
    """附在最终答复末尾的系统侧验证报告。模型只有解读权，这一段是程序写的。

    `write_failed` 是本轮末次写盘仍失败的产物路径。有它的时候**不能**再说
    「以本结果为准」：验证命令是在**现有文件**上跑的，改动一个字都没发生也照样
    能过（P3，rerun64 的 c6 就是这样拿到程序背书的）。这时改为明确限定
    自验的覆盖范围，并点名哪些文件没写成。
    """
    verdict = "通过" if res["ok"] else "失败"
    head = (f"【系统侧验证】任务收尾时由系统亲自运行了 `{cmd}` → "
            f"**{verdict}**（退出码 {res['code']}，耗时 {res['duration']}s"
            + (f"，期望输出含 `{res['expect']}`" if res.get("expect") else "")
            + "）。")
    if write_failed:
        head += ("此结果由程序产生，但它只证明**命令能跑通**，不证明改动发生过；"
                 "本轮末次写盘仍失败：" + "、".join(write_failed[:4])
                 + "。**不要据此声明任务已完成。**")
    else:
        head += "此结果由程序产生，不依赖模型自报；若与本回复正文有出入，以本结果为准。"
    if res["ok"]:
        return head
    return head + "\n验证输出（节选）：\n```\n" + _clip(res["output"], 800) + "\n```"


# 权威写盘块的逐行格式：`  - write_file → reports/x.md: 已写入磁盘`
# 第三态「已存在未变」= 幂等层拦下的重复写：文件内容本来就是这样，既不是失败
# （收尾不该判它没写），也不是一次落盘（不能计入"写过几次"）。
_WRITE_ECHO_RX = re.compile(
    r"-\s*(?P<tool>[a-zA-Z_][\w]*)\s*→\s*(?P<path>[^:：\n]+?)\s*[:：]\s*"
    r"(?P<state>已写入磁盘|已存在未变|写入失败)")


def write_outcomes(convo: list[dict] | None) -> dict[str, bool]:
    """按产物路径汇总**末次**写盘结果（True=已写入，False=仍失败）。

    只认 harness 自己印的【系统核对·写盘结果】块——脚本里的
    `print("updated successfully")` 不算（rerun64 的 c6 正是靠这句假 print
    撑起"已完成"）。为什么取「末次」：一次失败后重试成功属于自愈
    （soak02 的 c6），只有末次仍失败才说明文件真没改成。
    """
    last: dict[str, bool] = {}
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        text = str(m.get("content") or "")
        if "【系统核对·写盘结果】" not in text:
            continue
        for hit in _WRITE_ECHO_RX.finditer(text):
            path = hit.group("path").strip()
            if path in ("(未给 path)", "未给 path"):
                continue
            # 「已存在未变」算成功：文件内容与模型这次给的完全一致，
            # 交付物是对的，只是没重复落盘。
            last[path] = hit.group("state") in ("已写入磁盘", "已存在未变")
    return last



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
如果文件里已经有一份骨架（只有 `## 小节标题`、正文为空），用 fill_section 逐节填完即可，
不要整篇重写——骨架里的结构是对的，重写只会再烧一次整篇生成。
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


def delivery_failed_block(missing: list[str], skeleton_only: bool = False) -> str:
    """「只说不做」的终态声明：任务书点名要写出、复核后仍未交付。

    与 artifact_issue_block 的区别是**定性**：那个说「这些文件不是本轮产出」，
    这个直接判定「本任务未完成」——因为答复只可能是计划、分析或空壳骨架，
    不构成交付（rerun26：7 轮全在写「下一步计划」，一次工具都没调）。

    skeleton_only=True：写了骨架、正文一节没填。写动作确实发生过，但交付物
    客观上是空壳，同样不是交付——这条判据来自「骨架先行」这个新机制：
    否则模型写个标题就能满足「文件存在」。
    """
    lines = "\n".join(f"- {p}" for p in (missing or []))
    why = ("只写了骨架（小节标题在，正文是空的），没有任何一节被填上"
           if skeleton_only
           else "全程**没有任何写盘动作**，文件也不存在")
    return (
        "【系统侧核查 · 任务未交付】\n"
        f"任务书要求写出以下文件，但{why}：\n{lines}\n"
        "上面的内容是计划、分析或空架子，不是交付物。系统在收到答复后已要求补写，未执行。\n"
        "本任务据此标记为**未完成**。"
    )



# ---------------------------------------------------------------------------
# 停止路径的交付物存在性核查（rerun12 事故的直接补丁）
#
# 正常完成路径有 verify gate 多道核查兜底；手动停止走 stopped 事件直通用户，
# 任何核查都不经过——rerun12 里员工 7 次写交付物全被引用守卫拒收、文件从未
# 落盘，最终答复却声称「已写入并验证」并附了伪造的自检结果，就这样直达了
# 用户。这里在停止时补一道**纯程序**的最低限度核查：任务书点名的交付物
# （带扩展名的路径 token）逐一验存在性，缺了就在停止说明里如实标注。
# 只做存在性、不做引用审计——审计是正常路径 verify gate 的职责；停止时以
# 最快速度给用户真话即可，不做任何模型调用。
# ---------------------------------------------------------------------------
_GOAL_FILE_RX = re.compile(
    r"[\w\u4e00-\u9fff.\-/\\]+\.(?:txt|md|markdown|py|js|ts|jsx|tsx|json|ya?ml|"
    r"html?|css|csv|log|xml|ini|cfg|toml|bat|sh|ps1|xlsx?|docx?|pptx?|pdf|sql|env)\b",
    re.I,
)


def stop_integrity_note(goal: str, root: str | Path) -> str:
    """任务书点名、但磁盘上不存在的交付物清单；无缺失返回空串。

    与 artifact 核查用同一个 _resolve_inside：只认工作区内的相对路径，
    逃出工作区 / 解析失败的一律跳过（不猜、不误报）。
    """
    rootp = Path(root).resolve()
    missing: list[str] = []
    seen: set[str] = set()
    for raw in _GOAL_FILE_RX.findall(goal or ""):
        p = raw.strip().strip("`'\"").strip()
        k = p.lower()
        if not p or k in seen:
            continue
        seen.add(k)
        try:
            target = _resolve_inside(p, rootp)
        except (OSError, ValueError):
            target = None
        if target is not None and target.exists():
            continue
        missing.append(p)
    if not missing:
        return ""
    return (
        "【系统侧核查】任务被中途停止，且任务书点名的交付物在磁盘上不存在："
        + "；".join(missing)
        + "。本回复中任何「已完成 / 已写入」的表述仅代表停止时刻的模型说法，"
        "请以文件系统为准。"
    )


# ---------------------------------------------------------------------------
# 关键事实回显核查（收口「答案必须进答复」，w3 类 norm 失败的 harness 侧兜底）
#
# 任务书里明确要求「保留 X / 确保 Y / 写明 Z」时，程序从这些要求句里提取**硬 token**
# （引号内容 / 时间 / 日期 / 数字），汇总后比对最终答复是否包含；缺了给一轮修正，
# 仍缺就在答复末尾附系统侧声明。判定纯程序比对，与评测 judge 的 contains_norm 同思路，
# 但从「事后判卷」前移到「收尾前逼模型补上」。
# ---------------------------------------------------------------------------

# 要求句信号：句子里出现这些词，才认为是任务书在点名「答复里必须有」的内容。
_REQ_SENT_RX = re.compile(
    r"(?:保留|写明|注明|写上|标出|务必|必须|一定要|不要漏|确保|包含|含有|给出|回答|记住|体现)"
)

# 硬 token：引号内容 / 时间 / 日期 / 数字（含千分位、小数、百分号、万/亿）。
_QUOTED_RX = re.compile(r"[「『\"']([^「』\"'\n]{1,40})[」』\"']")
_TIME_RX = re.compile(r"\d{1,2}[:：]\d{2}|\d{1,2}点(?:\d{1,2}分?)?")
# 日期要求月/日至少有其一：`[-/年]` 后全可选会把路径段「reports/2026/」里的
# 裸「2026/」当成日期（R19 f1 端到端回归）。
_DATE_RX = re.compile(r"(?:19|20)\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2})?[日号]?")
_NUM_RX = re.compile(r"\d[\d,]*(?:\.\d+)?(?:%|万|亿)?")
# 掩码用：路径段（reports/2026、docs/r.md）与反引号代码里的数字是**定位符**，
# 不是要回显的事实，先掩掉再取数字。
_PATH_RX = re.compile(r"(?:[\w\u4e00-\u9fff.-]+[\\/])+(?:[\w\u4e00-\u9fff.-]+)?")
_CODE_RX = re.compile(r"`[^`\n]+`")
# 三类「长得像事实、其实不是」的数字，先掩掉再取（rerun31 实测：它们让事实闸门
# 误报『答复缺 2、5、31』，白烧一轮修正，模型还把这三个 token 抄进了交付物底部）：
#   ① 标识符里的编号：rerun31 / s07 / v1.2 —— 数字属于名字，不是要回显的事实；
#   ② 行首的序号：`2. 与 AgentForge 本体…` 的 2 是列表序号；
#   ③ 数量上限：`最多 5 条` 的 5 是格式约束（答复按格式写自然会带上）。
_IDENT_RX = re.compile(r"[A-Za-z][A-Za-z_\-]*\d+[\w.\-]*|\d+[A-Za-z][\w.\-]*")
_ORDINAL_RX = re.compile(r"^\s*\d{1,2}[.、)）]", re.MULTILINE)
_QUANTITY_RX = re.compile(r"(?:最多|至多|不超过)\s*\d+\s*[条个项类种行字]?")

MAX_FACT_TOKENS = 8      # 要求句里最多取几个 token（防极端任务书刷屏）


def _fact_norm(s: str) -> str:
    """比对口径：NFKC 全半角统一 → 小写 → 去空白与常见标点。

    「14：00」与「14:00」、「1,000」与「1000」算同一个事实；中文数字不转
    （「三个」≠「3」，宁可修正一轮让模型写准，也不猜）。
    """
    s = unicodedata.normalize("NFKC", str(s or "")).casefold()
    return re.sub(r"[\s，。：:、,;；！!？?·\-—_/\\()（）\"'「」『』]+", "", s)


def _mask(buf: list[str], spans: list[tuple[int, int]]) -> None:
    for a, b in spans:
        for i in range(max(0, a), min(len(buf), b)):
            buf[i] = " "


def _tokens_from_sentence(s: str) -> list[str]:
    """一个要求句里的硬 token：引号 / 时间 / 日期 / 代码 / 路径先取或掩掉，
    最后取数字。

    掩码是必须的：不掩的话 _NUM_RX 会把「14:00」再拆出「14」「00」两条假 token；
    路径段（reports/2026）里的数字是定位符，同样不能当事实。
    """
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    for rx in (_QUOTED_RX, _TIME_RX, _DATE_RX):
        for m in rx.finditer(s):
            t = (m.group(1) if m.lastindex else m.group(0)).strip()
            if t:
                out.append(t)
            spans.append(m.span())          # 引号取内容，但掩掉整个引号跨度
    for rx in (_CODE_RX, _PATH_RX, _IDENT_RX, _ORDINAL_RX, _QUANTITY_RX):
        for m in rx.finditer(s):
            spans.append(m.span())          # 只掩不取
    if spans:
        buf = list(s)
        _mask(buf, spans)
        s2 = "".join(buf)
    else:
        s2 = s
    for m in _NUM_RX.finditer(s2):
        t = m.group(0).strip()
        if t:
            out.append(t)
    return out


# 子句分隔只用中文逗号/分号：ASCII 逗号会拆断「1,024」这类千分位数字
_CLAUSE_SPLIT_RX = re.compile(r"[，；]")
_PROHIBITION_RX = re.compile(r"禁止|不得|不要|严禁|不许|勿")


def required_fact_tokens(text: str, cap: int = MAX_FACT_TOKENS) -> list[str]:
    """从任务书的要求句里提取「答复必须包含」的硬 token（去重保序限量）。

    只看含要求信号的句子；「验证：…」行里的数字是验收参数，不是要回显的事实，
    整行排除。宁漏勿误：没有要求句就没有 token。

    子句级禁止排除（rerun7 实测教训）：「每条结论必须有证据支撑，禁止凭印象写
    『对方如何如何』」整句含「必须」被当成要求句，否定示例里的引号内容反而成了
    「答复必须包含」的事实，最终验收被这条假阳性拖成未通过。因此要求句内部再按
    子句拆分——凡含禁止标记的子句一律不产出 token（禁止的是「不要写 X」，
    X 自然不是要求项），其余子句照常提取。
    """
    txt = str(text or "")
    # 排除验证命令行：那里的数字（预期输出、退出码）是给闸门亲跑用的
    lines = [ln for ln in txt.splitlines()
             if not ln.strip().startswith(("验证", "verify"))]
    sents = re.split(r"[。！？\n；;]", "\n".join(lines))
    out: list[str] = []
    seen: set[str] = set()
    for s in sents:
        if not _REQ_SENT_RX.search(s):
            continue
        # 子句级禁止排除：含禁止标记的子句不产出要求 token（见函数 docstring）
        clauses = [c for c in _CLAUSE_SPLIT_RX.split(s)
                   if c.strip() and not _PROHIBITION_RX.search(c)]
        for c in clauses:
            for t in _tokens_from_sentence(c):
                if len(t) > 40:
                    continue
                k = _fact_norm(t)
                if not k or k in seen:
                    continue
                seen.add(k)
                out.append(t)
                if len(out) >= cap:
                    return out
    return out


def artifacts_text(paths, root, *, limit: int = 6, max_chars: int = 20000) -> str:
    """把「本轮写成过的产物」读成一段文本，供关键事实比对（P1）。

    只读存在的、不太大的文本文件；读不到就跳过——这里只做「找到了就算存在」，
    读失败绝不能反过来变成新的误报。
    """
    base = Path(root) if root else Path(".")
    chunks: list[str] = []
    total = 0
    seen: set[str] = set()
    for p in paths or []:
        s = str(p or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        fp = Path(s)
        if not fp.is_absolute():
            fp = base / fp
        try:
            if not fp.is_file() or fp.stat().st_size > 400_000:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(text)
        total += len(text)
        if len(chunks) >= limit or total >= max_chars:
            break
    return "\n".join(chunks)[:max_chars]


def missing_required_facts(summary: str, text: str, *,
                          artifacts: str = "") -> list[str]:
    """任务书要求的关键事实里，哪些在答复**和已写出的产物里都**找不到。

    `artifacts` 只传「本轮确认写成过的文件内容」。写盘类任务里产物才是主出口：
    w5（rerun65 / soak03）文件里三个标题齐全、答复没复述，旧口径照样判
    「未能核对到」——误伤（P1）。命中产物即不再声明，修正轮也只针对
    「两边都没有」的事实。
    """
    toks = required_fact_tokens(text)
    if not toks:
        return []
    hay = _fact_norm(summary)
    if artifacts:
        hay += "\n" + _fact_norm(artifacts)
    return [t for t in toks if _fact_norm(t) not in hay]


def unrestated_facts(summary: str, text: str, *, artifacts: str = "") -> list[str]:
    """产物里有、答复里没复述的关键事实（只提示，不计缺失、不打回）。"""
    toks = required_fact_tokens(text)
    if not toks or not artifacts:
        return []
    hay = _fact_norm(summary)
    art = _fact_norm(artifacts)
    return [t for t in toks if _fact_norm(t) not in hay and _fact_norm(t) in art]


FACTS_CORRECTIVE_PROMPT = """【系统侧核查 · 需要修正】
任务书里明确要求保留 / 给出以下关键内容，但你的答复里没有出现：

{tokens}

请重新给出答复：把这些内容**原样写进去**（数字、时间、日期、引号里的文字一字不改，
不要换成中文数字或约略说法）。
若某项确实无法给出，如实说明原因，不要编造。
你刚才的答复：
{summary}"""


def missing_facts_block(tokens: list[str]) -> str:
    """附在最终答复末尾的系统侧声明。程序写的，模型只有解读权。"""
    lines = [
        "【系统侧核查】任务书明确要求保留 / 给出下列关键内容，系统在最终答复里"
        "未能核对到它们（已给过一轮修正机会）：",
    ]
    lines += [f"- {t}" for t in tokens]
    lines.append("请以产出文件为准核对这些事实。（此核查由程序完成，不依赖模型自报。）")
    return "\n".join(lines)


def unrestated_facts_block(tokens: list[str]) -> str:
    """产物里有、答复里没复述：只提醒，不判缺失（P1 的措辞口径）。"""
    lines = ["【系统侧核查】下列关键内容**已确认写在产出文件里**，但最终答复没有复述"
             "（不算缺失，仅供核对是否需要补充说明）："]
    lines += [f"- {t}" for t in tokens]
    return "\n".join(lines)
