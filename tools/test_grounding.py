"""操作类答复事实核查（Option A）单元测试。

复现 rerun51 实证：模型 fetch 了 README 却写出 `python main.py` / `npm run dev` /
`src/components` / `frontend/` 等源里没有的通用模板——这类必须在 final 答复阶段拦下。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentforge import grounding as G


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"测试未通过: {name}")


# 一份贴近真实 README 的「源」：含 npm 全局安装、bin 入口、Playwright/Storybook/Figma
SRC = """
# ui-critic
A second pair of eyes for a UI. Capture screenshots and measured facts from a running
site or app, get a ranked visual critique from Gemini or OpenAI.

## Quick start
npm i -g @akins20/ui-critic
Then run ui-critic on your site. Zero required dependencies beyond Node 20;
Playwright is optional, for capture. The entry is bin/ui-critic.mjs.
It reviews a Storybook component by component and compares with its Figma frames.
"""


def test_intent() -> None:
    _check("操作类意图识别：修改前端", G.is_howto_intent(
        "寻找git上的项目UI-Critic，这个项目是干嘛的，怎么样操作才能修改前端"))
    _check("操作类意图识别：怎么运行某项目", G.is_howto_intent("这个项目怎么运行"))
    _check("非操作类不触发：写一首诗", not G.is_howto_intent("帮我写一首关于春天的诗"))


def test_bad_reply_flagged() -> None:
    # rerun51 实况：注水通用模板，源里没有这些内容
    bad = """要修改前端：
