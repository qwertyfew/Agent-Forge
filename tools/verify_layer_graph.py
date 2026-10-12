"""验证本轮三项「借鉴 TencentDB Agent Memory」的落地：
A. 记忆分层（L1/L2/L3）—— 判定、存储、分层检索
B. Skill 资产元数据 —— 版本、触发边界（含负向边界让位）
C. impact_of 影响路径工具

用法：python tools/verify_layer_graph.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from agentforge import autoskill, loop
from agentforge.memory import MemoryStore, classify_layer
from agentforge.skills import SkillRegistry
from agentforge.tools.graph import build_graph_tools

ROOT = Path(__file__).resolve().parent.parent
ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


print("=" * 78)
print("A. 记忆分层：判定 / 存储 / 分层检索")
print("=" * 78)

LAYER_CASES = [
    ("用户偏好：代码注释一律使用中文", "L3"),
    ("用户偏好：以后写前端文案一律使用简体中文", "L3"),
    ("本地 Ollama 端口是 11434", "L1"),
    ("工作区根目录是 D:\\AI_develop\\project\\agent-forge", "L1"),
    ("依赖里没有额外库，只用标准库 http.server", "L1"),
    ("改动代码前先看影响范围再动手", "L2"),
    ("提交前先跑一遍测试", "L2"),
]
for text, want in LAYER_CASES:
    got = classify_layer(text)
    check(f"分层判定 {want}: {text[:26]}", got == want, f"got={got}")

tmp = Path(tempfile.mkdtemp()) / "memory.md"
store = MemoryStore(tmp)
e1 = store.append("用户偏好：注释一律用中文", "t1")          # 自动 L3
e2 = store.append("本地 Ollama 端口 11434", "t1")           # 自动 L1
e3 = store.append("改动前先跑测试", "t2")                    # 自动 L2
e4 = store.append("这条被强制标成事实层", "t2", layer="L1")   # 显式指定
check("append 自动判层（偏好→L3）", e1.get("layer") == "L3", str(e1.get("layer")))
check("append 自动判层（事实→L1）", e2.get("layer") == "L1", str(e2.get("layer")))
check("append 默认层为 L2", e3.get("layer") == "L2", str(e3.get("layer")))
check("append 支持显式指定层", e4.get("layer") == "L1", str(e4.get("layer")))

counts = store.layer_counts()
check("layer_counts 统计正确", counts == {"L1": 2, "L2": 1, "L3": 1}, str(counts))

# 旧格式（无层标签）行仍要能解析，并就地推断层
legacy = tmp.read_text(encoding="utf-8") + "- [2026-01-01 00:00 · old] 用户偏好：一律用中文\n"
tmp.write_text(legacy, encoding="utf-8")
ents = store.entries()
check("旧格式行可解析且推断出层",
      ents[-1]["text"].startswith("用户偏好") and ents[-1]["layer"] == "L3",
      str(ents[-1]))

wide = store.prompt_block(scope="wide")
facts = store.prompt_block(scope="facts")
check("wide 注入含「用户偏好」分组", "### 用户偏好" in wide)
check("facts 注入含「事实与环境」分组", "### 事实与环境" in facts)
check("两种 scope 的注入内容不同", wide != facts)
check("facts 时事实层排在偏好层前面",
      facts.index("### 事实与环境") < facts.index("### 用户偏好"))

print()
print("=" * 78)
print("B. Skill 资产元数据：版本 + 触发边界（含负向让位）")
print("=" * 78)

raw = (
    "NAME: doc-eval\n"
    "DESC: 评估外部资料的方法\n"
    "VERSION: 1.2\n"
    "WHEN: 需要对外部资料做客观评估与对比时\n"
    "WHEN_NOT: 纯代码改动任务\n"
    "KEYWORDS: 评估,调研,对比,资料\n"
    "---\n"
    "## 流程\n先读材料，再逐条对照，最后给结论。\n"
)
skill = autoskill.parse_model_skill(raw)
check("解析出 VERSION", skill and skill.get("version") == "1.2", str(skill and skill.get("version")))
check("解析出 WHEN", skill and "评估" in skill.get("when_to_use", ""))
check("解析出 WHEN_NOT", skill and skill.get("when_not_to_use") == "纯代码改动任务")
check("WHEN_NOT=NONE 视为空",
      autoskill.parse_model_skill(raw.replace("纯代码改动任务", "NONE")).get("when_not_to_use") == "")
check("含标点/非法字符的关键词被剔除",
      "评估,调研,资料" == ",".join(
          autoskill.parse_model_skill(raw.replace("KEYWORDS: 评估,调研,对比,资料",
                                                  "KEYWORDS: 评估,调研,a:b,c[d],资料"))["keywords"]))

# 落盘 → 重新扫描 → 元数据可被 SkillRegistry 读回
learned = Path(tempfile.mkdtemp())
reg = SkillRegistry([learned])
written = autoskill.write_skill(reg, learned, skill, "t-test")
check("write_skill 落盘成功", bool(written), str(written))
sk = reg.get("doc-eval")
check("注册表读回 version", sk and sk.version == "1.2", str(sk and sk.version))
check("注册表读回 when_to_use", sk and sk.when_to_use.startswith("需要对外部资料"))
check("注册表读回 when_not_to_use", sk and sk.when_not_to_use == "纯代码改动任务")
check("brief 头部带触发边界",
      sk and "不适用：纯代码改动任务" in sk.brief())
check("catalog 带版本号", "doc-eval (v1.2)" in reg.catalog())

# 负向边界：任务文本明显落在 when_not_to_use 场景里时，不该自动命中
hit_on = reg.match("帮我评估这份外部资料的调研对比")
hit_off = reg.match("纯代码改动任务 纯代码改动任务")
check("适用场景命中", bool(hit_on), str([(s.name, n) for s, n in hit_on]))
check("不适用场景让位（不再自动命中）", not hit_off, str([(s.name, n) for s, n in hit_off]))

print()
print("=" * 78)
print("C. impact_of：影响路径分析")
print("=" * 78)


class _StubCfg:
    max_tool_output_chars = 8000


class _StubGuard:
    def resolve(self, p):
        return (ROOT / p).resolve() if p not in (".", "") else ROOT


tools = build_graph_tools(_StubGuard(), _StubCfg())
impact = next(t for t in tools if t.name == "impact_of")
out = impact.run(symbol="MemoryStore", path="agentforge", file_glob="**/*.py")
check("impact_of 报告定义处", "[定义处]" in out and "memory.py" in out, out.splitlines()[1] if len(out.splitlines()) > 1 else "")
check("impact_of 报告引用处", "[引用处]" in out and "loop.py" in out)
check("impact_of 提示同步更新引用方", "引用方" in out)

out2 = impact.run(symbol="这个符号根本不存在_xyz", path="agentforge")
check("不存在的符号给出「无引用」结论", "没有任何定义或引用" in out2, out2.splitlines()[1] if len(out2.splitlines()) > 1 else "")

print()
print("=" * 78)
print("D. 检索深度随任务类型切换（loop.memory_scope）")
print("=" * 78)
check("技术细节任务 → facts", loop.memory_scope("修一下 agentforge/loop.py 的报错") == "facts")
check("常规对话 → wide", loop.memory_scope("你好，帮我规划一下这个季度") == "wide")

print()
print("=" * 78)
print("综合:", "全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
