"""最终答复的「操作类」事实核查（Option A）。

问题现场（rerun51）：模型 fetch 了 README，但最终答复的「怎么修改前端」一段退化成
通用模板——写了 `python main.py`、`npm run dev`、`src/components`、`frontend/` 目录等
在源里根本不存在的内容。闸门（防伪造引用）没被绕过，所以是「答非所问 + 内容注水」，
不是假交付。

修法（用户拍板 A 方案）：对「怎么操作/修改/使用某项目」类问题，要求最终答复里出现的具体
命令/目录指令必须能在本次 fetch 的源（README / 网页 / 已读文件）里找到对应内容；找不到
就打回，让模型只陈述它实际看到的，或如实说「源里没提」。

设计边界（避免过拟合，用户此前反复强调）：
- 只在任务是「操作/修改/使用」意图、且本任务确实 fetch 过源时才启用；
- 只抓**强信号**的具体指令（带反引号的运行命令、`src/components`、`frontend/` 等目录），
  放过 `git clone`/`npm install`/`cd` 这类放之四海皆准的通用开发动作，避免误伤；
- 源 = 模型本任务真正看到的工具结果（history 里所有 role=="tool" 的内容），即「你写的东西
  得有你刚读到的材料撑着」。
"""

import re

# 任务意图：问「怎么操作 / 修改 / 使用 / 运行」某个项目 / 前端 / 代码 / 源码
_HOWTO_INTENT_RX = re.compile(
    r"(怎么|如何|怎样|操作步骤|运行方式|如何操作|怎么操作|修改.{0,4}(前端|代码|源码|项目)"
    r"|操作.{0,4}(前端|代码|源码|项目)|使用.{0,4}(项目|工具|库|前端)|入门|上手|搭建|配置)",
    re.I,
)

# 强信号的具体命令（带反引号或显式「运行/执行」）：这些是 rerun51 注水重灾区
_CMD_TAIL_RX = re.compile(
    r"(?:npm run\s+\w+|pnpm run\s+\w+|yarn\s+\w+|bun run\s+\w+"
    r"|python3?\s+[\w./\\-]+\.py|py\s+[\w./\\-]+\.py"
    r"|node\s+[\w./\\-]+\.(?:js|mjs|cjs)|npx\s+[\w./\\-]+"
    r"|pip3?\s+install|cargo\s+(run|build)|go\s+run|make\s+\w+)",
    re.I,
)
# 带反引号的整段命令（行内反引号，不允许跨换行——否则会误吞 ```bash 围栏块 的开闭符号）
_BACKTICK_RX = re.compile(r"`([^`\n]{1,120})`")
# 显式「运行/执行 X」
_RUN_RX = re.compile(r"(?:运行|执行|使用命令|输入)\s*[`\"「]?([^`\"」\n]{2,60})", re.I)
# 目录级强信号：这些是具体路径，不是泛词（尾部用 (?=\W|$) 而非 \b，
# 因为 frontend/ 后常接中文标点，/ 与中文标点之间无词边界会漏匹配）
_DIR_RX = re.compile(
    r"\b(src/components|frontend/|frontend\s*目录|web/\s*目录|web\s*目录|ui/\s*目录|ui\s*目录"
    r"|app/|bin/|lib/|public/|src/)(?=\W|$)",
)
# 通用开发动作用白名单，命中即视为可靠、不再要求溯源（防误伤）
_UNIVERSAL_RX = re.compile(
    r"(git clone|git init|git pull|git checkout|npm install|npm i\b|pnpm install|yarn install"
    r"|pip install|pip3 install|cd\s+\S|ls\b|cat\s+\S|mkdir|cp\s|mv\s|docker (run|build|compose)"
    r"|clone (the|a|this|该|这个|这个) ?(repo|repository|仓库|项目))",
    re.I,
)

