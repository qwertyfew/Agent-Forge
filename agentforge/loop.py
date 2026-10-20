from __future__ import annotations

import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Iterator

from .config import Config
from . import gaps
from . import skeleton
from . import verify as verify_gate
from .artifact import FILE_TOOLS, classify_tool_write
from .context import Compressor, measure
from .evidence import EvidenceLedger
from .record import RECORDER_CTX
from .journal import Journal, new_sid
from .llm import OllamaClient, parse_text_tool_calls
from .toolselect import PTC_SURFACE
from .memory import MemoryStore
from .llm import GenerationStopped
from .tasksession import contract_paths
from .tools.ops import OPS_AGENT_CTX
from .permissions import (
    OUTSIDE_OK,
    OUTDIR_CTX,
    WRITE_ALLOW,
    Guard,
    PermissionDenied,
    claim_write,
)
from .pricing import cost_of, is_free
from .providers import ClientHub
from .router import Router
from .runcontrol import current_task
from .skills import SkillRegistry
from .tools import ToolRegistry
from .tools.base import SAFE

# 内部回灌消息的前缀：闸门拦截、预算提醒、空轮次催促、中途补充——它们都以
# user 角色进上下文，但不是用户本人说的话。会话日志靠这个把「用户要的」与
# 「系统要求的」分开记，重放时才能分清是谁在推着模型走。
_INTERNAL_PREFIX = (
    "（用户中途补充", "【停止拦截", "【系统", "你上一轮没有输出",
    "已达到轮次上限", "任务预算提醒",
)


def _msg_source(msg: dict) -> str:
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return ""
    return "system" if str(msg.get("content") or "").startswith(_INTERNAL_PREFIX) else "user"


SYSTEM_TEMPLATE = """你是 AgentForge，一个运行在本机上的智能体。你的职责是**真正动手完成任务**，而不是描述该怎么做。

## 工作环境
- 操作系统：{os}
- 今天的日期：{today}
  凡是要写「取数时间 / 当前时间 / 截至日期」的地方，一律以此为准；禁止自己编造或猜测日期。
- 工作区根目录：{root}
- 相对路径一律相对工作区根目录解析；{outside_rule}
- 当前使用的模型：{model}（{model_desc}）

## 工作方式
1. **先看清现状**：不确定就先用 list_dir / find_files / search_code / read_file 获取真实信息，绝不凭猜测回答。
2. **最小改动**：优先用 edit_file 做精确替换，不要用 write_file 整文件重写已有内容。
   改代码前，先用 impact_of 看清「这个函数/类/文件被谁引用了」，评估波及范围再动手——
   只改定义处、漏掉引用方，是回归最常见的来源。
3. **复用优先**：动手实现一个能力之前，先确认它是否已经存在——用 find_tools 或
   工具索引查工具，用 find_files / search_code 查项目里现成的脚本与函数。
   已有的直接复用；禁止在不知道现状的情况下重复造轮子。
4. **复制/备份走命令**：复制、备份文件且要求内容不变时，用 run_command 执行复制命令
   （Windows：copy /y；跨平台：cp），**禁止 read_file 后用 write_file 重写**——文本往返
   会丢换行符与编码，肉眼看不出来，逐字节比对一跑就现形。任务书声明了逐字节
   比对验证的，写完确认它通过再收工。
5. **一次只做一步**：可以连续调用多个互不依赖的只读工具，但有依赖时必须等上一步结果。
6. **批量只读用 ops_batch**：需要读多个文件、多轮检索或跨文件比对汇总时（例如分析一个
   仓库、扫一批文件提炼结论、对比多个模块），用 ops_batch 写一段 Python 脚本一次完成——
   脚本里 call("read_file", path=...) 调工具（等价写法 tools.call(...) 也行），print() 输出最终结论。这样只往返一轮，
   远比逐个工具调用省时间。单个文件、单次检索不需要，直接调对应工具即可。
7. **交付前自检**：重要产出交付前对照原始要求核对一遍——要求是否都满足、有无编造的细节、
   有无沿用作废的旧结论。发现就改，再交付。
8. **记住该记的**：如果这轮对话里出现了**用户的长期偏好**（稳定的倾向 / 风格 / 习惯，
   如「回复先给结论」「产出文档一律简体中文」），用 remember 把它写进长期记忆；下次任务会自动读到。
   环境事实（路径 / 端口 / 版本）工具能现查、项目约定（工作方式 / 流程规则）由架构保证，
   两类都**不要**写进记忆。只写偏好，不要写过程。
   某类任务反复用到的方法论，做成 skill 沉淀，而不是每次靠提示词提醒。
9. **缺能力就登记**：发现完成任务必需的工具/能力或技能在环境里没有时，用 report_gap
   登记（kind=tool 缺工具/能力；kind=skill 缺技能/方法论，spec 写清它需要做什么）。
   登记后按返回指令走：缺工具→如实告知用户无法完成，绝不编造；
   缺技能→用 github_search / web_search 检索通用做法，在最终回复里总结出可操作的流程。
10. **信息不足先问再猜**：任务需要的关键信息检索不到（用户提到的项目/文件/概念
   在工作区和已查来源里都没有）时，用 ask_user 发起澄清弹窗——question 一句话
   点明要澄清什么，options 给 2-4 个简明选项（每个不超过 12 字，覆盖最可能的
   情况，用户也可以自行补充文字）。用户的答复作为工具结果返回，拿到后**按答复
   继续**（如答复指向 git 项目就走检索、指向本地项目就读工作区）。确实拿不到
   答复才按最常见的情况假设并注明。禁止在关键信息缺失时编造。

## 输出要求
- 用中文回复，简洁直接，不复述工具返回的原文。
- 调用工具时不要向用户解释你要调什么；直接调用。
- 回复结构：第一句先给结论或结果，再给细节；内容多时分小节，每节一个主题；
  结尾一句说明下一步或需要用户决定的事。不要以寒暄或复述用户问题开头。
- 涉及本项目现状的结论（目录里有哪些文件、代码里有什么、配置是什么、某功能是否存在、
  某个工具支持什么参数）必须先用 list_dir / find_files / read_file / search_code 等工具
  查证后才能写进回复；没查证的就明说「未核实」。**「列出 / 看看」这类要求问的就是工作区
  现在有什么，不是让你回忆**——工作区里没看过的东西，一个文件名都不许写出来。
  收尾时系统会核查这一点（要列工作区现状却全程没读过工作区 → 先退回修正一轮，
  仍不改则在答复末尾附系统侧核查声明）。
- 涉及**外部可核实事实**的结论同样必须查证：数字（star / fork / 下载量 / 版本号 /
  日期 / 计数）、时效性（「最近还在更新」）以及任何具体指标，都要来自 fetch_url 或
  fetch_json 抓到的**一手来源**（官方站点、官方文档、GitHub API、包仓库 API），
  不能来自 web_search 的搜索摘要。搜索摘要一律算「转述」，只能用来找入口，
  不得当作事实引用。回复里区分两者：来自一手来源的照常写；只有转述的必须写明
  「据搜索结果，未经核实」。对象是某个仓库 / 库 / 产品时，先 fetch_json 打它的
  官方 API（如 GitHub 仓库用 https://api.github.com/repos/<owner>/<repo>）
  拿到真实数据，再写结论。
- 做仓库分析 / 差距清单 / 逆向报告这类「回写证据」的任务时，引用仓库内文件与代码
  必须遵守四条证据纪律，避免「路径看似合理但读者复现不到、数字看着精确但仓库里查无此处」。
  **其中 1/2/3 已由架构层在「写入报告」时强制校验**：引用了不存在的路径、把目录当文件
  引用、行号超出文件实际范围——写入会被直接拒绝并退回修正（你会在 write_file 的返回里
  看到具体哪条引用错了）。所以你只需专注第 4 条与整体质量；但引用前仍请尽量先 read_file /
  search_code 核实，因为架构层只拦「确定错误」，拦不了「查错了文件」这类软问题：
  1. 引用前先核实存在：任何写进回复的文件路径、类名、函数名，先用 list_dir / read_file /
     search_code 确认它真的存在；路径是自己拼出来的、或来自别处摘要的，必须先核实再写，
     禁止凭印象写路径（尤其目录名的大小写、连字符 / 下划线差异：`learn-claude-code`
     不是 `learn_claude_code`，`agentforge/tools/` 是包目录不是文件）。
  2. 区分文件还是包（目录）：写 `agentforge/tools.py` 这类名字前先确认它究竟是文件还是
     包目录——若实际是包 `agentforge/tools/`，必须落实到具体文件如 `agentforge/tools/fs.py`；
     别把包目录名当文件引用，读者按你写的路径去找会找不到。
  3. 行号绑定符号而非裸行号：引用具体代码位置时，优先写稳定的符号锚点
     （如「`class Agent` 内」「`def run()` 处」「`LeaderContext.add_progress`」），
     不要只写 `loop.py:96-124` 这样的裸行号——行号会随代码改动漂移，符号锚点才可复现
     （架构层会对此给软提醒，但不阻断写入）。
  4. 二手数字不当仓库证据：清单 / 报告里引用的任何数字（star / fork / 准确率 /
     子任务数…）必须是仓库一手证据（代码、README、注释、测试结果、官方 API 返回值）；
     来自任务书背景或别处摘要的数字，不得当作仓库内证据写入，必须标注
     「据…（未核实）」或直接删除（架构层在「只抓过网页却含数字」时会给软提醒）。
- 任务完成后如实总结：改了哪些文件、验证了什么、验证是否通过。没验证的说
  「未验证」，失败或没做完的直说，禁止把没验证的东西说成已验证。
- 加粗（**…**）克制使用：只给与用户问题直接相关的关键结论，标题、列表行首、
  普通强调一律不加粗。满屏黑体等于没有重点。

## 安全约束
- 当前权限模式：{permission}。默认权限下写文件与执行命令需要用户确认；完全访问权限下直接执行。
- 被拒绝时不要反复重试同一操作，换一种方式或向用户说明。
- 禁止删除工作区之外的任何东西。

{write_rules}
"""

