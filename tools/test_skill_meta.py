"""技能 frontmatter 编辑（agentforge.skills.write_meta）回归。

管理页「技能编辑」只开放 frontmatter 的三个字段——作用（description）、
适用（when_to_use）、不适用（when_not_to_use）——其余字段与正文必须原样保留。
删掉任何一条断言，对应行为坏了这里必须变红。

运行：python tools/test_skill_meta.py
不需要模型、不需要联网，退出码 0 = 全过。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.skills import SkillRegistry, split_frontmatter, write_meta  # noqa: E402

failed = 0


def check(name, cond, detail=""):
    global failed
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  —— {detail}"))
    if not cond:
        failed += 1


SKILL_MD = """---
name: pdf-export
description: 把 HTML 导出为 PDF
version: "1.2"
when_to_use: 用户要生成 PDF 文件时
when_not_to_use: 用户只要截图时
keywords: [pdf, export]
auto_learned: true
learned_at: 2026-10-01 10:00
---

# PDF 导出

第一步：准备 HTML。
第二步：调用导出。
"""


def make_skill(tmp: Path, text: str = SKILL_MD) -> Path:
    d = tmp / "pdf-export"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # W1 正常编辑：三字段替换，其余 frontmatter 字段与正文原样
    d = make_skill(tmp)
    err = write_meta(d, description="  把 HTML 页面导出为   PDF 文件 ",
                     when_to_use="需要生成 PDF 时", when_not_to_use="")
    check("W1 编辑成功返回空串", err == "", err)
    meta, body = split_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
    check("W2 description 已更新且内部空白压平",
          meta.get("description") == "把 HTML 页面导出为 PDF 文件", repr(meta.get("description")))
    check("W3 when_to_use 已更新", meta.get("when_to_use") == "需要生成 PDF 时", repr(meta))
    check("W4 传空的 when_not_to_use 会被移除（不残留空键）",
          "when_not_to_use" not in meta, repr(meta))
    check("W5 其余字段原样保留（name/version/keywords/auto_learned/learned_at）",
          meta.get("name") == "pdf-export" and str(meta.get("version")) == "1.2"
          and meta.get("keywords") == ["pdf", "export"]
          and meta.get("auto_learned") is True
          and meta.get("learned_at") == "2026-10-01 10:00", repr(meta))
    check("W6 正文一个字都不动", body.strip().endswith("第二步：调用导出。"), repr(body[-80:]))
    check("W7 没有留下临时文件", not (d / "SKILL.md.tmp").exists(), "")

    # W8 编辑后的文件能被注册表正常扫出来（frontmatter 仍是合法 YAML）
    reg = SkillRegistry([tmp])
    reg.scan()
    sk = reg.get("pdf-export")
    check("W8 编辑后注册表照常识别，新描述生效",
          sk is not None and sk.description == "把 HTML 页面导出为 PDF 文件"
          and sk.when_to_use == "需要生成 PDF 时" and sk.when_not_to_use == "",
          sk.description if sk else "未扫到")

    # W9-W11 拒绝路径
    d2 = tmp.parent / (tmp.name + "_plain")
    d2.mkdir(exist_ok=True)
    (d2 / "SKILL.md").write_text("# 没有 frontmatter 的技能\n正文", encoding="utf-8")
    err = write_meta(d2, description="x")
    check("W9 缺 frontmatter 的 SKILL.md 拒绝编辑", err != "" and "frontmatter" in err, err)
    err = write_meta(tmp / "no-such-dir", description="x")
    check("W10 目录不存在（读不到 SKILL.md）返回人话原因", err != "" and "读不到" in err, err)
    err = write_meta(make_skill(tmp / "empty-desc"), description="   ")
    check("W11 description 为空拒绝", err != "" and "description" in err, err)

print("")
if failed:
    print(f"{failed} 项失败。")
    sys.exit(1)
print("全部通过。")