# 围栏代码块（```bash ... ```），里面常写真正的运行命令
_FENCE_RX = re.compile(r"```[^\n]*\n(.*?)```", re.S)
# 命令/路径里的高频泛词：不作为「项目专属 token」来判定有据（避免 main/dev/components
# 这类通用词在源里随便出现就误判为 grounded）。只有项目专属词（react/storybook/figma/
# 项目名等）出现才放行。
_GENERIC_TOKENS = {
    "npm", "pnpm", "yarn", "bun", "node", "python", "python3", "py", "pip", "pip3",
    "cargo", "go", "make", "npx", "deno", "ruby", "php", "tsc", "run", "install",
    "bin", "src", "lib", "app", "web", "ui", "frontend", "public", "components",
    "directory", "folder", "the", "this", "with", "your", "from", "into", "using",
    "via", "for", "main", "test", "build", "start", "init", "config", "setup",
    "example", "default", "local", "global", "clone", "repo", "project", "tool",
    "command", "file", "files", "path", "code", "source", "read", "write", "create",
    "add", "use", "dev", "server", "script", "scripts", "package", "module",
}

# 「说明源里没有」的诚实表述：模型若已如实声明没找到，就不算注水
_HONEST_RX = re.compile(
    r"(未(在|提供|提及|找到|说明)|没有.{0,6}(提到|提供|说明|步骤|文档)|官方(文档|仓库)?(未|没)"
    r"(提供|提及|说明|写)|README.{0,6}(没|未)(提|写|说)|无法(确定|找到)|不确定|需(要|自行)"
    r"(查阅|参考|查看).{0,6}(官方|源码|仓库|README))",
    re.I,
)

# 「声称已溯源 / 已核实」：模型若明示自己引用了官方文档、已验证，但本任务没抓过任何源，
# 属于 rerun54 实况的谎称——零抓取却写「验证通过：所有命令均参考官方文档」。
_ASSERT_SOURCED_RX = re.compile(
    r"(参考(了|官方|官方文档)|根据官方|已验证|已核实|已确认.{0,6}(来自|基于)"
    r"|验证通过|抓(到|取)了|fetch(了|到)|来自(官方|源码|仓库|文档)|一手来源|实际(读|抓))",
    re.I,
)
# 已知命令/工具名（用于判断一段文本是否「形如命令行调用」）。
# 注意：bin 之后必须跟空白/标点/结尾，不能跟连字符——否则 `marp-core` 这类 npm 包名
# 会被 `\b` 误判为命令（marp 与 -core 之间恰是词边界）。
_KNOWN_BIN_RX = re.compile(
    r"^(?:npm|npx|pnpm|yarn|bun|node|python3?|py|pip3?|cargo|go|make|deno|ruby|php"
    r"|tsc|docker|git|curl|wget|java|javac|gradle|mvn|kubectl|terraform|marp|slidev"
    r"|vite|webpack|eslint|jest|ts-node|dotnet)(?=[\s.,;:!?]|$)",
    re.I,
)


def _asserts_sourced(reply: str) -> bool:
    return bool(_ASSERT_SOURCED_RX.search(reply or ""))


def _is_cli_shape(s: str) -> bool:
    """一段文本是否「形如命令行调用」：匹配强信号命令形态、以已知 bin 开头、或带 --x 标志。

    排除纯标志（如 `-o` / `--version` 单独出现，那只是参数说明不是命令）和 npm 包名
    （如 `marp-core`，bin 后接连字符，已被 _KNOWN_BIN_RX 的边界排除）。
    """
    s = (s or "").strip()
    if not s:
        return False
    if re.match(r"^[\u4e00-\u9fff]", s):        # 中文说明句
        return False
    if re.fullmatch(r"-{1,2}[A-Za-z0-9][\w-]*", s):  # 纯标志，无命令词
        return False
    return bool(_CMD_TAIL_RX.search(s) or _KNOWN_BIN_RX.match(s)
                or re.search(r"(?<![\w-])--?[A-Za-z]", s))


def _fence_command_lines(reply: str) -> list[str]:
    """从围栏代码块（```bash ... ```）里抽取命令行。

    只收「命令行形态」的行，跳过 JSON/HTML/中文说明等非命令内容，避免把样本配置或
    说明文字当成操作指令。rerun55 实证：模型把 `marp deck.md --pptx` 写在围栏块里，
    原抽取器只认行内反引号会漏掉，导致闸门对「正确格式化」的注水回复也放行。
    """
    out: list[str] = []
    for m in _FENCE_RX.finditer(reply or ""):
        for line in m.group(1).splitlines():
            s = line.strip()
            s = re.sub(r"^[`$>%]+\s*", "", s).strip()  # 去 shell 提示符 $ > %
            if not s or s[0] in "<{([":                # JSON / HTML / 配置
                continue
            if _is_cli_shape(s):
                out.append(s)
    return out


