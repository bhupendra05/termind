"""The termind REPL — a neon terminal agent, sandboxed and budgeted on AION.

Every /ask runs as an AION process granted only mem.search/mem.get with a credit budget;
chat goes straight to the local model. /status shows the audited spend.
"""
from __future__ import annotations

import json

from aion import Capabilities, Kernel

from . import __version__
from .indexer import index_folder
from .llm import MODEL, chat, offline_chat, ollama_available, parse_action

C, M, G, Y, D, N = "\033[36m", "\033[35m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"

ASK_SYSTEM = (
    "You answer using ONLY the user's indexed documents. Reply with EXACTLY ONE JSON object:\n"
    '  {"tool": "search", "args": {"query": "..."}}  or  {"final": "<answer citing the docs>"}\n'
    "Search first; never invent content."
)

BANNER = rf"""{M}
  ▀█▀ █▀▀ █▀█ █▀▄▀█ █ █▄░█ █▀▄
  ░█░ ██▄ █▀▄ █░▀░█ █ █░▀█ █▄▀{N}  {D}v{__version__} · local agent · on AION{N}
"""

FEATURES = f"""{C}┌─ FEATURES ─────────────────────────────────────────────────┐{N}
{C}│{N}  just type        chat with your local model ({MODEL})
{C}│{N}  /index <folder>  index your notes/docs/code (stays local)
{C}│{N}  /ask <question>  answer from YOUR docs, with source cites
{C}│{N}  /status          model · credits spent · sandbox audit
{C}│{N}  /help            show this again        /exit  quit
{C}└────────────────────────────────────────────────────────────┘{N}
  {D}private · $0/query · sandboxed & budgeted by the AION kernel{N}
"""


def _ask_agent(sb, question: str, think) -> str:
    msgs = [{"role": "system", "content": ASK_SYSTEM}, {"role": "user", "content": question}]
    for _ in range(5):
        act = parse_action(think(msgs))
        if "final" in act:
            return act["final"]
        res = sb.syscall("mem.search", **{"top_k": 3, **(act.get("args") or {})})
        msgs += [{"role": "assistant", "content": json.dumps(act)},
                 {"role": "user", "content": "RESULT: " + json.dumps(res)}]
    return "(step limit reached)"


def offline_ask_think(msgs):
    """Offline /ask brain: real retrieval, canned phrasing — cites the top passage."""
    if not any(m["role"] == "assistant" for m in msgs):
        return json.dumps({"tool": "search", "args": {"query": msgs[1]["content"]}})
    try:
        hits = json.loads(msgs[-1]["content"].split("RESULT:", 1)[1])["result"]
    except Exception:
        hits = []
    if not hits:
        return json.dumps({"final": "Nothing relevant in your indexed docs."})
    top = hits[0]
    passage = " ".join(str(top["value"]).split())[:300]
    return json.dumps({"final": f"From your notes ({top['key']}): {passage}"})


class Session:
    def __init__(self, kernel: Kernel = None, live: bool = None):
        self.k = kernel or Kernel()
        self.live = ollama_available() if live is None else live
        self.history = []
        self.chunks = 0
        self.spent = 0.0
        self.denied = 0

    def do_index(self, folder: str) -> str:
        entries = index_folder(folder)
        for e in entries:
            self.k.syscall("mem.put", key=e["key"], value=e["text"], tags=[e["source"]])
        self.chunks += len(entries)
        srcs = len({e["source"] for e in entries})
        return f"indexed {srcs} files → {len(entries)} chunks (all local)"

    def do_ask(self, q: str) -> str:
        think = (lambda m: chat(m, fmt_json=True)) if self.live else offline_ask_think
        pid = self.k.spawn("termind-ask", fn=_ask_agent, args=(q, think),
                           caps=Capabilities(["mem.search", "mem.get"]), budget=5.0)
        self.k.run()
        p = self.k.processes[pid]
        self.spent += p.meter.credits
        self.denied = self.k.meter.denied
        return p.result if p.state.value == "done" else f"(error: {p.error})"

    def do_chat(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        reply = chat(self.history) if self.live else offline_chat(self.history)
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def do_status(self) -> str:
        brain = f"{MODEL} (live, local)" if self.live else "offline brain (run ./setup.sh)"
        return (f"brain: {brain} · indexed chunks: {self.chunks} · "
                f"credits spent: {self.spent:.2f} · denied by sandbox: {self.denied} · "
                f"data off-machine: 0 bytes")

    def handle(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        if line in ("/exit", "/quit"):
            raise SystemExit(0)
        if line == "/help":
            return FEATURES
        if line == "/status":
            return self.do_status()
        if line.startswith("/index"):
            arg = line[6:].strip() or "."
            return self.do_index(arg)
        if line.startswith("/ask"):
            q = line[4:].strip()
            return self.do_ask(q) if q else "usage: /ask <question>"
        if line.startswith("/"):
            return f"unknown command {line.split()[0]} — try /help"
        return self.do_chat(line)


def run() -> int:
    s = Session()
    print(BANNER)
    print(FEATURES)
    if not s.live:
        print(f"  {Y}⚠ Ollama not detected — chatting uses the offline brain. ./setup.sh fixes this.{N}\n")
    while True:
        try:
            line = input(f"{M}termind{N} {C}❯{N} ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            out = s.handle(line)
        except SystemExit:
            print(f"{D}bye.{N}")
            return 0
        except Exception as e:  # the REPL never crashes
            out = f"{Y}error: {e}{N}"
        if out:
            print(f"\n{out}\n")
