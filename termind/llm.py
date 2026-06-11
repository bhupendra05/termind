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


def list_models() -> list:
    """Names of all models pulled on the local Ollama server."""
    try:
        with urllib.request.urlopen(HOST + "/api/tags", timeout=2) as r:  # noqa: S310
            return [str(m.get("name", "")) for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


def model_available(name: str = None) -> bool:
    """True only if the given (or configured) model is actually pulled on the server."""
    base = (name or MODEL).split(":")[0]
    return any(m.split(":")[0] == base for m in list_models())


def embed(texts: list) -> list:
    """Real embedding vectors via Ollama /api/embed; [] if unavailable (caller falls back)."""
    model = os.environ.get("TERMIND_EMBED_MODEL", MODEL)
    try:
        req = urllib.request.Request(HOST + "/api/embed",
                                     data=json.dumps({"model": model, "input": texts}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return json.loads(r.read()).get("embeddings", [])
    except Exception:
        return []


def claude_chat(messages: list) -> str:
    """Escalate a hard question to Claude (only if the user set ANTHROPIC_API_KEY)."""
    key = os.environ["ANTHROPIC_API_KEY"]
    sys_txt = " ".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    body = {"model": os.environ.get("TERMIND_CLOUD_MODEL", "claude-sonnet-4-6"),
            "max_tokens": 1024, "system": sys_txt, "messages": rest}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
        return json.loads(r.read())["content"][0]["text"]


def chat(messages: list, fmt_json: bool = False, model: str = None) -> str:
    # keep_alive keeps the model warm in RAM between calls → much faster follow-ups
    payload = {"model": model or MODEL, "messages": messages, "stream": False,
               "keep_alive": "15m"}
    if fmt_json:
        payload["format"] = "json"
    req = urllib.request.Request(HOST + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
            return json.loads(r.read())["message"]["content"]
    except urllib.error.HTTPError as e:
        name = model or MODEL
        if e.code == 404:  # server is up but the model was never pulled
            raise RuntimeError(f"model '{name}' is not on the Ollama server yet — "
                               f"run:  ollama pull {name}") from e
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