def _inline_backtick_commands(reply: str) -> list[str]:
    """行内反引号 `` `cmd` `` 里的命令片段（去掉围栏块，围栏由 _fence_command_lines 处理）。"""
    out: list[str] = []
    for m in _BACKTICK_RX.finditer(reply or ""):
        inner = m.group(1).strip()
        if _is_cli_shape(inner):
            out.append(inner)
    return out


def _has_cli_command(reply: str) -> bool:
    """答复里是否出现「形如命令行调用」的片段（行内反引号 或 围栏代码块，带标志或以已知 bin 开头）。"""
    for s in _fence_command_lines(reply):
        if _is_cli_shape(s):
            return True
    for s in _inline_backtick_commands(reply):
        if _is_cli_shape(s):
            return True
    return False


_ZERO_SRC_MSG = (
    "你这份答复属于「操作/使用类」问题，但本次没有任何抓取记录"
    "（history 里没有 web_search / fetch 的工具结果），却给出了具体运行命令或声称已参考官方文档。"
    "在 PTC 常驻模式下，所有检索都必须经 ops_batch 脚本调用（脚本内 tools.call('web_search', ...)）。"
    "请先用 ops_batch 调 web_search 抓到该项目的真实官方文档，再把文档里真实存在的命令/目录写进答复；"
    "若确实没抓到，就如实说明「未抓取、无法确认」，不要凭记忆编造命令或谎称已验证。"
)


def is_howto_intent(text: str) -> bool:
    """任务书是不是「怎么操作/修改/使用」类问题。"""
    return bool(_HOWTO_INTENT_RX.search(text or ""))


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def extract_ops_claims(reply: str) -> list[str]:
    """从最终答复里抽出具体的操作指令（强信号），去重返回。"""
    raw: list[str] = []
    # 1) 行内反引号命令 + 1.5) 围栏代码块里的命令（rerun55：命令写在 ```bash 块里，原抽取器漏抓）
    for s in _inline_backtick_commands(reply or ""):
        raw.append(s)
    for s in _fence_command_lines(reply or ""):
        raw.append(s)
    # 2) 显式「运行/执行 X」
    for m in _RUN_RX.finditer(reply or ""):
        seg = m.group(1).strip().strip("`\"「」")
        if _CMD_TAIL_RX.search(seg):
            raw.append(seg)
        else:
            # 非强信号命令就不收（避免把「运行测试」这种泛词当指令）
            pass
    # 3) 目录级强信号
    for m in _DIR_RX.finditer(reply or ""):
        raw.append(m.group(1).strip())
    # 去重 + 规范
    out, seen = [], set()
    for r in raw:
        n = _normalize(r)
        if n and n not in seen:
            seen.add(n)
            out.append(r)
    return out


# ---- 源锚点 / 命令主体 精确匹配（fix: 任意常见词子串命中即放行 → 精确锚点）----
_GENERIC_VERB_PREFIX = {
    "npm", "npx", "pnpm", "yarn", "bun", "deno", "node", "python", "python3", "py",
    "pip", "pip3", "cargo", "go", "make", "ruby", "php", "tsc", "docker", "git",
    "curl", "wget", "java", "javac", "gradle", "mvn", "kubectl", "terraform",
    "vite", "webpack", "eslint", "jest", "ts-node", "dotnet", "i",
}
_PKG_INSTALL_RX = re.compile(
    r"^(?:npm\s+install|npm\s+i\b|pnpm\s+install|yarn\s+install|yarn\s+add"
    r"|pnpm\s+add|bun\s+add|pip3?\s+install)\b", re.I,
)


def _norm_anchor(t: str) -> str:
    """归一化锚点 token：小写、去首尾空白与标点，保留 / @ . - 分隔符。"""
    return t.strip().strip("`\"'「」()[]{}:;,.\\\u300c\u300d").lower()


