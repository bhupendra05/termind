"""Toolchain auto-detection — termind learns what languages YOUR machine speaks.

On first boot (and on refresh) it probes the PATH for common runtimes, records the exact
command to invoke each one (python vs python3!), its version, and where it lives. The code
agent uses this so it never tells a Mac user to "install Python" again.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time

# language → candidate commands, in preference order
CANDIDATES = {
    "python": ["python3", "python"],
    "node": ["node"],
    "npm": ["npm"],
    "go": ["go"],
    "rust": ["cargo", "rustc"],
    "java": ["java"],
    "ruby": ["ruby"],
    "php": ["php"],
    "swift": ["swift"],
    "git": ["git"],
    "docker": ["docker"],
}

_VERSION_FLAGS = {"go": "version", "java": "-version"}


# A probe whose output contains any of these means the binary is a stub / not usable
# (e.g. macOS ships a `java` shim that errors with "Unable to locate a Java Runtime").
_NOT_INSTALLED = ("not present", "unable to locate", "couldn't be completed",
                  "cannot be opened", "no such", "command not found", "not found")


def _version(cmd: str, lang: str):
    """The version string, or None if the binary is present-but-not-actually-usable."""
    flag = _VERSION_FLAGS.get(lang, "--version")
    try:
        r = subprocess.run([cmd, flag], capture_output=True, text=True, timeout=6)
    except Exception:
        return None
    # be defensive: a malformed probe result must never crash REPL boot
    text = ((getattr(r, "stdout", "") or getattr(r, "stderr", "")) or "").strip()
    m = re.search(r"\d+(?:\.\d+)+", text)
    if m:
        return m.group(0)
    if any(x in text.lower() for x in _NOT_INSTALLED) or getattr(r, "returncode", 0) != 0:
        return None
    return (text.splitlines() or ["?"])[0][:30]


def detect() -> dict:
    """Probe the machine: {lang: {cmd, version, path}} for everything truly installed."""
    found = {}
    for lang, cands in CANDIDATES.items():
        for c in cands:
            p = shutil.which(c)
            if not p:
                continue
            v = _version(c, lang)
            if v is None:                 # present on PATH but a stub → skip it
                continue
            found[lang] = {"cmd": c, "version": v, "path": p}
            break
    found["_detected_at"] = time.time()
    return found


def summary(tc: dict) -> str:
    """One line for the agent's brain: 'python→python3 3.9.6 · node→node 20.1 …'"""
    return " · ".join(f"{k}→{v['cmd']} {v['version']}"
                      for k, v in tc.items() if not k.startswith("_"))
