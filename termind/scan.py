"""Proactive security scanner — termind v2.0.

The moment you point termind at a folder, it sweeps the files for the three things that bite
developers: leaked secrets, dangerous scripts, and insecure dependency setups. Pure stdlib
(regex + a file walk) — it runs locally, offline, and never uploads a byte. Findings come back
with the exact file, line, a REDACTED snippet, and a concrete fix the user can authorize.
"""
from __future__ import annotations

import os
import re

# directories and files that are noise (or huge) — never scanned
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
              ".next", ".cache", "target", ".idea", ".mypy_cache", ".pytest_cache"}
_BIN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar", ".whl",
            ".so", ".dylib", ".bin", ".onnx", ".gguf", ".woff", ".woff2", ".ico", ".mp4"}
_MAX_BYTES = 600_000          # skip files bigger than this — secrets don't hide in blobs


def _redact(s: str) -> str:
    s = s.strip().strip("'\"")
    return s if len(s) <= 10 else f"{s[:4]}…{s[-4:]} ({len(s)} chars)"


# (kind, compiled regex, severity, fix). group(0) is redacted in the report.
_SECRETS = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}"), "high",
     "Revoke this key in IAM now, rotate it, and load it from an env var / secrets manager."),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
     "high", "Remove the key from the repo, rotate it, and add its path to .gitignore."),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}"),
     "high", "Revoke it at github.com/settings/tokens and store it as a repo/CI secret."),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "high",
     "Rotate the Slack token and move it to an environment variable."),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high",
     "Restrict and rotate the key in Google Cloud Console; never commit it."),
    ("Stripe secret key", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b"), "high",
     "Roll the key in the Stripe dashboard immediately and use an env var."),
    ("Hardcoded secret", re.compile(
        r"(?i)(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
     "medium", "Move this value to an environment variable or a local .env that is git-ignored."),
    ("Bearer token", re.compile(r"(?i)bearer\s+[a-z0-9._\-]{20,}"), "medium",
     "Don't hardcode bearer tokens — inject them at runtime from a secret store."),
]

_SCRIPTS = [
    ("Pipe-to-shell install", re.compile(r"(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash)"),
     "high", "Never pipe a remote URL straight into a shell — download, READ it, then run it."),
    ("Destructive rm", re.compile(r"\brm\s+-[rf]+\s+['\"]?(?:/|~|\$|\*)"), "high",
     "Guard this delete: pin an absolute path, add a confirm, and avoid variables that could be empty."),
    ("Decode-and-execute", re.compile(
        r"(?:base64\s+(?:-d|--decode)[^\n|]*\|\s*(?:sh|bash))|(?:eval|exec)\s*\(\s*(?:base64|bytes\.fromhex|__import__)"),
     "high", "Obfuscated decode-then-run is a classic dropper pattern — inspect what it decodes to."),
    ("Shell-injection risk", re.compile(r"(?:os\.system\s*\(|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True)"),
     "medium", "Avoid shell=True / os.system with interpolated input; pass an argument list instead."),
    ("World-writable chmod", re.compile(r"\bchmod\s+(?:-R\s+)?777\b"), "low",
     "777 makes the path writable by anyone — use the least permission that works (e.g. 755/644)."),
]

# insecure dependency setups, checked only in dependency manifests
_DEPS = [
    ("Insecure package index", re.compile(r"(?i)(--trusted-host|index-url\s*=?\s*http://|http://[^\s'\"]+/simple)"),
     "medium", "Use https for your package index; --trusted-host disables TLS verification."),
    ("Unverified git dependency", re.compile(r"git\+http://"), "medium",
     "Pull git dependencies over https (git+https://), not http."),
]
_DEP_FILES = {"requirements.txt", "requirements-dev.txt", "pipfile", "pyproject.toml",
              "package.json", "package-lock.json", "yarn.lock", "gemfile", "go.mod"}


def scan_text(text: str, filename: str = "<text>") -> list:
    """Findings for one file's content. Returns [{severity, kind, file, line, snippet, fix}]."""
    findings = []
    is_dep = os.path.basename(filename).lower() in _DEP_FILES
    rules = _SECRETS + _SCRIPTS + (_DEPS if is_dep else [])
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:                       # minified/blob line → skip
            continue
        for kind, rx, sev, fix in rules:
            m = rx.search(line)
            if m:
                findings.append({"severity": sev, "kind": kind, "file": filename, "line": i,
                                 "snippet": _redact(m.group(0)), "fix": fix})
                break                               # one finding per line is enough
    return findings


def _is_binary(path: str) -> bool:
    if os.path.splitext(path)[1].lower() in _BIN_EXT:
        return True
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(2048)
    except OSError:
        return True


def scan_folder(path: str, max_files: int = 600) -> list:
    """Walk a folder (skipping junk dirs + binaries) and return all findings, worst first."""
    findings, seen = [], 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if seen >= max_files:
                break
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > _MAX_BYTES or _is_binary(fp):
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            seen += 1
            rel = os.path.relpath(fp, path)
            findings += scan_text(text, rel)
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["file"], f["line"]))
    return findings


def summary(findings: list) -> dict:
    return {
        "total": len(findings),
        "high": sum(1 for f in findings if f["severity"] == "high"),
        "medium": sum(1 for f in findings if f["severity"] == "medium"),
        "low": sum(1 for f in findings if f["severity"] == "low"),
        "clean": not findings,
    }
