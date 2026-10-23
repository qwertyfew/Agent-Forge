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

import difflib
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

# 围栏代码块（```bash ... ```）：group(1)=语言标记，group(2)=内容
_FENCE_RX = re.compile(r"```([^\n]*)\n(.*?)```", re.S)
# 这些语言的围栏块里是配置/标记，不是可执行命令（rerun57：```markdown 里的 `marp: true`）
_NONCMD_FENCE_LANGS = {
    "markdown", "md", "yaml", "yml", "json", "jsonc", "html", "xml", "css", "scss",
    "less", "text", "txt", "csv", "toml", "ini", "diff", "mermaid", "sql",
}
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
    # YAML/JSON 键值行（rerun57：```markdown 里的 `marp: true` 被当成命令 `marp`）
    if re.match(r"^[\w.-]+\s*:\s*\S", s):
        return False
    return bool(_CMD_TAIL_RX.search(s) or _KNOWN_BIN_RX.match(s)
                or re.search(r"(?<![\w-])--?[A-Za-z]", s))


def _fence_command_lines(reply: str) -> list[str]:
    """从围栏代码块（```bash ... ```）里抽取命令行。

    只收「命令行形态」的行，跳过 JSON/HTML/中文说明等非命令内容，避免把样本配置或
    说明文字当成操作指令。rerun55 实证：模型把 `marp deck.md --pptx` 写在围栏块里，
    原抽取器只认行内反引号会漏掉，导致闸门对「正确格式化」的注水回复也放行。
    rerun57 补充：```markdown 围栏里是 YAML front matter（`marp: true`），不是命令。
    """
    out: list[str] = []
    for m in _FENCE_RX.finditer(reply or ""):
        lang = (m.group(1) or "").strip().lower()
        if lang in _NONCMD_FENCE_LANGS:             # 配置/标记语言块，不抽命令
            continue
        for line in m.group(2).splitlines():
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
# 只放「包管理器 / 通用编排器」——它们后面的 token 才是主体。
# 不要把 vite / tsc / webpack / eslint / jest 放进来：它们本身就是被调用的程序，
# 放进来会把 `npx vite`、`tsc --noEmit` 的主体跳成空，导致正确命令被判无据。
_GENERIC_VERB_PREFIX = {
    "npm", "npx", "pnpm", "yarn", "bun", "deno", "node", "python", "python3", "py",
    "pip", "pip3", "cargo", "go", "make", "ruby", "php", "docker", "git",
    "curl", "wget", "java", "javac", "gradle", "mvn", "kubectl", "terraform",
    "dotnet", "i",
}
_PKG_INSTALL_RX = re.compile(
    r"^(?:npm\s+install|npm\s+i\b|pnpm\s+install|yarn\s+install|yarn\s+add"
    r"|pnpm\s+add|bun\s+add|pip3?\s+install)\b", re.I,
)
# 包管理器之后的子命令：`npm install X` 的实质主体是 X，不是 install。
# 不跳过的话 `_cmd_subject` 会返回 "install"，导致「包名是否在源里」这一判定失效
# （rerun56 实证：npm install -g @marp-team/marp 被判为可溯源，因为 install 是通用词）。
_PKG_SUBCMD = {"install", "i", "add", "get", "require", "upgrade", "update"}
_PKG_MANAGERS = {
    "npm", "yarn", "pnpm", "bun", "pip", "pip3", "cargo", "go", "brew", "dotnet",
}


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

    特殊处理两类「前缀不是主体」的形态：
    - `npm run dev` / `yarn run build`：连同 run 一起跳过，把脚本名作为主体，
      否则 `run` 会被当主体、又恰好在源里作为普通词出现导致误判有据。
    - `npm install -g @marp-team/marp`：连同 install 与选项一起跳过，把**包名**作为
      主体，否则主体会退化成通用词 `install`，包名是否在源里就查不出来了。
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
        prev = toks[i - 1].lower().strip("`\"'-:") if i >= 1 else ""
        # npm run / yarn run / pnpm run / bun run → 连同 run 一起跳过
        if base == "run" and prev in ("npm", "yarn", "pnpm", "bun", "deno"):
            i += 1
            if i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        # npm install X / pip install X → 跳过子命令与选项，取 X 作主体
        if base in _PKG_SUBCMD and prev in _PKG_MANAGERS:
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
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


