"""Agent Action Ledger — a tamper-evident, append-only record of every action the
code agent takes on your machine.

Built for the one place cloud AI can't go: environments where an autonomous agent must
be *auditable* before it's allowed near a codebase — who authorized each action, what it
touched, whether it was blocked, what it cost, and cryptographic proof the log wasn't
edited after the fact.

How the proof works (no external dependencies — stdlib hashlib/hmac):
  • append-only      — entries are written to one JSONL file, never rewritten in place.
  • hash-chained     — each entry stores sha256(prev_hash + entry_body). Altering ANY past
                       entry changes its hash, which breaks every hash after it. Anyone can
                       recompute the chain from the data alone — no key needed. This is the
                       tamper-evidence a security reviewer checks.
  • install-signed   — each entry also carries an HMAC-sha256 over its hash, keyed by a
                       per-install secret (~/.termind/ledger.key, 0600). Proves the entry
                       was produced by THIS install, not pasted in from elsewhere.

This is honest tamper-evidence + origin attribution, not PKI non-repudiation — the signing
key lives on the same machine. That's the right trust model for a local, single-user agent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

GENESIS = "0" * 64


def _home() -> str:
    return os.environ.get("TERMIND_HOME", os.path.expanduser("~/.termind"))


def _path() -> str:
    return os.path.join(_home(), "ledger.jsonl")


def _key(create: bool = False):
    """The per-install signing secret. Created once (0600) on first write; None if absent."""
    kp = os.path.join(_home(), "ledger.key")
    if not os.path.exists(kp):
        if not create:
            return None
        os.makedirs(_home(), exist_ok=True)
        with open(kp, "w") as f:
            f.write(os.urandom(32).hex())
        try:
            os.chmod(kp, 0o600)
        except OSError:
            pass
    with open(kp) as f:
        return bytes.fromhex(f.read().strip())


def _canonical(obj: dict) -> str:
    """Deterministic JSON (sorted keys, no spaces) so a hash is stable everywhere."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _body_of(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in ("prev", "hash", "sig")}


def _chain_hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(body)).encode()).hexdigest()


class Ledger:
    """Append-only, hash-chained log of agent actions. One JSONL file under TERMIND_HOME."""

    def __init__(self):
        self.entries = self._load()

    def _load(self) -> list:
        out = []
        try:
            with open(_path()) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            pass
        return out

    @property
    def _last_hash(self) -> str:
        return self.entries[-1]["hash"] if self.entries else GENESIS

    def record(self, *, session: str, tool: str, target: str, outcome: str,
               consent: str = "", bytes_written: int = 0, detail: str = "") -> dict:
        """Seal one action into the chain and append it to disk. Returns the entry."""
        body = {
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "session": str(session or "default"),
            "tool": str(tool),
            "target": str(target)[:300],
            "outcome": str(outcome),            # "ok" | "fail" | "blocked"
            "consent": str(consent)[:300],      # the user message that authorized this action
            "bytes": int(bytes_written or 0),
            "detail": str(detail)[:300],
        }
        prev = self._last_hash
        h = _chain_hash(prev, body)
        sig = hmac.new(_key(create=True), h.encode(), hashlib.sha256).hexdigest()
        entry = {**body, "prev": prev, "hash": h, "sig": sig}
        os.makedirs(_home(), exist_ok=True)
        with open(_path(), "a") as f:
            f.write(_canonical(entry) + "\n")
        self.entries.append(entry)
        return entry

    def verify(self) -> dict:
        """Recompute the chain; report the first tampered entry, if any.

        The chain check needs no key, so an auditor can run it on an exported file. The
        signature check runs only when the install key is present (i.e. on the origin)."""
        key = _key(create=False)
        prev = GENESIS
        for i, e in enumerate(self.entries):
            expect = _chain_hash(prev, _body_of(e))
            if e.get("prev") != prev or e.get("hash") != expect:
                return {"ok": False, "chain_ok": False, "sig_ok": None,
                        "broken_at": i, "count": len(self.entries)}
            if key is not None:
                want = hmac.new(key, expect.encode(), hashlib.sha256).hexdigest()
                if e.get("sig") != want:
                    return {"ok": False, "chain_ok": True, "sig_ok": False,
                            "broken_at": i, "count": len(self.entries)}
            prev = e["hash"]
        return {"ok": True, "chain_ok": True, "sig_ok": (key is not None or None),
                "broken_at": None, "count": len(self.entries)}

    def summary(self) -> dict:
        v = self.verify()
        return {
            "count": len(self.entries),
            "ok": sum(1 for e in self.entries if e.get("outcome") == "ok"),
            "fail": sum(1 for e in self.entries if e.get("outcome") == "fail"),
            "blocked": sum(1 for e in self.entries if e.get("outcome") == "blocked"),
            "bytes": sum(int(e.get("bytes", 0)) for e in self.entries),
            "integrity": "verified" if v["ok"] else f"TAMPERED@{v['broken_at']}",
        }

    def tail(self, n: int = 25) -> list:
        return self.entries[-n:]

    def export(self) -> dict:
        """A clean, self-describing artifact a security reviewer can verify offline."""
        return {
            "tool": "termind",
            "artifact": "agent-action-ledger",
            "spec": "append-only JSONL; sha256 hash chain (keyless-verifiable) + per-install HMAC",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "integrity": self.verify(),
            "summary": self.summary(),
            "entries": self.entries,
        }
