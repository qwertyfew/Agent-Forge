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
# 带反引号的整段命令（再判是不是上面的强信号）
_BACKTICK_RX = re.compile(r"`([^`]{1,120})`")
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


def is_howto_intent(text: str) -> bool:
    """任务书是不是「怎么操作/修改/使用」类问题。"""
    return bool(_HOWTO_INTENT_RX.search(text or ""))


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def extract_ops_claims(reply: str) -> list[str]:
    """从最终答复里抽出具体的操作指令（强信号），去重返回。"""
    raw: list[str] = []
    # 1) 反引号命令
    for m in _BACKTICK_RX.finditer(reply or ""):
        inner = m.group(1).strip()
        if _CMD_TAIL_RX.search(inner):
            raw.append(inner)
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


def _claims_grounded_in(claims: list[str], sources: str) -> list[str]:
    """返回在 sources 里找不到对应内容的指令（即注水/答非所问）。

    判定口径（避免短词撞子串误判）：
    - 目录 token（如 frontend/、src/components）必须整段出现在源里；
    - 命令分两种锚点才算有据：① 整段命令原文在源里（如「npm run dev」原文），
      ② 命令里含有**项目专属** token（去掉连字符后能在源里找到，如 ui-critic）。
      不再把 `npm run dev` 退化成子串 `dev` 去撞 README 里的 developers/device。
    """
    src = (sources or "").lower()
    src_norm = re.sub(r"[^\w]", "", src)  # 去掉非词字符，方便「ui-critic」≈「uicritic」比对
    ungrounded: list[str] = []
    for c in claims:
        n = _normalize(c)
        if not n:
            continue
        # 白名单：通用开发动作，放过
        if _UNIVERSAL_RX.search(n):
            continue
        # 目录：整段 token 出现在源里（如 src/components / frontend/ / web/）；
        # 带结尾斜杠的再试去掉斜杠的纯词（README 可能写「frontend」而非「frontend/」）
        token = n.strip("`")
        if token in src:
            continue
        if "/" in token and token.rstrip("/") in src:
            continue
        # 整段命令原文在源里
        if n in src:
            continue
        # 命令里含项目专属 token（去掉连字符后能在源里找到）→ 视为有据。
        # 只认项目专属词：dev/start/main/components/frontend 等通用词已被排除，
        # 因此 `npm run dev`、`npm start`、`python main.py` 不会因偶发子串漏过。
        sig = [t for t in re.findall(r"[a-z0-9][a-z0-9_\-]{3,}", n)
               if t not in _GENERIC_TOKENS and len(t) >= 4]
        hit = False
        for t in sig:
            tn = re.sub(r"[^\w]", "", t)  # ui-critic -> uicritic
            if tn and tn in src_norm:
                hit = True
                break
        if hit:
            continue
        ungrounded.append(c)
    return ungrounded


def reply_grounding_issues(reply: str, sources: str, *, intent: bool = True) -> list[str]:
    """最终答复的操作类事实核查。

    返回未在源里找到对应内容的指令列表；空列表=通过。
    `intent` 由调用方据任务书判定（只看「怎么操作/修改」类问题时才启用）。
    """
    if not intent:
        return []
    claims = extract_ops_claims(reply or "")
    if not claims:
        return []
    if not (sources or "").strip():
        # 没有任何源可核，无法判断——不拦（交给收尾闸门/人工）
        return []
    return _claims_grounded_in(claims, sources)


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
