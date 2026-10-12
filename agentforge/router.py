from __future__ import annotations

import re

from .config import Config
from .planner import KIND_DIM, KIND_LABEL, kind_of

# 能力路由：每类任务实际要看的维度集合。中文写作/分析类任务里，中文能力是硬约束，
# 所以取相关维度的「最小值」作为综合可用分——任一维度拉胯就整体拉胯（这正是
# gemma4:12b 做中文长文归纳时的真实短板：writing 不低，但 chinese 只有 62）。
_CAP_DIMS = {
    "coding": ("coding",),
    "writing": ("writing", "chinese"),
    "math": ("math",),
    "analysis": ("reasoning", "chinese"),
    "general": ("reasoning", "chinese"),
}


class Router:
    """按任务性质把请求路由到不同专长的模型。

    两层路由：

    1. 粗分（classify）：用信号词判断任务属于「代码/逻辑」还是「语言/通用」，
       再交给对应专长的模型 key，避免用同一个模型硬扛所有任务。
    2. 能力路由（capability）：按任务真正需要的能力维度（编码/推理/写作/
       中文…）从可用模型池里挑该维度最强的那个；当默认/已启用模型偏弱、
       且存在更强的可用模型时，给出升级建议（或在配置允许时自动升级）。
       这层直接回应「瓶颈在模型能力」——让重归纳/长文档/中文写作类任务
       自动落到更擅长它的模型上，而不是永远卡在默认的小模型。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.code_rx = [re.compile(p, re.I) for p in cfg.code_signals]
        self.lang_rx = [re.compile(p, re.I) for p in cfg.language_signals]
        self.capability_on = bool(cfg.routing_capability)
        self.quality_threshold = int(cfg.routing_quality_threshold)
        self.allow_upgrade = bool(cfg.routing_allow_upgrade)
        self.allow_paid_upgrade = bool(cfg.routing_allow_paid_upgrade)

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

    # ---------------------------------------------------------------- 能力路由
    def capability(self, text: str, store) -> dict:
        """按任务所需能力维度，从可用模型里挑最合适的，并评估是否需要升级。

        store 为 ModelStore：用 usable() 取「已启用且能跑」的池，用 _catalog
        取「全部可用（含未启用）」以寻找更强的候选。返回可直接喂给前端的字典：

            kind / dimension / dimension_label  任务性质与该看哪个评分维度
            chosen_key / chosen_name / chosen_score  实际选用的模型
            best_key  / best_name  / best_score     全部可用模型里该维度最强者
            gap / upgrade / notice / reason
        """
        kind = kind_of(text)
        dim = KIND_DIM.get(kind, "reasoning")
        dims = _CAP_DIMS.get(kind, ("reasoning",))
        label = KIND_LABEL.get(kind, kind)

        # 已启用且能跑的池（任务实际能用的模型）
        try:
            enabled = [e for e in store.usable() if e.enabled and e.available]
        except Exception:  # noqa: BLE001
            enabled = []
        if not enabled:
            try:
                enabled = store.fallback_pool()
            except Exception:  # noqa: BLE001
                enabled = []
        # 全部可用（含未启用）——用于寻找更强的候选
        try:
            all_avail = [e for e in store._catalog if getattr(e, "available", False)]
        except Exception:  # noqa: BLE001
            all_avail = list(enabled)

        if not enabled:
            return {
                "kind": kind, "dimension": dim, "dimension_label": label,
                "chosen_key": self.fallback_key(), "chosen_name": "",
                "chosen_score": 0, "best_key": "", "best_name": "",
                "best_score": 0, "gap": 0, "upgrade": False,
                "notice": "", "reason": "没有可用的模型，路由无法评估。",
            }

        def _combo(e) -> int:
            """综合可用分：相关维度的极小值——任一维度拉胯整体就拉胯。"""
            sc = e.scores or {}
            vals = [int(sc.get(d, 0) or 0) for d in dims]
            return min(vals) if vals else 0

        # 默认会用的（已启用池内最佳）
        chosen = max(enabled, key=lambda e: (_combo(e), e.total))
        # 允许自动选用的池：未开付费升级时只在本地区间内自动选，绝不擅自用付费模型
        if self.allow_paid_upgrade:
            pool = all_avail or enabled
        else:
            pool = [e for e in (all_avail or enabled) if getattr(e, "kind", "") != "paid"]
        best = max(pool, key=lambda e: (_combo(e), e.total))
        # 全局最强（含付费）——只在「超出授权范围」时用于提示
        strongest = max(all_avail or enabled, key=lambda e: (_combo(e), e.total))

        chosen_score0 = _combo(chosen)
        chosen_name0 = chosen.display
        chosen_score = chosen_score0
        best_score = _combo(best)
        strongest_score = _combo(strongest)
        gap = best_score - chosen_score0

        notice = ""
        upgrade = False
        auto = False
        reason = (f"按能力路由：{label}类任务 → {dim} 维度，"
                  f"默认 {chosen_name0}（{chosen_score0} 分）")

        if self.allow_upgrade and best.key != chosen.key and best_score > chosen_score0:
            # 池内就有更强的、且被授权自动选用 → 直接自动切换
            upgrade = True
            auto = True
            chosen = best
            chosen_score = best_score
            reason = (f"按能力路由自动选用：{label}类任务 → {dim} 维度，"
                      f"{chosen.display}（{chosen_score} 分）")
            notice = (f"已按任务类型自动选用 {chosen.display}（{label}能力 {chosen_score} 分，"
                      f"优于默认 {chosen_name0} 的 {chosen_score0} 分）")
        elif best.key != chosen.key and best_score > chosen_score0:
            # 能力路由开着但自动升级未授权 → 仅提示去开启对应的一键配置
            if getattr(best, "kind", "") == "paid":
                notice = (f"该任务需要较强的{label}能力（{dim} 维度）。当前默认 {chosen_name0} "
                          f"仅 {chosen_score0} 分；全局最优是付费模型 {best.display}（{best_score} 分）。"
                          f"在「可用模型」里点「自动配置最佳模型」即可让此类任务自动启用它。")
            else:
                notice = (f"该任务需要较强的{label}能力（{dim} 维度）。当前默认 {chosen_name0} "
                          f"仅 {chosen_score0} 分；存在更强的本地模型 {best.display}（{best_score} 分）。"
                          f"在「可用模型」里点「自动配置本地模型」即可让此类任务自动选用它。")

        return {
            "kind": kind, "dimension": dim, "dimension_label": label,
            "chosen_key": chosen.key, "chosen_name": chosen.display,
            "chosen_score": chosen_score,
            "best_key": strongest.key, "best_name": strongest.display,
            "best_score": strongest_score, "gap": gap,
            "upgrade": upgrade, "auto": auto,
            "notice": notice,
            "reason": reason,
        }
