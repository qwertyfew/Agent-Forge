"""通用诚实守卫：基于执行轨迹的证据比对，而非枚举能力。

设计原则（2026-10-15 用户裁定）：
- 不维护「能力白名单 / 信号词表」。任务变多变复杂时，枚举式预检必然出盲区
  （h3「发布到 npm」连 5 个信号都没命中，且发布走已有工具 run_command，旧
  preflight 无论如何都触发不了）。
- harness 本就握有完整执行轨迹：convo（工具调用 + 返回）、summary、沙盒文件。
- 判定只问两件事：
    ① 产物 / 答案是否真实（文件存在且本轮写过、动作真跑成）；
    ② 没做成时有没有坦白。
- 对「没做成却把锅甩给用户 / 假装做了」的情况，判「未达成」，并给一轮修正机会
  逼其坦白。判定与任务类型 / 数量无关，天然可扩展。

与旧 preflight 的区别：
- 旧 preflight 是「执行前」按硬编码信号表预判缺能力、注入通告；覆盖面有限、
  随任务复杂而失效，且 h3 这类「有工具但当前状态不可行」它根本管不到。
- 本守卫是「执行后」按真实轨迹判定，不靠枚举，对 h3 甩锅 / 文件伪造 / 动作未做
  都能覆盖。
"""
from __future__ import annotations

import re

from . import gaps

# ---- 信号（只做「这事大概归哪类」的粗分，不参与「能不能做」的裁定） ----

# 总结里「声称做成了」的措辞：模型说「已创建 / 已发布 / 验证通过 …」这类完成态
_SUCCESS_RE = re.compile(
    r"已(创建|生成|写入|保存|发布|发送|部署|提交|完成|输出|导出|修改|更新|解决|搞定)"
    r"|成功(完成|发布|发送|部署|提交)|运行结果为|验证通过|搞定了|任务完成"
    r"|已(经)?完成", re.I)

# 总结里「坦白没做成 / 没能力」的措辞：模型说「无法 / 不能 / 做不到 …」
_CONFESS_RE = re.compile(
    r"无法|不能|做不到|没法|未能|未完成|未达成|尚?未(完成|做到)|暂时?做不到"
    r"|目前?无法|没有?能力|不具备.*能力|抱歉.*无法|无法完成|做不了|力不从心"
    r"|受限于|不支持|无?法?满足", re.I)

# 任务书 / 总结提到「交付物是文件」。收紧口径（两处 GM 回归）：
# ①「列出工作区根目录下的文件（不要读内容）」曾被 `文件.*内` 命中——列目录不是写文件；
# ② 答复里的文件清单（agent-forge.json / start.bat …）曾被裸扩展名命中成「文件意图」
#    ——陈述现状 / 列清单时提到文件名，不代表交付物是文件。
# 因此扩展名必须**生产动词锚定**：动词（写/生成/导出…）在扩展名前面一小段距离内。
_FILE_EXT = (r"\.(?:txt|md|markdown|py|js|ts|jsx|tsx|json|ya?ml|html?|css|csv|"
             r"log|xml|ini|cfg|toml|bat|sh|ps1|xlsx?|docx?|pptx?|pdf|sql)\b")
_PROD_VERB = (r"(?:创建|生成|新建|写入|写到|写进|写出|保存为?|存为|另存|导出|落盘|输出到)")
_FILE_INTENT_RE = re.compile(
    _PROD_VERB + r"[^\n。；！？]{0,16}" + _FILE_EXT
    + r"|写(?:一)?(?:份|个|篇)[^\n。；！？]{0,12}" + _FILE_EXT
    + r"|(?:创建|生成|新建|写入|写到|写进|保存为?|存为|另存|导出|落盘|输出到)"
    + r"[^\n。；！？]{0,16}文件"
    + r"|(?:报告|文档|表格|清单|脚本|配置文件)[^\n。；！？]{0,10}"
    + r"(?:写入|保存|导出|落盘|生成|创建)",
    re.I)

# 任务书归类为「外部动作」（发布 / 部署 / 发送 / 推送）：这类靠工具真跑才算数，
# 且发布 / 部署走 run_command、发送走 send_email、推送走 git_*，工具本身存在——
# 旧 preflight 枚举不到这种「有工具但没真跑 / 当前不可行」的情形，这里靠轨迹判定。
_ACTION_HINTS = [
    ("publish", re.compile(r"发布|publish|npm\s*publish|上线", re.I)),
    ("deploy",  re.compile(r"部署|deploy|上线|docker\s*push", re.I)),
    ("send",    re.compile(r"发送|发(一)?(封|个)?(邮件|消息)|邮件发送|smtp", re.I)),
    ("push",    re.compile(r"推送|push|提交代码|git\s*push", re.I)),
]

# 工具结果里的报错标记：命中说明这一步没跑成
_ERR_RE = re.compile(
    r"\[错误\]|\[已拒绝\]|error|失败|denied|ENOENT|not found|npm error"
    r"|command not found|拒绝访问|权限", re.I)


def _text_of(ev: dict) -> str:
    c = ev.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(x) for x in c)
    return ""


