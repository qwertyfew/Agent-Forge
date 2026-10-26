"""P34 回归测试：web_search 相关性感知后端回退（中文长尾查询不再吃 Bing 词条垃圾）。

运行：python tools/test_p34_search.py
全部断言内建，退出码 0 = 全过。**不联网**——requests.get 用替身。

背景（n3 莓果复测实证）：Bing 国内端点对中文长尾查询返回整页无关内容
（搜「冷冻莓果 品牌 推荐」返回「冷冻（汉语词语）_百度百科」8 条零相关），
旧实现「第一个非空后端就用」→ 中文查询永远吃 Bing 垃圾。P34 改为逐后端
检查词面交集，第一个有相关结果的胜出（Bing → 百度 → 搜狗 → DDG）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.core.config import Config                        # noqa: E402
from agentforge.permissions import Guard                    # noqa: E402
from agentforge.tools import build_registry                 # noqa: E402
from agentforge.tools import web as W                       # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


JUNK_BING = """<html><body>
<li class="b_algo"><h2><a href="https://baike.baidu.com/item/冷冻">冷冻（汉语词语）_百度百科</a></h2><p>冷冻指降低温度…</p></li>
<li class="b_algo"><h2><a href="https://www.gov.cn/">中国政府网</a></h2><p>中华人民共和国门户…</p></li>
<li class="b_algo"><h2><a href="http://www.china.com.cn/">中国网</a></h2><p>网上中国…</p></li>
</body></html>"""

GOOD_BAIDU = """<html><body>
<h3><a href="https://www.baidu.com/link?url=aaa">冷冻莓果 <em>品牌</em>推荐 - 电商榜单</a></h3>
<h3><a href="https://www.baidu.com/link?url=bbb">2026 冷冻蓝莓牌子推荐 - 消费指南</a></h3>
</body></html>"""

GOOD_SOGOU = """<html><body>
<h3><a href="/link?url=ccc">冷冻树莓排行榜 - 京东</a></h3>
<h3><a href="/link?url=ddd">冷冻莓果怎么选 - 知乎</a></h3>
</body></html>"""


def main() -> None:
    cfg = Config.load(str(ROOT / "config.yaml"))
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ws = registry.get("web_search")

    # 按请求 URL 路由的替身：bing 永远给词条垃圾，搜狗给相关结果
    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        class R:
            status_code = 200
            text = ""
            def raise_for_status(self): ...
        q = (params or {}).get("q") or (params or {}).get("wd") or (params or {}).get("query") or ""
        if "bing.com" in url:
            R.text = JUNK_BING
        elif "sogou.com" in url:
            R.text = GOOD_SOGOU
        else:
            R.text = "<html></html>"
        return R()

    orig = W.requests.get
    W.requests.get = fake_get
    try:
        out = ws.run(query="冷冻莓果 品牌 推荐")
    finally:
        W.requests.set = None if False else None
        W.requests.get = orig

    check("P34 Bing 词条垃圾被跳过，落到有相关结果的后端",
          "电商榜单" in out or "京东" in out or "消费指南" in out, out[:300])
    check("P34 输出不再全量丢弃",
          "已丢弃" not in out, out[:200])
    check("P34 回执保留「转述未核实」标记",
          "转述 · 未核实" in out, out[:200])

    # 全后端都给垃圾时：照旧丢弃并指路（口径不放松）
    def all_junk_get(url, params=None, timeout=None, headers=None, **kw):
        class R:
            status_code = 200
            text = JUNK_BING
            def raise_for_status(self): ...
        return R()
    W.requests.get = all_junk_get
    try:
        out2 = ws.run(query="冷冻莓果 品牌 推荐")
    finally:
        W.requests.get = orig
    check("P34 全后端零相关时仍丢弃并指路（口径不放松）",
          "已丢弃" in out2 and "fetch" in out2, out2[:300])

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