def _bounded_in(needle: str, hay: str) -> bool:
    """needle 是否作为**完整 token**出现在 hay 里（不是子串）。

    `npm install -g @marp-team/marp` 是 `...-marp-cli` 的前缀子串——用 `in` 判断会
    把「包名写错」当成有据（rerun56 同类问题的命令层残留）。必须带两侧边界。
    """
    if not needle:
        return False
    pat = r"(?<![\w.-])" + re.escape(needle) + r"(?![\w.-])"
    return re.search(pat, hay, re.I) is not None


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
        # 整段命令 / 目录原文在源里连续出现（带边界，防包名前缀子串蒙过）
        token = n.strip("`")
        if (_bounded_in(n, src) or _bounded_in(token, src)
                or ("/" in token and _bounded_in(token.rstrip("/"), src))):
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


def _cli_line_spans(text: str) -> list[tuple[int, int]]:
    """答复里「命令行形态」的行所占的字符区间。

    命令行里的 token 是命令**参数**（`docker run … marpteam/marp-cli` 里的镜像名、
    `npm i github:owner/repo` 里的仓库参数），不是「引用来源」。rerun58 实证：
    镜像名 `marpteam/marp-cli` 被抽成 owner/repo，闸门反过来说模型「地址是自己
    拼的」——冤枉了一次正确答案。命令归命令核查，引用归引用核查，不能混。
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    for line in (text or "").splitlines(True):
        s = re.sub(r"^[`$>%#*\-\s]+", "", line.strip())
        if _is_cli_shape(s):
            spans.append((pos, pos + len(line)))
        pos += len(line)
    return spans


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
    cmd_spans = _cli_line_spans(reply or "")
    for m in _REPO_SLUG_RX.finditer(reply or ""):
        if any(s <= m.start() < e for s, e in url_spans):
            continue
        if any(s <= m.start() < e for s, e in cmd_spans):
            continue                           # 命令行参数，不是引用的源
        raw = m.group(1)
        owner, _, name = raw.partition("/")
        if "." in owner:                       # github.com/marp-team（URL 残片）
            continue
        if owner.lower() in _PATHY_SEGMENTS or name.lower() in _PATHY_SEGMENTS:
            continue
        if (owner.isalpha() and name.isalpha()
                and owner.isupper() and name.isupper()
                and len(owner) <= 4 and len(name) <= 4):
            continue                           # CI/CD、TCP/IP、A/B：缩写并列，不是仓库
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


def _ref_given_by_task(ref: str, task: str) -> bool:
    """这个"引用"是不是用户自己在任务书里给出的？

    soak17（h4）的现场：任务书点名「抓 agentforge-xyz/no-such-repo-… 这个仓库」，
    模型如实回答「源里未提及该仓库，请核实名称」，答复里当然出现了那个名字——
    旧口径把它当"未准入的引用"，于是给一条**诚实**答复盖了「不要照着操作」。
    用户给的标识不是模型在断言来源，压根不该进准入判定。
    """
    k = _norm_ref(ref)
    hay = _norm_ref(task)
    if not k or not hay:
        return False
    if k in hay:
        return True
    m = re.match(r"(?:www\.)?(?:github|gitlab)\.com/([\w.-]+)/([\w.-]+)", k)
    if m:
        return _token_in_pool(f"{m.group(1)}/{m.group(2)}", hay)
    if "/" in k:
        return _token_in_pool(k, hay)
    return False


def repo_citation_split(reply: str, evidence: str) -> tuple[list[str], list[str]]:
    """把答复里的仓库引用分成「能在本次工具输出里找到的」和「找不到的」。

    这是 P2 的判据实现：旧判据只看「答复含 REA + 过程调过检索」，同一个 pass
    既容臆造名字、也容逐条有据的真名，等于没测。改成可证伪的比对后，
    「说出来的名字是不是搜来的」变成可判定的事。

    刻意只比 `owner/name`（含 github URL 里的那一段），不比域名、不比描述性说法：
    证据是工具输出，不是"这个域名存不存在"。占位写法（`owner/repo`、`your-org/x`）
    不算引用——模型在教用户怎么填的时候会写这些。
    """
    hay = str(evidence or "").lower()
    ok: list[str] = []
    bad: list[str] = []
    for ref in extract_citations(reply):
        k = _norm_ref(ref)
        if not k or "/" not in k:
            continue
        m = re.match(r"(?:www\.)?(?:github|gitlab)\.com/([\w.-]+)/([\w.-]+)", k)
        repo = f"{m.group(1)}/{m.group(2)}" if m else k
        if repo.split("/")[0].lower() in _PLACEHOLDER_OWNERS:
            continue
        if k in hay or repo in hay:
            ok.append(ref)
        else:
            bad.append(ref)
    return ok, bad


# 明显是"示例写法"的 owner：模型教用户怎么填参数时会用到，不该被判成编造。
_PLACEHOLDER_OWNERS = {
    "owner", "your-org", "your_org", "your-name", "org", "user", "username",
    "example", "you", "xxx", "foo", "bar", "repo", "name", "someone",
}


def evidence_has_repo(evidence: str) -> bool:
    """本次工具输出里到底有没有仓库候选（诚实空结果时要靠它放过）。"""
    return bool(_REPO_SLUG_RX.search(str(evidence or "")))


def evidence_admission_issues(reply: str, candidates: str, *,
                              task: str = "") -> list[str]:
    """答复引用的源是否都来自本次检索候选。返回「未准入」的引用（空=通过）。

    只在**本次确实做过检索**（候选池非空）时启用——没检索就谈不上准入。
    任务书自己给出的标识（仓库名/链接）默认准入：那是用户输入，不是模型断言。
    """
    pool = (candidates or "")
    if not pool.strip():
        return []
    pool_urls = {_norm_ref(u) for u in _URL_RX.findall(pool)}
    bad: list[str] = []
    for ref in extract_citations(reply):
        if _ref_admitted(ref, pool, pool_urls):
            continue
        if task and _ref_given_by_task(ref, task):
            continue
        bad.append(ref)
    return bad


# ============ 溯源谎称（rerun58） ============
# 问题现场：全程只有 4 次 web_search（返回的是「搜索摘要」），模型却在答复里写
# 「从 npm 官方 registry 和 GitHub 源码仓库中提取了准确的安装命令」、
# 「验证状态：已通过官方仓库数据核实」。检索摘要 ≠ 抓过正文，这句话是假的。
# 文本溯源闸门看不出来：它比的是「命令字符串在不在源里」，而这里造假的是
# 「我读过原文」这个**元陈述**。
#
# 判据是确定性的：声称「从 X 提取/核实」+ 本次 history 里一次正文抓取都没有。

# 检索类工具输出里「不是正文」的接口（搜索接口的 JSON 只算候选，不算读过原文）
_SEARCH_ENDPOINT_RX = re.compile(
    r"api\.github\.com/search|registry\.npmjs\.org/-/v1/search|[?&]q=", re.I)

# 「声称已从官方源提取 / 已核实」的措辞
_PROVENANCE_RX = re.compile(
    r"(?:从|根据|依据|基于)[^\n。；！？]{0,40}?"
    r"(?:registry|npm|pypi|官方\s*(?:文档|仓库|源码|站点|网站)|GitHub|README|源码仓库)"
    r"[^\n。；！？]{0,40}?(?:提取|抓取|摘录|取回|核对|核实|校验)"
    r"|(?:已|均|都)?(?:经|通过)[^\n。；！？]{0,24}?(?:官方|源|文档|仓库)"
    r"[^\n。；！？]{0,12}?核实"
    r"|(?:核实|核对|校验)了?[^\n。；！？]{0,24}?(?:参数|命令|指令|用法|写法|格式|flag)"
    r"|(?:核实|校验|验证)(?:状态)?[：:]?[^\n。；！？]{0,8}?(?:通过|无误|准确)",
    re.I)


def fetched_pages(history: list[dict]) -> list[str]:
    """本次真正抓到的正文页 URL（fetch_url / fetch_json 的实抓结果）。

    只认 `[实测 · 网页]` / `[实测 · API]` 这两种「实抓」抬头，且排除搜索接口——
    搜索接口返回的是候选列表，不是读过的原文。
    """
    out: list[str] = []
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        c = msg.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        # 抬头后必须紧跟 http(s) 地址才算「实抓」——检索摘要的抬头是
        # `[实测 · API] GitHub 官方搜索接口`，紧跟的是「GitHub」不是网址，
        # 不卡这一条会把检索接口当成「抓到过正文」（rerun58 实测踩中）。
        for m in re.finditer(r"\[实测 · (?:网页|API)\]\s*(https?://\S+)", c):
            url = m.group(1).strip("，。、）)")
            if _SEARCH_ENDPOINT_RX.search(url):
                continue
            if url not in out:
                out.append(url)
    return out


def provenance_claims(reply: str) -> list[str]:
    """答复里「我已从官方源提取 / 已核实」这类元陈述（原文片段）。"""
    return [m.group(0).strip() for m in _PROVENANCE_RX.finditer(reply or "")]


def false_provenance_issues(reply: str, history: list[dict], *,
                            program_fetched: list[str] | None = None) -> list[str]:
    """声称已核实、但本次没抓过任何正文页 → 返回这些谎称句（空=没问题）。

    `program_fetched`：收尾阶段**程序自己**补抓到的正文页。此时「已核实」不是空话
    ——交付物确实被一份一手源核对过（谁抓的不影响它是不是真源），所以不再判为
    虚假来源声明。
    """
    claims = provenance_claims(reply)
    if not claims:
        return []
    if fetched_pages(history) or program_fetched:
        return []
    return claims[:4]


# ============ 诊断层：打回时说清「错在哪、源里对的什么样、下一步往哪走」 ============
#
# 问题（用户提出）：早期闸门只吐一句「这几条找不到，回去重写」，属于「驳回判决」。
# 模型下一轮只能猜——不知道是检索没命中、不知道候选池里到底有什么、也不知道源里
# 正确的写法是什么，于是反复重试同一个错（rerun54→56 一路的形态）。
#
# 诊断层把闸门输出从「判据列表」升级成「结构化病历」，三类信息：
#   1) 检索侧：本次检索有没有命中任务里的项目名？候选池里真实可引用的是哪些仓库？
#      （提词提错是 rerun56 的根因：Marp 被提成 presentation，模型只好自构 URL）
#   2) 引用侧：每条未准入的引用，候选中名字最接近的是什么？
#      （`@marp-team/marp` → `@marp-team/marp-cli`，一眼看出漏了 -cli）
#   3) 命令侧：每条无据命令，源里实际出现过的近似命令行是什么？
# 每类都附一句可执行的方向，而不是「请回去重写」。


def _is_repo_like(owner: str, name: str) -> bool:
    """owner/name 是否像仓库名（与 extract_citations 的过滤口径保持一致）。"""
    if not owner or not name:
        return False
    if "." in owner or "." in name:
        return False
    if owner.lower() in _PATHY_SEGMENTS or name.lower() in _PATHY_SEGMENTS:
        return False
    if owner.lower() in _TECHY_TOKENS or name.lower() in _TECHY_TOKENS:
        return False
    return bool(re.search(r"[A-Za-z]", owner) and re.search(r"[A-Za-z]", name))


def _is_pool_repo(owner: str, name: str) -> bool:
    """候选池里的 owner/name 判定（比答复侧宽松）。

    候选池是检索器渲染的 `owner/repo  star=.. URL` 结构，斜杠两侧本来就是仓库名，
    不会再出现 `HTML/JS` 那种并列写法。这里若沿用答复侧的技术词黑名单，会把
    `younghz/Markdown`、`marp-team/marp-cli` 这类**合法仓库**整体滤掉（rerun55
    实证：pool_repos 返回空，于是诊断只能说「候选池是空的」，把模型带偏）。
    折中口径：只有**斜杠两侧都是技术名词**（`PDF/HTML`）才判为并列写法，排除。
    """
    if not owner or not name:
        return False
    if "." in owner or "." in name:          # github.com/xxx 这类 URL 残片
        return False
    if owner.lower() in _PATHY_SEGMENTS or name.lower() in _PATHY_SEGMENTS:
        return False
    if owner.lower() in _TECHY_TOKENS and name.lower() in _TECHY_TOKENS:
        return False                          # PDF/HTML 这种并列，不是仓库
    return bool(re.search(r"[A-Za-z]", owner) and re.search(r"[A-Za-z]", name))


def pool_repos(pool: str, limit: int = 6) -> list[str]:
    """候选池里真实出现过的 owner/repo（去重保序）——回答「我能引用哪些」。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _REPO_SLUG_RX.finditer(pool or ""):
        raw = m.group(1)
        owner, _, name = raw.partition("/")
        if not _is_pool_repo(owner, name):
            continue
        k = _norm_ref(raw)
        if k in seen:
            continue
        seen.add(k)
        out.append(raw)
        if len(out) >= limit:
            break
    return out


