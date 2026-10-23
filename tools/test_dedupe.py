"""答复重复段去重单元测试（agentforge/dedupe.py）。

复现 rerun58 实证：第 9 轮把同一份答复连着输出三遍（措辞略有差异，逐字比较抓不住），
以及收尾把「任务完成总结」块隔一句又原样重说一遍。
同时守住反向：正常答复里重复同一个命令、重复引用同一个链接不能被删。

运行：python tools/test_dedupe.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentforge import dedupe


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"测试未通过: {name}")


# rerun58 现场：三个副本，第一份开头不同（「Marp（及其 CLI 工具 marp-cli）」），
# 后两份开头相同——所以必须靠「段落指纹 + 成块重复」判，不能靠整段相等。
TRIPLE = """推荐使用的开源命令行项目是 **Marp**（及其 CLI 工具 **marp-cli**）。

该项目的官方仓库为：`https://github.com/marp-team/marp-cli`，由 marp-team 维护。

### ① 本地安装与运行

```bash
npm install -g @marp-team/marp-cli
```

### ② 转换成 pptx

```bash
marp deck.md --pptx
```

### ③ 嵌入前端项目

在构建流程里加一步预处理，把 deck.md 转成 html 再嵌进页面。

任务完成总结：核实了实现仓库，提取了安装命令，给出了嵌入方案。

推荐使用的开源命令行项目是 **marp-cli**。

该项目的官方仓库为：`https://github.com/marp-team/marp-cli`，由 marp-team 维护。

### ① 本地安装与运行

```bash
npm install -g @marp-team/marp-cli
```

### ② 转换成 pptx

```bash
marp deck.md --pptx
```

### ③ 嵌入前端项目

在构建流程里加一步预处理，把 deck.md 转成 html 再嵌进页面。

任务完成总结：核实了实现仓库，提取了安装命令，给出了嵌入方案。

推荐使用的开源命令行项目是 **marp-cli**。

该项目的官方仓库为：`https://github.com/marp-team/marp-cli`，由 marp-team 维护。

### ① 本地安装与运行

```bash
npm install -g @marp-team/marp-cli
```

### ② 转换成 pptx

```bash
marp deck.md --pptx
```

### ③ 嵌入前端项目

在构建流程里加一步预处理，把 deck.md 转成 html 再嵌进页面。

任务完成总结：核实了实现仓库，提取了安装命令，给出了嵌入方案。
"""

NORMAL = """先说结论：这个场景用 Marp 的 CLI 就够，不需要上运行时。

安装方式有两种，团队里推荐用 npm 全局装，版本好对齐：

```bash
npm install -g @marp-team/marp-cli
```

容器里跑的话用官方镜像，注意工作目录要挂到 /home/marp/app：

```bash
docker run --rm -v "$(pwd)":/home/marp/app marpteam/marp-cli deck.md -o deck.pptx
```

最后提醒一句：转换失败时先看 node 版本，Marp CLI 对 Node 版本有下限要求。
"""

DUPLICATE_URL = """结论是 Marp 这个项目最合适，理由是它同时支持 html 与 pptx 两种输出。

参考：https://github.com/marp-team/marp-cli 这是官方仓库，安装说明都在里面。

参考：https://github.com/marp-team/marp-cli 上面这条链接是同一份资料，重复引用一次。

完。
"""


def test_triple_answer_collapsed() -> None:
    out, removed = dedupe.dedupe_repeated(TRIPLE)
    _check("三份答复被折叠", removed > 300, f"removed={removed}")
    _check("只剩一份正文", out.count("该项目的官方仓库为") == 1,
           str(out.count("该项目的官方仓库为")))
    _check("折叠后仍保留首份的开头", out.startswith("推荐使用的开源命令行项目是 **Marp**"))


def test_duplicate_tail_block() -> None:
    text = ("正文第一段，说明这个方案怎么落地，以及前置条件都有哪些，需要提前准备。\n\n"
            "任务完成总结：核实并确认了正确的实现仓库，从官方源提取了安装命令，"
            "并根据文档给出了转换与嵌入方案。\n\n"
            "验证状态：已通过官方仓库数据核实。\n\n"
            "任务完成总结：核实并确认了正确的实现仓库，从官方源提取了安装命令，"
            "并根据文档给出了转换与嵌入方案。\n\n"
            "以上。")
    out, removed = dedupe.dedupe_repeated(text)
    _check("隔一句重复的总结块被删掉", removed > 0, f"removed={removed}")
    _check("总结块只剩一次", out.count("任务完成总结") == 1, str(out.count("任务完成总结")))


def test_normal_answer_untouched() -> None:
    out, removed = dedupe.dedupe_repeated(NORMAL)
    _check("正常答复不删字", removed == 0 and out == NORMAL, f"removed={removed}")


def test_repeated_citation_kept() -> None:
    _check("重复引用同一链接不误删", dedupe.dedupe_repeated(DUPLICATE_URL)[1] == 0)


def test_short_text_fast_path() -> None:
    _check("短文本直接返回", dedupe.dedupe_repeated("一句话。") == ("一句话。", 0))
    _check("空文本不炸", dedupe.dedupe_repeated("") == ("", 0))


def main() -> None:
    test_triple_answer_collapsed()
    test_duplicate_tail_block()
    test_normal_answer_untouched()
    test_repeated_citation_kept()
    test_short_text_fast_path()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