WRITE_RULES_TEMPLATE = """
## 写文件：系统按记录核对，你不需要自证
- 你每次 read_file 读了哪些文件的哪些行，harness 都记在 `{rec}/evidence.jsonl`。
  写文档时引用 `path:行号`，行号必须落在这份记录里你真正读过的区间内；否则程序直接拒收，
  并回传「你实际读过的区间」。**不需要为了留痕额外写中间文件**——读这个动作本身已经被记录。
- 每次成功写盘都会在 `{rec}/versions/` 留一份全文快照（同一次任务里连续改写
  就是多个版本，谁也不覆盖谁）。所以：写错了**不用整篇重生成**，用 edit_file 改点到的几行即可；
  真要覆盖整篇也不会丢上一版，事后可从版本目录取回。
- 中间/过渡文件（草稿、提取的数据、临时脚本、对比档）**可以照常写**，放进产出目录即可，
  不进交付物清点（清空任务时可一并清理）；交付物仍然只有任务书点名的那几个文件。
- 超过两节的文档**先写骨架再分段填**：write_skeleton 只放 `## 小节标题` + 一句意图，
  再 fill_section 一节一节填；同一节要续写用 mode="append"（replace 是整节替换）。
  骨架不算交付：空着小节的文件，收尾核查判未交付。
- 引用密集的文档（≥3 处项目内路径）在写之前必须读过被引用的文件，否则写前门槛拒收并列出必读清单。
- 工具调用方式随模式走：PTC 常驻时这些都写在 ops_batch 脚本里，
  如 `call("write_skeleton", path=..., outline=...)`、`call("fill_section", path=..., heading=..., content=...)`。
"""

# 产出目录规则：任务发起时由编排层按发起时间算好一个文件夹名填进 outdir，
# 中间/最终产物一律集中放进去——工作区根目录不被散落的报告、草稿、脚本糊满，
# 收尾清理或复盘时整个文件夹一删/一归档就是一次任务的全部产出。
OUTDIR_RULE = """
## 产出与中间文件
- 本任务的产出目录（相对工作区根目录）：**{outdir}**（目录名即任务发起时间）
- 你要新建的文件——分析报告、草稿、提取的数据、临时脚本、生成的文档等
  中间产物与最终产物——**一律创建在上述产出目录里**。
- 这是**架构保证**：write_file 的相对路径会被自动重定向进产出目录，写偏了
  系统会告诉你实际落点。例外通道：任务书里明确点名的路径（按任务书写），
  或绝对路径（显式意图，受工作区边界约束）。
- 引用产出时在回复里给出相对工作区的完整路径，方便用户直接打开。
"""

# 系统提示里的路径规则一句话，按权限模式二选一（渲染见 _system()）。
# 完全访问权限放开的是「读写」工作区之外；「禁止删除工作区之外的任何东西」
# 是安全底线，写在模板末尾，两种模式都保留。
OUTSIDE_RULE = {
    True: "完全访问权限下也允许读写工作区之外的文件夹（用绝对路径）",
    False: "越出工作区的路径会被拒绝",
}

# 长对话末端的语言提醒。系统提示开头明明写了「用中文回复」，但本地小模型
# 在几十轮工具调用之后经常把它「忘」了，回复漂移成英文（实测 gemma4:12b，
# 中文维度分只有 62）。把关键输出要求复述一份放在消息列表最末尾——
# 那是生成时注意力离得最近的位置。
#
# 【为什么还是 system 角色】语言提醒必须贴在消息列表最末尾才有用（生成时
# 注意力离得最近）。但严格 provider 要求 system 只能在开头，末尾追加会 400。
# 解法不是换成 user——实测那样模型会把提醒当成一轮新对话去「回复」它，反而
# 打断干活节奏、甚至在没写完文件时就收尾——而是维持在末尾追加 system，同时由
# providers._convert_messages 在发出去前把所有 system 上提合并成一条。
# 于是：本地 Ollama 保留「贴末尾」的注意力优势（它容忍任意位置的 system），
# 在线严格 provider 拿到的是合法的「开头单条 system」。
LANG_REMINDER = {"role": "system",
                 "content": "提醒：忽略过程中的语言，最终回复一律用简体中文。"
                            "加粗只用于与用户问题直接相关的关键结论，标题与列表行首不要加粗。"}

# 工具失败结果的系统注脚。第八轮测试的教训：edit_file 三连失败，agent 没读
# 错误信息就谎报「所有断言通过」。错误返回不能只靠模型自觉去看——失败结果
# 显式加标记，并点名两条禁令：禁止无视失败继续，禁止未验证就声称完成。
FAIL_NOTE = (
    "\n[系统要求] 上面是一个失败的工具结果。必须先读错误信息、修正做法后重试；"
    "禁止无视这个失败继续执行后续步骤；禁止在未重新执行并看到成功结果的情况下，"
    "声称这一步已经完成。"
)

FAIL_REPEAT_NOTE = (
    "\n[升级警告] 同一工具已连续失败 {n} 次。停止用同样的参数重试："
    "先 read_file / list_dir 核实现场，换一种方法；还是不行就如实向用户说明卡在哪里。"
)

# 长期记忆注入时的标题。写成指令而不是资料，模型才会真的照做
MEMORY_HEADER = """## 长期记忆（用户偏好，来自以往任务，请照此执行）
以下是用户以往表达过的偏好与习惯，请在本轮工作中遵守。
"""

# 任务笔记的注入头。笔记是跨压缩持久的：每轮从磁盘现读现注入，
# 旧工具结果被压成一行后，笔记里记下的发现仍然完整在场。
TASK_NOTES_HEADER = """## 任务笔记（你自己记下的发现，不受上下文压缩影响）
以下是本任务里你用 task_notes 记下的关键信息。注意：较早的工具返回可能已被
压缩成一行摘要，但笔记是完整的——**先查笔记再决定要不要重新调用工具**。
"""

# 工具索引：只给名字，不给说明。完整 schema 里 4000 tokens 大部分当轮用不上，
# 但模型得知道"有这么个能力"，否则会说"我做不到"。要用时用 find_tools 现取。
TOOL_INDEX_HEADER = """## 其他可用工具（本次未展开说明）
下面这些工具现在就能用，只是为了省上下文没有下发详细参数。需要哪个就先调用
find_tools("关键词") 取它的用法，然后再调用它；也可以直接按名字调用。
  {names}
"""

# PTC 常驻：模型只在 ops_batch 脚本里用 call("工具名", ...) 调这些工具，
# 不能直接调用，所以索引说明换成「脚本内调用」口径。
TOOL_INDEX_HEADER_PTC = """## 可编入脚本的工具（本次未展开说明）
下面这些工具不能直接调用，只能在 ops_batch 脚本里用 call("工具名", ...) 调用（等价写法 tools.call(...) 也行）。
需要哪个就先 find_tools("关键词") 取它的参数，再写进脚本：
  {names}
"""

# PTC 常驻模式追加到系统提示的说明块（见 _system）。
PTC_RESIDENT_BLOCK = """## PTC 常驻（程序化工具调用）
本模式下你**不直接调用任何工具**，所有工具操作都写进一段 Python 脚本、用 ops_batch 一次性执行：
- 脚本里用 `call("工具名", **参数)` 调工具（等价写法 `tools.call("工具名", ...)` 也行），`print(...)` 把最终结果回传；只有 print 的内容会回到你这里。
- **切勿在脚本里写 `import tools`**——这会覆盖内置的 tools 壳；直接用 call(...) 即可。
  （`from tools import read_file`、`tools.read_file(...)` 也能用，但没有必要。）
- 只读工具自动放行；写/命令类工具需本次运行是完全访问权限；脚本里不能调 ops_batch / ask_user。
- 想知道某工具的精确参数，先 `find_tools("关键词")` 取用法，再写脚本。
- 唯一允许直接调用的例外是 ask_user（交互式澄清弹窗，脚本内无法弹窗）。
"""

# 工具调用名格式校验：合法名应为小写字母/数字/下划线/点（如 read_file / git_clone /
# 模型偶发的 git.clone 点号变体）。出现空白、CJK、问叹号、括号等特殊符号，或长度异常，
# 即视为「任务原文泄漏 / 乱码」，不当作能力缺口，避免污染台账（实测曾出现
# "this??...?" 这类名字被误记成 tool_missing）。点号与大小写差异是「可恢复的名字写法」，
# 不算乱码——前者由 _execute 归一化、后者交由选择器建议，都不该被当成缺口。
_TOOL_NAME_OK = re.compile(r"[a-z][a-z0-9_.]*")


def _is_malformed_tool_name(name: str) -> bool:
    s = str(name or "")
    if not s or len(s) > 64:
        return True
    if any(ch.isspace() for ch in s):
        return True
    if any(0x4E00 <= ord(ch) <= 0x9FFF for ch in s):
        return True
    if set(s) & set("?!\"'#%&*(){}[]<>=+$|\\/"):
        return True
    if not _TOOL_NAME_OK.fullmatch(s.lower()):
        return True
    return False

COMPACT_THRESHOLD = 60_000

