"""验证 autonote / autoskill 的生成逻辑修复：用真实脏数据回放。"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from agentforge import autonote, autoskill

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


print("=" * 78)
print("A. 规则抽取：任务书原句不该被记成偏好（历史脏数据回放）")
print("=" * 78)

# 这 4 句是真实的错误记忆（来自 memory.md）
DIRTY = [
    "1. 必须先用 read_file 读完全文再动笔，不许凭印象编",
    "3. 素材里有多条故事线（三国穿越线、雨林演化寓言线、现代都市线等），请先梳理出每条线的主角和核心设定，"
    "再判断哪条是主线，不要把某个配角的特殊能力当成故事核心",
    "交接说明：已确认：1. 必须完整读取指定路径下的写作.txt（约3.3万字）",
    "4. 大纲用简体中文写。",
    # 这一条是修复过程中，正在跑的评测任务用旧逻辑又写进记忆的
    "它的官方资料我已经抓好放在工作区里了，直接用 read_file 读，不要联网：",
    "1. 项目概述：它是什么、解决什么问题、关键事实（star 数、语言、开源时间、默认分支）",
    "【产出】把评估报告写入 docs/TENCENTDB_AGENT_MEMORY_EVAL.md（相对工作区根目录）",
]
# 这 2 句是真实的正确记忆，必须仍然能收进来
GOOD = [
    "用户偏好：代码注释一律使用中文。回复风格：先给结论，再给细节。",
    "用户偏好：以后写前端文案一律使用简体中文。",
]

msgs_dirty = [{"role": "user", "content": s} for s in DIRTY]
got = autonote.rule_notes(msgs_dirty)
for s in DIRTY:
    hit = s in got
    check(f"已过滤任务书句式: {s[:34]}…", not hit)

msgs_good = [{"role": "user", "content": s} for s in GOOD]
got_good = autonote.rule_notes(msgs_good)
print("  真实偏好解析结果:", got_good)
# rule_notes 按句切分，所以用「关键片段是否被收到」判断，而不是整句全等
for key, label in [("代码注释一律使用中文", "代码注释用中文"),
                   ("前端文案一律使用简体中文", "前端文案用简体中文")]:
    check(f"仍能收到真实偏好: {label}",
          any(key in n for n in got_good))

# 用户明说「记住」的，哪怕像任务要求也必须收（强信号优先）
strong = [{"role": "user", "content": "记住：以后所有报告都要先给结论再给细节"}]
check("强信号（记住/以后）不受任务书过滤影响", len(autonote.rule_notes(strong)) == 1)

print()
print("=" * 78)
print("B. 模型抽取：本次任务的结论不该进长期记忆")
print("=" * 78)

BAD_RAW = (
    "- 小说《翡翠王朝》素材（D:\\AI_develop\\project\\novel\\notes_extracted\\写作.txt）：唯一主线是三国穿越线，"
    "主角是刘备；「小师弟」全篇仅 4 行，是边缘搞笑配角\n"
    "- 用户偏好：代码注释一律使用中文\n"
    "- 本次任务的产出已写入 outline.md，共 1794 字\n"
)
notes = autonote.parse_model_notes(BAD_RAW)
print("  解析结果:", notes)
check("带绝对路径的本次结论被丢弃", not any("写作.txt" in n for n in notes))
check("提「本次任务」的结论被丢弃", not any("本次" in n for n in notes))
check("真正的偏好被保留", any("代码注释" in n for n in notes))

print()
print("=" * 78)
print("C. 技能抽取：正文混入本次任务专有内容 → 整条丢弃")
print("=" * 78)

task_msgs = [{"role": "user", "content":
              "读 docs/ref/README_CN.md 和 agentforge/memory.py，把评估写入 docs/EVAL.md"}]
arts = autoskill.task_artifacts(task_msgs)
print("  从任务原文抽出的产物:", sorted(arts))
check("抽出 README_CN.md", "readme_cn.md" in arts)
check("抽出 EVAL.md", "eval.md" in arts)

clean_skill = {"name": "doc-eval", "description": "评估外部资料的方法",
               "keywords": ["评估", "调研", "对比"],
               "body": "## 流程\n先读材料，再逐条对照，最后给结论。"}
check("干净的 skill 不被误杀", autoskill.task_residue(clean_skill, arts) == "")

dirty_skill = {"name": "doc-eval", "description": "评估外部资料的方法",
               "keywords": ["评估", "调研"],
               "body": "## 反面案例\n这次把 README_CN.md 当成零散素材，只读了开头。"}
check("含本次文件名的 skill 被拦下",
      autoskill.task_residue(dirty_skill, arts) != "",
      autoskill.task_residue(dirty_skill, arts))

path_skill = {"name": "x", "description": "d", "keywords": ["a"],
              "body": "参考 D:\\AI_develop\\project\\novel 里的做法"}
check("含绝对路径的 skill 被拦下", autoskill.task_residue(path_skill, arts) != "")

print()
print("=" * 78)
print("综合:", "全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
