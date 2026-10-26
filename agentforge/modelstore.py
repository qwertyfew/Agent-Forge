"""可用模型库：AgentForge 能调用的所有模型的目录与使用状态。

三层概念要分清：

    可用模型目录（catalog）  所有能被调用的模型：config.yaml 里的本地模型 +
                             自动发现的 Ollama 模型 + 用户添加的付费模型
    启用组合（enabled）      用户勾选的那几个，真正参与任务执行的模型池
    最优组合（best）         按各维度得分算出来覆盖最全的一组，可一键应用

本地模型全部自动进入目录（用户不用手填）；付费模型由用户在设置页添加，
填好厂商 / 模型号 / API Key 之后自动成为可用模型，并参与评分与路由。

状态分两个文件落盘，**两个都不进版本库**——密钥和本机探测结果都不该跟着代码走：

    models.json       在线模型配置与 API Key —— 含密钥，只留本机；
                      仓库里放的是模板 models.example.json（api_key 留空）
    models.local.json 本地模型的自动探测结果（窗口该开多大）—— 只对探测时那台
                      机器（那台机器的模型与显存）成立
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ModelSpec, DEFAULT_CONFIG_PATH
from .scores import DIMENSIONS, DIM_KEYS, INACTIVE_DIMS, combo, lookup, total_of

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "models.json"
# 本地模型的自动配置（原生窗口等）单独存一份：每台机器上的模型与显存不同，
# 探测结果跟着代码走版本库只会误导（那边可能根本没装这个模型）。
# models.json 同样不进版本库——里面有在线模型的 API Key，一旦提交就永久留在 git 历史里；
# 仓库里只放模板 models.example.json（api_key 留空），照它复制一份再填密钥即可。
LOCAL_PATH = ROOT / "models.local.json"

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


# 自动发现的新模型默认开多大窗口。
# 上限不是随手定的：窗口越大 KV cache 越吃显存（gemma4:12b 开到 256K 时约占 13.8GB），
# 小显存机器上一个过大的默认值会把模型挤到 CPU 上跑，反而更慢。
# 想要更长，在 config.yaml 里显式声明该模型即可——显式配置的优先级高于自动值。
AUTO_CTX_CAP = 65536
DEFAULT_LOCAL_CTX = 8192


def auto_ctx(native: int) -> int:
    """按模型原生窗口挑一个稳妥的默认值：不超过上限，也不低于 4K。"""
    return max(4096, min(int(native or 0) or DEFAULT_LOCAL_CTX, AUTO_CTX_CAP))


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
    native_ctx: int = 0        # 模型原生的上下文长度（探测所得，仅自动发现的模型有）
    auto_ctx: bool = False     # 当前窗口是不是自动探测决定的（而不是 config 里写的）
    # 这台机器上现在能不能真的跑起来（本地=Ollama 装了没，付费=密钥填了没）
    available: bool = True

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
            "native_ctx": self.native_ctx,
            "auto_ctx": self.auto_ctx,
            "available": self.available,
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
        # 本地模型配置固定放在 models.json 旁边（测试传临时 path 时也跟着走）
        self.local_path = self.path.with_name("models.local.json")
        self.paid: list[dict] = []
        self.enabled_ids: list[str] | None = None   # None = 尚未初始化，默认全选
        self.budget: int = 3                        # 最优组合最多挑几个模型
        self.score_overrides: dict[str, dict] = {}   # 用户手动覆盖的维度分
        # P35：最后一次「自动配置」记录（scope + 该方案勾选的模型 id）。
        # 「生效中」标记与「再次点击=取消」都以此为对照——勾选偏离方案即不再生效。
        self.last_auto_config: dict | None = None
        # 自动发现的本地模型的自动配置：模型名 -> {num_ctx, native_ctx, probed_at}
        # 第一次见到某个模型时问一次 Ollama（原生窗口只有模型自己知道），
        # 结果存这里，以后不再重复问；用户手填 config.yaml 时以用户的为准。
        self.local_cfg: dict[str, dict] = {}
        # 本次 catalog() 里刚探测并写好配置的模型名，用于给用户一句反馈
        self.just_probed: list[str] = []
        self._catalog: list[ModelEntry] = []
        self.load()
        # 配置文件里显式声明的本地模型名（构造时的快照）。
        # catalog() 会把自动发现的模型也注册进 cfg.models，此后就再也分不清
        # 「配置声明的」和「自动发现的」了——而窗口来源与 auto_ctx 标记都要靠这个区分，
        # 所以在这里先记一份。
        self.declared: set[str] = {
            s.name for s in cfg.models.values() if getattr(s, "kind", "") != "paid"
        }

    # ------------------------------------------------------------ 持久化
    def _load_local(self) -> None:
        """读本地模型的自动配置（单独文件，不进版本库）。"""
        if not self.local_path.exists():
            return
        try:
            raw = json.loads(self.local_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, dict):
            self.local_cfg = dict(raw)

    def load(self) -> None:
        self._load_local()
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
        if isinstance(raw.get("last_auto_config"), dict):
            self.last_auto_config = dict(raw["last_auto_config"])
        # 兼容旧格式：早期把本地模型配置混在 models.json 里，读到就顺手迁走
        if not self.local_cfg and isinstance(raw.get("local_cfg"), dict):
            self.local_cfg = dict(raw["local_cfg"])
            self._save_auto_cfg()

    def save(self) -> None:
        """写 models.json。**不含**本地模型的自动配置——那部分在 models.local.json。"""
        data = {
            "version": 1,
            "enabled": [e.id for e in (self._catalog or self.catalog()) if e.enabled],
            "paid": self.paid,
            "budget": self.budget,
            "score_overrides": self.score_overrides,
            "last_auto_config": self.last_auto_config,
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _save_auto_cfg(self) -> None:
        """把自动探测到的本地模型配置写到 models.local.json。

        这个文件**故意不进版本库**：里面是"这台机器上这个模型的窗口该开多大"，
        换台机器可能连模型都没有。从 catalog() 里调用它还能避开 save()——
        save() 会反过来读 catalog()，那样就递归了。
        """
        try:
            self.local_path.write_text(
                json.dumps(self.local_cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

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
    def catalog(self, discovered: list[str] | None = None,
                probe=None) -> list[ModelEntry]:
        """构建完整目录，并把模型注册进 cfg（这样 agent.set_model 才能找到）。

        probe 传入时（形如 name -> {"native_ctx": int}），会对**首次见到**的本地
        模型问一次原生上下文长度并把结果缓存进 models.local.json，之后不再重复问。
        """
        entries: list[ModelEntry] = []

        # 1) config.yaml 里显式声明的本地模型（保留各自的调参）
        for key, spec in list(self.cfg.models.items()):
            if spec.kind == "paid":
                continue
            entries.append(self._local_entry(
                key, spec.name, spec.desc, spec,
                discovered=(spec.name not in self.declared)))

        # 2) Ollama 里发现但 config.yaml 没声明的模型：自动成为可用模型。
        #    首次见到时自动探一次原生窗口，省得用户自己去查模型文档再手填 num_ctx。
        known_names = {e.name for e in entries}
        fresh = [n for n in (discovered or []) if n not in known_names]
        if probe is not None:
            self.just_probed = []          # 每次都重置，提示只弹一次
        if fresh and probe is not None:
            changed = False
            for name in fresh:
                if name in self.local_cfg:
                    continue               # 探测过一次就够，用户改过也尊重用户
                info = probe(name) or {}
                native = int(info.get("native_ctx") or 0)
                if native > 0:
                    self.local_cfg[name] = {
                        "num_ctx": auto_ctx(native),
                        "native_ctx": native,
                    }
                    self.just_probed.append(name)
                    changed = True
            if changed:
                self._save_auto_cfg()
        for name in fresh:
            entries.append(
                self._local_entry(name, name, "自动发现的本地模型", None, discovered=True)
            )

        # 3) 用户添加的付费模型
        for item in self.paid:
            entries.append(self._paid_entry(item))

        # 启用状态：首次运行默认全选；之后以保存的列表为准
        enabled = self.enabled_ids
        for e in entries:
            e.enabled = True if enabled is None else (e.id in enabled)
        if enabled is None:
            self.enabled_ids = [e.id for e in entries]

        # 注册进 cfg，供 Agent / Router 使用；顺手标一下「这台机器能不能跑」。
        # have 为 None 表示这次没问过 Ollama（例如 save()/apply_best 等
        # 内部修改触发的 catalog() 重建）——此时**保留上一轮的判断**，别乐观重置：
        # 否则每次改配置都会把「未安装」悄悄洗成「已启用」，黄字绿字来回翻。
        # 上一轮也没有（首次启动且 Ollama 没应答）才默认 True，宁可真报错。
        have = None if discovered is None else set(discovered)
        prev_avail = {e.id: e.available for e in (self._catalog or [])}
        for e in entries:
            spec = self.cfg.register_model(self._spec_of(e))
            if e.kind == "local":
                if have is not None:
                    e.available = e.name in have
                else:
                    e.available = prev_avail.get(e.id, True)
            else:
                e.available = bool(getattr(spec, "api_key", ""))
            spec.available = e.available

        self._catalog = entries
        return entries

    def usable(self) -> list[ModelEntry]:
        """启用且这台机器上真能跑的模型——任务池的来源。

        比 enabled() 多一层「能不能跑」：勾了但本机没装的本地模型不该进池子，
        否则任务会在第一步就撞上 Ollama 的 model not found。
        """
        return [e for e in self._catalog if e.enabled and e.available]

    def fallback_pool(self) -> list[ModelEntry]:
        """任务池：优先用「勾选且能跑」的那组。

        如果那组是空的（典型场景：换台电脑 clone 下来，一个本地模型都还没 pull，
        enabled 里只剩勾着的本地模型），就退回到**所有能跑的模型**——付费模型
        只要密钥在就立刻能用，这正是「拿到仓库就能开工」该有的行为。
        """
        pool = self.usable()
        if pool:
            return pool
        spare = [e for e in self._catalog if e.available]
        return sorted(spare, key=lambda e: -e.total)

    def _local_entry(self, key: str, name: str, desc: str, spec: ModelSpec | None,
                     discovered: bool) -> ModelEntry:
        info = lookup(name, provider="ollama", kind="local")
        scores = self._apply_override(f"local:{key}", info.scores)
        # 窗口来源优先级：config.yaml 显式声明 > 自动探测缓存 > 兜底值
        auto = self.local_cfg.get(name) or {}
        win = (getattr(spec, "num_ctx", 0) or int(auto.get("num_ctx") or 0)
               or DEFAULT_LOCAL_CTX)
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
            context_window=win,
            discovered=discovered,
            native_ctx=int(auto.get("native_ctx") or 0),
            # 「自动配置」= 这个窗口确实来自自动探测，且模型不是配置文件里声明的。
            # 判据不看 spec.num_ctx——自动发现的模型被注册进 cfg 之后那个值就有了，
            # 会把这个标记错误地翻成 False。
            auto_ctx=bool(auto) and name not in self.declared,
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
            # 没写进 config.yaml 的模型：用自动探测到的窗口
            auto = self.local_cfg.get(e.name) or {}
            return ModelSpec(
                key=e.key, name=e.name,
                num_ctx=int(auto.get("num_ctx") or DEFAULT_LOCAL_CTX),
                temperature=0.6,
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

    def find_any(self, token: str) -> ModelEntry | None:
        """按 id 或 key 找目录项，**只查当前目录、不重新发现模型**。

        任务执行期间不能调 catalog()：那会带上 discovered=None 重建一遍，
        把「本机装没装」的判断抹掉（见 catalog 里的 have）。所以这里查缓存。
        """
        for e in self._catalog:
            if e.id == token or e.key == token:
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

    def remove_model(self, entry_id: str, delete_file: bool = True,
                     client=None) -> dict:
        """移除一个模型（付费在线模型或本地模型），供设置页的「移除」按钮调用。

        - 付费模型：等价于 remove_paid——从 models.json 的付费列表里删掉，密钥一并移除。
        - 本地模型：``delete_file=True`` 时让 Ollama 真正删掉它（`ollama rm`，
          释放磁盘上的权重文件）；再从配置文件的声明里摘掉（显式声明的本地模型
          若不摘，下次 catalog() 又会被加回来），最后清出可用目录与启用列表。

        返回 ``{"ok": bool, "kind": "local"/"paid", ...}``。免费发现得到的本地模型
        不在配置文件里声明，删除后只会从 Ollama 消失，不会再被重新发现。
        """
        entry = self.find(entry_id)
        if entry is None:
            return {"ok": False, "error": "未找到该模型"}

        if entry.kind == "paid":
            ok = self.remove_paid(entry_id)
            return {"ok": ok, "kind": "paid"}

        # ---- 本地模型 ----
        deleted_file = False
        if delete_file and client is not None:
            try:
                deleted_file = bool(client.delete_model(entry.name))
            except Exception:  # noqa: BLE001
                deleted_file = False

        # 从配置文件里摘掉声明。source_path 是本次实际加载的文件
        # （通常是 config.lanrun.yaml），DEFAULT_CONFIG_PATH 是仓库模板 config.yaml；
        # 两处都试一遍，哪份声明了就从哪份删掉。
        removed_config = False
        for p in (self.cfg.source_path, DEFAULT_CONFIG_PATH):
            if p and Config.remove_local_model_entry(p, entry.name):
                removed_config = True

        self.cfg.unregister_model(entry.key)
        if self.enabled_ids:
            self.enabled_ids = [i for i in self.enabled_ids if i != entry_id]

        self.catalog()
        self.save()
        return {
            "ok": True, "kind": "local",
            "deleted_file": deleted_file, "removed_config": removed_config,
        }

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
        池子一律排除 available=False 的模型——「最佳配置」不该启用本机跑不了的
        模型（没 pull 的本地模型、没密钥的付费模型）。注意付费模型的 available
        只代表「密钥在」，余额耗尽 / 接口报错要等运行期才暴露（那是另一层问题）。
        """
        pool = [e for e in self._catalog
                if e.available and (not local_only or e.kind == "local")]
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
            # P35：记住这套方案勾了哪些模型——「生效中」以当前勾选与方案一致为准，
            # 再次点击生效中的按钮 = 取消（勾选同步取消）。
            self.last_auto_config = {
                "scope": "local" if local_only else "best",
                "ids": [e.id for e in picks],
            }
            self.save()
        return picks

    def apply_best_local(self, budget: int | None = None) -> list[ModelEntry]:
        """一键应用「本地最佳配置」：启用组合收窄为本地最优的这一组。"""
        return self.apply_best(budget, local_only=True)

    def cancel_auto_config(self) -> tuple[list[str], list[str]]:
        """P35：取消最后一次自动配置——方案勾选的模型同步取消勾选。

        返回 (取消勾选的 id, 取消后仍勾选的 id)。没有方案记录时返回 ([], 当前勾选)。
        手动勾选偏离方案的（当前勾选 ⊃ 方案 id），只取消方案勾过的那部分，
        用户手动加的不动。"""
        ac = self.last_auto_config or {}
        preset_ids = set(ac.get("ids") or [])
        current = [e.id for e in (self._catalog or self.catalog()) if e.enabled]
        removed = [i for i in current if i in preset_ids]
        if removed:
            self.set_enabled([i for i in current if i not in preset_ids])
        self.last_auto_config = None
        self.save()
        remaining = [i for i in current if i not in preset_ids]
        return removed, remaining

    # ------------------------------------------------------------ 展示
    def snapshot(self, discovered: list[str] | None = None, probe=None) -> dict:
        entries = self.catalog(discovered, probe=probe)
        pool = [e for e in entries if e.enabled]
        usable = self.usable()
        best_local = self.best_local_combo()
        # 勾选的模型里有多少是本机真能跑的？前端靠它提示「勾了但没装」
        return {
            "dimensions": [{"key": k, "label": v} for k, v in DIMENSIONS
                           if k not in INACTIVE_DIMS],
            "models": [e.as_dict(self.cfg.models.get(e.key)) for e in entries],
            "enabled": [e.id for e in pool],
            "usable": [e.id for e in usable],
            "unavailable": [e.id for e in pool if not e.available],
            "fallback": not usable,   # 勾选的全不可用时，任务池会退回到所有能跑的模型
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
            # 本次刚自动配好的新模型，前端拿它给一句提示
            "auto_configured": list(self.just_probed),
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