# ---------------------------------------------------------------------------
# 确定性澄清触发：检索「未命中 / 多候选」不靠提示词祈祷，程序亲自识别。
#
# 背景（2026-10-15 评测 r1 两轮同因失败）：gemma4:12b 检索不到时会把澄清
# 问题写在回复文本里，而不是调用 ask_user——意图对、通道错。提示词第 9 条
# 已在位但管不住小模型，所以把触发下沉到架构层：
#   1. 每次工具返回后，按工具层**固定渲染格式**识别「未命中 / 多候选」信号
#      （纯正则，不依赖模型任何自觉；工具改格式这里同步改）；
#   2. 模型给出最终回复时，若本场 run 存在未消化的信号且回复带追问语气
#      （同样是正则判定），harness 亲自发起 ask_user 弹窗——问题取模型
#      追问的原话（拿不到就按信号合成），多候选时选项直接用命中项目名；
#   3. 拿到答复（真实用户 / 评测脚本 / 无用户提示）回灌给模型继续任务。
# 弹窗真实发生、答复真实回传，评测的 must_ask 由此按事实通过。
# ---------------------------------------------------------------------------
_GH_ROW_RX = re.compile(r"^\s*\d+\.\s+(\S+)\s+star=", re.M)   # github_search 渲染行
_BRACKET_Q_RX = re.compile(r"「([^」]{1,60})」")
# 追问语气：有问号 + 追问惯用词。宁可宽松——触发还要同时满足「本场有检索信号」
# 和「每场至多一次」，误触发的代价只是一次正常澄清。
_CLARIFY_RX = re.compile(r"[^。！？!?\n]{4,80}[？?]")
_CLARIFY_HINTS = ("请问", "请告知", "请提供", "请说明", "请补充", "请确认",
                  "是指", "哪一个", "哪个", "哪些", "是什么", "指的是")
_EMPTY_MARKERS = (
    "没拿到结果",                 # github_search：仓库搜索没拿到结果
    "未找到匹配",                 # search_code / find_files
    "共 0 个目录 / 0 个文件",     # list_dir：空目录
    "没有返回结果",               # web_search
    "文件不存在",                 # read_file
)
_FETCH_MARKERS = ("请求失败", "未找到")   # fetch_url / fetch_json 拿不到来源
_RETRIEVAL_TOOLS = {"list_dir", "find_files", "search_code", "read_file",
                    "github_search", "web_search", "fetch_url", "fetch_json"}
_EMPTY_OPTIONS = ["本地项目", "git上的项目", "其他"]


def _lookup_signal(name: str, out: str) -> dict | None:
    """按工具层固定输出格式识别检索信号。返回
    {"kind": "multi"|"empty", "query": str, "candidates": [..]} 或 None。
    只看检索类工具——写类工具报「文件不存在」是另一回事，不算检索未命中。"""
    out = str(out or "")
    if not out or name not in _RETRIEVAL_TOOLS:
        return None
    rows = _GH_ROW_RX.findall(out)
    if len(rows) >= 2:                       # ≥2 个编号候选 =「多种可能不确定」
        q = _BRACKET_Q_RX.search(out)
        return {"kind": "multi", "query": q.group(1) if q else "",
                "candidates": rows[:3]}
    markers = list(_EMPTY_MARKERS) + (
        list(_FETCH_MARKERS) if name.startswith("fetch") else [])
    if any(m in out for m in markers):
        q = _BRACKET_Q_RX.search(out)
        if q:
            query = q.group(1)
        else:                                # list_dir 等无「」格式：首行就是路径
            query = out.splitlines()[0].strip()[:60] if out.splitlines() else ""
        return {"kind": "empty", "query": query, "candidates": []}
    return None


def _has_clarify_intent(text: str) -> bool:
    """最终回复是否在向用户追问（问号 + 追问惯用词，双条件同时满足）。"""
    if "？" not in text and "?" not in text:
        return False
    return any(w in text for w in _CLARIFY_HINTS)


# 记忆检索时排除的泛词：命中它们等于没查（几乎所有记忆条目都带这些词）
_MEM_STOPWORDS = {"项目", "文件", "信息", "检索", "记忆", "任务", "工作",
                  "目录", "内容", "情况", "说明", "结论", "回复", "建议",
                  "检查", "以上", "如下", "以下", "本地", "来源"}


def _memory_hits(memory, query: str) -> list[str]:
    """在长期记忆条目里找与缺失信息相关的记录（确定性关键词匹配）。

    口径：查询词切成字母数字段与中文段（剔除泛词），任一段被条目包含即命中。
    纯子串双向不比了——用「短 token 在条目文本里出现」一个方向，token 先按
    分隔符拆细（r1-rea-roadmap → r1/rea/roadmap），保证 REA 这类名字能命中。
    """
    toks: set[str] = set()
    for seg in re.findall(r"[A-Za-z_][\w-]{1,}", query or ""):
        toks |= {t.casefold() for t in re.split(r"[-_.]+", seg) if len(t) >= 2}
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
        if seg not in _MEM_STOPWORDS:
            toks.add(seg)
    if not toks:
        return []
    hits: list[str] = []
    for e in memory.entries():
        text = str(e.get("text") or "").strip()
        if not text:
            continue
        tl = text.casefold()
        if any(t in tl for t in toks):
            hits.append(text)
    return hits[:5]


