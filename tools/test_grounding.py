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
    test_citations_extracted()
    test_url_trailing_backtick_stripped()
    test_admission_flags_self_constructed()
    test_admission_passes_retrieved()
    test_admission_no_substring_cross()
    test_admission_empty_pool_skips()
    test_admission_ignores_non_retrieval_tools()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
