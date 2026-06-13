"""Persistent memory — termind remembers you across sessions.

Everything lives in ONE local JSON file (~/.termind/memory.json by default; override with
TERMIND_HOME). Facts you teach it and docs you index are reloaded into AION's semantic
memory on every boot. Local file, never uploaded.
"""
from __future__ import annotations

import json
import os

EMPTY = {"facts": [], "docs": {}, "history": [], "vecs": {}, "model": None,
         "chats": {}, "active_chat": None, "profile": {}, "workspace": None, "agent_mode": "act", "toolchain": {}}


def store_path() -> str:
    root = os.environ.get("TERMIND_HOME", os.path.expanduser("~/.termind"))
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "memory.json")


def load() -> dict:
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"facts": list(data.get("facts", [])), "docs": dict(data.get("docs", {})),
                "history": list(data.get("history", [])), "vecs": dict(data.get("vecs", {})),
                "model": data.get("model"),
                "chats": dict(data.get("chats", {})),
                "active_chat": data.get("active_chat"),
                "profile": dict(data.get("profile", {})),
                "workspace": data.get("workspace"),
                "agent_mode": data.get("agent_mode", "act"),
                "toolchain": dict(data.get("toolchain", {}))}
    except Exception:
        return {"facts": [], "docs": {}, "history": [], "vecs": {}, "model": None,
                "chats": {}, "active_chat": None, "profile": {}, "workspace": None, "agent_mode": "act", "toolchain": {}}


def save(data: dict) -> None:
    with open(store_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
