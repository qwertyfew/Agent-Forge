"""用户偏好持久化：目前只存「新建任务的默认权限」。

为什么必须落盘：默认权限是**完全访问**，但用户在某个任务里把权限切回
「默认权限」之后，后续新建的任务应当沿用这个选择。如果只记在浏览器内存里，
刷新页面或换一个标签页就丢了。CLI、后台脚本发起的任务也读同一份偏好。

文件落在项目根目录的 .prefs.json（已加入 .gitignore）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

# full    = 完全访问：写文件、执行命令直接放行
# default = 默认权限：require_approval 里的工具需要人工确认
DEFAULT_PERMISSION = "full"
PERMISSIONS = ("full", "default")

# 悬停卡片的显示延迟（毫秒）：悬停够久才弹，扫过不算。默认 1.5s，
# 通用设置里可调；钳制范围防手滑填出 0ms（等于常驻遮挡）或 10 分钟
DEFAULT_TIP_DELAY = 1500
TIP_DELAY_MIN = 300
TIP_DELAY_MAX = 10000

# 权限值 → 给人看的中文名（前端也会用，保持一致）
PERMISSION_LABEL = {"full": "完全访问权限", "default": "默认权限"}


def _clamp_tip_delay(value) -> int:
    """悬停延迟钳制：非数字/越界都拉回合法区间，绝不存出离谱值。"""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_TIP_DELAY
    return max(TIP_DELAY_MIN, min(TIP_DELAY_MAX, v))


class PrefsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> dict:
        data = {"permission": DEFAULT_PERMISSION,
                "tip_delay": DEFAULT_TIP_DELAY}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                for key in ("permission", "tip_delay"):
                    if key in raw:
                        data[key] = raw[key]
        if data.get("permission") not in PERMISSIONS:
            data["permission"] = DEFAULT_PERMISSION
        data["tip_delay"] = _clamp_tip_delay(data.get("tip_delay"))
        return data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            pass  # 偏好写不进去不该拖垮任务

    # ---------- 读写 ----------
    @property
    def permission(self) -> str:
        with self._lock:
            return self._data["permission"]

    def set_permission(self, value: str) -> str:
        """写入新默认值，返回值是实际生效的值（非法值回落成 full）。"""
        v = str(value or "").strip().lower()
        if v not in PERMISSIONS:
            v = DEFAULT_PERMISSION
        with self._lock:
            self._data["permission"] = v
            self._save()
            return v

    @property
    def tip_delay(self) -> int:
        with self._lock:
            return self._data["tip_delay"]

    def set_tip_delay(self, value) -> int:
        """悬停卡片延迟（毫秒）。非法输入回落成默认 1500，返回实际生效值。"""
        with self._lock:
            self._data["tip_delay"] = _clamp_tip_delay(value)
            self._save()
            return self._data["tip_delay"]

    def snapshot(self) -> dict:
        with self._lock:
            perm = self._data["permission"]
            return {
                "permission": perm,
                "label": PERMISSION_LABEL.get(perm, perm),
                "allow_write": perm == "full",
                "tip_delay": self._data["tip_delay"],
            }


__all__ = ["PrefsStore", "DEFAULT_PERMISSION", "PERMISSIONS", "PERMISSION_LABEL",
           "DEFAULT_TIP_DELAY", "TIP_DELAY_MIN", "TIP_DELAY_MAX"]
