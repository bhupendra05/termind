"""The termind REPL — a neon cyberpunk terminal agent, sandboxed and budgeted on AION.

Every /ask runs as an AION process granted only mem.search/mem.get with a credit budget;
chat goes straight to the local model. /status shows the audited spend.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time

from aion import Capabilities, Kernel

from . import __version__
from .indexer import index_folder
from .llm import (MODEL, chat, claude_chat, embed, model_available, offline_chat,
                  ollama_available, parse_action)
from .store import load as store_load, save as store_save

# Auto-memory: only when a SENTENCE STARTS with a self-statement — "should i use X?" must
# not become a remembered "fact".
AUTO_FACT = re.compile(
    r"(?:^|[.!?]\s+)(i am|i'm|my name is|i work|i live|i like|i prefer|i build|call me)\b", re.I)

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
{CY}║{N}  {GR}◉{N} {WH}/think <hard q>{N}  {D}»{N} escalate: big model › cloud › deep local
{CY}║{N}  {GR}◉{N} {WH}/do <task>{N}       {D}»{N} proposes a shell command · runs on YOUR y/N
{CY}║{N}  {GR}◉{N} {WH}/build <idea>{N}    {D}»{N} scaffold a project · write code · open VS Code
{CY}║{N}  {GR}◉{N} {WH}/write <file> <spec>{N} {D}» generate one file (preview + y/N){N}
{CY}║{N}  {GR}◉{N} {WH}/mkdir <path>{N}  {D}» create folder{N}   {GR}◉{N} {WH}/code <path>{N}  {D}» open VS Code{N}
{CY}║{N}  {GR}◉{N} {WH}/remember <fact>{N} {D}»{N} teach it about you · survives restarts
{CY}║{N}  {GR}◉{N} {WH}/recall <query>{N}  {D}»{N} embedding search over all memories
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
        self.history = list(self.store["history"])  # conversation survives restarts too
        self.actions = 0

    def do_index(self, folder: str) -> str:
        entries = index_folder(folder)
        for e in entries:
            self.k.syscall("mem.put", key=e["key"], value=e["text"], tags=[e["source"]])
            self.store["docs"][e["key"]] = {"value": e["text"], "tags": [e["source"]]}
        store_save(self.store)  # indexed docs survive restarts
        self.chunks += len(entries)
        srcs = len({e["source"] for e in entries})
        return f"indexed {srcs} files → {len(entries)} chunks (all local · remembered)"

    def do_remember(self, fact: str, auto: bool = False) -> str:
        key = f"fact#{len(self.store['facts'])}"
        self.store["facts"].append(fact)
        self.k.syscall("mem.put", key=key, value=fact, tags=["fact"])
        if self.live:  # upgrade memory with a real embedding when a model is available
            vecs = embed([fact])
            if vecs:
                self.store["vecs"][key] = vecs[0]
        store_save(self.store)
        tag = "auto-remembered" if auto else "remembered"
        return f"{tag} ({len(self.store['facts'])} facts on file): {fact}"

    @staticmethod
    def _cos(a, b) -> float:
        num = sum(x * y for x, y in zip(a, b))
        da = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        return num / da if da else 0.0

    def do_recall(self, query: str) -> str:
        # embeddings first (true semantic recall), lexical kernel search as fallback
        if self.live and self.store["vecs"]:
            qv = embed([query])
            if qv:
                scored = sorted(((self._cos(qv[0], v), k) for k, v in self.store["vecs"].items()),
                                reverse=True)[:3]
                vals = {**{f"fact#{i}": f for i, f in enumerate(self.store["facts"])},
                        **{k: e["value"] for k, e in self.store["docs"].items()}}
                hits = [(s, k, vals.get(k, "")) for s, k in scored if s > 0.3]
                if hits:
                    return "\n".join(f"{s:.2f}  [{k}]  " + " ".join(str(v).split())[:160]
                                     for s, k, v in hits)
        hits = self.k.syscall("mem.search", query=query, top_k=3)["result"]
        if not hits:
            return "no memories match."
        return "\n".join(f"{h['score']:.2f}  [{h['key']}]  "
                         + " ".join(str(h['value']).split())[:160] for h in hits)

    def do_think(self, q: str) -> str:
        """Escalation ladder for hard questions: big local model → Claude → deep local CoT."""
        big = os.environ.get("TERMIND_BIG_MODEL")
        if big and self.live:
            try:
                return chat(self.chat_messages(q + "\n\nThink step by step."), model=big)
            except RuntimeError:
                pass  # big model not pulled → next rung
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                return claude_chat(self.chat_messages(q))
            except Exception:
                pass  # cloud unreachable → next rung
        if self.live:
            return chat(self.chat_messages(
                "This is a HARD question. Reason step by step, check yourself, "
                "then give the answer:\n" + q))
        return offline_chat(self.chat_messages(q))

    # ── builder powers: folders, code files, whole projects, VS Code ─────────
    @staticmethod
    def _strip_fences(t: str) -> str:
        t = t.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        return t.strip() + "\n"

    def do_mkdir(self, path: str) -> str:
        full = os.path.expanduser(path)
        os.makedirs(full, exist_ok=True)
        self.actions += 1
        return f"created folder: {full}"

    def do_code(self, path: str) -> str:
        full = os.path.expanduser(path or ".")
        for cmd in (["code", full], ["open", "-a", "Visual Studio Code", full]):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=20)
                self.actions += 1
                return f"opened in VS Code: {full}"
            except Exception:
                continue
        return "couldn't launch VS Code — install it (or its 'code' CLI) first"

    def do_write(self, file: str, spec: str, confirm=None) -> str:
        if not self.live:
            return "code generation needs a live model — install Ollama (./setup.sh)"
        content = self._strip_fences(chat([
            {"role": "system", "content":
             "You write complete, working file contents. Reply ONLY with the raw file "
             "content — no markdown fences, no commentary."},
            {"role": "user", "content": f"Write the file {file}. It should: {spec}"}]))
        preview = "\n".join(content.splitlines()[:15])
        print(f"\n  {YL}⚡ will write {WH}{file}{N} {D}({len(content.splitlines())} lines){N}\n"
              f"{D}{preview}{N}\n")
        if str((confirm or input)(f"  {PK}write it? [y/N]{N} ")).strip().lower() != "y":
            return "aborted — nothing was written."
        full = os.path.expanduser(file)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        self.actions += 1
        return f"wrote {full} ({len(content.splitlines())} lines) — open it:  /code {file}"

    def do_build(self, idea: str, confirm=None, open_editor: bool = True) -> str:
        if not self.live:
            return "project building needs a live model — install Ollama (./setup.sh)"
        plan = parse_action(chat([
            {"role": "system", "content":
             'Scaffold a SMALL starter project (2-4 short files). Reply with EXACTLY '
             '{"folder": "<kebab-name>", "files": {"<relative path>": "<full file content>"}}. '
             "Include a README.md. Keep files short and working."},
            {"role": "user", "content": idea}], fmt_json=True))
        folder, files = plan.get("folder"), plan.get("files") or {}
        if not folder or not files:
            return "couldn't plan that project — try rephrasing."
        print(f"\n  {YL}⚡ will create {WH}{folder}/{N} {D}with {len(files)} files:{N} "
              + ", ".join(files) + "\n")
        if str((confirm or input)(f"  {PK}build it? [y/N]{N} ")).strip().lower() != "y":
            return "aborted — nothing was created."
        root = os.path.expanduser(folder)
        for rel, content in files.items():
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(self._strip_fences(str(content)))
        self.actions += 1
        opened = f" · {self.do_code(root)}" if open_editor else ""
        return f"built {root}/ with {len(files)} files{opened}"

    def do_action(self, task: str, confirm=None) -> str:
        """Operator mode: the model proposes ONE shell command; runs only on your explicit y."""
        if not self.live:
            return "operator mode needs a live model — install Ollama (./setup.sh)"
        act = parse_action(chat([
            {"role": "system", "content":
             'Propose ONE safe macOS shell command for the task. Reply with EXACTLY '
             '{"cmd": "<command>", "why": "<one line>"}. Never propose destructive commands.'},
            {"role": "user", "content": task}], fmt_json=True))
        cmd = act.get("cmd", "")
        if not cmd:
            return "couldn't form a command for that."
        print(f"\n  {YL}⚡ proposed:{N} {WH}{cmd}{N}\n  {D}{act.get('why', '')}{N}")
        ans = (confirm or input)(f"  {PK}execute? [y/N]{N} ")
        if str(ans).strip().lower() != "y":
            return "aborted — nothing was run."
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            self.actions += 1
            out = (r.stdout or r.stderr or "(no output)").strip()[:1500]
            return f"$ {cmd}\n{out}"
        except subprocess.TimeoutExpired:
            return "command timed out (60s cap)."

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
        sys = ("You are termind, a private local AI agent running in the user's terminal. "
               "The HUMAN typing to you is your user — a separate person, not you. "
               "Be concise and direct.")
        if self.store["facts"]:
            sys += (" Facts the USER has told you about THEMSELVES (when they ask 'who am I' "
                    "or about their identity, answer from these): "
                    + "; ".join(self.store["facts"])
                    + ". Never confuse yourself (termind, the agent) with the user.")
        return [{"role": "system", "content": sys}] + self.history + [
            {"role": "user", "content": text}]

    def do_chat(self, text: str) -> str:
        msgs = self.chat_messages(text)
        reply = chat(msgs) if self.live else offline_chat(msgs)
        self.history += [{"role": "user", "content": text},
                         {"role": "assistant", "content": reply}]
        self.store["history"] = self.history[-20:]   # conversation survives restarts
        note = ""
        if AUTO_FACT.search(text) and text not in self.store["facts"]:
            self.do_remember(text, auto=True)        # auto-memory: it learns you from chat
            note = f"\n{D}◆ auto-remembered this about you{N}"
        store_save(self.store)
        return reply + note

    def do_status(self) -> str:
        if self.live:
            brain = f"{MODEL} (live, local)"
        elif getattr(self, "server", False):
            brain = f"server up, model missing (run: ollama pull {MODEL})"
        else:
            brain = "offline brain (run ./setup.sh)"
        return (f"brain: {brain} · facts remembered: {len(self.store['facts'])} · "
                f"indexed chunks: {self.chunks} · chat turns kept: {len(self.history)} · "
                f"actions run: {getattr(self, 'actions', 0)} · credits spent: {self.spent:.2f} · "
                f"denied by sandbox: {self.denied} · data off-machine: 0 bytes")

    def handle(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        # bare "exit"/"quit"/"bye" must actually quit — never let the model fake an exit
        if line.lower().rstrip("!. ") in ("/exit", "/quit", "exit", "quit", "bye", "q"):
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
        if line.startswith("/think"):
            q = line[6:].strip()
            return self.do_think(q) if q else "usage: /think <hard question>"
        if line.startswith("/do"):
            t = line[3:].strip()
            return self.do_action(t) if t else "usage: /do <task in plain english>"
        if line.startswith("/mkdir"):
            p = line[6:].strip()
            return self.do_mkdir(p) if p else "usage: /mkdir <path>"
        if line.startswith("/code"):
            return self.do_code(line[5:].strip())
        if line.startswith("/write"):
            parts = line[6:].strip().split(None, 1)
            if len(parts) < 2:
                return "usage: /write <file> <what it should do>"
            return self.do_write(parts[0], parts[1])
        if line.startswith("/build"):
            i = line[6:].strip()
            return self.do_build(i) if i else "usage: /build <project idea>"
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
        if s.startswith("/think"):
            return _panel("DEEP THOUGHT", out.replace("\n", f"\n{CY}│{N} "), CY)
        if s.startswith("/do"):
            return _panel("OPERATOR", out.replace("\n", f"\n{YL}│{N} "), YL)
        if s.startswith(("/build", "/write", "/mkdir", "/code")):
            return _panel("BUILDER", out.replace("\n", f"\n{GR}│{N} "), GR)
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
