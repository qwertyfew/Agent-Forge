from __future__ import annotations

import re

from .config import Config
from .planner import KIND_DIM, KIND_LABEL, STAKES_LABEL, kind_of, stakes_of

# 能力路由：每类任务实际要看的维度集合。中文写作/分析类任务里，中文能力是硬约束，
# 所以取相关维度的「最小值」作为综合可用分——任一维度拉胯就整体拉胯（这正是
# gemma4:12b 做中文长文归纳时的真实短板：writing 不低，但 chinese 只有 62）。
_CAP_DIMS = {
    "coding": ("coding",),
    "inspect": ("reasoning",),
    "writing": ("writing", "chinese"),
    "math": ("math",),
    "analysis": ("reasoning", "chinese"),
    "general": ("reasoning", "chinese"),
}


class Router:
    """按任务性质把请求路由到不同专长的模型。

    实际生效的路由是 capability()（webui 的 auto 模式走它），两层结合：

    1. 后果定档（stakes）：按「做错了会怎样」给任务分档（低风险/常规/高后果），
       每档对应一条模型能力下限——这是 swe-router 的核心思想：路由不该猜
       「任务像什么」，该问「失败了多少」。
    2. 能力选型：按任务所需能力维度（编码/推理/写作/中文…）算每个模型的
       综合分，在「满足该档下限」的模型里挑最便宜的（cost 维度），没人达标
       才用最强者兜底并明说差距。

    classify() 是信号词粗分的旧路径，已不在执行链上，仅剩 CLI /route
    诊断命令在用；不要在新代码里依赖它。
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

        # ---- 按失败后果定档（swe-router 思路）----
        # 先问「这个任务做错了会怎样」，给模型能力设下限，再在达标者里挑
        # 最便宜的——而不是无条件挑最强。低风险任务不值得动用大模型，
        # 高后果任务则宁可多花算力也要守住质量线。
        stakes, stakes_why = stakes_of(text)
        tiers = self.cfg.routing_stakes_tiers or {}
        floor = int(tiers.get(stakes, tiers.get("medium", 78)))

        def _cheap_first(e):
            # cost 分数越高 = 越便宜（scores.py 的约定：本地模型 94~96，付费 35~68）。
            # 同价位取综合分更高的：便宜且更准的那个人人都选。
            sc = e.scores or {}
            return (int(sc.get("cost", 0) or 0), _combo(e), e.total)

        # 允许自动选用的池：未开付费升级时只在本地区间内自动选，绝不擅自用付费模型
        if self.allow_paid_upgrade:
            pool = all_avail or enabled
        else:
            pool = [e for e in (all_avail or enabled) if getattr(e, "kind", "") != "paid"]
        pool = sorted(pool, key=_cheap_first, reverse=True)

        enabled_keys = {e.key for e in enabled}
        enabled_best = max(enabled, key=lambda e: (_combo(e), e.total))
        qualified = [e for e in pool if _combo(e) >= floor]
        if qualified:
            pick = qualified[0]              # 最便宜的够用者
            shortfall = 0
        else:
            # 池子里没人达标：用最强者兜底，并把差距如实报出去
            pick = max(pool, key=lambda e: (_combo(e), e.total))
            shortfall = floor - _combo(pick)

        # 授权检查：pick 不在已启用池里时，改用它需要 allow_upgrade 授权
        use = pick
        auto = False
        if pick.key not in enabled_keys:
            if self.allow_upgrade:
                auto = True
            else:
                use = enabled_best           # 实际仍用已启用池里最强的

        chosen_score = _combo(use)
        strongest = max(all_avail or enabled, key=lambda e: (_combo(e), e.total))
        strongest_score = _combo(strongest)

        reason = (f"按后果路由：{label}类 · {STAKES_LABEL.get(stakes, stakes)}档"
                  f"（下限 {floor}）→ {use.display}（{chosen_score} 分）。{stakes_why}")
        notice = ""
        upgrade = auto
        if shortfall > 0:
            notice = (f"⚠ {STAKES_LABEL.get(stakes, stakes)}任务（下限 {floor} 分）"
                      f"没有达标模型，当前最高 {strongest_score} 分，已用最强者兜底。")
        elif auto and use.key != enabled_best.key:
            notice = (f"已按后果档位自动选用 {use.display}（{chosen_score} 分 ≥ 下限 {floor}，"
                      f"且比 {enabled_best.display} 更省）。")
        elif use.key != pick.key:
            notice = (f"存在满足下限的更省候选 {pick.display}（{_combo(pick)} 分）："
                      f"在「可用模型」里勾选它，或开启 routing.allow_upgrade 即可自动选用。")

        return {
            "kind": kind, "dimension": dim, "dimension_label": label,
            "stakes": stakes, "floor": floor, "shortfall": shortfall,
            "chosen_key": use.key, "chosen_name": use.display,
            "chosen_score": chosen_score,
            "best_key": strongest.key, "best_name": strongest.display,
            "best_score": strongest_score, "gap": strongest_score - chosen_score,
            "upgrade": upgrade, "auto": auto,
            "notice": notice,
            "reason": reason,
        }
