"""操作类答复事实核查（Option A）单元测试。

复现 rerun51 实证：模型 fetch 了 README 却写出 `python main.py` / `npm run dev` /
`src/components` / `frontend/` 等源里没有的通用模板——这类必须在 final 答复阶段拦下。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentforge import grounding as G


def _check(name: str, cond: bool) -> None:
    print(("OK  " if cond else "FAIL") + "  " + name)
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


def test_no_sources_no_flag() -> None:
    # 没源可核时不拦（交给收尾闸门）
    issues = G.reply_grounding_issues("运行 `python main.py`", "", intent=True)
    _check("无源时不拦", not issues)


def test_non_howto_skips() -> None:
    # 非操作类问题不启用核查
    issues = G.reply_grounding_issues("运行 `python main.py`", SRC, intent=False)
    _check("非操作类意图跳过", not issues)


def test_correct_cmd_with_project_token() -> None:
    # `node bin/ui-critic.mjs` 含项目专属 token ui-critic（源里有），应放行
    ok = "运行 `node bin/ui-critic.mjs` 即可启动。"
    issues = G.reply_grounding_issues(ok, SRC, intent=True)
    _check("含项目名的正确命令不误伤", not issues)


def main() -> None:
    test_intent()
    test_bad_reply_flagged()
    test_good_reply_passes()
    test_honest_disclaimer_passes()
    test_universal_whitelisted()
    test_no_sources_no_flag()
    test_non_howto_skips()
    test_correct_cmd_with_project_token()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
