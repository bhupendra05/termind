"""Local brain: Ollama via stdlib urllib, with an offline fallback so termind always runs."""
from __future__ import annotations

import json
import os
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("TERMIND_MODEL", "gemma3")


def ollama_available() -> bool:
    try:
        urllib.request.urlopen(HOST + "/api/tags", timeout=2)  # noqa: S310
        return True
    except Exception:
        return False


def model_available() -> bool:
    """True only if the configured MODEL is actually pulled on the server."""
    try:
        with urllib.request.urlopen(HOST + "/api/tags", timeout=2) as r:  # noqa: S310
            models = json.loads(r.read()).get("models", [])
        base = MODEL.split(":")[0]
        return any(str(m.get("name", "")).split(":")[0] == base for m in models)
    except Exception:
        return False


def chat(messages: list, fmt_json: bool = False) -> str:
    payload = {"model": MODEL, "messages": messages, "stream": False}
    if fmt_json:
        payload["format"] = "json"
    req = urllib.request.Request(HOST + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
            return json.loads(r.read())["message"]["content"]
    except urllib.error.HTTPError as e:
        if e.code == 404:  # server is up but the model was never pulled
            raise RuntimeError(f"model '{MODEL}' is not on the Ollama server yet — "
                               f"run:  ollama pull {MODEL}") from e
        raise RuntimeError(f"Ollama error (HTTP {e.code}) — is the model loaded?") from e


def offline_chat(messages: list) -> str:
    """Offline stand-in so the REPL works before any model is installed."""
    last = messages[-1]["content"]
    return ("[offline brain] I can't reason without a model yet — run ./setup.sh to install "
            f"Ollama. Meanwhile /index and /ask still do real retrieval. You said: {last[:120]!r}")


def parse_action(text: str) -> dict:
    t = text.strip().strip("`")
    if t[:4].lower() == "json":
        t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return {"final": text.strip()[:300]}
