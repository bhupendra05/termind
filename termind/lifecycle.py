"""Workspace lifecycle & isolation — termind v2.0.

Termind is meant to be a disposable, modular workspace: everything it installs or downloads
(venvs, DB drivers, model files, scratch DBs) is tracked in a MANIFEST so the whole thing can be
removed cleanly, with nothing left scattered across your system. This module keeps that manifest
and computes the uninstall plan. It never deletes on its own — `cleanup_plan()` shows exactly what
*would* go, and removal is the caller's explicit, consented action.

Isolation guarantee: termind keeps its assets under TERMIND_HOME (default ~/.termind) and the
project's own `.venv`. The one asset it can't fully own is Ollama's model store — Ollama is a
shared daemon that writes to ~/.ollama by default — so we report how to relocate it
(OLLAMA_MODELS) rather than pretend we already contained it.
"""
from __future__ import annotations

import json
import os
import time

KINDS = ("venv", "driver", "db", "model-dir", "download", "workspace")


def home() -> str:
    return os.environ.get("TERMIND_HOME", os.path.expanduser("~/.termind"))


def _manifest_path() -> str:
    return os.path.join(home(), "manifest.json")


def _dir_size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Manifest:
    """The list of every asset termind created — the basis for a clean, complete uninstall."""

    def __init__(self):
        self.assets = self._load()

    def _load(self) -> list:
        try:
            with open(_manifest_path(), encoding="utf-8") as f:
                return list(json.load(f).get("assets", []))
        except (OSError, ValueError):
            return []

    def _save(self):
        os.makedirs(home(), exist_ok=True)
        with open(_manifest_path(), "w", encoding="utf-8") as f:
            json.dump({"assets": self.assets}, f, indent=1)

    def record(self, kind: str, path: str, note: str = "") -> dict:
        """Track an asset (idempotent on kind+path). Returns the entry."""
        path = os.path.abspath(os.path.expanduser(path))
        for a in self.assets:
            if a["kind"] == kind and a["path"] == path:
                return a
        entry = {"kind": kind if kind in KINDS else "download", "path": path,
                 "note": note[:160], "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.assets.append(entry)
        self._save()
        return entry

    def cleanup_plan(self) -> dict:
        """What a full uninstall would remove — with live existence + sizes. Deletes nothing."""
        items, total = [], 0
        for a in self.assets:
            exists = os.path.exists(a["path"])
            size = _dir_size(a["path"]) if exists else 0
            total += size
            items.append({**a, "exists": exists, "bytes": size})
        # the termind home itself (memory, ledger, manifest) is always part of a full removal
        hb = _dir_size(home()) if os.path.exists(home()) else 0
        return {"home": home(), "home_bytes": hb, "assets": items,
                "asset_bytes": total, "total_bytes": total + hb, "count": len(items)}


def ollama_models_dir() -> dict:
    """Where Ollama stores models, and whether termind has isolated it."""
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        return {"path": env, "isolated": True,
                "note": "models are isolated to this termind-controlled directory"}
    return {"path": os.path.expanduser("~/.ollama/models"), "isolated": False,
            "note": ("Ollama stores models system-wide by default. To isolate them inside "
                     "termind, set OLLAMA_MODELS to a path under your workspace and restart the "
                     "Ollama daemon — termind can't relocate a shared daemon's files on its own.")}


def isolation_summary(manifest: Manifest) -> str:
    plan = manifest.cleanup_plan()
    mb = plan["total_bytes"] / 1e6
    om = ollama_models_dir()
    return (f"termind workspace: {home()} · tracked assets: {plan['count']} · "
            f"reclaimable on uninstall: {mb:.1f} MB · "
            f"models: {'isolated' if om['isolated'] else 'system-wide (~/.ollama)'}")
