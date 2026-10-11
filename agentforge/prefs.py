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

# 权限值 → 给人看的中文名（前端也会用，保持一致）
PERMISSION_LABEL = {"full": "完全访问权限", "default": "默认权限"}


class PrefsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> dict:
        data = {"permission": DEFAULT_PERMISSION}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                for key in ("permission",):
                    if key in raw:
                        data[key] = raw[key]
        if data.get("permission") not in PERMISSIONS:
            data["permission"] = DEFAULT_PERMISSION
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

    def snapshot(self) -> dict:
        with self._lock:
            perm = self._data["permission"]
            return {
                "permission": perm,
                "label": PERMISSION_LABEL.get(perm, perm),
                "allow_write": perm == "full",
            }


__all__ = ["PrefsStore", "DEFAULT_PERMISSION", "PERMISSIONS", "PERMISSION_LABEL"]