def generic_name_flood(repos: list[str], threshold: int = 3) -> list[str]:
    """候选里「同名不同 owner」≥ threshold 个 → 检索词落到了通用词上。

    rerun55/56 的根因不是「任务书里的项目名没命中」（任务书写的是 Markdown / PPT），
    而是检索把**通用词**当成了主题词：返回 19 个都叫 markdown、14 个都叫 presentation
    的项目，真正的答案项目一个都不在里面。这个信号不依赖任务书措辞，比关键词比对稳。
    """
    cnt: dict[str, int] = {}
    for r in repos:
        _, _, name = r.partition("/")
        if not name:
            continue
        k = name.lower()
        cnt[k] = cnt.get(k, 0) + 1
    return [n for n, c in cnt.items() if c >= threshold]


# 「总览/入口仓库」自述：这类仓库本来就不含安装与使用说明，从里面推命令必然靠记忆补
_META_SRC_RX = re.compile(
    r"(entrance repository|entry (repository|point)|meta-?repo|ecosystem of"
    r"|this repository (is|contains|serves as)|overview of the"
    r"|入口仓库|总览仓库|生态入口|此仓库为.{0,8}(入口|总览))", re.I)
# 任何安装类命令：有它说明这份源确实讲了怎么装，不算总览页
_INSTALL_CMD_RX = re.compile(
    r"(npm\s+(?:install|i)\b|yarn\s+add|pnpm\s+add|bun\s+add|pip3?\s+install"
    r"|brew\s+install|cargo\s+install|go\s+install|docker\s+pull|npx\s+[\w@])", re.I)


