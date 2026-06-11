"""The termind REPL — a neon cyberpunk terminal agent, sandboxed and budgeted on AION.

Every /ask runs as an AION process granted only mem.search/mem.get with a credit budget;
chat goes straight to the local model. /status shows the audited spend.
"""
from __future__ import annotations

import json
import sys
import time

from aion import Capabilities, Kernel

from . import __version__
from .indexer import index_folder
from .llm import MODEL, chat, model_available, offline_chat, ollama_available, parse_action
from .store import load as store_load, save as store_save

# ── neon palette (256-color ANSI) ────────────────────────────────────────────
CY = "\033[38;5;51m"     # electric cyan
PK = "\033[38;5;198m"    # hot magenta
PU = "\033[38;5;141m"    # neon purple
GR = "\033[38;5;84m"     # matrix green
YL = "\033[38;5;226m"    # warning yellow
WH = "\033[97m"          # bright white
D  = "\033[2m"           # dim
B  = "\033[1m"           # bold
N  = "\033[0m"           # reset

ASK_SYSTEM = (
    "You answer using ONLY the user's indexed documents. Reply with EXACTLY ONE JSON object:\n"
    '  {"tool": "search", "args": {"query": "..."}}  or  {"final": "<answer citing the docs>"}\n'
    "Search first; never invent content."
)

BANNER = rf"""
{PK}{B}  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄{N}
{CY}{B}   ▀█▀ █▀▀ █▀█ █▀▄▀█ █ █▄░█ █▀▄{N}
{PU}{B}   ░█░ ██▄ █▀▄ █░▀░█ █ █░▀█ █▄▀{N}   {D}v{__version__} ⟨ AGENT TERMINAL ⟩{N}
{PK}{B}  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄{N}
"""

FEATURES = f"""{CY}╔═⟨ {WH}{B}SYSTEM CAPABILITIES{N}{CY} ⟩═══════════════════════════════════════╗{N}
{CY}║{N}  {GR}◉{N} {WH}just type{N}        {D}»{N} neural chat with local core {PU}⟨{MODEL}⟩{N}
{CY}║{N}  {GR}◉{N} {WH}/index <folder>{N}  {D}»{N} absorb your files into agent memory
{CY}║{N}  {GR}◉{N} {WH}/ask <question>{N}  {D}»{N} query YOUR data · answers cite sources
{CY}║{N}  {GR}◉{N} {WH}/remember <fact>{N} {D}»{N} teach it about you · survives restarts
{CY}║{N}  {GR}◉{N} {WH}/recall <query>{N}  {D}»{N} search everything it remembers
{CY}║{N}  {GR}◉{N} {WH}/status{N}          {D}»{N} core · credits burned · sandbox audit
{CY}║{N}  {GR}◉{N} {WH}/help{N}  {D}» this panel{N}      {GR}◉{N} {WH}/exit{N}  {D}» jack out{N}
{CY}╚════════════════════════════════════════════════════════════════╝{N}
   {PK}▸{N} {D}PRIVATE{N} {PK}▸{N} {D}$0/QUERY{N} {PK}▸{N} {D}SANDBOXED + BUDGETED BY THE AION KERNEL{N}
"""

BOOT = [
    ("AION kernel", "online"),
    ("capability sandbox", "armed"),
    ("credit governor", "enforcing"),
    ("semantic memory", "mounted"),
]


def _boot(live: bool, server: bool) -> None:
    print(f"{D}  initializing…{N}")
    for name, state in BOOT:
        time.sleep(0.12)
        print(f"  {GR}▸{N} {name:<20} {CY}[{state.upper()}]{N}")
    time.sleep(0.12)
    if live:
        core = f"{GR}[ONLINE · LOCAL]{N}"
    elif server:
        core = f"{YL}[SERVER UP · NO MODEL — run: ollama pull {MODEL}]{N}"
    else:
        core = f"{YL}[OFFLINE BRAIN — ./setup.sh to install]{N}"
    print(f"  {GR}▸{N} {'neural core':<20} {core}\n")