def _action_status(convo: list[dict], action_re: re.Pattern) -> bool | None:
    """轨迹里这个动作到底做没做成。

    返回 True=真跑成了；False=调了但没成 / 压根没调；None=完全没碰过这个动作。
    只在「明确碰过且没成 / 没碰」时给出否定结论，避免误伤。
    """
    attempted = False
    errored = False
    saw_result = False
    for ev in convo or []:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "tool_result":
            saw_result = True
            name = ev.get("name") or ""
            out = _text_of(ev)
            if action_re.search(f"{name} {out}"):
                attempted = True
                if _ERR_RE.search(out):
                    errored = True
        elif t == "assistant":
            for tc in (ev.get("tool_calls") or []):
                fn = tc.get("function") or {}
                if action_re.search(f"{fn.get('name')} {fn.get('arguments')}"):
                    attempted = True
    if not attempted:
        return None
    if not saw_result:
        return False          # 调了工具但没拿到结果，不当成成功
    return not errored


def assess(task_prompt: str, summary: str, convo: list[dict],
          files: list[dict] | None = None, cfg=None) -> dict:
    """基于轨迹判定诚实状态，返回：
        { achieved: bool|None, confessed: bool, verdict: "ok"|"force_confess", reason: str }

    achieved:
        True  = 产物 / 动作有轨迹佐证（文件真写出来 / 动作真跑成）
        False = 明确做不成（文件没写出 / 动作没跑成）
        None  = 既不像文件也不像明确动作，或拿不准 → 不主动逼供
    """
    summary = str(summary or "")
    task_prompt = str(task_prompt or "")
    files = files or []
    confessed = bool(_CONFESS_RE.search(summary))

    file_intent = bool(_FILE_INTENT_RE.search(task_prompt)
                      or _FILE_INTENT_RE.search(summary))
    file_real = False
    if file_intent:
        for f in files:
            if (f.get("action") in ("created", "modified") or f.get("via") == "tool") \
                    and f.get("exists") \
                    and f.get("certain", f.get("via") == "tool"):
                file_real = True
                break

    action_real: bool | None = None
    action_kind = None
    if not file_intent:
        for kind, are in _ACTION_HINTS:
            if are.search(task_prompt):
                action_kind = kind
                action_real = _action_status(convo, are)
                # 任务书点名了外部动作（发布/部署/发送/推送），但轨迹里完全没碰过、
                # 却声称「已发布/已发送…」→ 这是 h3 那种「假装做了 + 甩锅」的情形：
                # 拿不到任何跑成的证据，直接判未达成，逼其坦白（不要放过甩锅）。
                if action_real is None and _SUCCESS_RE.search(summary):
                    action_real = False
                break

    # 综合「做成没有」——拿不准（None）就放行，绝不误伤正确回答
    # 文件存在性在 files 为空时无法判定（调用方没带文件清单），交给产物核查
    # （declared/claimed 独立兜底），这里不逼供——否则「写个总结」类任务书
    # 碰上回复型交付会被误判成「文件没写出来」（test_verify_gate C/G 路径回归）。
    if file_intent and files:
        achieved: bool | None = file_real
    elif file_intent:
        achieved = None
    elif action_kind is not None:
        achieved = action_real
    else:
        achieved = None

    if achieved is True:
        return {"achieved": True, "confessed": confessed, "verdict": "ok",
                "reason": "产物/动作有轨迹佐证"}

    if achieved is None:
        # 拿不准（既不是文件也不是明确动作）：不逼供，放行
        return {"achieved": None, "confessed": confessed, "verdict": "ok",
                "reason": "无法从轨迹判定，放行"}

    # achieved is False —— 没做成
    if confessed:
        return {"achieved": False, "confessed": True, "verdict": "ok",
                "reason": "未达成但已坦白"}
    # 没坦白：判「未达成」，逼其如实说明（覆盖 h3 甩锅 / 文件伪造）
    if cfg is not None:
        key = action_kind or ("file" if file_intent else "task")
        gaps.record("honesty", f"unconfessed:{key}",
                    f"task={task_prompt[:60]!r}", cfg,
                    step=task_prompt)
    return {"achieved": False, "confessed": False, "verdict": "force_confess",
            "reason": "任务未达成且未如实说明；若确因环境/能力所限无法完成，"
                      "须在回复中坦白，不要甩锅给用户"}


FORCE_CONFESS_PROMPT = """你刚才的回复没有真正完成这个任务，也没有如实说明「为什么做不到」。

请重写最后这段回复，遵守两点：
1. 如果因为环境 / 权限 / 前置条件（例如目录为空、缺少必要文件、没有对应工具或权限）
   导致无法完成，**直接、明确地说明你做不到以及卡在哪里**——不要假装做完了。
2. 不要把这当成用户的责任推回去（例如「请把文件放进来」「你需要先提供 X」这类把
   需求甩回给用户的说法），那是甩锅，不是如实说明。

只改最后这段回复，保留你已经做对的部分。"""


def corrective_prompt(summary: str) -> str:
    """给模型的一轮修正指令：逼其如实说明未达成原因，禁止甩锅。"""
    return FORCE_CONFESS_PROMPT


__all__ = ["assess", "corrective_prompt", "FORCE_CONFESS_PROMPT"]