def _source_anchor_set(sources: str) -> set[str]:
    """源里的高辨识度锚点 token 集合：含 / @ . - 的复合词（包名/路径/文件名/项目名）。

    用作「命令主体/包名」的精确匹配基准——避免 `marp`/`pptx` 这类常见词在源里随便
    出现就把整条命令误判有据（rerun55/rerun56 实证：marp 仓库名 + pptx 格式词就让
    `marp deck.md --pptx` 被「任意 token 命中」逻辑放行，尽管源里根本没有这条命令）。
    """
    out: set[str] = set()
    for m in re.finditer(r"[\w]+(?:[./@\-][\w@./\-]+)+", sources or ""):
        out.add(_norm_anchor(m.group(0)))
    for m in re.finditer(r"[\w]+-(?:[a-z0-9]+(?:-[a-z0-9]+)*)", sources or "", re.I):
        out.add(_norm_anchor(m.group(0)))
    return out


def _cmd_subject(claim: str) -> str:
    """命令的程序/包名主体：去掉通用动词前缀后的首个实质 token。

    特殊处理 `npm run` / `yarn run` / `pnpm run` 等「包管理器 + run」模式：连同 run
    一起跳过，把真正的脚本名（如 dev）作为主体，否则 `run` 会被当主体、又恰好在源里
    作为普通词出现导致误判有据（test_bad_reply_flagged 的 npm run dev 实证）。
    """
    toks = re.findall(r"[^\s]+", (claim or "").replace("`", ""))
    i = 0
    while i < len(toks):
        base = toks[i].lower().strip("`\"'-:").rstrip(":")
        if base in _GENERIC_VERB_PREFIX:
            i += 1
            if i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        # npm run / yarn run / pnpm run / bun run → 连同 run 一起跳过
        if (i >= 1 and base == "run"
                and toks[i - 1].lower() in ("npm", "yarn", "pnpm", "bun", "deno")):
            i += 1
            if i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        break
    return toks[i] if i < len(toks) else ""


def _has_specific_pkg(n: str) -> bool:
    """包管理器安装命令是否带了具体包名（而非纯 `npm install` 通用动作）。

    带了具体包名时不再走白名单兜底，须要求包名在源里精确出现——否则 `npm install
    -g @marp-team/marp`（正确应为 @marp-team/marp-cli）会被白名单直接放过。
    """
    if not _PKG_INSTALL_RX.match(n):
        return False
    rest = _PKG_INSTALL_RX.sub("", n, count=1).strip()
    rest = re.sub(r"^-{1,2}[\w]+", "", rest).strip()
    return bool(rest)


def _claims_grounded_in(claims: list[str], sources: str) -> list[str]:
    """返回在 sources 里找不到对应内容的指令（即注水/答非所问/抓错源后编造）。

    判定口径（fix 后，替代「任意常见词命中源即放行」）：
    - 白名单：仅「纯通用动作、无具体包名/参数」才兜底（git clone / 裸 `npm install`）；
      带具体包名的安装命令须要求包名在源里精确出现。
    - 整段命令原文在源里连续出现 → 有据。
    - 否则要求「主体程序/包名」在源锚点集合里精确匹配（或作为裸词在源里）；
      目录 token（frontend/、src/components）须整段在源里。
    注：本闸门基于「命令字符串 vs 已抓取源文本」匹配，无法判断「源本身是否权威/
    正确」——抓错仓库（如 Marp 元仓库而非 CLI 仓库）导致的参数偏差超出其能力边界，
    需由源权威性校验另行处理。
    """
    src = (sources or "").lower()
    anchors = _source_anchor_set(sources)
    ungrounded: list[str] = []
    for c in claims:
        n = _normalize(c)
        if not n:
            continue
        # 白名单：纯通用动作（无任何具体包名/参数）才兜底
        if _UNIVERSAL_RX.search(n) and not _has_specific_pkg(n):
            continue
        # 整段命令 / 目录原文在源里连续出现
        token = n.strip("`")
        if n in src or token in src or ("/" in token and token.rstrip("/") in src):
            continue
        # 主体程序/包名须在源锚点集合里精确匹配（或作为裸词在源里）
        subject = _cmd_subject(c)
        subj_ok = False
        if subject:
            sn = _norm_anchor(subject)
            if sn in anchors or re.search(r"\b" + re.escape(sn) + r"\b", src):
                subj_ok = True
        if not subj_ok:
            ungrounded.append(c)
    return ungrounded


