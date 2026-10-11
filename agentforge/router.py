from __future__ import annotations

import re

from .config import Config


class Router:
    """按任务性质把请求路由到不同专长的模型。

    思路：用一个快模型（或规则）判断任务属于「代码/数学/逻辑」还是「语言/通用」，
    再交给对应专长的模型，避免用同一个模型硬扛所有任务。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.code_rx = [re.compile(p, re.I) for p in cfg.code_signals]
        self.lang_rx = [re.compile(p, re.I) for p in cfg.language_signals]

    def scores(self, text: str) -> tuple[int, int]:
        code_score = sum(1 for rx in self.code_rx if rx.search(text))
        lang_score = sum(1 for rx in self.lang_rx if rx.search(text))
        return code_score, lang_score

    def _usable(self, key: str) -> bool:
        """这个 key 对应的模型在这台机器上现在能不能真的跑起来。"""
        spec = self.cfg.models.get(key)
        return bool(spec and getattr(spec, "available", True))

    def fallback_key(self) -> str:
        """默认模型跑不起来时挑一个能顶上的。

        优先在线模型：换台电脑 clone 下来时本地模型往往还没 pull，
        而在线模型只要密钥在就立刻能用——这正是「拿到仓库就能开工」的路径。
        都不行就退回原默认值，让上层把真实错误报出来。
        """
        if self._usable(self.cfg.routing_default):
            return self.cfg.routing_default
        for k, s in self.cfg.models.items():
            if getattr(s, "available", True) and getattr(s, "kind", "") == "paid":
                return k
        for k, s in self.cfg.models.items():
            if getattr(s, "available", True):
                return k
        return self.cfg.routing_default

    def _pick(self, key: str) -> str:
        """语义 key（general/code）不可用时退回一个本机能跑的模型。"""
        if key in self.cfg.models and self._usable(key):
            return key
        return self.fallback_key()

    def classify(self, text: str) -> str:
        code_score, lang_score = self.scores(text)
        # 语言类信号优先：明确的写作/翻译请求即使带代码词也走通用模型
        if lang_score > code_score:
            return self._pick("general")
        if code_score > 0:
            return self._pick("code")
        return self.fallback_key()

    def explain(self, text: str) -> str:
        c, l = self.scores(text)
        key = self.classify(text)
        return f"代码/逻辑信号 {c} 分，语言信号 {l} 分 → 选用 {key}（{self.cfg.model(key).name}）"