def source_is_meta(sources: str) -> bool:
    """源看起来是「总览/入口仓库」而非实现仓库。

    rerun56 抓的 `marp-team/marp` 自述 *The entrance repository of Markdown
    presentation ecosystem*，且整篇没有一个安装命令——CLI 的真身在 `marp-cli`。
    在这种源上「找命令」，只能靠记忆补，正是幻觉的温床。
    """
    s = sources or ""
    if not s.strip():
        return False
    return bool(_META_SRC_RX.search(s)) and not _INSTALL_CMD_RX.search(s)


def source_command_lines(sources: str, limit: int = 60) -> list[str]:
    """从源文本里抽「形如命令行」的行——回答「源里真有的命令长什么样」。"""
    out: list[str] = []
    seen: set[str] = set()
    for line in (sources or "").splitlines():
        s = line.strip()
        s = re.sub(r"^[-*+]\s+", "", s)          # markdown 列表符号
        s = re.sub(r"^[`$>%#]+\s*", "", s).strip()  # shell 提示符 / 引用符
        if not s or len(s) > 220:
            continue
        if re.match(r"^[\u4e00-\u9fff]", s):     # 中文说明句
            continue
        if not (_KNOWN_BIN_RX.match(s)
                or re.match(r"^[\w./@\-]+\s+--?[A-Za-z]", s)):
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= limit:
            break
    return out