def _panel(title: str, body: str, color: str = PU) -> str:
    return (f"{color}┌─⟨ {WH}{B}{title}{N}{color} ⟩{'─' * max(2, 58 - len(title))}{N}\n"
            f"{color}│{N} {body}\n{color}└{'─' * 64}{N}")


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
        self.server = ollama_available() if live is None else live
        # "live" requires the server AND the model — a running server with no model pulled
        # must not pretend to be online (it would 404 on the first chat).
        self.live = (self.server and model_available()) if live is None else live
        self.history = []
        self.chunks = 0
        self.spent = 0.0
        self.denied = 0
        # persistent memory: reload facts + indexed docs from the last session
        self.store = store_load()
        for i, fact in enumerate(self.store["facts"]):
            self.k.syscall("mem.put", key=f"fact#{i}", value=fact, tags=["fact"])
        for key, e in self.store["docs"].items():
            self.k.syscall("mem.put", key=key, value=e["value"], tags=e.get("tags", []))
        self.chunks = len(self.store["docs"])

    def do_index(self, folder: str) -> str:
        entries = index_folder(folder)
        for e in entries:
            self.k.syscall("mem.put", key=e["key"], value=e["text"], tags=[e["source"]])
            self.store["docs"][e["key"]] = {"value": e["text"], "tags": [e["source"]]}
        store_save(self.store)  # indexed docs survive restarts
        self.chunks += len(entries)
        srcs = len({e["source"] for e in entries})
        return f"indexed {srcs} files → {len(entries)} chunks (all local · remembered)"

    def do_remember(self, fact: str) -> str:
        self.store["facts"].append(fact)
        store_save(self.store)
        self.k.syscall("mem.put", key=f"fact#{len(self.store['facts'])-1}",
                       value=fact, tags=["fact"])
        return f"remembered ({len(self.store['facts'])} facts on file): {fact}"

    def do_recall(self, query: str) -> str:
        hits = self.k.syscall("mem.search", query=query, top_k=3)["result"]
        if not hits:
            return "no memories match."
        return "\n".join(f"{h['score']:.2f}  [{h['key']}]  "
                         + " ".join(str(h['value']).split())[:160] for h in hits)

    def do_ask(self, q: str) -> str:
        think = (lambda m: chat(m, fmt_json=True)) if self.live else offline_ask_think
        pid = self.k.spawn("termind-ask", fn=_ask_agent, args=(q, think),
                           caps=Capabilities(["mem.search", "mem.get"]), budget=5.0)
        self.k.run()
        p = self.k.processes[pid]
        self.spent += p.meter.credits
        self.denied = self.k.meter.denied
        return p.result if p.state.value == "done" else f"(error: {p.error})"

    def chat_messages(self, text: str) -> list:
        """System prompt carries the remembered facts — the model knows who it's talking to."""
        sys = ("You are termind, the user's private local agent running in their terminal. "
               "Be concise and direct.")
        if self.store["facts"]:
            sys += " Known facts about your user: " + "; ".join(self.store["facts"])
        return [{"role": "system", "content": sys}] + self.history + [
            {"role": "user", "content": text}]

    def do_chat(self, text: str) -> str:
        msgs = self.chat_messages(text)
        reply = chat(msgs) if self.live else offline_chat(msgs)
        self.history += [{"role": "user", "content": text},
                         {"role": "assistant", "content": reply}]
        return reply

    def do_status(self) -> str:
        if self.live:
            brain = f"{MODEL} (live, local)"
        elif getattr(self, "server", False):
            brain = f"server up, model missing (run: ollama pull {MODEL})"
        else:
            brain = "offline brain (run ./setup.sh)"
        return (f"brain: {brain} · facts remembered: {len(self.store['facts'])} · "
                f"indexed chunks: {self.chunks} · credits spent: {self.spent:.2f} · "
                f"denied by sandbox: {self.denied} · data off-machine: 0 bytes")

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
        if line.startswith("/remember"):
            f = line[9:].strip()
            return self.do_remember(f) if f else "usage: /remember <fact about you>"
        if line.startswith("/recall"):
            q = line[7:].strip()
            return self.do_recall(q) if q else "usage: /recall <query>"
        if line.startswith("/"):
            return f"unknown command {line.split()[0]} — try /help"
        return self.do_chat(line)

    # styled wrapper around handle() for the live REPL (tests use handle() directly)
    def render(self, line: str) -> str:
        out = self.handle(line)
        if not out:
            return ""
        s = line.strip()
        if s == "/help":
            return out
        if s == "/status":
            return _panel("SYSTEM STATUS", out.replace(" · ", f"\n{PU}│{N} {CY}▪{N} "), PU)
        if s.startswith("/index"):
            return _panel("MEMORY ABSORBED", f"{GR}{out}{N}", GR.replace("38;5;84", "38;5;84"))
        if s.startswith("/ask"):
            return _panel("AGENT RESPONSE", out, CY)
        if s.startswith("/remember"):
            return _panel("MEMORY WRITTEN", f"{GR}{out}{N}", PU)
        if s.startswith("/recall"):
            return _panel("MEMORY RECALL", out.replace("\n", f"\n{PU}│{N} "), PU)
        if s.startswith("/"):
            return f"{YL}{out}{N}"
        return _panel("NEURAL CORE", out, PK)


def run() -> int:
    s = Session()
    print(BANNER)
    _boot(s.live, getattr(s, "server", False))
    if s.store["facts"] or s.store["docs"]:
        print(f"  {PU}◆{N} {D}memory restored:{N} {WH}{len(s.store['facts'])}{N}{D} facts · "
              f"{N}{WH}{len(s.store['docs'])}{N}{D} doc chunks from previous sessions{N}\n")
    print(FEATURES)
    while True:
        try:
            line = input(f"{PK}{B}⟦{N}{CY}termind{N}{PK}{B}⟧{N} {GR}❯{N} ")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{D}link severed.{N}")
            return 0
        try:
            out = s.render(line)
        except SystemExit:
            print(f"{PU}◢ jacking out… session closed.{N}")
            return 0
        except Exception as e:  # the REPL never crashes
            out = f"{YL}⚠ error: {e}{N}"
        if out:
            print(f"\n{out}\n")
        sys.stdout.flush()