class Agent:
    def __init__(
        self,
        cfg: Config,
        client: OllamaClient,
        registry: ToolRegistry,
        guard: Guard,
        router: Router,
        model_key: str | None = None,
        skill_registry: SkillRegistry | None = None,
        hub: ClientHub | None = None,
    ):
        self.cfg = cfg
        self.client = client
        # hub 负责把请求分发到本地 Ollama 或付费云端 provider；
        # 不传就退化成"只有本地 Ollama"的单 provider 模式
        self.hub = hub or ClientHub(cfg, client)
        self.registry = registry
        self.guard = guard
        self.router = router
        self.skills = skill_registry
        self.compressor = Compressor(cfg, client, hub=self.hub)
        self.model_key = model_key or cfg.routing_default
        # 本任务的产出目录（相对工作区根目录的路径，按任务发起时间命名）。
        # 编排层每次任务算好填进来；空字符串表示未设置（系统提示里就不出这条规则）。
        self.outdir: str = ""
        # 任务开局预热注入（agentforge/warmup.py）：编排层算好填进来，系统提示
        # 附加项目速览（目录树/README/命中文件）。放系统提示是刻意的——
        # _compact 只压历史、永不碰系统提示，预热块处于「免压缩区」。
        self.warmup_block: str = ""
        # 用户是否显式指定过模型。自动路由的开关看它，而不是看「当前是不是默认模型」——
        # 默认模型本机跑不起来时会被替换成别的 key，那种情况下不该从此不再路由。
        self.model_pinned = bool(model_key)
        self.system_prompt = ""
        self.last_compact: str = ""
        # 会话事件日志（journal.py）：当前 run 的记账句柄与两个游标。
        # 关闭（cfg.journal=False 或开不出文件）时为 None，所有记账调用都退化。
        self.journal = None
        self._journal_system = ""
        self._journal_step = 0
        self._skill_hint: str = ""
        # 本轮实际下发的工具名集合：系统提示里的「工具索引」要把它们排除掉
        self._active_tool_names: set[str] = set()
        # 长期记忆：任务开始前读进来注入系统提示，任务结束后由 remember 工具追加
        self.memory = MemoryStore(cfg.state() / "memory.md")
        # 权限模式的中文名，只用于系统提示里的一句话说明
        self.permission_label = "完全访问权限"
        # 是否处于完全访问权限（决定路径边界放不放工作区之外，见 OUTSIDE_RULE）
        self.full_access = True
        # 连续失败计数：同一 agent 连续收到失败的工具结果时逐次升级提醒
        # （FAIL_NOTE / FAIL_REPEAT_NOTE），见 run() 里的工具结果分发处
        self.fail_streak = 0

    # ---------- 模型 ----------
    def set_model(self, key: str) -> None:
        if key not in self.cfg.models:
            raise KeyError(f"未定义的模型 key: {key}")
        self.model_key = key
        # 调用方明确点名了模型（编排层给每一步分配、用户 /model 切换），
        # 后续这一轮就不该再被自动路由改掉。
        self.model_pinned = True
        self.system_prompt = ""

    @property
    def model_name(self) -> str:
        return self.cfg.model(self.model_key).display

    @property
    def model_spec(self):
        return self.cfg.model(self.model_key)

    def _system(self) -> str:
        spec = self.cfg.model(self.model_key)
        base = SYSTEM_TEMPLATE.format(
            os=f"{platform.system()} {platform.release()}",
            # 当前日期注入系统提示：REA 三/四测里模型连续把取数时间编成
            # 「2024-05-2x」——它不知道今天是几号，只好幻觉一个
            today=time.strftime("%Y-%m-%d %H:%M %A"),
            root=self.cfg.workspace_root,
            model=spec.display,
            model_desc=spec.desc,
            permission=self.permission_label,
            outside_rule=OUTSIDE_RULE[self.full_access],
            # 记录目录随任务产出目录走；没有产出目录（CLI 直连）时退到 history/_records
            write_rules=WRITE_RULES_TEMPLATE.format(
                rec=(f"{self.outdir}/_records" if self.outdir else "history/_records")),
        )
        # 产出目录规则放在「工作环境」之后：目录名由编排层按任务发起时间算好，
        # 员工只管往里写；不设置（普通闲聊等）就不占提示词字节。
        if self.outdir:
            base += OUTDIR_RULE.format(outdir=self.outdir)
        # 预热注入紧跟产出目录规则：同属「工作环境」段，开局就有项目地图。
        # 不设置（闲聊/CLI 直连）就不占提示词字节。
        if self.warmup_block:
            base += "\n" + self.warmup_block + "\n"
        if self.skills and self.skills.skills:
            base += "\n" + self.skills.catalog() + "\n"
        # 工具索引：把没下发说明的工具名字列出来（只列名字，很便宜），
        # 让模型知道能力边界，而不是"没看到就当没有这个功能"
        if getattr(self.cfg, "ptc_resident", False):
            base += "\n" + PTC_RESIDENT_BLOCK
        sel = self.registry.selector
        if sel is not None and getattr(self.cfg, "tools_lazy", True):
            idx = sel.index_text(exclude=self._active_tool_names)
            if idx:
                header = (TOOL_INDEX_HEADER_PTC
                          if getattr(self.cfg, "ptc_resident", False)
                          else TOOL_INDEX_HEADER)
                base += "\n" + header.format(names=idx)
        # 长期记忆放在最后：它是「上一批任务留下的偏好」，越靠后模型越容易当约束看。
        block = self.memory.prompt_block()
        if block:
            base += "\n" + MEMORY_HEADER + block + "\n"
        return base

    def messages_with_system(self, history: list[dict]) -> list[dict]:
        msgs = [{"role": "system", "content": self._system()}]
        if self._skill_hint:
            msgs.append({"role": "system", "content": self._skill_hint})
        # 任务笔记：每轮重新读盘注入。它是模型在压缩风暴里唯一的「随身草稿纸」——
        # 旧工具结果被压成一行后，笔记里记下的定义位置/结论仍然完整在场。
        notes = self._task_notes_block()
        if notes:
            msgs.append({"role": "system", "content": TASK_NOTES_HEADER + notes})
        return [*msgs, *history]

    # ---------- 任务笔记 ----------
    def _task_notes_path(self) -> Path:
        tid = str(current_task() or "adhoc")
        safe = re.sub(r"[^\w.-]", "_", tid)[:64] or "adhoc"
        return self.cfg.state() / "task-notes" / f"{safe}.md"

    def _task_notes_block(self) -> str:
        try:
            p = self._task_notes_path()
            if not p.is_file():
                return ""
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                return ""
            if len(text) > 2000:
                text = "…（更早的笔记已截断）\n" + text[-2000:]
            return text
        except OSError:
            return ""

    # ---------- 上下文压缩 ----------
    def _note_system(self, system) -> None:
        """系统提示变化时记一条（warmup / skill 注入 / 权限模式都会让它变）。

        入参是本轮 messages 的第 0 条（system 消息字典），也接受纯文本。
        """
        text = str((system or {}).get("content") or "") if isinstance(system, dict) else str(system or "")
        if self.journal is None or text == self._journal_system:
            return
        self._journal_system = text
        self.journal.append("system", content=text, chars=len(text))

    def _journal_compact(self, res, before: list[dict], after: list[dict]) -> dict:
        """把一次压缩记进会话日志，返回可下发的事件。

        快照单独落 JSON（before / after 两份），事件里只放路径与统计——
        这样日志体积与上下文长度解耦，而「模型当时看到什么」仍然逐字可查。
        """
        ev = {"type": "compact", "strategy": res.strategy,
              "before": res.before, "after": res.after, "note": res.note}
        if self.journal is None:
            return ev

        def _hist(ms: list[dict]) -> list[dict]:
            # 系统提示由 system 事件单独表达，快照只存历史——否则重放时
            # system 会被当成普通消息混进对话（dsh 里 system 是 Surface 的节点，
            # 不是 transcript 的一条 message）。
            return ms[1:] if ms and ms[0].get("role") == "system" else ms

        tag = f"step-{self._journal_step:03d}"
        before_path = self.journal.snapshot(f"{tag}-before", {"messages": _hist(before)})
        after_path = self.journal.snapshot(f"{tag}-after", {"messages": _hist(after)})
        self.journal.append(
            "compact", strategy=res.strategy, before=res.before, after=res.after,
            note=res.note, step=self._journal_step,
            before_snapshot=before_path, after_snapshot=after_path,
            before_messages=len(_hist(before)), after_messages=len(_hist(after)),
        )
        ev["journal"] = self.journal.dir.as_posix()
        return ev

    def _compact(self, messages: list[dict]) -> tuple[list[dict], dict | None]:
        """每轮调用前的自动瘦身，只处理历史部分，系统提示不参与。

        返回 (新消息列表, 压缩事件或 None)。压缩不再只改内存：前后快照落盘、
        事件写进会话日志，`journal.replay()` 能还原模型当轮看到的完整上下文。
        """
        system, *rest = messages
        self._note_system(system)
        if not self.cfg.compact_auto:
            return messages, None
        res = self.compressor.compact(rest, self.model_key, force=False)
        if not res.changed:
            return [system, *res.messages], None
        self.last_compact = res.note
        after = [system, *res.messages]
        return after, self._journal_compact(res, messages, after)

    def compress_now(self, history: list[dict]) -> tuple[list[dict], str]:
        """手动压缩入口（CLI 的 /compact 与 Web 界面按钮都走这里）。"""
        before = list(history)
        res = self.compressor.compact(
            history, self.model_key, force=True, allow_summarize=True
        )
        self.last_compact = res.note
        if res.changed:
            self._journal_compact(res, before, res.messages)
        return res.messages, res.note or "上下文仍很紧凑，无需压缩。"

    def context_size(self, history: list[dict]) -> int:
        return measure(history)

    # ---------- 本轮该下发哪些工具 ----------
    def _active_tools(self, messages: list[dict]) -> list[dict]:
        """按需下发工具 schema：核心 + 关键词命中 + 本轮已用过的。

        顺便把结果记进 self._active_tool_names，供系统提示里的「工具索引」排重。
        """
        sel = self.registry.selector
        if sel is None:
            schemas = self.registry.schemas()
            self._active_tool_names = {s["function"]["name"] for s in schemas}
            return schemas

        texts: list[str] = []
        for m in messages[-6:]:
            if m.get("role") not in ("user", "assistant"):
                continue
            c = m.get("content")
            if isinstance(c, str) and c:
                texts.append(c[-1500:])       # 取尾部：结论与要求通常在后面
        sticky: set[str] = set(self.registry.pinned)
        for m in messages[-12:]:
            for tc in (m.get("tool_calls") or []):
                nm = (tc.get("function") or {}).get("name") or tc.get("name")
                if nm:
                    sticky.add(nm)

        schemas = sel.select(texts, sticky=sticky)
        self._active_tool_names = {s["function"]["name"] for s in schemas}
        return schemas

    # ---------- 主循环 ----------
    def run(
        self,
        history: list[dict],
        user_text: str,
        auto_route: bool = False,  # 已停用（见 run 体内说明），仅为兼容保留
        stop_check=None,
        auto_approve: bool | None = None,
        file_log: list | None = None,
        relay: list[dict] | None = None,
        read_cache=None,
        steer_check=None,
        contract: list[str] | None = None,
        session_id: str = "",
        ledger=None,
    ) -> Iterator[dict[str, Any]]:
        """跑一轮完整任务。

        stop_check 是个无参可调用对象，返回 True 表示用户请求停止。每个检查点
        （每轮推理开始前、每次调工具前）问一次，命中就正常收尾退出。

        auto_approve  True = 完全访问权限（写文件与命令直接放行，工作区之外的
                      路径也允许读写）；False/None = 走 Guard 的审批逻辑，此时若
                      Guard.auto_approve_all 已打开（CLI 的 --yes / /yes），效果等同 True
        每轮运行单独传入而不是改 Guard 的全局开关——同一个 Agent 实例可能被
        多个请求共用，改全局状态会串味。

        file_log 传进来时，写类工具动过的文件会被追加进去，供任务台账记录产出。

        relay 给定时，用它**替代**全量历史作为发给模型的上下文（多步任务的接力
        省法：只带计划 + 摘要 + 文件路径，而不是把前面每一步的工具输出全带上）。
        注意它只影响"模型看到什么"：yield 出去的 history 仍是全量，界面回看与
        任务台账不受影响。

        read_cache 是编排层传进来的整任务共享只读缓存（agentforge.readcache.
        ReadCache，跨并行泳道/子任务共用）：同参数只读调用第二次起直接回放缓存
        结果并明说。不传就退回本 run 私有的 call_cache（行为同旧版）。任何
        非只读工具执行后缓存全量作废，防止「读了→改了→再读」拿到旧内容。

        ledger 传进来时沿用这份证据台账（同一任务的修正轮共用，读过的文件不会
        因为换了轮次就被判「从未读过」）；不传则新建并在本次 run 内独占。
        """
        stopped = stop_check or (lambda: False)
        # 小步骤兜底标记：缺口台账记录时要说明「当时在做哪一步」。编排层会为
        # 每个子任务/多路径方案标更细的值（orchestrator set_step），这里只在
        # 没人标记时（CLI 直跑 / 单发对话）用任务原文兜底，不覆盖已有标记。
        gaps.ensure_step(user_text)
        # 证据台账：本任务「实际核实过哪些文件」的唯一事实源（架构层记录，供写报告时
        # 校验引用）。每次 run 重置，避免跨任务串味。
        #
        # rerun27 事故：修正轮也是「一次 run」，重置把前面读过的五十来个文件全忘了。
        # 于是第二轮写交付物时，引用核查说「这个文件本任务从未 read_file 过」、
        # 写前门槛说「一次 read_file 都没有」——模型把同一批文件又读一遍，第三轮
        # 重置再忘一次，永远过不去，最后零交付。取证发生在任务里，不在某一轮里：
        # 调用方（编排层修正轮）显式传 ledger，同一任务共用一份；不传才新建。
        if ledger is not None:
            self.guard.ledger = ledger
        else:
            # 新建台账也带上任务记录器：构造时从 evidence.jsonl 回填「读过什么、
            # 读到哪些行」。于是不需要任何人在各轮之间传对象——换了 run、换了
            # 步骤、甚至进程重启，证据都还在文件里（文件层级保证，而不是靠
            # 调用方记得传参）。
            self.guard.ledger = EvidenceLedger(recorder=getattr(self, "recorder", None))
        # ops_batch（PTC）脚本内的写操作要记入本任务台账；agent 实例本身在
        # 工具执行点临时挂到 OPS_AGENT_CTX（见主循环 _execute 调用处）。
        self._file_log = file_log
        # 本轮开始墙钟：契约收工对账用「mtime 是否晚于本轮开始」判定交付物
        # 是否本轮真的更新过（rerun15 盲点：上轮留下的旧文件糊弄「存在即完成」）。
        self._t0 = time.time()
        # 任务内即时验证的失败回灌计数（0-3）：每次 run 归零，跨轮次保留，
        # 防「改挂→提醒→再改挂」死循环；到顶后不再回灌，终审交给收尾闸门。
        self.iv_fails = 0
        # 完全访问 = 本次调用显式传入 auto_approve=True，或 Guard 的全局自动批准
        # 已打开（CLI 的 --yes / /yes 走这条路）。两条通道必须给同一个答案，
        # 否则会出现「命令不用确认了、路径还是被拦」的分裂状态。
        full = auto_approve is True or bool(getattr(self.guard, "auto_approve_all", False))
        self.permission_label = "完全访问权限" if full else "默认权限"
        self.full_access = full

        # 写入放行名单：任务书（user_text）里点名的带目录层的相对路径。
        # 这些路径不受「强制落产出目录」重定向——那是「必须放特定位置才能
        # 生效」的例外通道（如任务书点名要交付到 evals/ 的报告）。只认带
        # 目录层的路径（含 ./ 前缀）：裸文件名一律进产出目录，那正是根目录
        # 违规写入的形态。绝对路径不走名单，本身就不受重定向。
        allow: set[str] = set()
        for m in re.finditer(r"(?:\./|(?:[\w.\-]+/)+)[\w.\-]+\.[A-Za-z0-9]{1,8}", user_text or ""):
            allow.add(m.group(0).replace("\\", "/").lstrip("./"))
        self._write_allow = frozenset(allow)

        # 交付物契约（做之前就锁定「做对」的标准，而不是做完再核实返工）：
        # 任务书点名、开工时**还不存在**的带目录层文件 = 本轮必须写出来的交付。
        # 已存在的路径是要读的材料，不该被要求重写。契约在两处生效：
        #   ① 模型想收工时（不再调工具、准备给出最终答复）程序先对契约——缺了
        #      就拦截（见下方 not result.tool_calls 分支），把缺口当观察塞回去
        #      继续跑，同一回合内自愈；此时分析内容都在模型上下文里，写文件
        #      只是落盘，不用重新分析。Claude Code Stop hook 同款思想。
        #   ② 收尾闸门（orchestrator _verify_gate）仍是最后防线，但从「主要
        #      修正机制」降级为「兜底」——此前 10 次核查拦截全发生在收尾，
        #      每次都要整轮返工，rerun14 实测就是这个形态。
        # 空列表也必须回落兜底：orchestrator 传的是 contract_paths(t.goal)，而
        # 步骤级任务书（skill/子上下文改写过的 goal）常常不含交付物路径，于是
        # 传进来的是 []。旧写法 `if contract is None` 把 [] 当成「编排层已给」，
        # 兜底提取被跳过，契约恒为空 —— rerun23 因此整套契约闸门空转：
        # 交付物没落盘、模型谎报 7 次「已写入」，contract_gate 事件 0 次。
        if not contract:
            # 编排层没显式给（CLI 直跑 / 单发对话 / 空 step_contract）：从任务文本
            # 兜底提取。判定逻辑（写入动词 + 对冲词排除）在 tasksession 单一实现。
            contract = (contract_paths(user_text or "", self.cfg.workspace_root)
                        if getattr(self.cfg, "contract_gate", True) else [])
        else:
            contract = list(contract)
        self._contract = contract

        # auto_route 已停用：旧的信号词粗分（classify）选的是不存在的语义 key，
        # 实际永远落到 fallback，属于死逻辑；真正的路由在编排层
        # （Router.capability 的后果定档 + 调度模型指派）。参数保留只为兼容旧调用。
        del auto_route  # noqa: B018 — 显式标记未使用

        history = list(history)
        this_turn = {"role": "user", "content": user_text}
        history.append(this_turn)
        # ctx = 真正发给模型的消息序列。没给 relay 时直接复用 history 这个列表
        # （同一个对象，后续 append 两边都生效）；给了 relay 就分道走。
        ctx = history if relay is None else [*relay, this_turn]

        # 会话事件日志（dsh「模型可见即已记录」）：本次 run 的每一条模型可见消息、
        # 每次系统提示变化、每次压缩前后的完整快照都落盘。此前压缩只改内存
        # messages，日志里没有痕迹，触发压缩后无法回答「模型看到的是原文还是摘要」。
        # 记账是旁路：开不出文件就退化成 None，绝不影响任务本身。
        self.journal = Journal.open(self.cfg, session_id or new_sid())
        self._journal_system = ""
        self._journal_step = 0
        if self.journal is not None:
            self.journal.append(
                "run_start", model=self.model_key, model_name=self.model_name,
                user_text=user_text, history_len=len(history),
                relay=bool(relay), contract=list(contract or ()),
            )

        def _push(msg: dict) -> None:
            history.append(msg)
            if ctx is not history:
                ctx.append(msg)
            if self.journal is not None:
                self.journal.message(msg, source=_msg_source(msg))

        def _pop() -> None:
            if history:
                history.pop()
            if ctx is not history and ctx:
                ctx.pop()

        yield {"type": "user", "content": user_text}

        # Skill 自动激活：命中就把正文摘要注入本轮上下文（Tier 2）
        self._skill_hint = ""
        if self.skills and self.cfg.skill_auto_load:
            hint = self.skills.auto_hint(
                user_text, self.cfg.skill_min_score,
                getattr(self.cfg, "skill_auto_max_chars", 1600),
            )
            if hint:
                hits = self.skills.match(user_text, 1)
                self._skill_hint = hint
                nm = hits[0][0].name if hits else ""
                yield {
                    "type": "skill",
                    "name": nm,
                    "detail": (f"已加载 skill 摘要：{nm}"
                               f"（正文过长时只注入开头，需要全文用 use_skill 取）") if nm else "",
                }

        spec = self.cfg.model(self.model_key)

        # 连续「空轮次」计数：见下面遇到空回复时的处理
        empty_turns = 0
        # 契约拦截计数（0-2）：同一回合最多拦两次，拦满放行交给收尾闸门兜底，
        # 防「模型就是不写」的死循环。
        contract_blocks = 0
        # 契约对账辅助：路径是否「未交付」——不存在，或自本轮开始从未更新。
        def _owe(p: str) -> bool:
            f = Path(self.cfg.workspace_root) / p
            try:
                if not f.exists():
                    return True
                # 骨架先行后，「文件存在」不再等于「已交付」：write_skeleton 会
                # 真的落盘并刷新 mtime，但里面只有标题。空着小节就当没写。
                if skeleton.unfinished_file(self.cfg.workspace_root, p):
                    return True
                return f.stat().st_mtime <= self._t0
            except OSError:
                return True
        # 任务内只读调用缓存：同样的（工具+参数）第二次出现时直接回放上次结果。
        # 小模型忘了自己查过什么时会用一模一样的参数再查一遍（实测 40+ 轮任务里
        # 同一个 search_code pattern 出现 3 次），重新执行毫无新信息，纯烧 GPU。
        # 编排层传进 read_cache 时用共享版（跨泳道/子任务），否则用这份私有的。
        call_cache: dict[str, str] = {}
        # 确定性澄清触发：本场 run 收集到的检索信号（未命中/多候选），
        # 每场至多发起一次弹窗（见模块头「确定性澄清触发」）
        amb_signals: list[dict] = []
        asked_once = False
        # 轮次预算：软预算起每轮催收敛；硬上限撤掉工具强制交卷。
        # hard 相对 max_steps 留 3 轮余量，保证「强制交卷」自己有轮次可用。
        soft = max(0, int(getattr(self.cfg, "rounds_soft", 30)))
        hard = max(soft + 1, min(int(getattr(self.cfg, "rounds_hard", 60)),
                                 self.cfg.max_steps - 3))

        for step in range(1, self.cfg.max_steps + 1):
            if stopped():
                # 把已经累积的上下文一起带出去，调用方可以据此保存/回看
                yield {"type": "stopped", "step": step, "reason": "用户停止",
                       "history": history}
                return
            # 中途补充（Steering，DeepSeek Harness 语义）：用户在任务运行中
            # 插的话，在「下一个 Step」（= 本轮模型决策）立刻采纳并调整方向。
            # 以用户消息推入上下文，模型把它当最新指令对待。
            for st_text in (steer_check() if steer_check else ()):
                if st_text:
                    _push({"role": "user",
                           "content": f"（用户中途补充，立即采纳并调整方向）{st_text}"})
                    yield {"type": "steered", "text": st_text}
            yield {"type": "step", "step": step, "model_key": self.model_key, "model": spec.display}
            self._journal_step = step
            # 每轮重算：这轮刚用过的工具、刚 find_tools 取过的工具，下一轮要常驻下发
            tools = self._active_tools(ctx)
            messages, _compact_ev = self._compact(self.messages_with_system(ctx))
            if _compact_ev:
                yield _compact_ev
            # 语言提醒以 system 贴在最后（本地保留注意力优势；在线由
            # providers._convert_messages 上提合并，不会触发「system 必须在开头」的 400）
            messages.append(dict(LANG_REMINDER))
            # 轮次预算：软提醒贴在语言提醒前面，同样借「离生成最近」的注意力位置
            if soft and step >= soft:
                if step >= hard:
                    tools = []                      # 硬收敛：不再提供任何工具
                    messages.append({"role": "system", "content": (
                        "已达到轮次上限，从本轮起不再提供任何工具。请立刻基于已掌握的"
                        "信息输出最终结果；没能完成的部分如实说明缺什么，不要编造。")})
                else:
                    messages.append({"role": "system", "content": (
                        f"任务预算提醒：本步已进行 {step} 轮推理。已掌握的信息大概率足够，"
                        "请尽快汇总产出（写文件或给出最终答复）；只有确有必要时才再发起新的探索。")})

            result = None
            _t_llm0 = time.monotonic()
            try:
                for kind, payload in self.hub.chat_stream(spec, messages, tools,
                                                          stop_check=stopped):
                    if kind == "delta":
                        yield {"type": "delta", "text": payload}
                    elif kind == "thinking":
                        yield {"type": "thinking", "text": payload}
                    elif kind == "result":
                        result = payload
            except GenerationStopped:
                # 用户在生成中途点了停止：此时 assistant 消息还没 push 进历史，
                # 半截内容直接丢弃，按「用户停止」收尾，不等下一个检查点。
                yield {"type": "stopped", "step": step,
                       "reason": "用户停止（生成中断）", "history": history}
                return
            except Exception as e:  # noqa: BLE001
                yield {"type": "error", "message": f"模型调用失败：{e}"}
                return

            if result is None:
                yield {"type": "error", "message": "模型未返回任何内容"}
                return

            # 计时埋点：本轮 LLM 回复的真实 wall-clock（含 API 往返），以及 Ollama
            # 自报的纯推理耗时（eval_duration，排除排队/网络）。两者一起给，才能
            # 区分「流程慢」还是「模型生成慢」。
            _llm_ms = (time.monotonic() - _t_llm0) * 1000.0
            _eval_ms = getattr(result, "eval_duration_ns", 0) / 1e6
            # 首轮慢的两个来源单独报：模型冷加载（load）与上下文预填充（prompt_eval）。
            _load_ms = getattr(result, "load_duration_ns", 0) / 1e6
            _peval_ms = getattr(result, "prompt_eval_duration_ns", 0) / 1e6

            # stats 里把用量与费用一并带上：界面靠它实时累加「已消耗预估费用」
            yield {
                "type": "stats",
                "tps": result.tokens_per_second,
                "llm_ms": round(_llm_ms, 1),
                "eval_ms": round(_eval_ms, 1),
                "load_ms": round(_load_ms, 1),
                "prompt_eval_ms": round(_peval_ms, 1),
                "tokens": result.eval_count,
                "prompt_tokens": result.prompt_eval_count,
                "model_key": self.model_key,
                "model": spec.display,
                "kind": spec.kind,
                "cost": cost_of(
                    result.prompt_eval_count, result.eval_count,
                    spec.provider, spec.remote_model, spec.kind,
                ),
                "free": is_free(spec.provider, spec.kind),
            }

            # 部分模型（实测 qwen2.5-coder:14b）不走 tool_calls 字段，而是把调用
            # 写成正文 JSON。这里按已知工具名白名单把它抢救回来，否则 Agent 会把
            # 这段 JSON 当成最终答案念给用户，工具永远不执行。
            if not result.tool_calls and result.content:
                rescued = parse_text_tool_calls(result.content, self.registry.names())
                if rescued:
                    result.tool_calls = rescued
                    result.content = ""

            # 空轮次：既没有工具调用、也没有正文。思考模型（gemma / GLM 等）
            # 有时会把输出预算全花在 reasoning 上，最后正文和工具调用都为空。
            # 这种情况下**绝不能当成「任务完成」**——否则界面显示成功、实际一个
            # 字节都没产出（实测：小说任务读完素材后空回复，outline.md 没被写）。
            # 给有限次重来的机会，并明确点一句「该动手了」。
            if not result.tool_calls and not (result.content or "").strip():
                empty_turns += 1
                if empty_turns > 2:
                    yield {"type": "error",
                           "message": (f"模型连续 {empty_turns} 轮没有输出任何内容"
                                       f"（既无工具调用也无正文），任务未完成。")}
                    return
                _push({"role": "user",
                       "content": ("你上一轮没有输出任何内容。请继续完成当前任务："
                                   "素材已经读完，就直接调用 write_file 把结果写出来；"
                                   "否则给出最终的完整答复。不要只思考不产出。")})
                continue
            empty_turns = 0

            # DeepSeek 等「思考模式」的接口要求把上一轮的 reasoning_content 原样回传，
            # 否则下一轮请求直接 400（"reasoning_content ... must be passed back"）。
            # Ollama 不需要，也就不往历史里塞，免得白占上下文。
            replay_think = (
                {"reasoning_content": result.thinking}
                if result.thinking and getattr(spec, "provider", "ollama") != "ollama"
                else {}
            )

            if not result.tool_calls:
                # 停止拦截：模型宣布收工，程序先对交付物契约。缺了就不放行——
                # 不 yield assistant（那张「已完成」的嘴替用户看不到），把缺口
                # 写回上下文让它继续。分析内容此刻都在上下文里，补写文件只是
                # 落盘，成本远低于收尾闸门的整轮返工。
                if (contract and contract_blocks < 2 and not stopped()):
                    missing = [p for p in contract if _owe(p)]
                    if missing:
                        contract_blocks += 1
                        # 措辞必须与工具面一致：PTC 常驻下顶层 write_file 会被守卫
                        # 拒绝，旧文案却写「直接调用 write_file」——模型照做被拒、
                        # 再被拦一次，白烧一轮（rerun24/25 各出现一次）。
                        _hint = ("在 ops_batch 脚本里用 call('write_file', path=..., "
                                 "content=...) 写入"
                                 if getattr(self.cfg, "ptc_resident", False)
                                 else "直接调用 write_file 把它们写成文件")
                        _fill = ("call('fill_section', path=..., heading=..., content=...)"
                                 if getattr(self.cfg, "ptc_resident", False)
                                 else "fill_section(path=..., heading=..., content=...)")
                        _push({"role": "assistant",
                               "content": result.content or "", **replay_think})
                        _push({"role": "user", "content": (
                            "【停止拦截·交付物未落盘】你宣布任务完成，但程序核对磁盘发现"
                            "任务书要求的交付物还不存在、自本轮开始后从未更新，"
                            "或者只有骨架而正文是空的：\n- "
                            + "\n- ".join(missing)
                            + f"\n\n你刚才的分析内容都在上文——现在{_hint}"
                              "（内容不必重新分析，路径必须一字不差；已存在的"
                              "要重新写出，让修改时间晚于本轮开始）。"
                              f"文件里已经有骨架的，用 {_fill} 把空着的小节逐节填完，"
                              "别整篇重写。"
                              "写完后给出最终答复。若你认为该文件不该由本轮写出，"
                              "请在最终答复里明确说明原因。")})
                        yield {"type": "contract_gate", "missing": missing,
                               "round": contract_blocks}
                        continue
                assistant_msg = {"role": "assistant", "content": result.content, **replay_think}
                _push(assistant_msg)
                yield {"type": "assistant", "content": result.content, "history": history}

                # 确定性澄清触发：本场发生过「检索未命中 / 多候选」，模型的最终
                # 回复又在向用户追问 → 不指望它自觉调 ask_user，harness 亲自处理。
                # 顺序：先查长期记忆（确定性架构步，见模块头），记忆里也没有才弹窗。
                if (not asked_once and amb_signals
                        and _has_clarify_intent(result.content or "")):
                    sig = amb_signals[-1]
                    m = _CLARIFY_RX.search(result.content or "")

                    # 第一步：本地没找到，先翻记忆条目（调记忆数据，不靠模型自觉）。
                    # 检索源=信号 query + 模型追问原话——list_dir 的 query 只是
                    # 路径（如 r1 场景），缺的实体名（REA）在追问句里
                    mem_lines = []
                    if sig["kind"] == "empty":
                        mem_src = sig["query"] + "\n" + (m.group(0) if m else "")
                        mem_lines = _memory_hits(self.memory, mem_src)
                    if mem_lines:
                        amb_signals.pop()          # 信号已被记忆消化
                        _push({"role": "user", "content": (
                            "（系统侧自动检索记忆）任务需要的信息在本地没有检索到，"
                            "但长期记忆里有这些相关记录：\n- "
                            + "\n- ".join(mem_lines)
                            + "\n请基于记忆里的信息继续完成任务；"
                              "若与任务无关，如实说明并按最常见的情况假设。")})
                        continue          # 模型拿着记忆线索进入下一轮

                    # 第二步：记忆里也没有 → 发起澄清弹窗
                    asked_once = True
                    if sig["kind"] == "multi" and sig["candidates"]:
                        question = (m.group(0).strip() if m else
                                    f"检索到多个可能的结果，您指的是哪一个？")
                        options = [*sig["candidates"], "其他"]
                    else:
                        question = (m.group(0).strip() if m else
                                    (f"「{sig['query']}」没有检索到，您指的是什么？"
                                     if sig["query"] else
                                     "任务需要的关键信息没有检索到，请补充说明？"))
                        options = list(_EMPTY_OPTIONS)
                    question = question[:200]
                    # 24 字上限：中文选项 12 字足够，但多候选取的是 repo 名
                    #（如 react/react-native 18 字符），截到 12 会把名字切坏
                    options = [str(o)[:24] for o in options if str(o).strip()][:4]
                    yield {"type": "tool_start", "name": "ask_user",
                           "args": {"question": question, "options": options}}
                    from . import interact as _amb_interact
                    answer = _amb_interact.ask(question, options)
                    yield {"type": "tool_result", "name": "ask_user", "output": answer}

                    # 第三步（仅信息补充型弹窗）：问用户要不要把这条信息存入记忆。
                    # 确定性流程的一部分，不指望模型记得问；选是/否都有绿色提示。
                    if sig["kind"] == "empty":
                        confirm_q = f"是否将当前信息（{answer[:40]}）存入记忆？"
                        yield {"type": "tool_start", "name": "ask_user",
                               "args": {"question": confirm_q, "options": ["是", "否"]}}
                        confirm = _amb_interact.ask(confirm_q, ["是", "否"])
                        yield {"type": "tool_result", "name": "ask_user",
                               "output": confirm}
                        if confirm.strip().startswith(("是", "y", "Y")):
                            note = f"用户澄清：{question[:60]} → {answer[:80]}"
                            try:
                                self.memory.append(note)
                                yield {"type": "notify", "kind": "ok",
                                       "reload": "memory",
                                       "text": f"已存入记忆：{note[:60]}"}
                            except OSError as mem_err:
                                yield {"type": "notify", "kind": "bad",
                                       "text": f"存入记忆失败：{mem_err}"}
                        else:
                            yield {"type": "notify", "kind": "ok",
                                   "reload": "memory",
                                   "text": "已按你的选择跳过存入记忆"}

                    _push({"role": "user",
                           "content": (f"（澄清弹窗答复）你刚才追问「{question}」，"
                                       f"用户的答复：{answer}。请按答复继续完成任务。")})
                    continue          # 模型拿着答复进入下一轮，任务未结束

                return

            # 带工具调用的 assistant 消息
            tc_payload = [
                {"function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in result.tool_calls
            ]
            _push({"role": "assistant", "content": result.content or "",
                   "tool_calls": tc_payload, **replay_think})

            for tc in result.tool_calls:
                if stopped():
                    # 这次工具没执行，把已追加的 assistant(tool_calls) 摘掉：
                    # 否则历史里会留下一个没有 tool 应答的悬空调用，下次续聊会报错
                    if history and history[-1].get("tool_calls"):
                        _pop()
                    yield {"type": "stopped", "step": step,
                           "reason": "用户停止（工具尚未执行）", "history": history}
                    return
                # PTC 常驻：只允许「脚本面」工具（ops_batch / ask_user / find_tools）
                # 直接调用，其余一律改走 ops_batch 写脚本——这是把 PTC 常驻真正落地的
                # 硬闸，否则模型仍会绕过系统提示直接发工具调用。
                if getattr(self.cfg, "ptc_resident", False) and tc.name not in PTC_SURFACE:
                    msg = (f"[PTC 常驻] 直接调用 {tc.name} 已被禁用：所有工具必须通过 "
                           f"ops_batch 写脚本调用（脚本内 tools.call('{tc.name}', ...)）。"
                           "请把你要做的工具操作写进一段 Python 脚本用 ops_batch 执行。")
                    yield {"type": "tool_result", "name": tc.name,
                           "output": msg, "failed": True}
                    _push({"role": "tool", "content": msg})
                    continue

                yield {"type": "tool_start", "name": tc.name, "args": tc.arguments}
                # 重复调用拦截：只读工具的（名字+参数）指纹在任务内出现过，
                # 就直接回放上次结果并明说，不再执行。打断「忘了→重查→再忘」的循环。
                # 只缓存 SAFE 只读调用；任何非只读工具执行后缓存全量作废
                # （文件系统可能变了），之后的重复读取必须真执行。
                tool_def = self.registry.get(tc.name)
                sig = tc.name + "|" + json.dumps(
                    tc.arguments or {}, ensure_ascii=False, sort_keys=True, default=str,
                )
                # ops_batch 是脚本执行器，副作用取决于脚本内容（可能写文件），绝不能按
                # 只读缓存/去重——否则模型重试写脚本会被「重复调用」拦截，且首写失败时
                # 也没机会重来。只读去重只对真正无副作用的工具生效。
                safe = (tool_def is not None and tool_def.risk == SAFE
                        and tc.name != "ops_batch")
                hit = None
                _tool_ms = 0.0
                if safe:
                    if read_cache is not None:
                        hit = read_cache.get(sig)
                    elif sig in call_cache:
                        hit = (call_cache[sig], 1)
                if hit is not None:
                    n = hit[1]
                    times = f"（第 {n} 次重复）" if n > 1 else ""
                    out = (f"[重复调用]{times} 这个只读调用本任务里已经执行过，"
                           "下面是上次的结果，请直接利用；确实需要新信息就换参数，"
                           "或把关键发现记进 task_notes：\n" + hit[0])
                else:
                    # ops_batch（PTC）在本工具 run() 里需要找到当前 agent——
                    # 只在工具执行这一小段挂上下文，执行完立刻摘除
                    _ops_t = OPS_AGENT_CTX.set(self)
                    _t_tool0 = time.monotonic()
                    try:
                        out = self._execute(tc.name, tc.arguments, auto_approve, file_log)
                    finally:
                        OPS_AGENT_CTX.reset(_ops_t)
                    _tool_ms = (time.monotonic() - _t_tool0) * 1000.0
                    if safe:
                        if read_cache is not None:
                            read_cache.put(sig, out)
                        else:
                            call_cache[sig] = out
                    else:
                        # 写类/命令类工具可能改了文件系统：只读缓存整体作废
                        if read_cache is not None:
                            read_cache.invalidate_all()
                        else:
                            call_cache.clear()

                # 检索信号登记（确定性，见模块头）：未命中 / 多候选都记下，
                # 供模型收尾追问时由 harness 亲自发起澄清弹窗
                sig = _lookup_signal(tc.name, out)
                if sig is not None:
                    amb_signals.append(sig)

                # 失败结果强呈现：错误/拒绝不能混在普通输出里被无视。
                # [错误] 附上两条禁令（不许无视失败继续、不许未验证声称完成）；
                # 连续失败第 2 次起升级为「换方法或如实说明」的硬提醒。
                # [已拒绝] 用户已明确说不要，注脚换成「别重试」，避免鼓励硬刚。
                failed = out.startswith("[错误]")
                denied = out.startswith("[已拒绝]")
                if failed:
                    self.fail_streak += 1
                    # 工具执行失败记入能力缺口台账：按 (工具名) 合并计数，反复失败
                    # 的同款工具会自然聚成一条、count 累加——这就是「哪些工具反复失败」
                    # 的权威来源，供后续决定要不要换工具/补技能。
                    gaps.record("tool_failed", tc.name, out[:200], self.cfg)
                    out = out + FAIL_NOTE
                    if self.fail_streak >= 2:
                        out += FAIL_REPEAT_NOTE.format(n=self.fail_streak)
                else:
                    if not denied:
                        self.fail_streak = 0
                    # 任务内即时验证（Aider --auto-test 同款）：写类工具成功落盘后，
                    # 若任务书声明了验证命令（含「运行 X 确认输出 N」式），程序亲自
                    # 复跑并比对期望输出；失败把真实输出回灌给模型当场修——错不拖到
                    # 收尾才暴露。最多回灌 3 次（防死循环），终审仍归收尾闸门。
                    if tc.name in FILE_TOOLS and self.iv_fails < 3:
                        note, _ = verify_gate.inline_verify_note(
                            user_text, self.cfg.workspace_root)
                        if note:
                            self.iv_fails += 1
                            out = out + "\n\n" + note
                yield {"type": "tool_result", "name": tc.name, "output": out,
                       "dur_ms": round(_tool_ms, 1),
                       **({"failed": True} if (failed or denied) else {})}
                _push({"role": "tool", "content": out})

                # A 方案：契约闸门下沉到 PTC 脚本边界。PTC 常驻下模型用 ops_batch
                # （带 tool_calls）反复写-失败死循环，永不到「无 tool_calls 收工」，
                # 使 800 行的收工拦截整轮碰不到（rerun20：22 轮死循环、0 次闸门）。
                # 这里在脚本执行后立即对账：脚本确实调了写工具、但交付物仍 _owe，
                # 就触发闸门回灌，终止无效重试。
                if tc.name == "ops_batch":
                    wr = getattr(self, "_ops_write_report", None)
                    # 诊断：脚本确实调了写工具、闸门却没机会出手时，把原因记进
                    # 事件流。rerun24 出现过「3 次写盘失败 + 权威块正确 + 闸门 0 次」
                    # 的静默，靠这个字段能一次定位是契约空、还是 stopped() 已触发。
                    if wr and not (contract and contract_blocks < 2 and not stopped()):
                        yield {"type": "gate_skip",
                               "reason": ("contract_empty" if not contract
                                          else "gate_exhausted" if contract_blocks >= 2
                                          else "stopped"),
                               "contract": list(contract or []),
                               "self_contract": list(getattr(self, "_contract", None) or []),
                               "writes": len(wr)}
                if (tc.name == "ops_batch"
                        and contract and contract_blocks < 2 and not stopped()):
                    wr = getattr(self, "_ops_write_report", None)
                    if wr:
                        missing = [p for p in contract if _owe(p)]
                        if missing:
                            contract_blocks += 1
                            _push({"role": "assistant", "content": result.content or ""})
                            _push({"role": "user", "content": (
                                "【停止拦截·交付物未落盘】你刚才在 ops_batch 脚本里调用了写文件工具，"
                                "但程序核对磁盘发现任务书要求的交付物仍不存在或本轮从未更新：\n- "
                                + "\n- ".join(missing)
                                + "\n\n常见原因：引用核查拒绝了编造的文件路径/行号"
                                  "（例如带「推测」「大概」字样、或你并未真正读到的行号）。"
                                  "请在脚本里删除无法复现的引用、只保留你真正读到的文件与行号，"
                                  "然后用 call('write_file', path=..., content=...) 重新写入同一份交付物。"
                                  "若文件里已经有骨架、只是有空着的小节，"
                                  "用 call('fill_section', path=..., heading=..., content=...) 逐节填完即可，"
                                  "不必整篇重写。"
                                  "写完后给出最终答复。若你确认该文件不该由本轮写出，"
                                  "请在最终答复里明确说明原因，不要声称已写入。")})
                            yield {"type": "contract_gate", "missing": missing,
                                   "round": contract_blocks, "via": "ops_batch"}
                            break

        yield {"type": "error", "message": f"已达到最大步数 {self.cfg.max_steps}，任务未收敛，请拆小任务或提高上限。"}

    # ---------- 工具执行 ----------
    def _execute(
        self,
        name: str,
        args: dict,
        auto_approve: bool | None = None,
        file_log: list | None = None,
    ) -> str:
        tool = self.registry.get(name)
        if tool is None:
            # 名字规范化：模型可能用 git.clone / git-clone 这类分隔符变体（注册表是
            # git_clone）。先把 . - 空格归一成 _ 再查一次，命中就顺正确名字走下去——
            # 否则会误判成「工具缺失」污染能力缺口台账（实测 git.clone 就因此被记成缺口）。
            norm = re.sub(r"[\.\-\s]+", "_", str(name or "")).strip("_").lower()
            if norm and norm != name:
                tool = self.registry.get(norm)
                if tool is not None:
                    name = norm
        if tool is None:
            # 按需下发模式下，模型可能凭印象报了个名字（例如把 git_push 叫成 push）。
            # 这里替它找一次：名字沾边的直接把用法递过去并粘住，下一轮就带完整说明了。
            sel = self.registry.selector
            if sel is not None:
                probe = name.lower()
                hits = [n for n in self.registry.names()
                        if probe == n or probe in n or n in probe or
                        (probe.split("_")[0] and probe.split("_")[0] in n)]
                if hits:
                    self.registry.pinned.update(hits[:3])
                    return (f"[工具名不符] 没有名为 {name} 的工具，你要用的可能是下面这个，"
                            f"请用正确的名字重新调用：\n" + sel.describe_many(hits[:3]))
            # 名字格式非法（任务原文泄漏 / 乱码，如 "this??...?"）：不是能力缺口，
            # 不该污染台账——直接退回格式错误，让模型用正确通道重试。
            if _is_malformed_tool_name(name):
                return ("[错误] 工具调用名格式非法（应为小写字母/数字/下划线组合，如 read_file），"
                        "请检查是否把任务描述或正文误当作工具名发出了。当前可用工具见工具索引。")
            # 模型认为该有这个工具却没有：架构层确定的能力缺口，记台账
            # （「工具名不符」不算——那说明工具在，只是叫法错了）。
            gaps.record("tool_missing", name,
                        f"模型调用了不存在的工具，可用：{', '.join(self.registry.names())[:120]}",
                        self.cfg)
            return f"[错误] 未知工具 {name}。可用工具：{', '.join(self.registry.names())}"

        if (
            auto_approve is not True
            and self.guard.needs_approval(name)
            and not self.guard.approve(name, args)
        ):
            return f"[已拒绝] 用户拒绝了 {name} 的执行。请不要重试，改换方案或向用户说明。"

        # 多 agent 并行时可能有两个任务恰好改同一个文件：这里给该文件的写入排队
        # （见 permissions.claim_write）。只对写类工具生效，不同文件互不阻塞；
        # key 用「绝对路径归一化」而不是 guard.resolve，免得受越界权限影响。
        claim_key = ""
        if name in FILE_TOOLS:
            raw = str((args or {}).get("path") or "")
            if raw:
                p = Path(raw)
                if not p.is_absolute():
                    p = Path(self.cfg.workspace_root) / p
                claim_key = os.path.normcase(os.path.abspath(str(p)))

        # 完全访问权限的任务里，这一次工具调用允许读写工作区之外的路径。
        # 判据与 run() 里的 full 保持一致：显式 auto_approve，或 Guard 全局自动批准。
        # 只放开「读写」；命令黑名单不随之放开（见 Guard.check_command）。
        token = OUTSIDE_OK.set(auto_approve is True or bool(getattr(self.guard, "auto_approve_all", False)))
        # 产出目录 + 放行名单随调用注入：写类工具据此把相对路径强制落进产出目录
        ot = OUTDIR_CTX.set(self.outdir or None)
        wt = WRITE_ALLOW.set(getattr(self, "_write_allow", frozenset()))
        # 任务记录器随调用注入：写类工具在真正落盘后，会把这一版全文快照进
        # history/<task>/versions/。覆盖因此永不丢内容（每次写都是一个新文件），
        # 整节替换、整篇重写都只是「多了一个版本」，而不是不可逆的破坏。
        # 脚本线程里执行的内层调用也会经过这里，所以 PTC 路径自动覆盖。
        rec_token = None
        _rec = getattr(getattr(self.guard, "ledger", None), "recorder", None)
        if _rec is not None:
            rec_token = RECORDER_CTX.set(_rec)
        try:
            with claim_write(claim_key):
                out = str(tool.run(**(args or {})))
        except PermissionDenied as e:
            return f"[已拒绝] {e}"
        except TypeError as e:
            msg = f"[错误] 工具 {name} 参数不对：{e}"
            # 直接把该工具接受的参数名列出来：rerun22 里模型用 query= 调 search_code，
            # 报错只说「unexpected keyword argument」，它又猜了两轮。参数名回传后
            # 一轮就能改对——这是「一次做对」比「事后返工」便宜的典型。
            try:
                params = tool.parameters or {}
                props = list((params.get("properties") or {}).keys())
                if props:
                    req = set(params.get("required") or [])
                    msg += "\n[参数] " + name + " 接受：" + "、".join(
                        f"{k}（必填）" if k in req else k for k in props)
            except Exception:  # noqa: BLE001
                pass
            if name == "write_file" and "path" in str(e):
                msg += ("\n[提示] write_file 需要两个参数都要给：path（写入路径，"
                        "如 evals/foo.json）+ content（完整内容）。你这次漏了 path。")
            return msg
        except Exception as e:  # noqa: BLE001
            return f"[错误] 工具 {name} 执行异常：{type(e).__name__}: {e}"
        finally:
            OUTSIDE_OK.reset(token)
            OUTDIR_CTX.reset(ot)
            WRITE_ALLOW.reset(wt)
            if rec_token is not None:
                RECORDER_CTX.reset(rec_token)

        # 证据台账：成功读取 / 检索 / 抓取到的路径记进来（架构层，供写报告校验引用）。
        # 只记成功结果（以 [错误] / [已拒绝] 开头视为失败，不记）。
        ledger = getattr(self.guard, "ledger", None)
        if ledger is not None and not out.startswith(("[错误]", "[已拒绝]")):
            self._record_evidence(name, args or {}, out, ledger)

        # 记下这次调用改动的文件（任务台账据此展示「任务产出」，并在清空时可选删除）
        if file_log is not None:
            rec = classify_tool_write(name, args or {}, out)
            if rec:
                file_log.append(rec)
        return out

    def _record_evidence(self, name: str, args: dict, out: str, ledger: EvidenceLedger) -> None:
        """把一次成功工具调用里出现的真实路径记入证据台账。

        读取类（read_file）记成已核实文件；检索类（search_code / find_files /
        list_dir）从结果里扒出 path:line 记成文件或目录；抓取类（fetch_url /
        fetch_json）记成网页（二手来源标记，供规则 4 警示）。
        """
        if name == "read_file":
            raw = str(args.get("path") or "")
            if raw:
                try:
                    p = self.guard.resolve(raw)
                except Exception:
                    return
                if p.is_dir():
                    ledger.mark_dir(p)
                    return
                # 成功回显头部带「(N 行, 显示 a-b)」：总行数供行号越界校验，
                # **显示区间**供「引用的行号是否真读过」校验（rerun28 前的核查
                # 只到文件级：读过第 1-60 行，照样能引用第 300 行而没人拦）。
                m = re.search(r"\((\d+)\s*行(?:,\s*显示\s*(\d+)-(\d+))?\)", out)
                n = int(m.group(1)) if m else None
                rng = [(int(m.group(2)), int(m.group(3)))] if (m and m.group(2)) else []
                # read=True：read_file 是台账里唯一算「真读过」的通道。
                # 交付物引用 external/ 仓库文件时，硬闸只认这个标记（rerun12 后
                # 新增）——search_code 片段不算已读，防止片段结论直达交付物。
                ledger.mark_file(p, lines=n, read=True, ranges=rng)
            return

        if name in ("search_code", "find_files"):
            # 每行形如  path/to/file.py:LINE: 内容  —— 扒出文件路径记成已核实
            for m in re.finditer(r"^([^\s:][^\n:]*?):\d+:\s", out, re.M):
                try:
                    p = self.guard.resolve(m.group(1))
                except Exception:
                    continue
                if p.is_dir():
                    ledger.mark_dir(p)
                else:
                    ledger.mark_file(p)
            return

        if name == "list_dir":
            raw = str(args.get("path") or ".")
            try:
                p = self.guard.resolve(raw)
            except Exception:
                return
            ledger.mark_dir(p)
            return

        if name in ("fetch_url", "fetch_json"):
            url = str(args.get("url") or args.get("uri") or "")
            if url:
                ledger.mark_web(url)
            return