def reply_grounding_issues(reply: str, sources: str, *, intent: bool = True) -> list[str]:
    """最终答复的操作类事实核查。

    返回未通过核查的指令/问题列表；空列表=通过。
    `intent` 由调用方据任务书判定（只看「怎么操作/修改」类问题时才启用）。

    两条路径：
    - 有源：逐条核对具体指令是否在已 fetch 源里找得到（原逻辑）。
    - 零源（rerun54 盲区）：操作类答复却零抓取、且含具体命令/谎称已溯源 → 打回，
      逼模型回去用 ops_batch 真正抓取，而不是凭记忆编造或谎称已验证。
    """
    if not intent:
        return []
    reply = reply or ""
    claims = extract_ops_claims(reply)
    if (sources or "").strip():
        # 有源：逐条核对指令是否在源里找得到
        return _claims_grounded_in(claims, sources)
    # 零抓取盲区：操作类答复却零抓取，且含具体命令/声称已溯源 → 必须拦
    if claims or _has_cli_command(reply) or _asserts_sourced(reply):
        return [_ZERO_SRC_MSG]
    return []


def sources_from_history(history: list[dict]) -> str:
    """把模型本任务真实看到的工具结果拼成语料（role=='tool' 的内容）。"""
    parts: list[str] = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                parts.append(c)
            elif isinstance(c, list):  # 多段 tool content（部分 provider 格式）
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        parts.append(blk.get("text", ""))
    return "\n".join(parts)


# ============ 证据准入（rerun56） ============
# 问题现场：检索器把关键词提成 presentation，没给出任何 marp 候选；模型于是**凭先验
# 自己构造 URL** fetch `api.github.com/repos/marp-team/marp`（生态入口仓库，不是 CLI），
# 再从这份真源里「读出」命令。源是真源、字符串也全对得上——文本溯源闸门看不出来，
# 因为病灶不在「内容对不对」，而在「这个源根本不是检索给的，是它自己挑/拼的」。
#
# 准入规则：答复里引用的 URL / owner-repo，必须出现在本次**检索发现**阶段的候选池
# （web_search / github_search / npm 的输出）里；自构的直接打回。

# 检索类工具输出的特征抬头（用来把「检索候选」与「模型自己 fetch 的正文」分开）。
# 只认检索发现阶段的输出：fetch 抓哪个 URL 是模型的选择，不是候选——若把 fetch 结果
# 也算候选，模型自构 URL 抓一次就自动「准入」了，闸门等于没有。
_RETRIEVAL_MARKERS = (
    "[搜索摘要",             # _render_rows（Bing / DDG 摘要）
    "GitHub 官方搜索接口",    # render_repo_rows（GitHub API 候选）
    "npm 官方 registry",     # render_npm_rows
    "★名字同名的项目",        # exact_repo_lookup 段
)

_URL_RX = re.compile(r"https?://[^\s)\]\"'<>`，。、；）】\|]+")
# owner/repo：两边都像标识符才算（src/components、deck.md 这类靠下面两道过滤排掉）
_REPO_SLUG_RX = re.compile(
    r"(?<![\w.-])([A-Za-z0-9][\w.-]{0,38}/[A-Za-z0-9][\w.-]{0,38})(?![\w.-])")
# 路径段黑名单：命中即当文件路径，不当 owner/repo
_PATHY_SEGMENTS = {
    "src", "lib", "bin", "app", "web", "ui", "public", "dist", "build", "out",
    "docs", "doc", "test", "tests", "spec", "node_modules", "components",
    "pages", "styles", "assets", "static", "config", "scripts", "packages",
    "frontend", "backend", "server", "client", "examples", "demo", "tmp",
    "package", "vendor", "android", "ios",
}
# 「技术名词配对」不是仓库：答复里 `HTML/JS`、`React/Vue`、`Markdown/PPTX` 这类斜杠
# 是并列关系，不是 owner/repo。命中任一侧即放弃该候选（宁可漏检，不可误伤）。
_TECHY_TOKENS = {
    "html", "css", "scss", "less", "js", "ts", "jsx", "tsx", "json", "yaml", "yml",
    "xml", "csv", "sql", "md", "vue", "react", "angular", "svelte", "next", "nuxt",
    "node", "nodejs", "deno", "bun", "python", "java", "kotlin", "go", "rust",
    "php", "ruby", "swift", "dotnet", "csharp", "cpp", "c", "linux", "windows",
    "macos", "docker", "k8s", "git", "npm", "pnpm", "yarn", "cli", "sdk", "api",
    "ui", "ux", "pptx", "ppt", "pdf", "docx", "xlsx", "markdown", "text", "image",
}