- 检查是否存在 frontend/、web/ 或 ui/ 文件夹
- 如果是 React 项目，寻找 src/components 目录
- 运行 `python main.py` 启动
- 或 `npm run dev` 本地预览"""
    issues = G.reply_grounding_issues(bad, SRC, intent=True)
    joined = " / ".join(issues)
    _check("拦截 python main.py", "python main.py" in joined or "main.py" in joined)
    _check("拦截 npm run dev", "npm run dev" in joined)
    _check("拦截 src/components", "src/components" in joined)
    _check("拦截 frontend/", "frontend" in joined)
    _check("确实拦截到了（非空）", bool(issues))


def test_good_reply_passes() -> None:
    # 只陈述源里真实出现的命令/目录
    good = ("安装：`npm i -g @akins20/ui-critic`，然后运行 `ui-critic` 命令；"
            "入口是 bin/ui-critic.mjs，可选依赖 Playwright。")
    issues = G.reply_grounding_issues(good, SRC, intent=True)
    _check("合规答复不拦截", not issues)


def test_honest_disclaimer_passes() -> None:
    # 如实声明源里没提，不补模板
    honest = ("官方文档未提供修改前端的具体步骤，README 只讲了怎么用它来评审你的站点。")
    issues = G.reply_grounding_issues(honest, SRC, intent=True)
    _check("诚实声明不拦截", not issues)


def test_universal_whitelisted() -> None:
    # 通用开发动作即使不在源里也放过（防误伤）
    u = "先 `git clone https://github.com/Akins20/ui-critic` 再 `npm install`。"
    issues = G.reply_grounding_issues(u, SRC, intent=True)
    _check("git clone 白名单不拦截", "git clone" not in " / ".join(issues))


def test_zero_source_with_command_flagged() -> None:
    # 盲区已闭（rerun54）：操作类答复零抓取却含具体命令 → 必须拦
    issues = G.reply_grounding_issues(
        "运行 `python main.py` 启动，并 `npm run dev` 本地预览", "", intent=True)
    _check("无源+含具体命令：必须拦", bool(issues))
    _check("拦回指明需经 ops_batch 抓取", "ops_batch" in " / ".join(issues))


def test_zero_source_asserts_sourced_flagged() -> None:
    # rerun54 实况：零抓取却写「验证通过：所有命令均参考官方文档」
    lie = ("推荐 Marp：`npm install -g @marp-team/marp-cli`，`marp deck.md --output deck.pdf`。"
           "验证通过：所有命令均参考官方文档。")
    issues = G.reply_grounding_issues(lie, "", intent=True)
    _check("零抓取却谎称已溯源：必须拦", bool(issues))


def test_zero_source_no_command_no_claim_passes() -> None:
    # 操作类但只讲概念、无具体命令、未声称已溯源 → 不拦（避免误伤）
    soft = "Marp 是 Markdown 转幻灯片的生态，核心思路是用 Markdown 写内容、由渲染器生成幻灯片。"
    issues = G.reply_grounding_issues(soft, "", intent=True)
    _check("无源+无命令+未声称溯源：不拦", not issues)


def test_non_howto_skips() -> None:
    # 非操作类问题不启用核查
    issues = G.reply_grounding_issues("运行 `python main.py`", SRC, intent=False)
    _check("非操作类意图跳过", not issues)


def test_correct_cmd_with_project_token() -> None:
    # `node bin/ui-critic.mjs` 含项目专属 token ui-critic（源里有），应放行
    ok = "运行 `node bin/ui-critic.mjs` 即可启动。"
    issues = G.reply_grounding_issues(ok, SRC, intent=True)
    _check("含项目名的正确命令不误伤", not issues)


def test_wrong_package_name_flagged() -> None:
    # 收窄白名单：带具体包名的安装命令须要求包名在源里精确出现。
    # `npm install -g @wrong-scope/wrong-pkg` 源里没有 → 必须拦
    bad = "安装：`npm install -g @wrong-scope/wrong-pkg`，然后运行。"
    issues = G.reply_grounding_issues(bad, SRC, intent=True)
    joined = " / ".join(issues)
    _check("错误包名必须拦", bool(issues) and "@wrong-scope/wrong-pkg" in joined)


def test_correct_package_passes() -> None:
    # 源里真实存在的包名（@akins20/ui-critic）应放行
    good = "安装：`npm i -g @akins20/ui-critic`，然后运行 `ui-critic`。"
    issues = G.reply_grounding_issues(good, SRC, intent=True)
    _check("源里真实包名不误伤", not issues)


def test_fenced_commands_extracted() -> None:
    # rerun55 盲区：命令写在 ```bash 围栏块里，抽取器必须抓到，且不能把语言提示行
    # `bash` 和命令黏成一条带换行的脏 claim
    reply = ("安装并运行：\n"
             "```bash\n"
             "npm install -g @marp-team/marp-cli\n"
             "marp deck.md --pptx\n"
             "```\n"
             "也可用 `marp --version` 查看版本。")
    claims = G.extract_ops_claims(reply)
    joined = " / ".join(claims)
    _check("围栏块命令被抽取(marp deck.md --pptx)", "marp deck.md --pptx" in joined)
    _check("围栏块命令被抽取(npm install -g ...)", "npm install -g @marp-team/marp-cli" in joined)
    _check("行内命令也被抽取(marp --version)", "marp --version" in joined)
    _check("无 bash\\n 脏 claim（每条 claim 不含换行）",
           all("\n" not in c for c in claims))


def test_fenced_fabricated_flagged() -> None:
    # 有源、但围栏块里写了源里没有的命令（且非项目专属 token）→ 必须拦
    reply = ("用法如下：\n```bash\nmarp deck.md --pptx\n```")
    issues = G.reply_grounding_issues(reply, SRC, intent=True)
    _check("围栏块里的编造命令被拦", bool(issues) and "marp deck.md --pptx" in " / ".join(issues))


def test_no_pure_flag_or_package_name() -> None:
    # 误报边界：纯标志 `-o` 和 npm 包名 `marp-core` 不该被当成命令 claim
    reply = ("可用 `-o` 参数指定输出；也可集成 `marp-core` 做动态渲染。")
    claims = G.extract_ops_claims(reply)
    joined = " / ".join(claims)
    _check("纯标志 -o 不被当命令", "-o" not in joined)
    _check("npm 包名 marp-core 不被当命令", "marp-core" not in joined)


# ---------------- 证据准入（rerun56） ----------------

# 一次真实检索的候选池形态：★ 同名段 + star 排序段
POOL = """
[实测 · API] GitHub 官方搜索接口 · 查询「Marp」· 排序：相关度 · 命中 3 个
  1. marp-team/marp  star=12662  The entrance repository of Markdown presentation ecosystem
     https://github.com/marp-team/marp
  2. marp-team/marp-cli  star=3879  A CLI interface for Marp and Marpit based converters
     https://github.com/marp-team/marp-cli
