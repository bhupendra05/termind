"""Chunk a folder of text files for local RAG (stdlib only; nothing leaves the machine)."""
from __future__ import annotations

import os
from typing import Dict, List

TEXT_EXT = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".go", ".rs", ".json",
            ".toml", ".yaml", ".yml", ".cfg", ".ini", ".html", ".csv"}


def chunk_text(text: str, size: int = 220) -> List[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks or ([text.strip()] if text.strip() else [])


SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", "dist", "build",
             ".pytest_cache", ".mypy_cache", "site-packages", ".tox"}


def index_folder(folder: str, size: int = 220) -> List[Dict]:
    entries: List[Dict] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            src = os.path.relpath(path, folder)
            for i, c in enumerate(chunk_text(text, size)):
                entries.append({"key": f"{src}#{i}", "text": c, "source": src})
    return entries
