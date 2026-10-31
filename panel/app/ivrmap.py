"""IVR map exported by the Asterisk config generator (ivr_map.json): node titles, keys, queues, staff.

Reloaded automatically when the file changes. Missing file → empty map (panel shows raw node ids).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import i18n


class IvrMap:
    def __init__(self, path: Path):
        self.path = path
        self._mtime = None
        self._data: dict = {}
        self._lock = threading.Lock()

    def data(self) -> dict:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return self._data
        if m != self._mtime:
            with self._lock:
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._mtime = m
                except (OSError, ValueError):
                    pass
        return self._data

    @property
    def nodes(self) -> dict:
        return self.data().get("nodes", {})

    @property
    def queues(self) -> dict:
        return self.data().get("queues", {})

    @property
    def users(self) -> dict:
        return self.data().get("users", {})

    def node_title(self, path: str | None) -> str:
        if not path:
            return "—"
        n = self.nodes.get(path)
        if not n:
            return path
        return n.get("title_en") if i18n.get() == "en" and n.get("title_en") else n.get("title", path)

    def queue_title(self, q: str | None) -> str:
        if not q:
            return "—"
        info = self.queues.get(q)
        return f"{info['name']}" if info else q

    def user_title(self, ext: str | None) -> str:
        if not ext:
            return "—"
        name = self.users.get(ext)
        return f"{ext} · {name}" if name else ext

    def ext_by_name(self, name: str | None) -> str | None:
        """Queue member name (queue_log agent field) → extension."""
        if not name:
            return None
        for ext, n in self.users.items():
            if n == name:
                return ext
        return None

    def key_text(self, path: str, key: str) -> str:
        """What a key does in a node, for the call timeline."""
        n = self.nodes.get(path, {})
        a = n.get("keys", {}).get(key)
        if not a:
            return ""
        kind = a.get("type")
        if kind == "menu":
            return "→ " + self.node_title(a.get("target"))
        if kind == "queue":
            return "→ " + self.queue_title(a.get("target"))
        return {"repeat": i18n.t("repeat"), "return": i18n.t("back"), "exit": i18n.t("exit from IVR")}.get(kind, kind)