"""


def test_citations_extracted() -> None:
    # 只认 URL 与 owner/repo；`HTML/JS`、`React/Vue`、路径、文件名都不算
    reply = ("参考 https://github.com/marp-team/marp-cli 与 marp-team/marp-core。"
             "前端技术栈是 React/Vue 与 HTML/JS，入口在 `src/components`，"
             "幻灯片文件是 deck.md，下载 `https://marp.app/`。")
    cites = G.extract_citations(reply)
    joined = " / ".join(cites)
    _check("抓到 github URL", "https://github.com/marp-team/marp-cli" in joined)
    _check("抓到 owner/repo", "marp-team/marp-core" in joined)
    _check("抓到官网 URL", "https://marp.app" in joined)
    _check("React/Vue 不当仓库", "React/Vue" not in joined)
    _check("HTML/JS 不当仓库", "HTML/JS" not in joined)
    _check("src/components 不当仓库", "src/components" not in joined)
    _check("deck.md 不当仓库", "deck.md" not in joined)


def test_url_trailing_backtick_stripped() -> None:
    # rerun56 实况：答复把 URL 写在反引号里，旧正则把尾反引号吞进了 URL
    cites = G.extract_citations("见 `https://github.com/marp-team/marp` 。")
    _check("URL 尾部反引号被剥掉",
           cites == ["https://github.com/marp-team/marp"])


def test_admission_flags_self_constructed() -> None:
    # 池里只有 presentation 类的无关项目；答复却引用 marp-team/marp → 未准入
    pool = ("[实测 · API] GitHub 官方搜索接口 · 查询「presentation」\n"
            "  1. hyperoslo/Presentation  star=3068  https://github.com/hyperoslo/Presentation\n")
    reply = "推荐 marp-team/marp，命令见 https://github.com/marp-team/marp"
    bad = G.evidence_admission_issues(reply, pool)
    _check("自构源被判未准入", bool(bad))
    _check("未准入里包含该仓库", any("marp-team/marp" in b for b in bad), str(bad))


def test_admission_passes_retrieved() -> None:
    # 候选池里真有 marp-team/marp-cli → 引用它（含同仓库子页面）应放行
    reply = ("见 marp-team/marp-cli，文档 "
             "https://github.com/marp-team/marp-cli/blob/main/README.md")
    bad = G.evidence_admission_issues(reply, POOL)
    _check("检索候选里的源不误伤", not bad, str(bad))


def test_citation_ignores_cli_arguments() -> None:
    # rerun58 误伤：docker 镜像名 marpteam/marp-cli 被当成「自构的仓库地址」，
    # 闸门反过来说正确答案「地址是自己拼的」。命令行里的 token 是参数，不是引用。
    reply = ('可以直接用镜像：\n```bash\ndocker run --rm -v "$(pwd)":/data '
             'marpteam/marp-cli deck.md -o deck.pptx\n```')
    cites = G.extract_citations(reply)
    _check("命令行里的镜像名不当引用", not any("marpteam" in c for c in cites), str(cites))


def test_citation_ignores_acronym_pairs() -> None:
    # CI/CD、TCP/IP 这类缩写并列不是 owner/repo（rerun58 把它报成未准入引用）
    reply = "可以在项目构建流程（如 Webpack、Vite 或 CI/CD 流程）里加一步预处理。"
    cites = G.extract_citations(reply)
    _check("CI/CD 不当引用", not any(c.lower() == "ci/cd" for c in cites), str(cites))


def test_false_provenance_detected() -> None:
    # rerun58 实况：只有 4 次 web_search（摘要），却称「从 npm registry 提取/已核实」
    history = [{"role": "tool", "content":
                "[实测 · API] GitHub 官方搜索接口 · 查询「marp-team/marp-cli」\n"
                "  1. marp-team/marp-cli  https://github.com/marp-team/marp-cli"}]
    reply = ("2. 从 npm 官方 registry 和 GitHub 源码仓库中提取了准确的安装命令。\n"
             "验证状态：已通过官方仓库数据核实。")
    bad = G.false_provenance_issues(reply, history)
    _check("未抓正文却称已核实 → 判谎称", len(bad) >= 2, str(bad))
    _check("搜索接口不算抓过正文", G.fetched_pages(history) == [], str(G.fetched_pages(history)))


def test_false_provenance_passes_with_fetch() -> None:
    # 真抓过 README 再说「据 README 提取」是诚实的，不能误伤
    history = [{"role": "tool", "content":
                "[实测 · 网页] https://github.com/marp-team/marp-cli\n"
                "[标题] marp-cli\n## Install\nnpm install -g @marp-team/marp-cli"}]
    reply = "从官方仓库的 README 中提取了安装命令：npm install -g @marp-team/marp-cli"
    _check("真抓过正文时不判谎称", not G.false_provenance_issues(reply, history))
    _check("抓过的正文页被记账",
           G.fetched_pages(history) == ["https://github.com/marp-team/marp-cli"],
           str(G.fetched_pages(history)))


def test_diagnose_reports_false_provenance() -> None:
    history = [{"role": "tool", "content":
                "[实测 · API] npm 官方 registry · 查询「marp」\n"
                "  1. @marp-team/marp-cli  4.5.1"}]
    reply = ("安装：npm install -g @marp-team/marp-cli\n"
             "从 npm 官方 registry 中提取了准确的安装命令。")
    diag = G.diagnose(reply, G.sources_from_history(history),
                      G.retrieval_candidates(history), task="怎么安装", history=history)
    kinds = [f["kind"] for f in diag["findings"]]
    _check("诊断里出现「谎称已核实」根因", "false_provenance" in kinds, str(kinds))
    text = G.render_diagnosis(diag)
    _check("诊断说清错在「摘要当原文」", "摘要" in text and "原文" in text, text[:200])


def test_unverified_note_is_deterministic() -> None:
    diag = {"ok": False,
            "issues": ["npm install -g @x/y"],
            "refs": ["someone/repo"],
            "findings": [{"kind": "false_provenance", "items": []}]}
    note = G.render_unverified_note(diag)
    _check("拦满放行时有程序侧未核实声明", "未经核实" in note and "打回 2 轮" in note, note)
    _check("通过时不给声明", G.render_unverified_note({"ok": True}) == "")


def test_admission_no_substring_cross() -> None:
    # 关键边界：池里只有 marp-cli，答复引用 marp（入口仓库）——不能靠子串蒙过
    pool = ("[实测 · API] GitHub 官方搜索接口 · 查询「Marp」\n"
            "  2. marp-team/marp-cli  star=3879  https://github.com/marp-team/marp-cli\n")
    bad = G.evidence_admission_issues("部署用 marp-team/marp。", pool)
    _check("marp 不能靠 marp-cli 的子串被准入", bool(bad), str(bad))


def test_admission_empty_pool_skips() -> None:
    # 本次没做检索 → 谈不上准入（不误伤）
    _check("空候选池不启用准入",
           not G.evidence_admission_issues("参考 marp-team/marp", ""))


def test_admission_ignores_non_retrieval_tools() -> None:
    # 只把「检索发现」类输出当候选：模型自己 fetch 回来的正文不算候选，
    # 否则它自构 URL 抓一次就自动「准入」，闸门等于没有。
    hist = [
        {"role": "tool", "content": "https://github.com/marp-team/marp 抓到的正文"}
    ]
    _check("fetch 正文不构成候选池", not G.retrieval_candidates(hist).strip())
    hist2 = [{"role": "tool", "content": POOL}]
    _check("检索输出构成候选池", "marp-team/marp-cli" in G.retrieval_candidates(hist2))


# ---------- 诊断层：说明「犯了什么错 + 依据」，且不给正确答案 ----------

_META_SRC = (
    "marp-team/marp — The entrance repository of Markdown presentation ecosystem. "
    "This repository is the entry point for the ecosystem.\n"
)
_META_POOL = (
    "[实测 · API] GitHub 官方搜索接口 · 查询「presentation」· 排序：相关度\n"
    "  1. hyperoslo/Presentation  star=3068  https://github.com/hyperoslo/Presentation\n"
    "  2. UnityTechnologies/Presentation  star=1130  https://github.com/UnityTechnologies/Presentation\n"
    "  3. ChangbaFE/presentation  star=90  https://github.com/ChangbaFE/presentation\n"
)


def test_near_miss_points_to_longer_token() -> None:
    # rerun56 的关键：模型写 @marp-team/marp，源里其实是 @marp-team/marp-cli
    got = G.near_miss("@marp-team/marp", "install with npm i -g @marp-team/marp-cli")
    _check("near_miss 指向前缀更长的真实写法",
           any("marp-cli" in g for g in got), str(got))


def test_near_miss_ignores_unrelated() -> None:
    _check("near_miss 对无关内容返回空",
           not G.near_miss("@marp-team/marp", "hello world nothing here"),
           str(G.near_miss("@marp-team/marp", "hello world nothing here")))


def test_pool_repos_from_render() -> None:
    repos = G.pool_repos(_META_POOL)
    _check("pool_repos 能从检索渲染里抽出仓库",
           "hyperoslo/Presentation" in repos, str(repos))
    # PDF/HTML 这类斜杠并列不是仓库
    _check("pool_repos 排除 PDF/HTML 并列写法",
           not any(r.lower() == "pdf/html" for r in G.pool_repos("见 PDF/HTML 两种导出")))


def test_generic_flood_detected() -> None:
    flood = G.generic_name_flood(G.pool_repos(_META_POOL, limit=40))
    _check("识别「检索词落到通用词」（同名多 owner）",
           "presentation" in flood, str(flood))


def test_meta_source_detected() -> None:
    _check("识别入口/总览仓库（无安装命令）", G.source_is_meta(_META_SRC))
    _check("带安装命令的源不算总览页",
           not G.source_is_meta(_META_SRC + "\nnpm install -g @marp-team/marp-cli\n"))


def test_diagnose_clean_passes() -> None:
    d = G.diagnose("见 README 的说明。", "nothing", "", task="怎么用")
    _check("无问题时 ok=True", d["ok"] and not d["findings"])


def test_diagnose_zero_source_has_mistake() -> None:
    d = G.diagnose("运行 `python main.py` 即可。", "", "", task="怎么运行这个项目")
    _check("零检索→判不合格", not d["ok"])
    kinds = [f["kind"] for f in d["findings"]]
    _check("零检索归为 zero_retrieval", "zero_retrieval" in kinds, str(kinds))
    _check("每条 finding 都说明犯了什么错",
           all(f.get("mistake") for f in d["findings"]), str(d["findings"]))


def test_diagnose_grounded_reports_evidence() -> None:
    # 有源、命令无据：依据必须说明「源里命令行共 N 条，逐字比对没有」
    src = "Some readme.\n```bash\nnpm install -g @marp-team/marp-cli\n```\n"
    d = G.diagnose("安装：`npm install -g @marp-team/marp`。", src, "",
                   task="怎么安装这个项目")
    _check("有源无据→判不合格", not d["ok"])
    txt = G.render_diagnosis(d)
    _check("依据里给出源内命令行条数", "可执行命令行共" in txt, txt[:200])
    _check("依据里给出源内实际写法作比对（指出不一致，非答案）",
           "marp-cli" in txt, txt[:400])


def test_render_says_why_without_answer() -> None:
    # 用户明确要求：闸门只证伪，不给正确答案。
    d = G.diagnose(
        "这个项目是 marp-team/marp，安装：\n```bash\nnpm install -g @marp-team/marp\n```\n",
        _META_SRC, _META_POOL, task="找一个 markdown 转 ppt 的工具并说明怎么装")
    txt = G.render_diagnosis(d)
    _check("打回含「犯了什么错」", "犯了什么错" in txt, txt[:300])
    _check("打回含「依据」", "依据：" in txt, txt[:300])
    _check("打回声明不给答案", "不会给出正确答案" in txt, txt[:300])
    _check("识别检索落到通用词", "通用词" in txt, txt[:300])
    _check("识别抓的是入口/总览仓库", "入口/总览仓库" in txt, txt[:300])
    # 不得出现可照抄的成品答案
    _check("不含「query 直接写 Marp」这类答案",
           "query 直接写" not in txt, txt[:300])
    _check("不含「候选里这几个像」这类推荐",
           "候选里这几个像" not in txt, txt[:300])


def test_yaml_frontmatter_not_command() -> None:
    # rerun57：```markdown 里的 `marp: true` 是 YAML front matter，不是命令
    reply = ("配置示例：\n```markdown\n---\nmarp: true\ntheme: default\n---\n```\n"
             "然后运行 `marp deck.md --pptx`。")
    claims = G.extract_ops_claims(reply)
    joined = " / ".join(claims)
    _check("YAML 键值行不被当命令", "marp: true" not in joined, joined)
    _check("markdown 围栏整体跳过", "theme: default" not in joined, joined)
    _check("真正的命令仍被抽出", any("marp deck.md" in c for c in claims), joined)


def main() -> None:
    test_intent()
    test_bad_reply_flagged()
    test_good_reply_passes()
    test_honest_disclaimer_passes()
    test_universal_whitelisted()
    test_zero_source_with_command_flagged()
    test_zero_source_asserts_sourced_flagged()
    test_zero_source_no_command_no_claim_passes()
    test_non_howto_skips()
    test_correct_cmd_with_project_token()
    test_wrong_package_name_flagged()
    test_correct_package_passes()
    test_fenced_commands_extracted()
    test_fenced_fabricated_flagged()
    test_no_pure_flag_or_package_name()
    test_yaml_frontmatter_not_command()
    test_citations_extracted()
    test_url_trailing_backtick_stripped()
    test_admission_flags_self_constructed()
    test_admission_passes_retrieved()
    test_citation_ignores_cli_arguments()
    test_citation_ignores_acronym_pairs()
    test_false_provenance_detected()
    test_false_provenance_passes_with_fetch()
    test_diagnose_reports_false_provenance()
    test_unverified_note_is_deterministic()
    test_admission_no_substring_cross()
    test_admission_empty_pool_skips()
    test_admission_ignores_non_retrieval_tools()
    test_near_miss_points_to_longer_token()
    test_near_miss_ignores_unrelated()
    test_pool_repos_from_render()
    test_generic_flood_detected()
    test_meta_source_detected()
    test_diagnose_clean_passes()
    test_diagnose_zero_source_has_mistake()
    test_diagnose_grounded_reports_evidence()
    test_render_says_why_without_answer()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