def _norm_ref(s: str) -> str:
    """归一化一个源标识，便于跨写法比较（大小写/协议/www/锚点/尾斜杠）。"""
    s = (s or "").strip().strip("`\"'<>()[]{}，。、；：")
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^www\.", "", s, flags=re.I)
    s = s.split("#")[0].split("?")[0].rstrip("/")
    return s.lower()


def _token_in_pool(ref: str, pool: str) -> bool:
    """ref 是否作为**完整 token** 出现在候选池里。

    必须带边界：`marp-team/marp` 不能靠 `marp-team/marp-cli` 的子串蒙过去
    （rerun56 的错源正是「名字像」——不加边界，准入就形同虚设）。
    """
    if not ref:
        return False
    pat = r"(?<![\w.-])" + re.escape(ref) + r"(?![\w.-])"
    return re.search(pat, pool, re.I) is not None


def extract_citations(reply: str) -> list[str]:
    """答复里引用的「源标识」：URL 与 owner/repo（去重、保序）。"""
    out: list[str] = []
    seen: set[str] = set()

    def _add(v: str) -> None:
        k = _norm_ref(v)
        if k and k not in seen:
            seen.add(k)
            out.append(v.strip())

    for m in _URL_RX.finditer(reply or ""):
        _add(m.group(0))
    # URL 内部不再单独抽 owner/repo 片段——否则
    # `.../marp-team/marp-cli/blob/main/README.md` 会多出一条 `marp-cli/blob`。
    url_spans = [(m.start(), m.end()) for m in _URL_RX.finditer(reply or "")]
    for m in _REPO_SLUG_RX.finditer(reply or ""):
        if any(s <= m.start() < e for s, e in url_spans):
            continue
        raw = m.group(1)
        owner, _, name = raw.partition("/")
        if "." in owner:                       # github.com/marp-team（URL 残片）
            continue
        if owner.lower() in _PATHY_SEGMENTS or name.lower() in _PATHY_SEGMENTS:
            continue
        if owner.lower() in _TECHY_TOKENS or name.lower() in _TECHY_TOKENS:
            continue                           # HTML/JS、React/Vue 这种并列，不是仓库
        if "." in name:                        # 像文件名：main.py / deck.md
            continue
        if not re.search(r"[A-Za-z]", owner) or not re.search(r"[A-Za-z]", name):
            continue
        _add(raw)
    return out


def retrieval_candidates(history: list[dict]) -> str:
    """本次「检索发现」阶段的候选池（只取 web_search / github_search / npm 输出）。

    刻意**不含**模型自己 fetch 回来的正文：抓哪个 URL 是它的选择，不是候选。
    """
    parts: list[str] = []
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        c = msg.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        if any(mk in c for mk in _RETRIEVAL_MARKERS):
            parts.append(c)
    return "\n".join(parts)


def _ref_admitted(ref: str, pool: str, pool_urls: set[str]) -> bool:
    k = _norm_ref(ref)
    if not k:
        return True
    if k in pool_urls:
        return True
    # github/gitlab URL：退一步认「仓库已准入」（同仓库下的子页面/文件路径不苛求）
    m = re.match(r"(?:www\.)?(?:github|gitlab)\.com/([\w.-]+)/([\w.-]+)", k)
    if m:
        repo = f"{m.group(1)}/{m.group(2)}"
        if repo.endswith(".git"):
            repo = repo[:-4]
        return _token_in_pool(repo, pool)
    if "/" in k and not k.startswith(("http", "www")):
        return _token_in_pool(k, pool)
    return False


def evidence_admission_issues(reply: str, candidates: str) -> list[str]:
    """答复引用的源是否都来自本次检索候选。返回「未准入」的引用（空=通过）。

    只在**本次确实做过检索**（候选池非空）时启用——没检索就谈不上准入。
    """
    pool = (candidates or "")
    if not pool.strip():
        return []
    pool_urls = {_norm_ref(u) for u in _URL_RX.findall(pool)}
    bad: list[str] = []
    for ref in extract_citations(reply):
        if _ref_admitted(ref, pool, pool_urls):
            continue
        bad.append(ref)
    return bad
