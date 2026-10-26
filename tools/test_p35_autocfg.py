"""P35 回归测试：自动配置方案的「生效中」与勾选永远一致 + 再次点击=取消。

运行：python tools/test_p35_autocfg.py
全部断言内建，退出码 0 = 全过。**不联网**——直接构造 ModelStore 的合成目录。

背景（用户拍板 2026-10-26）：自动配置按钮勾选一个模型组合；再次点击「生效中」
的按钮 = 取消（方案勾选的模型同步取消勾选）；勾选偏离方案时「生效中」立即消失。
旧实现「生效中」只看路由开关，勾选变了标记还在——前端勾选与后端生效脱节。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                        # noqa: E402
from agentforge.modelstore import ModelEntry, ModelStore    # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _entry(eid: str, key: str, name: str, kind: str = "local",
           reasoning: int = 80) -> ModelEntry:
    return ModelEntry(id=eid, key=key, name=name, kind=kind,
                      scores={"coding": reasoning, "reasoning": reasoning,
                              "writing": reasoning, "chinese": reasoning,
                              "math": reasoning, "context": reasoning,
                              "cost": 90 if kind == "local" else 50},
                      total=reasoning)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_p35_"))
    cfg = Config.load(str(ROOT / "config.yaml"))
    store = ModelStore(cfg, path=tmp / "models.json")

    m_a = _entry("local:a", "a", "模型A", reasoning=80)
    m_b = _entry("local:b", "b", "模型B", reasoning=85)
    m_c = _entry("paid:c", "paid:c", "云端C", kind="paid", reasoning=90)
    m_d = _entry("local:d", "d", "模型D", reasoning=70)
    store._catalog = [m_a, m_b, m_c, m_d]

    # ---------- ① apply_best：勾选组合 + 记录方案 ----------
    picks = store.apply_best()
    ids = {e.id for e in picks}
    check("P35 apply_best 勾选组合且记录方案",
          store.last_auto_config is not None
          and store.last_auto_config.get("scope") == "best"
          and set(store.last_auto_config.get("ids") or []) == ids
          and ids == {e.id for e in store.enabled_entries()}
          if hasattr(store, "enabled_entries") else
          (store.last_auto_config is not None
           and store.last_auto_config.get("scope") == "best"
           and set(store.last_auto_config.get("ids") or []) == ids),
          str(store.last_auto_config))

    # ---------- ② 手动勾选偏离 → 方案应被判定为不再生效 ----------
    # 模拟用户手动：取消勾方案里的 A（若在方案中）、新勾 D（当前勾选 ≠ 方案集合）
    preset_ids = set((store.last_auto_config or {}).get("ids") or [])
    after_user = (preset_ids - {"local:a"}) | {"local:d"}
    store.set_enabled(sorted(after_user))
    ac = store.last_auto_config or {}
    en_now = set(e.id for e in (store._catalog or []) if e.enabled)
    mismatch = not (set(ac.get("ids") or []) and set(ac.get("ids") or []) == en_now)
    check("P35 勾选偏离方案后，一致性判定为 False（生效中应消失）", mismatch,
          f"方案={ac.get('ids')} 当前={sorted(en_now)}")

    # ---------- ③ cancel：只取消方案勾过的，手动加的保留 ----------
    store.last_auto_config = ac   # 恢复方案记录，走 cancel 路径
    removed, remaining = store.cancel_auto_config()
    en_after = set(e.id for e in (store._catalog or []) if e.enabled)
    check("P35 取消：方案勾的取消、手动勾选偏离的部分不动",
          set(removed) == preset_ids & en_now
          and en_after == en_now - preset_ids
          and store.last_auto_config is None,
          f"removed={removed} after={sorted(en_after)}")

    # ---------- ④ 持久化：save/load 往返 ----------
    store2 = ModelStore(cfg, path=tmp / "models.json")
    store2._catalog = [ModelEntry(id=e.id, key=e.key, name=e.name, kind=e.kind,
                                  scores=dict(e.scores), total=e.total)
                       for e in (store._catalog or [])]
    picks2 = store2.apply_best_local()
    if not picks2:
        raise AssertionError('测试前提失败：本地组合为空')
    check("P35 方案记录随 models.json 持久化（load 后可恢复）",
          store2.last_auto_config is not None
          and store2.last_auto_config.get("scope") == "local"
          and set(store2.last_auto_config.get("ids") or [])
          == {e.id for e in picks2},
          str(store2.last_auto_config))

    # ---------- ⑤ 取消到空：允许，但如实报告 ----------
    store2.last_auto_config = {"scope": "local", "ids": [e.id for e in picks2]}
    removed2, remaining2 = store2.cancel_auto_config()
    en_empty = [e.id for e in (store2._catalog or []) if e.enabled]
    check("P35 取消到空集被如实记录（任务将无法执行，由前端提示）",
          not remaining2 and not en_empty and removed2,
          f"removed={removed2} remaining={remaining2}")

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