# near-miss 时不该当锚点的通用词（否则 `npm` 会前缀命中 `npm-cli` 之类噪声）
_NEAR_MISS_STOP = {
    "npm", "npx", "pnpm", "yarn", "bun", "node", "python", "python3", "pip", "pip3",
    "cargo", "make", "deno", "docker", "git", "curl", "wget", "install", "run",
}


def near_miss(needle: str, corpus: str, limit: int = 3) -> list[str]:
    """在 corpus 里找与 needle 最接近的写法（前缀超串优先，其次高相似度）。

    rerun56 的关键用例：needle=`@marp-team/marp`、corpus 含 `@marp-team/marp-cli`
    → 返回 `@marp-team/marp-cli`，模型一眼看出自己漏了 `-cli`，不用瞎试。
    """
    n = _norm_anchor(needle)
    if len(n) < 5 or n in _NEAR_MISS_STOP:
        return []
    cands: set[str] = set(re.findall(r"[@\w][\w./@\-]{3,}", corpus or ""))
    cands |= _source_anchor_set(corpus or "")
    scored: list[tuple[int, float, str]] = []
    for t in cands:
        tl = _norm_anchor(t)
        if not tl or tl == n or len(tl) < 5 or tl in _NEAR_MISS_STOP:
            continue
        if tl.startswith(n):
            scored.append((0, -float(len(tl)), tl))   # 前缀超串：最强信号
            continue
        ratio = difflib.SequenceMatcher(None, n, tl).ratio()
        if ratio >= 0.85 and abs(len(tl) - len(n)) <= max(4, len(n) // 3):
            scored.append((1, -ratio, tl))
    scored.sort()
    out: list[str] = []
    for _, _, t in scored:
        if t in out:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _task_keyword(task: str) -> str:
    """任务书里的「项目名」关键词（与检索器提词同源，失败则空）。"""
    try:
        from .tools.web import repo_keyword
        return repo_keyword(task or "") or ""
    except Exception:
        return ""


_KIND_TITLE = {
    "retrieval_generic": "检索词落到了通用词上",
    "retrieval_miss": "检索没命中项目名",
    "zero_retrieval": "零检索：没抓取就作答",
    "false_provenance": "谎称已核实：只看了摘要就说读过原文",
    "meta_source": "抓的是入口/总览仓库",
    "unadmitted_citation": "引用未准入：地址是自己拼的",
    "ungrounded_command": "命令/目录无据：源里没有",
}


def diagnose(reply: str, sources: str, candidates: str, *,
             task: str = "", intent: bool = True,
             history: list[dict] | None = None,
             program_fetched: list[str] | None = None) -> dict:
    """答复诊断报告。ok=True 表示无需打回（此时 findings 为空）。

    注意：**是否打回的判定与旧逻辑完全一致**（reply_grounding_issues +
    evidence_admission_issues），诊断层只负责在已决定打回时补充「为什么、怎么改」，
    不擅自放宽或收紧闸门。
    """
    reply = reply or ""
    src = (sources or "").strip()
    pool = (candidates or "").strip()
    kw = _task_keyword(task)
    all_repos = pool_repos(pool, limit=40)
    repos = all_repos[:6]
    retrieval = {
        "had_candidates": bool(pool),
        "task_keyword": kw,
        "keyword_in_candidates": (kw.lower() in pool.lower()) if (kw and pool) else None,
        "repos": repos,
        "generic_flood": generic_name_flood(all_repos),
    }

    issues = reply_grounding_issues(reply, src, intent=intent)
    refs = evidence_admission_issues(reply, pool, task=task) if pool else []
    prov = false_provenance_issues(reply, history or [],
                                   program_fetched=program_fetched)
    if not issues and not refs and not prov:
        return {"ok": True, "findings": [], "retrieval": retrieval,
                "issues": [], "refs": [], "provenance": []}

    findings: list[dict] = []
    real_issues = [c for c in issues if c != _ZERO_SRC_MSG]

    # ---- 根因 A：检索词落到「通用词」上（rerun55/56 的真起点）----
    # 任务书写的是 markdown / PPT，检索就返回一堆同名项目——看似「命中了」，
    # 实际命中的是通用词，答案项目一个都不在。这个信号不依赖任务书措辞。
    flood = retrieval["generic_flood"]
    if pool and flood:
        flood_items = []
        for n in flood[:2]:
            same = [r for r in all_repos if r.partition("/")[2].lower() == n]
            flood_items.append({
                "value": n,
                "why": f"候选里 {len(same)} 个不同作者的仓库都叫这个名字（"
                       + "、".join(same[:2]) + "…）",
            })
        findings.append({
            "kind": "retrieval_generic",
            "severity": "root",
            "mistake": (
                "把定向检索做成了泛搜——拿通用词当主题词，命中的是「一类东西」而不是"
                "「那个项目」。于是手上从来没有真候选，后面写命令只能靠记忆补。"),
            "items": flood_items,
            "next": (
                "用你认定的那个具体项目名做精确检索（exact_repo_lookup 可做同名匹配），"
                "并核对候选描述是否真的符合需求。在拿到描述相符的候选之前，"
                "不要开始写安装/使用步骤。"),
        })
    # ---- 根因 A2：任务书里的项目名没出现在候选里 ----
    elif pool and kw and kw.lower() not in pool.lower():
        findings.append({
            "kind": "retrieval_miss",
            "severity": "root",
            "mistake": "检索结果与任务要的项目无关，却仍然据它作答。",
            "items": [{
                "value": kw,
                "why": f"候选池 {len(all_repos)} 个仓库里没有任何一个与该名字相关",
            }],
            "next": (
                f"用项目名「{kw}」重做精确检索，拿到真候选再引用；"
                "拿不到就如实说明「没检索到」，不要用无关候选凑。"),
        })

    # ---- 根因 B：零检索（没有任何工具结果就作答）----
    if not pool and not src:
        cites = extract_citations(reply)
        findings.append({
            "kind": "zero_retrieval",
            "severity": "root",
            "mistake": "零抓取就下断言——把训练记忆当成了「已核实的材料」。",
            "items": [{"value": c, "why": "本轮 history 里工具结果 0 条，这个源没有任何来路"}
                      for c in cites[:5]] or
                     [{"value": "（本轮答复含具体命令/自称已核实）",
                       "why": "本轮 history 里工具结果 0 条"}],
            "next": (
                "先经 ops_batch 发起检索并抓源（tools.call('web_search', ...)），"
                "拿到候选再写；确实抓不到就如实说明「未检索到、无法确认」。"),
        })

    # ---- 根因 D：声称已核实，但一次正文抓取都没有（rerun58）----
    if prov:
        n_ret = sum(1 for m in (history or [])
                    if isinstance(m, dict) and m.get("role") == "tool"
                    and any(mk in str(m.get("content") or "")
                            for mk in _RETRIEVAL_MARKERS))
        findings.append({
            "kind": "false_provenance",
            "severity": "root",
            "mistake": (
                "把「检索摘要」当成了「读过的原文」——摘要只告诉你有个什么东西、"
                "标题是什么，不含正文里的写法。基于摘要说「已提取/已核实」，"
                "等于给没读过的文档盖了核对章，后面的命令自然是凭记忆补的。"),
            "items": [{
                "value": c,
                "why": (f"本次 history 里实抓正文 0 篇（fetch_url / fetch_json 未调用），"
                        f"只有 {n_ret} 份检索摘要"),
            } for c in prov[:4]],
            "next": (
                "先去抓那一手正文（实现仓库的 README / 官方文档页，用 fetch_url 或 "
                "fetch_json），抓到了再写「据 X 提取」；抓不到就把这类话删掉，"
                "如实说「只看到检索摘要，未核对原文」。"),
        })

    # ---- 根因 C：抓的是「入口/总览仓库」，本来就没有安装说明 ----
    if src and real_issues and source_is_meta(src):
        findings.append({
            "kind": "meta_source",
            "severity": "root",
            "mistake": (
                "在「入口/总览仓库」里找用法——这类仓库的职责是介绍生态，本身不写安装与"
                "用法。在它上面推命令，只能靠记忆补，答错是必然的。"),
            "items": [{
                "value": "本次抓取的源",
                "why": ("源里自述 entrance / ecosystem 之类的总览身份，"
                        "且全文 0 条安装命令（无 npm install / pip install / brew install 等）"),
            }],
            "next": (
                "改抓该项目的**实现仓库**（描述里写明「是 CLI / 是库本身」的那个，"
                "不是介绍生态的入口页），从它的 README 取命令；"
                "找不到实现仓库就如实说明。"),
        })

    # ---- 症状 A：引用未准入 ----
    if refs:
        items: list[dict] = []
        for r in refs[:6]:
            near = near_miss(r, pool, limit=1)
            why = f"候选池共 {len(all_repos)} 个仓库，逐字比对没有这一条"
            if near:
                why += f"；候选里实际出现的是「{near[0]}」（与你写的不一致）"
            items.append({"value": r, "why": why})
        findings.append({
            "kind": "unadmitted_citation",
            "severity": "symptom",
            "mistake": (
                "引用了不在检索候选里的源——地址是自己拼的，等于绕过检索自行选定证据，"
                "之后的所有结论都建立在自己挑的源上。"),
            "items": items,
            "next": (
                "只引用候选池里真实出现过的仓库/网址（同一仓库的子页面可以）；"
                "候选里没有就重搜，或如实说明没有检索到。"),
        })

    # ---- 症状 B：命令/目录在源里无据 ----
    if real_issues:
        src_lines = source_command_lines(src)
        items = []
        for c in real_issues[:6]:
            subject = _cmd_subject(c)
            near = near_miss(subject, src, limit=1) if subject else []
            if not near:
                near = near_miss(c, src, limit=1)
            if not src:
                why = "本轮 0 条工具结果，这条命令没有任何来源"
            else:
                why = f"源里可执行命令行共 {len(src_lines)} 条，逐字比对没有这一条"
                if near:
                    why += f"；源里实际出现的是「{near[0]}」，与你写的不一致"
                else:
                    why += "；源里也没有任何近似写法，说明这段不是你读到的内容"
            items.append({"value": c, "why": why})
        next_text = (
            "回源逐条核对。上面「依据」里源内实际出现的写法只作**比对基准**，"
            "不是给你的答案——命令要你自己按源重组；"
            "源里没有这一段就如实说「官方文档未提供这一步的具体步骤」。")
        if src and not src_lines:
            next_text = (
                "你抓到的这份源里**一条可执行命令行都没有**——它不是用法文档。"
                "换成带安装与用法的源（实现仓库的 README）再写命令；拿不到就如实说明。")
        findings.append({
            "kind": "ungrounded_command",
            "severity": "symptom",
            "mistake": (
                "给出的命令/目录在已抓取的源里不存在——把记忆里的通用写法当成了"
                "读到的内容，属于凭记忆补全。"),
            "items": items,
            "next": next_text,
        })

    return {"ok": False, "findings": findings, "retrieval": retrieval,
            "issues": issues, "refs": refs, "provenance": prov}


def render_diagnosis(diag: dict, *, round_no: int = 1, max_rounds: int = 2) -> str:
    """渲染凭证式打回消息：只说明「犯了什么错 + 依据」，不给正确答案。

    边界（用户明确要求）：闸门负责**证伪**——证明这次答复为什么一定错；
    正确答案必须由模型自己回源求，闸门不提供可照抄的成品。
    """
    if not diag or diag.get("ok"):
        return ""
    lines = [
        f"【答复核查未通过 · 第 {round_no}/{max_rounds} 轮】",
        "以下每条都有可复核的确定性依据，不是措辞建议——按现状不能交付。",
        "请读懂「犯了什么错」和「依据」，自行回源改正。这里不会给出正确答案。",
    ]
    order = {"root": 0, "symptom": 1}
    for f in sorted(diag.get("findings") or [],
                    key=lambda x: order.get(x.get("severity"), 1)):
        title = _KIND_TITLE.get(f.get("kind"), f.get("kind") or "问题")
        n = len(f.get("items") or [])
        lines.append("")
        lines.append(f"— {title}" + (f"（{n} 条）" if n else ""))
        if f.get("mistake"):
            lines.append("  犯了什么错：" + f["mistake"])
        for it in (f.get("items") or [])[:6]:
            seg = "  · " + str(it.get("value") or "")
            if it.get("why"):
                seg += "\n     依据：" + it["why"]
            near = it.get("nearest") or []
            if near:
                seg += "；源内出现的相关写法：" + "、".join(f"「{x}」" for x in near[:3])
            lines.append(seg)
        if f.get("next"):
            lines.append("  → 该做的：" + f["next"])
    lines.append("")
    lines.append("改完再交。若确实拿不到可靠材料，就如实说明「没检索到 / 源里没提」——"
                 "这比编一个像样的答案要好。")
    return "\n".join(lines)


def render_unverified_note(diag: dict, checked: list[str] | None = None) -> str:
    """打回次数用尽仍未通过时，程序侧追加的「未经核实」声明。

    为什么由程序写、不让模型自己写：模型两次都没改对，第三次的自我声明同样
    不可信；用户看到的交付物上必须有一个不依赖模型的印记。措辞只陈述**机器
    能复核的事实**（打回几轮、几条没依据），不评判对错。

    `checked`：收尾阶段程序自己补抓（复核）过的源。有它时声明要说清「程序抓过、
    对不上」，与「没人核实过」是两件事——前者能让人放心删掉这段内容照着做，
    后者只能提醒读者存疑。
    """
    if not diag or diag.get("ok"):
        return ""
    n_cmd = len([c for c in (diag.get("issues") or []) if c != _ZERO_SRC_MSG])
    n_ref = len(diag.get("refs") or [])
    kinds = [f.get("kind") for f in (diag.get("findings") or [])]
    bits: list[str] = []
    if n_cmd:
        bits.append(f"{n_cmd} 条命令/目录在已抓到的源里找不到")
    if n_ref:
        bits.append(f"{n_ref} 处引用的地址不在本次检索候选里")
    if "false_provenance" in kinds:
        bits.append("存在「未抓正文却称已核实」的表述")
    if not bits:
        bits.append("答复的关键内容未通过程序核查")
    checked = [u for u in (checked or []) if u]
    if checked:
        head = ("⚠ **程序核查未通过（已打回 2 轮，程序又自行补抓 "
                f"{len(checked)} 个源复核后仍不合格）**：")
        tail = ("以上内容与程序抓到的一手源对不上，**不要照着操作**"
                "——需要可采信的版本，让我重新查一遍即可。")
    else:
        head = "⚠ **程序核查未通过（已打回 2 轮仍不合格）**："
        tail = ("以上内容未经核实，**不作为事实依据**"
                "——需要可采信的版本，让我重新查一遍即可。")
    return ("\n\n---\n" + head + "；".join(bits) + "。\n" + tail)


__all__ = [
    "diagnose", "render_diagnosis", "render_unverified_note",
    "reply_grounding_issues", "sources_from_history", "retrieval_candidates",
    "evidence_admission_issues", "extract_citations", "extract_ops_claims",
    "is_howto_intent", "fetched_pages", "provenance_claims",
    "false_provenance_issues", "source_is_meta", "source_command_lines",
    "pool_repos", "near_miss",
]
