"""可用模型库：AgentForge 能调用的所有模型的目录与使用状态。

三层概念要分清：

    可用模型目录（catalog）  所有能被调用的模型：config.yaml 里的本地模型 +
                             自动发现的 Ollama 模型 + 用户添加的付费模型
    启用组合（enabled）      用户勾选的那几个，真正参与任务执行的模型池
    最优组合（best）         按各维度得分算出来覆盖最全的一组，可一键应用

本地模型全部自动进入目录（用户不用手填）；付费模型由用户在设置页添加，
填好厂商 / 模型号 / API Key 之后自动成为可用模型，并参与评分与路由。

状态持久化在项目根目录的 models.json（含 API Key，已加入 .gitignore）。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ModelSpec
from .scores import DIMENSIONS, DIM_KEYS, combo, lookup, total_of

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "models.json"

# 付费厂商预设：base_url 与常见模型号，前端用它填默认值
PROVIDERS: dict[str, dict] = {
    "openai":      {"label": "OpenAI", "base_url": "https://api.openai.com/v1",
                    "hint": "gpt-5.5 / gpt-5.4-mini", "protocol": "openai"},
    "anthropic":   {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com",
                    "hint": "claude-opus-4-5 / claude-sonnet-4-5", "protocol": "anthropic"},
    "deepseek":    {"label": "DeepSeek 深度求索", "base_url": "https://api.deepseek.com/v1",
                    "hint": "deepseek-chat / deepseek-reasoner", "protocol": "openai"},
    "moonshot":    {"label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1",
                    "hint": "kimi-k2-0905-preview", "protocol": "openai"},
    "zhipu":       {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "hint": "glm-5 / glm-4.6", "protocol": "openai"},
    "aliyun":      {"label": "阿里通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "hint": "qwen3-max / qwen-plus", "protocol": "openai"},
    "siliconflow": {"label": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
                    "hint": "任意 OpenAI 兼容模型号", "protocol": "openai"},
    "custom":      {"label": "自定义（OpenAI 兼容）", "base_url": "",
                    "hint": "自行填写 base_url，例如中转站或私有部署", "protocol": "openai"},
}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text or "")).strip("-").lower()
    return s or uuid.uuid4().hex[:8]


@dataclass
class ModelEntry:
    id: str
    key: str                 # cfg.models 里的 key
    name: str                # 送接口的模型名 / 展示名
    label: str = ""
    kind: str = "local"      # local | paid
    provider: str = "ollama"
    desc: str = ""
    enabled: bool = True
    scores: dict = field(default_factory=dict)
    total: int = 0
    source: str = ""
    evidence: str = ""
    version: str = ""
    context_window: int = 0
    discovered: bool = False   # 从 Ollama 自动发现、未在 config.yaml 里显式声明

    @property
    def display(self) -> str:
        return self.label or self.name

    def as_dict(self, spec: ModelSpec | None = None) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "name": self.display,
            "raw_name": self.name,
            "kind": self.kind,
            "provider": self.provider,
            "provider_label": PROVIDERS.get(self.provider, {}).get("label", self.provider),
            "desc": self.desc,
            "enabled": self.enabled,
            "scores": self.scores,
            "total": self.total,
            "source": self.source,
            "evidence": self.evidence,
            "version": self.version,
            "context_window": self.context_window,
            "discovered": self.discovered,
            "spec": {
                "num_ctx": getattr(spec, "num_ctx", 0),
                "temperature": getattr(spec, "temperature", 0),
                "think": getattr(spec, "think", None),
            } if spec else None,
        }


class ModelStore:
    """可用模型的目录 + 启用状态 + 付费模型配置。"""

    def __init__(self, cfg: Config, path: Path | None = None):
        self.cfg = cfg
        self.path = Path(path) if path else STORE_PATH
        self.paid: list[dict] = []
        self.enabled_ids: list[str] | None = None   # None = 尚未初始化，默认全选
        self.budget: int = 3                        # 最优组合最多挑几个模型
        self.score_overrides: dict[str, dict] = {}   # 用户手动覆盖的维度分
        self._catalog: list[ModelEntry] = []
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.paid = list(raw.get("paid") or [])
        if isinstance(raw.get("enabled"), list):
            self.enabled_ids = list(raw["enabled"])
        self.budget = int(raw.get("budget") or 3)
        if isinstance(raw.get("score_overrides"), dict):
            self.score_overrides = dict(raw["score_overrides"])

    def save(self) -> None:
        data = {
            "version": 1,
            "enabled": [e.id for e in self.catalog() if e.enabled],
            "paid": self.paid,
            "budget": self.budget,
            "score_overrides": self.score_overrides,
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _apply_override(self, entry_id: str, scores: dict) -> dict:
        """用户手动覆盖优先于内置跑分（内置库查不到或不认同时使用）。"""
        ov = self.score_overrides.get(entry_id)
        if not ov:
            return scores
        out = dict(scores)
        for k, v in ov.items():
            if k in DIM_KEYS:
                out[k] = max(0, min(100, int(v)))
        return out

    # ------------------------------------------------------------ 目录构建
    def catalog(self, discovered: list[str] | None = None) -> list[ModelEntry]:
        """构建完整目录，并把模型注册进 cfg（这样 agent.set_model 才能找到）。"""
        entries: list[ModelEntry] = []

        # 1) config.yaml 里显式声明的本地模型（保留各自的调参）
        for key, spec in list(self.cfg.models.items()):
            if spec.kind == "paid":
                continue
            entries.append(self._local_entry(key, spec.name, spec.desc, spec, discovered=False))

        # 2) Ollama 里发现但 config.yaml 没声明的模型：自动成为可用模型
        known_names = {e.name for e in entries}
        for name in (discovered or []):
            if name in known_names:
                continue
            entries.append(self._local_entry(name, name, "自动发现的本地模型", None, discovered=True))

        # 3) 用户添加的付费模型
        for item in self.paid:
            entries.append(self._paid_entry(item))

        # 启用状态：首次运行默认全选；之后以保存的列表为准
        enabled = self.enabled_ids
        for e in entries:
            e.enabled = True if enabled is None else (e.id in enabled)
        if enabled is None:
            self.enabled_ids = [e.id for e in entries]

        # 注册进 cfg，供 Agent / Router 使用
        for e in entries:
            self.cfg.register_model(self._spec_of(e))

        self._catalog = entries
        return entries

    def _local_entry(self, key: str, name: str, desc: str, spec: ModelSpec | None,
                     discovered: bool) -> ModelEntry:
        info = lookup(name, provider="ollama", kind="local")
        scores = self._apply_override(f"local:{key}", info.scores)
        return ModelEntry(
            id=f"local:{key}",
            key=key,
            name=name,
            label="",
            kind="local",
            provider="ollama",
            desc=desc or ("自动发现的本地模型" if discovered else ""),
            scores=scores,
            total=total_of(scores),
            source=info.source,
            evidence=info.evidence,
            version=info.version,
            context_window=getattr(spec, "num_ctx", 8192),
            discovered=discovered,
        )

    def _paid_entry(self, item: dict) -> ModelEntry:
        provider = item.get("provider", "custom")
        model_id = item.get("model_id", "")
        label = item.get("label") or model_id
        entry_id = item.get("id") or f"paid:{_slug(provider + '-' + model_id)}"
        info = lookup(model_id, provider=provider, kind="paid")
        scores = self._apply_override(entry_id, info.scores)
        return ModelEntry(
            id=entry_id,
            key=item.get("key") or entry_id,
            name=model_id,
            label=label,
            kind="paid",
            provider=provider,
            desc=item.get("desc") or f"{PROVIDERS.get(provider, {}).get('label', provider)} 云端模型",
            scores=scores,
            total=total_of(scores),
            source=info.source,
            evidence=info.evidence,
            version=info.version,
            context_window=int(item.get("context_window") or info.context_window or 0),
        )

    def _spec_of(self, e: ModelEntry) -> ModelSpec:
        """把目录项翻译成 ModelSpec。本地模型沿用 config 里的调参。"""
        if e.kind == "local" and e.key in self.cfg.models:
            spec = self.cfg.models[e.key]
            spec.entry_id = e.id
            spec.kind = "local"
            return spec
        if e.kind == "local":
            return ModelSpec(
                key=e.key, name=e.name, num_ctx=8192, temperature=0.6,
                provider="ollama", entry_id=e.id, kind="local", desc=e.desc,
            )
        item = next((p for p in self.paid if (p.get("id") == e.id)), {})
        return ModelSpec(
            key=e.key,
            name=e.name,
            label=e.label,
            num_ctx=int(item.get("num_ctx") or 32000),
            temperature=float(item.get("temperature") or 0.6),
            top_p=0.95,
            model_id=e.name,
            provider=e.provider,
            base_url=item.get("base_url") or PROVIDERS.get(e.provider, {}).get("base_url", ""),
            api_key=item.get("api_key", ""),
            entry_id=e.id,
            kind="paid",
            desc=e.desc,
        )

    # ------------------------------------------------------------ 查询
    def find(self, entry_id: str) -> ModelEntry | None:
        for e in self._catalog:
            if e.id == entry_id:
                return e
        return None

    def enabled(self) -> list[ModelEntry]:
        return [e for e in self._catalog if e.enabled]

    def combo_info(self, entries: list[ModelEntry] | None = None) -> dict:
        pool = entries if entries is not None else self.enabled()
        payload = [{"id": e.id, "name": e.display, "scores": e.scores} for e in pool]
        return combo(payload)

    # ------------------------------------------------------------ 修改
    def set_enabled(self, ids: list[str]) -> None:
        self.enabled_ids = list(ids)
        for e in self._catalog:
            e.enabled = e.id in self.enabled_ids
        self.save()

    def add_paid(self, provider: str, model_id: str, api_key: str,
                 base_url: str = "", label: str = "", num_ctx: int = 32000) -> ModelEntry:
        provider = provider or "custom"
        base_url = (base_url or PROVIDERS.get(provider, {}).get("base_url", "")).rstrip("/")
        item = {
            "id": f"paid:{_slug(provider + '-' + model_id)}",
            "key": f"paid:{_slug(provider + '-' + model_id)}",
            "provider": provider,
            "model_id": model_id,
            "label": label or model_id,
            "api_key": api_key,
            "base_url": base_url,
            "num_ctx": int(num_ctx or 32000),
        }
        self.paid = [p for p in self.paid if p.get("id") != item["id"]]
        self.paid.append(item)
        if self.enabled_ids is not None and item["id"] not in self.enabled_ids:
            self.enabled_ids.append(item["id"])   # 新加的付费模型默认参与组合
        self.catalog()
        self.save()
        return self.find(item["id"])  # type: ignore[return-value]

    def remove_paid(self, entry_id: str) -> bool:
        before = len(self.paid)
        self.paid = [p for p in self.paid if p.get("id") != entry_id]
        self.cfg.unregister_model(entry_id)
        if self.enabled_ids:
            self.enabled_ids = [i for i in self.enabled_ids if i != entry_id]
        self.catalog()
        self.save()
        return len(self.paid) < before

    def set_scores(self, entry_id: str, scores: dict) -> None:
        """手动覆盖某个模型的维度分（内置跑分不认同时的出口）。"""
        clean = {k: int(v) for k, v in (scores or {}).items() if k in DIM_KEYS}
        if clean:
            self.score_overrides[entry_id] = {**self.score_overrides.get(entry_id, {}), **clean}
        else:
            self.score_overrides.pop(entry_id, None)
        self.catalog()
        self.save()

    def best_combo(self, budget: int | None = None,
                   local_only: bool = False) -> list[ModelEntry]:
        """贪心选组合：每次挑「能给组合总分带来最大提升」的模型，直到收益为 0。

        这样选出来的组合既覆盖了强项维度，又不会塞进一堆能力重叠的模型。
        local_only=True 时只在本地模型（kind == "local"）里挑，付费模型一律不参与。
        """
        pool = [e for e in self._catalog if (not local_only or e.kind == "local")]
        limit = budget or self.budget or 3
        selected: list[ModelEntry] = []
        rest = list(pool)
        cur_total = 0
        while len(selected) < limit and rest:
            best_e, best_gain = None, 0
            for e in rest:
                cand = [{"id": x.id, "name": x.display, "scores": x.scores} for x in selected + [e]]
                gain = combo(cand)["total"] - cur_total
                if gain > best_gain:
                    best_e, best_gain = e, gain
            if best_e is None:
                break
            selected.append(best_e)
            rest.remove(best_e)
            cur_total += best_gain
        return selected

    def best_local_combo(self, budget: int | None = None) -> list[ModelEntry]:
        """只在本地模型里挑的最优组合（不碰任何付费模型）。"""
        return self.best_combo(budget, local_only=True)

    def apply_best(self, budget: int | None = None,
                   local_only: bool = False) -> list[ModelEntry]:
        """把启用组合设为最优组合。

        local_only=True 时结果里只会有本地模型——付费模型全部取消勾选，
        这样后续任务无论自动分配还是手动指定，都不可能走到付费 API。
        """
        picks = self.best_combo(budget, local_only=local_only)
        if picks:
            self.set_enabled([e.id for e in picks])
        return picks

    def apply_best_local(self, budget: int | None = None) -> list[ModelEntry]:
        """一键应用「本地最佳配置」：启用组合收窄为本地最优的这一组。"""
        return self.apply_best(budget, local_only=True)

    # ------------------------------------------------------------ 展示
    def snapshot(self, discovered: list[str] | None = None) -> dict:
        entries = self.catalog(discovered)
        pool = [e for e in entries if e.enabled]
        best_local = self.best_local_combo()
        return {
            "dimensions": [{"key": k, "label": v} for k, v in DIMENSIONS],
            "models": [e.as_dict(self.cfg.models.get(e.key)) for e in entries],
            "enabled": [e.id for e in pool],
            "combo": self.combo_info(pool),
            "best": [e.id for e in self.best_combo()],
            "best_local": [e.id for e in best_local],
            "best_local_combo": self.combo_info(best_local),
            "local_count": len([e for e in entries if e.kind == "local"]),
            "budget": self.budget,
            "providers": [
                {"id": pid, "label": p["label"], "base_url": p["base_url"], "hint": p["hint"]}
                for pid, p in PROVIDERS.items()
            ],
        }

    def summary_line(self) -> str:
        """给聊天页做「淡淡提示」用的一句话。"""
        pool = self.enabled()
        if not pool:
            return ""
        info = self.combo_info(pool)
        top = sorted(info["scores"].items(), key=lambda kv: -kv[1])[:4]
        label = {"coding": "编码", "reasoning": "推理", "writing": "写作", "chinese": "中文",
                 "math": "数学", "speed": "速度", "cost": "成本", "context": "上下文"}
        parts = " · ".join(f"{label.get(k, k)} {v}" for k, v in top)
        return f"{len(pool)} 个模型 · 总分 {info['total']} · {parts}"


__all__ = ["ModelStore", "ModelEntry", "PROVIDERS", "STORE_PATH", "DIM_KEYS", "total_of"]
