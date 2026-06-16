"""The termind REPL — a neon cyberpunk terminal agent, sandboxed and budgeted on AION.

Every /ask runs as an AION process granted only mem.search/mem.get with a credit budget;
chat goes straight to the local model. /status shows the audited spend.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from aion import Capabilities, Kernel

from . import __version__
from .indexer import index_folder
from .llm import (MODEL, chat, claude_chat, embed, list_models, model_available,
                  offline_chat, ollama_available, parse_action, pull_stream)

# Curated model catalog — guidance for "which brain should I download?"
CATALOG = [
    ("gemma3", "3.3 GB", "best all-rounder — chat + vision (recommended)"),
    ("llama3.2", "2.0 GB", "fast, lightweight chat"),
    ("qwen2.5", "4.7 GB", "strongest at coding"),
    ("deepseek-r1", "4.7 GB", "deep reasoning & math"),
    ("moondream", "1.7 GB", "tiny + quick image understanding"),
    ("mistral", "4.4 GB", "solid general model"),
    ("llava", "4.7 GB", "dedicated vision model"),
]
from .helpdocs import DOC as HELP_DOC, best_topic
from . import toolchain as tcmod
from . import db as dbmod
from . import scan as scanmod
from . import lifecycle as lcmod
from .ca import (bank as cabank, scrutiny as cascrutiny, gst as cagst,
                 notice as canotice, finstmt as cafinstmt)
from .ledger import Ledger
from .store import load as store_load, save as store_save

# "edit/fix/update <file.ext>: <instruction>" → the file-editing engine (code mode).
EDIT_FILE = re.compile(
    r"^\s*(?:edit|fix|update|modify|refactor|change)\s+(?:the\s+)?"
    r"([\w./-]+\.[A-Za-z0-9]{1,8})\b[:,]?\s*(.*)$", re.I)

# Questions about termind itself → the support bot (answers FROM the built-in docs).
HELP_HINT = re.compile(
    r"\btermind\b.*\b(what|how|can|do|does|limit|work|use)|"
    r"\b(what|how)\b.*\b(this tool|this app|termind)\b|"
    r"\byour (limitations?|features?|capabilit)|"
    r"\bwhat can you do\b|\bhow do i (import|add|download|switch|delete|index|build)\b", re.I)

# Auto-memory: only when a SENTENCE STARTS with a self-statement — "should i use X?" must
# not become a remembered "fact".
AUTO_FACT = re.compile(
    r"(?:^|[.!?]\s+)(i am|i'm|my name is|i work|i live|i like|i prefer|i build|call me)\b", re.I)

# Natural-language actions: "create a folder", "open vs code", "build me a tool…" — no slash
# needed. The hint gate keeps ordinary chat away from the intent classifier.
ACTION_HINT = re.compile(
    r"\b(create|make|build|scaffold|new|open|write|generate)\b[\s\S]*"
    r"\b(folder|directory|project|tool|app|file|script|code|vs ?code|editor)\b", re.I)
# Edit-intent: "remove the background", "remove the logo", "make it brighter", "rotate it"…
# routes straight to the edit engine instead of vision-Q&A or chat.
EDIT_HINT = re.compile(
    r"\b(remove|change|delete|erase)\s+(the\s+)?(background|bg)\b"
    r"|\b(remove|erase|delete)\s+(the\s+)?\S+"          # "remove the <anything>" (image in play)
    r"|\b(grayscale|greyscale|b&w|black and white|sepia|rotate|resize|crop|flip|sharpen|blur)\b"
    r"|\bmake\s+(it|this|the\s+(image|photo|picture))\b.*\b(bright|dark|sharp|big|small|square)"
    r"|\bedit\s+(this|the|that)\s+(image|photo|picture)\b", re.I)

# A bare position reply ("right bottom corner .") — resumes a pending removal.
POSITION_ONLY = re.compile(
    r"^[\s.,!]*(?:it'?s\s+)?(?:in|at|on)?\s*(?:the\s+)?"
    r"((?:top|bottom|upper|lower|left|right|center|middle)[\w\s-]*?)[\s.,!]*$", re.I)

# "send/show me the image" — return the ACTUAL current image, never let the model fake it.
SHOW_IMG = re.compile(r"\b(send|show|give|display)\b.*\b(image|picture|photo|it)\b", re.I)

# "remove/erase the <object>" (but NOT the background — that's rembg's job).
# Position words ("in the top right") are KEPT — they localize deterministically.
OBJ_REMOVE = re.compile(
    r"\b(?:remove|erase|delete)\s+(?:the\s+)?(?!background\b|bg\b)"
    r"([\w][\w\s''&.-]{1,80}?)(?:\s+from\b.*)?$", re.I)

# Map "top right corner", "bottom left", "center"… to a region in percent coords.
POSITIONS = [
    (("top", "right"), (50, 0, 100, 50)), (("top", "left"), (0, 0, 50, 50)),
    (("bottom", "right"), (50, 50, 100, 100)), (("bottom", "left"), (0, 50, 50, 100)),
    (("top",), (0, 0, 100, 45)), (("bottom",), (0, 55, 100, 100)),
    (("left",), (0, 0, 45, 100)), (("right",), (55, 0, 100, 100)),
    (("center",), (25, 25, 75, 75)), (("middle",), (25, 25, 75, 75)),
]

INTENT_SYS = (
    'Classify the user\'s request. Reply with EXACTLY one JSON object:\n'
    '{"intent": "mkdir|open_editor|write_file|build_project|chat",'
    ' "path": "<folder/file path if any>", "file": "<target file for write_file>",'
    ' "spec": "<what the code should do>"}\n'
    "mkdir = just create a folder. open_editor = open something in VS Code. "
    "write_file = write ONE code file. build_project = create a tool/app/project "
    "(multiple files). chat = anything else.")

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
{CY}║{N}  {GR}◉{N} {WH}/reach <q>{N}       {D}»{N} private-by-exception: frontier model, consented + logged
{CY}║{N}  {GR}◉{N} {WH}/do <task>{N}       {D}»{N} proposes a shell command · runs on YOUR y/N
{CY}║{N}  {GR}◉{N} {WH}/build <idea>{N}    {D}»{N} scaffold a project · write code · open VS Code
{CY}║{N}  {GR}◉{N} {WH}/write <file> <spec>{N} {D}» generate one file (preview + y/N){N}
{CY}║{N}  {GR}◉{N} {WH}/mkdir <path>{N}  {D}» create folder{N}   {GR}◉{N} {WH}/code <path>{N}  {D}» open VS Code{N}
{CY}║{N}  {PK}◆{N} {WH}or just say it{N}  {D}» "create a folder x" · "build a tool that…"{N}
{CY}║{N}  {GR}◉{N} {WH}/img <path> [q]{N}  {D}»{N} the model SEES your image (gemma3/llava)
{CY}║{N}  {GR}◉{N} {WH}/edit <req>{N}      {D}»{N} "brighter + b&w" · remove background · crop…
{CY}║{N}  {GR}◉{N} {WH}/chats{N} {D}» past conversations{N}  {GR}◉{N} {WH}/chat new{N} {D}» fresh chat{N}
{CY}║{N}  {GR}◉{N} {WH}/model [name]{N}    {D}»{N} list · switch your brain (any Ollama model)
{CY}║{N}  {GR}◉{N} {WH}/pull <name>{N}     {D}»{N} download a new model (llama3.2, qwen2.5…)
{CY}║{N}  {GR}◉{N} {WH}/remember <fact>{N} {D}»{N} teach it about you · survives restarts
{CY}║{N}  {GR}◉{N} {WH}/recall <query>{N}  {D}»{N} embedding search over all memories
{CY}║{N}  {GR}◉{N} {WH}/status{N}          {D}»{N} core · credits burned · sandbox audit
{CY}║{N}  {GR}◉{N} {WH}/ledger{N}          {D}»{N} tamper-evident log of every agent action · export
{CY}║{N}  {GR}◉{N} {WH}/db <nl|sql>{N}     {D}»{N} query your DB · verifies + previews before destructive ops
{CY}║{N}  {GR}◉{N} {WH}/scan{N}            {D}»{N} sweep the folder for secrets · risky scripts · bad deps
{CY}║{N}  {GR}◉{N} {WH}/ca <section>{N}    {D}»{N} CA workbench · /ca bank <statement> → Tally vouchers (local)
{CY}║{N}  {GR}◉{N} {WH}/termind{N}         {D}»{N} isolated workspace · uninstall plan   {GR}◉{N} {WH}/tier{N} {D}» smart/smarter/max{N}
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

CA_HELP = (
    "CA workbench — runs entirely on your machine, so client data never leaves it.\n"
    "  /ca bank <statement>            parse → classify each line → Tally XML + ledger CSV\n"
    "  /ca scrutiny <ledger>           anomaly pass: round/duplicate/weekend/spike/personal\n"
    "  /ca gst <register> <gstr-2b>    reconcile books vs 2B → ITC-at-risk + mismatch buckets\n"
    "  /ca notice <notice.pdf|.txt>    identify the notice → draft a point-wise reply\n"
    "  /ca fs <trial-balance>          Schedule III Balance Sheet + P&L\n"
    "files can be .csv / .xlsx / .pdf. Every parse and export is sealed into the audit ledger "
    "(/ledger) — your DPDP 'data never left the device' proof.")


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


class Thinking:
    """Claude-style activity indicator: an animated neon spinner while the model works.
    Only animates on a real terminal (silent when piped, so tests/scripts stay clean)."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str = "thinking") -> None:
        self.label = label
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        t0 = time.time()
        for f in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r  {PK}{f}{N} {D}{self.label}… {time.time()-t0:.0f}s{N}  ")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\r" + " " * (len(self.label) + 16) + "\r")
        sys.stdout.flush()

    def __enter__(self) -> "Thinking":
        if sys.stdout.isatty():
            self._t.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._t.is_alive():
            self._t.join(timeout=0.3)


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
        # the active model is the user's choice (persisted) — env default otherwise
        self.model = store_load().get("model") or MODEL
        # "live" requires the server AND the model — a running server with no model pulled
        # must not pretend to be online (it would 404 on the first chat).
        self.live = (self.server and model_available(self.model)) if live is None else live
        self.history = []
        self.chunks = 0
        self.spent = 0.0
        self.denied = 0
        self._confirm = input            # how y/N prompts are answered (web overrides this)
        self._lock = threading.Lock()    # serialize terminal + web access to one shared brain
        # persistent memory: reload facts + indexed docs from the last session
        self.store = store_load()
        for i, fact in enumerate(self.store["facts"]):
            self.k.syscall("mem.put", key=f"fact#{i}", value=fact, tags=["fact"])
        for key, e in self.store["docs"].items():
            self.k.syscall("mem.put", key=key, value=e["value"], tags=e.get("tags", []))
        self.chunks = len(self.store["docs"])
        # conversations: resume the active chat (migrating any pre-chats history once)
        if self.store["history"] and not self.store["chats"]:
            self.chat_new(title="Earlier conversation")
            self.store["chats"][self.store["active_chat"]]["messages"] = \
                list(self.store["history"])
        cid = self.store.get("active_chat")
        self.history = list(self.store["chats"].get(cid, {}).get("messages", [])) \
            if cid else list(self.store["history"])
        self.actions = 0
        self.last_image = None       # (name, base64) of the most recent image, for /edit
        self.last_options = []       # clickable quick-reply choices the agent offered
        self._pending_remove = None  # target of a failed removal awaiting "where is it"
        self._stuck_task = None       # a build the local model stalled on, awaiting escalate consent
        self.pull = {"status": "idle"}  # background model download state (web progress bar)
        # toolchain: detect once, cache for a week (what languages this machine speaks)
        tc = self.store.get("toolchain") or {}
        if not tc or time.time() - tc.get("_detected_at", 0) > 7 * 86400:
            tc = tcmod.detect()
            self.store["toolchain"] = tc
            store_save(self.store)
        self.toolchain = tc
        self.ledger = Ledger()       # tamper-evident audit log of every code-agent action
        self.manifest = lcmod.Manifest()   # every termind-managed asset → clean uninstall
        self._db = None              # the open Database connection for the selected DB
        self._pending_sql = None     # (db_name, sql) awaiting a destructive-op confirmation
        self.last_scan = []          # findings from the last folder security sweep
        self._state = "idle"         # current activity: idle/Reading/Analyzing/Editing/Writing

    # ── chat sessions: new chat, continue previous, switch ───────────────────
    def _new_chat_id(self) -> str:
        return f"{int(time.time() * 1000)}-{len(self.store['chats'])}"  # collision-proof

    def chat_new(self, title: str = "New chat", mode: str = "chat") -> str:
        cid = self._new_chat_id()
        self.store["chats"][cid] = {"title": title, "ts": time.time(), "messages": [],
                                    "mode": mode}
        self.store["active_chat"] = cid
        self.history = []
        store_save(self.store)
        return cid

    def chat_open(self, cid: str) -> bool:
        c = self.store["chats"].get(cid)
        if not c:
            return False
        self.store["active_chat"] = cid
        self.history = list(c.get("messages", []))
        store_save(self.store)
        if c.get("ws") and os.path.isdir(c["ws"]):
            os.chdir(c["ws"])                   # session switch → its workspace
        return True

    def chat_rename(self, cid: str, title: str) -> bool:
        c = self.store["chats"].get(cid)
        if not c or not title.strip():
            return False
        c["title"] = title.strip()[:60]
        store_save(self.store)
        return True

    def chat_delete(self, cid: str) -> bool:
        if cid not in self.store["chats"]:
            return False
        del self.store["chats"][cid]
        if self.store.get("active_chat") == cid:        # deleted the open one → fall back
            rest = sorted(self.store["chats"].items(), key=lambda kv: -kv[1].get("ts", 0))
            if rest:
                self.chat_open(rest[0][0])
            else:
                self.store["active_chat"] = None
                self.history = []
        if not self.store["chats"]:
            self.store["history"] = []   # else the legacy field resurrects a deleted chat
        store_save(self.store)
        return True

    def chats_list(self, mode: str = None) -> list:
        items = sorted(self.store["chats"].items(), key=lambda kv: -kv[1].get("ts", 0))
        return [{"id": k, "title": v.get("title", "Chat"),
                 "mode": v.get("mode", "chat"), "ws": v.get("ws"),
                 "active": k == self.store.get("active_chat")} for k, v in items
                if mode is None or v.get("mode", "chat") == mode]

    def active_mode(self) -> str:
        c = self.store["chats"].get(self.store.get("active_chat") or "")
        return (c or {}).get("mode", "chat")

    def _save_chat(self, first_user_text: str) -> None:
        cid = self.store.get("active_chat")
        if not cid or cid not in self.store["chats"]:
            # create the record inline — chat_new() would wipe the history we're saving
            cid = self._new_chat_id()
            self.store["chats"][cid] = {"title": "New chat", "ts": time.time(),
                                        "messages": [], "mode": getattr(self, "view_mode", "chat")}
            self.store["active_chat"] = cid
        c = self.store["chats"][cid]
        c["messages"] = self.history[-40:]
        if c.get("title") in ("New chat", "", None):
            c["title"] = first_user_text.strip()[:48]

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

    def frontier_ready(self) -> bool:
        """Is consented cloud escalation available? (only if the user set a key)"""
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _frontier(self, messages: list, consent: str = "", why: str = "escalation") -> str:
        """The ONE path off this machine: a frontier cloud model, on explicit consent, ALWAYS
        logged. termind is private by default — this is the audited exception. Records the byte
        count that leaves the machine into the tamper-evident ledger; raises if no key/unreachable
        so callers can fall back to local."""
        if not self.frontier_ready():
            raise RuntimeError("no ANTHROPIC_API_KEY — staying local")
        model = os.environ.get("TERMIND_CLOUD_MODEL", "claude-sonnet-4-6")
        nbytes = len(json.dumps(messages).encode())
        cid = self.store.get("active_chat") or "default"
        try:
            reply = claude_chat(messages)
        except Exception as e:
            self.ledger.record(session=cid, tool="escalate", target=f"cloud:{model}",
                               outcome="fail", consent=consent, bytes_written=nbytes,
                               detail=str(e)[:120])
            raise
        self.ledger.record(session=cid, tool="escalate", target=f"cloud:{model}",
                           outcome="ok", consent=consent, bytes_written=nbytes, detail=why)
        return reply

    def do_reach(self, q: str) -> str:
        """Explicitly escalate ONE query to the frontier cloud model — private-by-exception,
        every byte that leaves the machine logged in the audit ledger. Nothing leaves unless
        you run this."""
        if not q.strip():
            return "usage: /reach <question> — escalate one query to the frontier model (logged)"
        if not self.frontier_ready():
            return ("frontier escalation is off — termind is 100% local until you set "
                    "ANTHROPIC_API_KEY. When you do, every escalation is consented and logged "
                    "in /ledger, and /status shows the exact bytes that left the machine.")
        with Thinking("reaching the frontier model"):
            try:
                ans = self._frontier(self.chat_messages(q), consent=q, why="/reach")
            except Exception as e:
                return f"frontier unreachable: {e} — staying local."
        return (ans + "\n\n— escalated to the cloud on your request · logged in /ledger · "
                "/status shows bytes off-machine")

    def do_think(self, q: str) -> str:
        """Escalation ladder for hard questions: big local model → Claude → deep local CoT."""
        with Thinking("deep thinking"):
            big = os.environ.get("TERMIND_BIG_MODEL")
            if big and self.live:
                try:
                    return chat(self.chat_messages(q + "\n\nThink step by step."), model=big)
                except RuntimeError:
                    pass  # big model not pulled → next rung
            if self.frontier_ready():
                try:
                    return self._frontier(self.chat_messages(q), consent=q, why="/think cloud rung")
                except Exception:
                    pass  # cloud unreachable → next rung
            if self.live:
                return self._chat(self.chat_messages(
                    "This is a HARD question. Reason step by step, check yourself, "
                    "then give the answer:\n" + q))
            return offline_chat(self.chat_messages(q))

    # ── builder powers: folders, code files, whole projects, VS Code ─────────
    @staticmethod
    def _dry_run(cmd: str) -> str:
        """Verify a command can actually run BEFORE executing: the binary must exist.
        Auto-repairs macOS's missing 'python' by swapping to python3. '' ⇒ don't run."""
        tok = cmd.split()
        if not tok:
            return ""
        if shutil.which(tok[0]) is None:
            if tok[0] == "python" and shutil.which("python3"):
                tok[0] = "python3"            # macOS ships python3 only
            else:
                return ""                      # binary missing → skip instead of erroring
        return " ".join(tok)

    @staticmethod
    def _py_error(path: str, code: str):
        """Syntax-check Python without executing it; None if clean."""
        if not path.endswith(".py"):
            return None
        try:
            compile(code, path, "exec")
            return None
        except SyntaxError as e:
            return f"line {e.lineno}: {e.msg}"

    def _gen_code(self, file: str, spec: str) -> str:
        """Generate file content and SELF-HEAL: if Python doesn't compile, the model
        gets the error back and fixes its own code (up to 2 repair rounds)."""
        sys_p = ("You write complete, working file contents. Reply ONLY with the raw file "
                 "content — no markdown fences, no commentary.")
        with Thinking(f"writing {os.path.basename(file)}"):
            content = self._strip_fences(self._chat([
                {"role": "system", "content": sys_p},
                {"role": "user", "content": f"Write the file {file}. It should: {spec}"}]))
            for _ in range(2):
                err = self._py_error(file, content)
                if not err:
                    break
                content = self._strip_fences(self._chat([
                    {"role": "system", "content": sys_p},
                    {"role": "user", "content":
                     f"This {file} has a syntax error ({err}). Reply with the FULL corrected "
                     f"file content only:\n\n{content}"}]))
        return content

    @staticmethod
    def _strip_fences(t: str) -> str:
        t = t.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        return t.strip() + "\n"

    def do_mkdir(self, path: str) -> str:
        full = self._safe_path(path)
        if full is None:
            return f"⛔ blocked: '{path}' is outside the workspace (code mode is jailed)"
        os.makedirs(full, exist_ok=True)
        self.actions += 1
        return f"created folder: {full}"

    def do_code(self, path: str) -> str:
        full = os.path.expanduser(path or ".")
        apps = [["code", full]]
        if os.environ.get("TERMIND_VSCODE"):          # custom app name, e.g. "Visual Studio Code 2"
            apps.append(["open", "-a", os.environ["TERMIND_VSCODE"], full])
        apps += [["open", "-a", "Visual Studio Code", full],
                 ["open", "-a", "Visual Studio Code 2", full]]
        for cmd in apps:
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
        content = self._gen_code(file, spec)
        warn = self._py_error(file, content)
        if warn:
            print(f"  {YL}⚠ couldn't fully self-heal ({warn}) — review before running{N}")
        preview = "\n".join(content.splitlines()[:15])
        print(f"\n  {YL}⚡ will write {WH}{file}{N} {D}({len(content.splitlines())} lines){N}\n"
              f"{D}{preview}{N}\n")
        if str((confirm or self._confirm)(f"  {PK}write it? [y/N]{N} ")).strip().lower() != "y":
            return "aborted — nothing was written."
        full = self._safe_path(file)
        if full is None:
            return f"⛔ blocked: '{file}' is outside the workspace (code mode is jailed)"
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        self.actions += 1
        return f"wrote {full} ({len(content.splitlines())} lines) — open it:  /code {file}"

    def do_build(self, idea: str, confirm=None, open_editor: bool = True) -> str:
        """The full pipeline: plan → folder → code → ARCHITECTURE.md → VS Code → run."""
        if not self.live:
            return "project building needs a live model — install Ollama (./setup.sh)"
        with Thinking("planning your project"):
            plan = parse_action(self._chat([
                {"role": "system", "content":
                 'Scaffold a SMALL working starter project (2-5 short files). Reply with EXACTLY '
                 '{"folder": "<kebab-name>", "files": {"<relative path>": "<full file content>"}, '
                 '"run": "<shell command to run it from inside the folder>"}. '
                 "files MUST include README.md and ARCHITECTURE.md (explain the design: "
                 "components, data flow, why). Keep code short and working. In the run command "
                 "always use python3, never bare python."},
                {"role": "user", "content": idea}], fmt_json=True))
        folder, files = plan.get("folder"), plan.get("files") or {}
        if not folder or not files:
            return "couldn't plan that project — try rephrasing."
        print(f"\n  {YL}⚡ will create {WH}{folder}/{N} {D}with {len(files)} files:{N} "
              + ", ".join(files) + "\n")
        if str((confirm or self._confirm)(f"  {PK}build it? [y/N]{N} ")).strip().lower() != "y":
            return "aborted — nothing was created."
        root = self._safe_path(folder)
        if root is None:
            return f"⛔ blocked: '{folder}' is outside the workspace (code mode is jailed)"
        healed = 0
        for rel, content in files.items():
            code = self._strip_fences(str(content))
            if self._py_error(rel, code):  # self-heal broken python before it hits disk
                code = self._gen_code(rel, f"(repair) original intent: {idea}\n\n{code}")
                healed += 1
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(code)
        self.actions += 1
        out = [f"built {root}/ with {len(files)} files (incl. ARCHITECTURE.md)"
               + (f" · self-healed {healed}" if healed else "")]
        if open_editor:
            out.append(self.do_code(root))
        wanted = str(plan.get("run") or "").strip().replace(f"{folder.rstrip('/')}/", "")
        run_cmd = self._dry_run(wanted)          # verify the binary exists before running
        if run_cmd:
            try:
                r = subprocess.run(run_cmd, shell=True, cwd=root, capture_output=True,
                                   text=True, timeout=30)
                out.append(f"▶ ran: {run_cmd}\n" + (r.stdout or r.stderr or "(no output)").strip()[:600])
            except Exception as e:
                out.append(f"▶ run skipped ({e})")
        elif wanted:
            out.append(f"▶ run skipped — '{wanted.split()[0]}' not found on this machine")
        return "\n".join(out)

    # ── natural language → action (no slash needed) ──────────────────────────
    def do_intent(self, text: str, confirm=None) -> str:
        """Route a plain-English request to the right power."""
        low = text.lower()
        # fast offline paths first
        m = re.search(r"(?:folder|directory)(?:\s+(?:called|named|name))?\s+([\w./~-]+)", text, re.I)
        wants_project = re.search(r"\b(project|tool|app)\b", low)
        if re.search(r"vs ?code|editor", low) and re.search(r"\bopen\b", low) and not wants_project:
            mp = re.search(r"(?:open|in)\s+([~./][\w./-]*)", text)
            return self.do_code(mp.group(1) if mp else ".")
        if m and re.search(r"\b(create|make|new)\b", low) and not wants_project \
                and not re.search(r"\b(file|code|script)\b", low):
            return self.do_mkdir(m.group(1))
        if not self.live:
            return self.do_chat(text)  # can't plan actions without a model
        with Thinking("parsing your intent"):
            act = parse_action(self._chat([{"role": "system", "content": INTENT_SYS},
                                     {"role": "user", "content": text}], fmt_json=True))
        intent = act.get("intent", "chat")
        if intent == "mkdir" and act.get("path"):
            return self.do_mkdir(act["path"])
        if intent == "open_editor":
            return self.do_code(act.get("path") or ".")
        if intent == "write_file":
            return self.do_write(act.get("file") or "main.py",
                                 act.get("spec") or text, confirm=confirm)
        if intent == "build_project":
            return self.do_build(text, confirm=confirm)
        return self.do_chat(text)

    def _image_followups(self, text: str):
        """Replies that continue an image task: a bare position resumes a failed removal;
        'send me the image' returns the real image. None ⇒ not a follow-up."""
        if self.last_image and self._pending_remove:
            m = POSITION_ONLY.match(text)
            if m:
                target, self._pending_remove = self._pending_remove, None
                return self.do_edit(f"remove the {target} in the {m.group(1).strip()}")
        if self.last_image and SHOW_IMG.search(text):
            return f"here's the current image ({self.last_image[0]}) ⤵"
        return None

    def _code_route(self, text: str):
        """Code sessions: every plain message goes through the tool-execution loop."""
        self.last_options = []   # both terminal + web enter here first → never leak stale chips
        in_code = getattr(self, "view_mode", "chat") == "code" or self.active_mode() == "code"
        m = EDIT_FILE.match(text)
        if m and os.path.isfile(os.path.join(self.workspace(), m.group(1))):
            return self.do_edit_file(m.group(1), m.group(2) or "improve it")
        if in_code and self.agent_mode == "plan" and (ACTION_HINT.search(text)
                                                      or EDIT_FILE.match(text)):
            return self.do_plan(text)
        if in_code and self.live and not text.startswith("/"):
            return self.do_code_agent(text)        # ACT, don't advise
        return None

    def route(self, text: str) -> str:
        pend = self._db_followup(text)               # a pending destructive query awaits confirm
        if pend is not None:
            return pend
        follow = self._image_followups(text)
        if follow is not None:
            return follow
        code = self._code_route(text)
        if code is not None:
            return code
        if HELP_HINT.search(text):
            return self.do_helpbot(text)              # questions about termind → support bot
        if self.last_image and EDIT_HINT.search(text):
            return self.do_edit(text)                 # "rotate it 90" etc. → edit engine
        return self.do_intent(text) if ACTION_HINT.search(text) else self.do_chat(text)

    def do_action(self, task: str, confirm=None) -> str:
        """Operator mode: the model proposes ONE shell command; runs only on your explicit y."""
        if not self.live:
            return "operator mode needs a live model — install Ollama (./setup.sh)"
        with Thinking("proposing a command"):
            act = parse_action(self._chat([
                {"role": "system", "content":
                 'Propose ONE safe macOS shell command for the task. Reply with EXACTLY '
                 '{"cmd": "<command>", "why": "<one line>"}. Never propose destructive commands.'},
                {"role": "user", "content": task}], fmt_json=True))
        cmd = act.get("cmd", "")
        if not cmd:
            return "couldn't form a command for that."
        print(f"\n  {YL}⚡ proposed:{N} {WH}{cmd}{N}\n  {D}{act.get('why', '')}{N}")
        ans = (confirm or self._confirm)(f"  {PK}execute? [y/N]{N} ")
        if str(ans).strip().lower() != "y":
            return "aborted — nothing was run."
        checked = self._dry_run(cmd)
        if not checked:
            return f"can't run — '{cmd.split()[0]}' not found on this machine."
        try:
            r = subprocess.run(checked, shell=True, capture_output=True, text=True, timeout=60)
            self.actions += 1
            out = (r.stdout or r.stderr or "(no output)").strip()[:1500]
            return f"$ {cmd}\n{out}"
        except subprocess.TimeoutExpired:
            return "command timed out (60s cap)."

    def do_ask(self, q: str) -> str:
        think = (lambda m: self._chat(m, fmt_json=True)) if self.live else offline_ask_think
        pid = self.k.spawn("termind-ask", fn=_ask_agent, args=(q, think),
                           caps=Capabilities(["mem.search", "mem.get"]), budget=5.0)
        with Thinking("querying your data"):
            self.k.run()
        p = self.k.processes[pid]
        self.spent += p.meter.credits
        self.denied = self.k.meter.denied
        return p.result if p.state.value == "done" else f"(error: {p.error})"

    def _chat(self, msgs: list, fmt_json: bool = False, model: str = None) -> str:
        """All model calls go through here so the user's chosen /model applies everywhere."""
        return chat(msgs, fmt_json=fmt_json, model=model or self.model)

    def chat_messages(self, text: str) -> list:
        """Master system prompt — carries session state, capabilities, and behaviour rules."""
        tier = self.store.get("tier", "smart")
        tier_desc = {
            "smart": "local model, private, $0/query",
            "smarter": "deeper local reasoning (bigger local model if pulled)",
            "max": "frontier model on consent — every call logged in the audit ledger",
        }.get(tier, tier)
        p = self.profile()
        ws = self.workspace()
        db = getattr(self, "_db", None)
        sys = (
            "You are termind, a private local AI agent — terminal REPL + Claude-style web UI. "
            "The HUMAN typing to you is your user. You are NOT the user.\n\n"
            "CAPABILITIES:\n"
            "• CHAT — answer, explain, advise (always concise and direct)\n"
            "• IMAGES — you SEE images attached; the app EDITS them on request (background "
            "removal, brightness, crop, rotate, resize, sepia, blur, object removal with "
            "inpainting) — all local, never leaves the machine\n"
            "• DOCS — /index /ask /recall: index folders, answer from docs with source cites\n"
            "• CODE MODE — set a workspace folder; write & run code in an act-observe loop, "
            "jailed to the workspace, with plan/act/bypass modes\n"
            "• DATABASE — /db: connect SQLite/Postgres/MySQL/MongoDB; query in plain English "
            "or SQL. termind verifies every query and shows EXPLAIN + exact affected-row count "
            "BEFORE any destructive op — nothing runs until the user confirms\n"
            "• SCAN — /scan: sweep selected folder offline for secrets, dangerous scripts, "
            "insecure deps — alerts automatically on folder select\n"
            "• AUDIT — /ledger: tamper-evident hash-chained log of every action\n"
            "• FRONTIER — /reach /think: escalate to a cloud model — explicit consent only, "
            "every byte that leaves the machine is logged\n\n"
            "BEHAVIOUR RULES — follow these exactly:\n"
            "1. GATHER UPFRONT: for any multi-step task (analysis, build, database work): "
            "collect ALL open questions in your FIRST reply before doing any work. "
            "Do NOT interrupt mid-task with follow-up questions. Deliver the complete "
            "result, then stop.\n"
            "2. ONE TASK AT A TIME: if a task is running, do not accept or start another.\n"
            "3. DATABASE GATE: if the user requests database analysis, queries, or any "
            "data work and NO database is currently connected, your ONLY valid reply is: "
            "'No database is connected. Open Settings (⚙) → 🗄 Databases, add your DB, "
            "then come back.' Do not guess, fake a connection, or continue past this gate.\n"
            "4. TOOLCHAIN GATE: if the user wants to build in a language not installed "
            "locally, tell them: the language is not installed, give the install command, "
            "offer to write the code they can run themselves — do NOT attempt to run it.\n"
            "5. NO FALSE ACTIONS: never claim you wrote a file, ran a command, or sent "
            "anything. The app performs and reports actions. You describe; the app acts.\n"
            "6. BE CONCISE: no trailing summaries, no restatements. Say it once, well.\n\n"
            f"SESSION STATE:\n"
            f"Tier: {tier} ({tier_desc})\n"
        )
        if ws:
            sys += f"Workspace: {ws}\n"
        if self.active_mode() == "code":
            sys += "Mode: CODE — elevated file/run powers active in the workspace.\n"
        if db:
            sys += f"Active DB: {db.name} ({db.engine})\n"
        else:
            sys += ("Active DB: none — user must add a database in Settings → Databases "
                    "before any database work.\n")
        if p.get("name"):
            sys += (f"User: {p['name']}"
                    + (f", {p['role']}" if p.get("role") else "")
                    + (f" — style: {p['prefs']}" if p.get("prefs") else "")
                    + ". Address by name when natural.\n")
        if self.store.get("facts"):
            sys += ("Remembered facts about the USER (use for 'who am I?' etc.): "
                    + "; ".join(self.store["facts"])
                    + "\nNever confuse the user's facts with your own identity.\n")
        return [{"role": "system", "content": sys}] + self.history[-8:] + [
            {"role": "user", "content": text}]

    # ── vision: see and edit images ───────────────────────────────────────────
    def do_vision(self, text: str, image_b64: str, name: str = "image") -> str:
        """Send an image to the model (Ollama multimodal: gemma3, llava, llama3.2-vision…)."""
        if not self.live:
            return "image understanding needs a live model — install Ollama (./setup.sh)"
        self.last_image = (name, image_b64)
        msgs = self.chat_messages(text or "Describe this image.")
        msgs[-1]["images"] = [image_b64]              # Ollama's multimodal message format
        with Thinking("looking at your image"):
            try:
                reply = self._chat(msgs)
            except RuntimeError as e:
                return (f"{e}\nTip: your model may not support vision — try "
                        f"/pull llava  or  /pull llama3.2-vision, then /model it.")
        self.history += [{"role": "user", "content": f"[sent image: {name}] {text}".strip()},
                         {"role": "assistant", "content": reply}]
        self.store["history"] = self.history[-20:]
        self._save_chat(f"image: {name}")
        store_save(self.store)
        return reply

    def do_img(self, arg: str) -> str:
        """Terminal: /img <path> [question]."""
        parts = arg.split(None, 1)
        path = os.path.expanduser(parts[0]) if parts else ""
        if not path or not os.path.isfile(path):
            return "usage: /img <image path> [question]"
        import base64
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return self.do_vision(parts[1] if len(parts) > 1 else "", b64,
                              os.path.basename(path))

    EDIT_OPS = ("grayscale · sepia · rotate <deg> · resize <pct>% · flip · brightness <pct> · "
                "contrast <pct> · blur <px> · sharpen · crop square · remove background · "
                "remove the <object> — or just describe it: 'make it brighter and b&w'")

    @staticmethod
    def _apply_edit(img, op: str, val: float = None):
        """One deterministic edit op on a PIL image."""
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        if op == "grayscale":
            return ImageOps.grayscale(img).convert("RGB")
        if op == "sepia":
            g = ImageOps.grayscale(img)
            return ImageOps.colorize(g, "#2e1f0e", "#f5e3c2").convert("RGB")
        if op == "rotate":
            return img.rotate(-(val if val is not None else 90), expand=True)
        if op == "resize":
            p = (val if val is not None else 50) / 100
            return img.resize((max(1, int(img.width * p)), max(1, int(img.height * p))))
        if op == "flip":
            return ImageOps.mirror(img)
        if op == "brightness":
            return ImageEnhance.Brightness(img).enhance((val if val is not None else 120) / 100)
        if op == "contrast":
            return ImageEnhance.Contrast(img).enhance((val if val is not None else 120) / 100)
        if op == "blur":
            return img.filter(ImageFilter.GaussianBlur(val if val is not None else 4))
        if op == "sharpen":
            return img.filter(ImageFilter.SHARPEN)
        if op == "crop":
            side = min(img.width, img.height)
            left, top = (img.width - side) // 2, (img.height - side) // 2
            return img.crop((left, top, left + side, top + side))
        return img

    @staticmethod
    def _img_b64(img) -> str:
        import base64
        import io
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _ask_image(self, img, system: str, user: str) -> dict:
        return parse_action(self._chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user, "images": [self._img_b64(img)]}],
            fmt_json=True))

    def _verify_region(self, img, bbox, target: str) -> bool:
        """Crop the candidate box and ask the model if the target is really in it."""
        w, h = img.size
        crop = img.crop((int(bbox[0] / 100 * w), int(bbox[1] / 100 * h),
                         int(bbox[2] / 100 * w), int(bbox[3] / 100 * h)))
        ans = self._ask_image(crop, 'Reply EXACTLY {"present": true} or {"present": false}.',
                              f"Is the {target} visible in this image?")
        return bool(ans.get("present"))

    def _grid_locate(self, img, target: str):
        """Grid voting: small VLMs are bad at pixel coords but good at 'which cells?'."""
        GRID = 4
        ans = self._ask_image(
            img,
            f"The image is divided into a {GRID}x{GRID} grid. Columns are A,B,C,D from LEFT "
            f"to RIGHT; rows are 1,2,3,4 from TOP to BOTTOM (A1 = top-left). Reply EXACTLY "
            f'{{"cells": ["<col><row>", ...]}} listing every cell containing the target, '
            f'or {{"cells": []}} if absent.',
            f"Which cells contain the {target}?")
        cells = [str(c).strip().upper() for c in (ans.get("cells") or [])]
        cols = [ord(c[0]) - 65 for c in cells if len(c) >= 2 and c[0] in "ABCD"
                and c[1] in "1234"]
        rows = [int(c[1]) - 1 for c in cells if len(c) >= 2 and c[0] in "ABCD"
                and c[1] in "1234"]
        if not cols:
            return None
        step = 100.0 / GRID
        return (min(cols) * step, min(rows) * step,
                (max(cols) + 1) * step, (max(rows) + 1) * step)

    @staticmethod
    def _position_region(text: str):
        """If the USER said where ('in the top right'), trust them — deterministic region."""
        low = text.lower()
        for words, region in POSITIONS:
            if all(w in low for w in words):
                return region
        return None

    def _quadrant_locate(self, img, target: str):
        """Visual binary search: yes/no presence questions on overlapping crops — the one
        geometry task small VLMs are actually reliable at."""
        region = (0.0, 0.0, 100.0, 100.0)
        w, h = img.size
        for _depth in range(2):                       # 2 levels: 100% → 50% → 25% region
            x1, y1, x2, y2 = region
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            ov_x, ov_y = (x2 - x1) * 0.10, (y2 - y1) * 0.10   # 10% overlap between quads
            quads = [(x1, y1, mx + ov_x, my + ov_y), (mx - ov_x, y1, x2, my + ov_y),
                     (x1, my - ov_y, mx + ov_x, y2), (mx - ov_x, my - ov_y, x2, y2)]
            hits = []
            for q in quads:
                crop = img.crop((int(q[0] / 100 * w), int(q[1] / 100 * h),
                                 int(q[2] / 100 * w), int(q[3] / 100 * h)))
                ans = self._ask_image(
                    crop, 'Answer strictly. Reply EXACTLY {"present": true} or '
                          '{"present": false}.',
                    f"Does this image contain the {target} (fully or partially)?")
                if ans.get("present"):
                    hits.append(q)
            if not hits or len(hits) == 4:            # nowhere / everywhere → stop here
                return region if _depth > 0 else None
            region = (min(q[0] for q in hits), min(q[1] for q in hits),
                      max(q[2] for q in hits), max(q[3] for q in hits))
        return region

    def _find_region(self, img, target: str):
        """Locate the target: the user's own position words first (deterministic), then
        visual binary search (yes/no on crops), then direct bbox — verified when possible."""
        pos = self._position_region(target)
        if pos:
            return pos                                # the user said where — believe them
        with Thinking(f"locating '{target}'"):
            quad = self._quadrant_locate(img, target)
            if quad and self._verify_region(img, quad, target):
                return quad
            box = self._ask_image(
                img,
                'You locate objects in images. Reply EXACTLY {"found": true|false, '
                '"x1": <0-100>, "y1": <0-100>, "x2": <0-100>, "y2": <0-100>} — PERCENT of '
                'width/height, top-left origin, tightly around the object.',
                f"Locate the {target}.")
            try:
                if box.get("found"):
                    x1, y1, x2, y2 = (float(box[k]) for k in ("x1", "y1", "x2", "y2"))
                    if x2 > x1 and y2 > y1:
                        cand = (max(0, x1 - 3), max(0, y1 - 3),
                                min(100, x2 + 3), min(100, y2 + 3))
                        if self._verify_region(img, cand, target):
                            return cand
            except (KeyError, TypeError, ValueError):
                pass
            if quad:
                return quad                            # unverified but best evidence we have
        return None

    def _erase(self, img, bbox):
        """Erase a region — generative LaMa when available (photo-quality reconstruction),
        classical cv2 as the fallback. Downloads LaMa once with your consent."""
        from . import inpaint
        try:
            if not inpaint.model_ready():
                ans = self._confirm("  download the neural inpainter for photo-quality "
                                    "removal? (~200MB, one-time) [y/N] ")
                if str(ans).strip().lower() == "y":
                    with Thinking("downloading LaMa inpainter (one-time)"):
                        inpaint.download_model()
            if inpaint.model_ready():
                return inpaint.inpaint_bbox(img, bbox)
        except Exception:
            pass                                     # any LaMa trouble → classical fallback
        return self._inpaint_region(img, bbox)

    @staticmethod
    def _inpaint_region(img, bbox):
        """Erase a region and reconstruct it from its surroundings (OpenCV inpainting)."""
        import cv2
        import numpy as np
        rgb = np.array(img.convert("RGB"))[:, :, ::-1].copy()      # PIL RGB → cv2 BGR
        h, w = rgb.shape[:2]
        x1, y1 = int(bbox[0] / 100 * w), int(bbox[1] / 100 * h)
        x2, y2 = int(bbox[2] / 100 * w), int(bbox[3] / 100 * h)
        mask = np.zeros((h, w), np.uint8)
        mask[y1:y2, x1:x2] = 255
        out = cv2.inpaint(rgb, mask, 7, cv2.INPAINT_TELEA)
        from PIL import Image
        return Image.fromarray(out[:, :, ::-1])                     # BGR → RGB → PIL

    def _parse_edit_ops(self, text: str) -> list:
        """Map an edit request to [(op, val), …] — keywords first, model for free-form."""
        low = text.lower()
        if "background" not in low and "bg" not in low.split():
            m_obj = OBJ_REMOVE.search(text)
            if m_obj:                                  # "remove the gemini logo" → targeted erase
                return [("removeobj", m_obj.group(1).strip())]
        num = re.search(r"-?\d+", low)
        val = float(num.group()) if num else None
        KEYS = [("remove background", [("rembg", None)]), ("bg remove", [("rembg", None)]),
                ("gray", [("grayscale", None)]), ("grey", [("grayscale", None)]),
                ("black and white", [("grayscale", None)]), ("monochrome", [("grayscale", None)]),
                ("b&w", [("grayscale", None)]), ("sepia", [("sepia", None)]),
                ("rotate", [("rotate", val)]), ("resize", [("resize", val)]),
                ("scale", [("resize", val)]), ("flip", [("flip", None)]),
                ("bright", [("brightness", val)]), ("contrast", [("contrast", val)]),
                ("blur", [("blur", val)]), ("sharp", [("sharpen", None)]),
                ("square", [("crop", None)]), ("crop", [("crop", None)])]
        hits = [(k, ops) for k, ops in KEYS if k in low]
        if len(hits) == 1 and not (" and " in low or "," in low or len(low.split()) > 4):
            return hits[0][1]
        if not self.live:        # offline: apply every keyword found, in order of appearance
            found = sorted(((low.find(k), ops) for k, ops in hits), key=lambda x: x[0])
            return [o for _, ops in found for o in ops]
        plan = parse_action(self._chat([{"role": "system", "content":
            'Convert the image-edit request to a JSON plan. Ops: grayscale, sepia, rotate(deg), '
            'resize(pct), flip, brightness(pct, 100=same), contrast(pct), blur(px), sharpen, '
            'crop, rembg(remove background). Reply EXACTLY '
            '{"ops": [{"op": "<name>", "val": <number or null>}, ...]}'},
            {"role": "user", "content": text}], fmt_json=True))
        return [(str(o.get("op", "")), o.get("val")) for o in (plan.get("ops") or [])
                if o.get("op")]

    def do_edit(self, op: str) -> str:
        """Edit the last image: deterministic Pillow ops, natural-language multi-step plans,
        and neural background removal (rembg) — all local."""
        if not self.last_image:
            return "no image yet — send one first (web 📎 or /img <path>)"
        if not op.strip():
            return "edits: " + self.EDIT_OPS
        try:
            from PIL import Image
        except ImportError:
            return "image editing needs Pillow — run: pip install pillow"
        import base64
        import io
        ops = self._parse_edit_ops(op)
        if not ops:
            return "couldn't map that to edits — try: " + self.EDIT_OPS
        name, b64 = self.last_image
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")
        applied = []
        for o, val in ops:
            if o == "removeobj":           # "remove the gemini logo" → locate + inpaint
                if not self.live:
                    return "targeted removal needs a live vision model (install Ollama)"
                try:
                    import cv2  # noqa: F401
                except ImportError:
                    return ("targeted removal needs OpenCV — run: "
                            "pip install opencv-python-headless numpy")
                bbox = self._find_region(img, str(val))
                if not bbox:
                    self._pending_remove = str(val)   # a bare "bottom right" reply resumes this
                    return (f"couldn't locate '{val}' — just tell me WHERE it is "
                            f"(e.g. 'top right corner') and I'll erase it there.")
                self._pending_remove = None
                with Thinking(f"erasing '{val}'"):
                    img = self._erase(img, bbox).convert("RGBA")
                    # post-check: still visible? expand the region 15% and erase again
                    still = self._ask_image(img, 'Reply EXACTLY {"present": true} or '
                                            '{"present": false}.',
                                            f"Is the {val} still visible in this image?")
                    if still.get("present"):
                        gx = (bbox[2] - bbox[0]) * 0.15
                        gy = (bbox[3] - bbox[1]) * 0.15
                        bigger = (max(0, bbox[0] - gx), max(0, bbox[1] - gy),
                                  min(100, bbox[2] + gx), min(100, bbox[3] + gy))
                        img = self._erase(img, bigger).convert("RGBA")
                applied.append(f"removed '{val}'")
                continue
            if o == "rembg":               # neural background removal, fully local
                try:
                    from rembg import remove
                except ImportError:
                    return ("background removal needs rembg — run: pip install rembg "
                            "(one-time ~170MB model download on first use)")
                with Thinking("removing background (neural)"):
                    img = remove(img)
            else:
                img = self._apply_edit(img.convert("RGB"), o, val).convert("RGBA")
            applied.append(o + (f" {val:g}" if val is not None else ""))
        out_path = os.path.join(os.getcwd(), os.path.splitext(name)[0] + "_edited.png")
        img.save(out_path, "PNG")
        with open(out_path, "rb") as f:
            self.last_image = (os.path.basename(out_path),
                               base64.b64encode(f.read()).decode())
        self.actions += 1
        return (f"applied {' → '.join(applied)}\nsaved: {out_path} "
                f"({img.width}x{img.height}) — it's now the active image")

    # ── the CODE AGENT: a tool-execution loop (mkdir/write/read/run/done) ────
    CODE_SYS = (
        "You are termind's CODE AGENT — an autonomous build assistant, not an advisor.\n"
        "Workspace: {ws}\n\n"
        "TOOLS — reply with EXACTLY ONE JSON object per turn, never prose:\n"
        '{{"tool":"mkdir","path":"<relative folder>"}}\n'
        '{{"tool":"write","path":"<relative file>","content":"<FULL file content>"}}\n'
        '{{"tool":"read","path":"<relative file>"}}\n'
        '{{"tool":"run","cmd":"<shell command, runs inside the workspace>"}}\n'
        '{{"tool":"ask","say":"<question>","options":["<A>","<B>"]}}  '
        "— use ONLY in your first reply, to gather ALL unknowns at once\n"
        '{{"tool":"say","say":"<reply>"}}  — pure conversation, no side effects\n'
        '{{"tool":"done","say":"<short summary>"}}  '
        "— ONLY when every file is written and the task is verified\n\n"
        "BUILD RULES:\n"
        "1. GATHER FIRST: your FIRST reply on any new task MUST resolve all open decisions "
        "(language, scope, key features) using ask or say — everything at once. "
        "After the user answers, build the entire thing without asking again mid-build.\n"
        "2. NEW PROJECT (no language chosen yet): FIRST reply = ask tool with language options "
        '["Python","JavaScript / Node","Go","HTML/CSS/JS (web)"]. '
        "Then ask scope (CLI / web / GUI) if needed. Then build — no more questions.\n"
        "3. INSTALLED TOOLCHAINS on this machine (use these exact commands): {tools}\n"
        "   TOOLCHAIN GUARD: if the user picks a language NOT in the list above, use say to:\n"
        "   a) state the language is not installed on this machine\n"
        "   b) give the exact install command (e.g. 'brew install go', 'nvm install node')\n"
        "   c) offer to write the code they can save and run once installed\n"
        "   d) do NOT attempt mkdir/write/run for an unavailable runtime\n"
        "4. Python packages: run 'pip install <pkg>' — termind auto-creates a project .venv.\n"
        "5. One tool per turn, wait for the result, then continue.\n"
        "6. After mkdir, immediately write the files — never stop at just a folder.\n"
        "7. A build is NOT done until every file is written. Only then emit done.\n"
        "8. If the same step fails 3× with identical errors, stop and use say to report "
        "the blocker clearly.\n\n"
        "SAFETY: every path is jailed to the workspace — ../ escapes are blocked and logged.")

    def do_code_agent(self, text: str, escalated: bool = False) -> str:
        """Code-session messages go through a real act-observe loop: the model calls
        tools, termind EXECUTES them, results feed back. No more command-printing.
        When the local model gets stuck it can be ESCALATED (on consent) to a frontier
        model for the rest of the task — every step logged in the audit ledger."""
        self.last_options = []       # fresh turn → clear any prior quick-replies
        stuck = getattr(self, "_stuck_task", None)
        if stuck and re.search(r"keep it local", text, re.I):
            self._stuck_task = None
            return "Staying local. Try 'write a simpler version', or split it into smaller files."
        if stuck and re.search(r"escalat", text, re.I):   # user clicked the escalate chip
            text, escalated = stuck, True
            self._stuck_task = None
        elif stuck:
            self._stuck_task = None                        # moved on → drop the stale offer
        if self.agent_mode == "plan":
            return self.do_plan(text)
        sys_p = self.CODE_SYS.format(ws=self.workspace(),
                                     tools=tcmod.summary(self.toolchain) or "unknown")
        listing = self._ws_filelist()
        if listing:
            sys_p += "\nFiles currently in the workspace: " + listing
        msgs = [{"role": "system", "content": sys_p}] + self.history[-8:] + [
            {"role": "user", "content": text}]
        log = []
        final = None
        nudges = 0
        last_fail = None
        repeats = 0
        tier = self.store.get("tier", "smart")
        if escalated:
            brain = (lambda m: self._frontier(m, consent=text, why="code-agent escalation"))
            log.append("⤴ escalated to the frontier model (logged in /ledger)")
        elif tier == "max" and self.frontier_ready():
            brain = (lambda m: self._frontier(m, consent=text, why="max-tier code agent"))
            log.append("⚡ max tier — frontier model active (logged in /ledger)")
        else:
            brain = (lambda m: self._chat(m, fmt_json=True))
        for _ in range(12):
            with Thinking("frontier model building" if (escalated or tier == "max") else "code agent working"):
                try:
                    raw = brain(msgs)
                except Exception as e:                   # frontier unreachable mid-task
                    final = f"frontier unreachable: {e} — the task stays where the local model left it."
                    break
            act = parse_action(raw)
            if "final" in act and "tool" not in act:    # prose → treat as conversation
                act = {"tool": "say", "say": act["final"]}
            tool = str(act.get("tool", "")).lower()
            if tool == "ask":                        # clarifying question with clickable choices
                opts = [str(o) for o in (act.get("options") or [])][:6]
                self.last_options = opts              # web renders these as quick-reply chips
                q = str(act.get("say") or "Which option?")
                final = q + "".join(f"\n  {i}. {o}" for i, o in enumerate(opts, 1))
                break
            if tool in ("done", "say") or ("say" in act and not tool):
                wrote = any(line.startswith("✓ write") for line in log)
                buildish = re.search(r"\b(create|build|make|website|project|app|tool|script)\b",
                                     text, re.I)
                asking = "?" in str(act.get("say") or "")
                if not wrote and buildish and nudges < 2 and not asking:
                    nudges += 1
                    msgs += [{"role": "assistant", "content": json.dumps(act)},
                             {"role": "user", "content":
                              "RESULT: no files have been written yet — the task is not done. "
                              "Continue NOW with the next tool call (write the files)."}]
                    continue
                final = str(act.get("say") or "done.")
                break
            self._set_state({"mkdir": "Editing", "write": "Writing", "read": "Reading",
                             "run": "Analyzing"}.get(tool, "Analyzing"))
            if tool == "mkdir":
                res = self.do_mkdir(str(act.get("path", "")))
            elif tool == "write":
                res = self._agent_write(str(act.get("path", "")),
                                        str(act.get("content", "")))
            elif tool == "read":
                res = self.ws_read(str(act.get("path", "")))[:2000]
            elif tool == "run":
                res = self._agent_run(str(act.get("cmd", "")))
            else:
                res = f"unknown tool: {tool or act}"
            short = res.split("\n")[0][:110]
            failed = short.startswith(("⛔", "✗", "(", "can'", "couldn", "unknown",
                                       "command timed", "REJECTED"))
            if tool in ("mkdir", "write", "read", "run"):   # seal the action into the audit ledger
                outcome = ("blocked" if short.startswith(("⛔", "REJECTED"))
                           else "fail" if failed else "ok")
                nbytes = len(str(act.get("content", ""))) if tool == "write" and not failed else 0
                self.ledger.record(session=self.store.get("active_chat") or "default",
                                   tool=tool, target=str(act.get("path") or act.get("cmd") or ""),
                                   outcome=outcome, consent=text, bytes_written=nbytes, detail=short)
            log.append(("✗ " if failed else "✓ ") + f"{tool} → {short}")
            if failed and short == last_fail:
                repeats += 1
            else:
                repeats = 0
            last_fail = short if failed else None
            if repeats >= 2:                        # same failure 3x → stop the spiral
                if self.frontier_ready() and not escalated:
                    self._stuck_task = text          # offer a consented escalation (v0.22)
                    self.last_options = ["⤴ Escalate this step to Claude", "Keep it local"]
                    final = ("The local model is stuck on this. I can escalate the task to a "
                             "frontier model (Claude) — one consented step, every byte logged in "
                             "/ledger. Or keep it local.")
                else:
                    final = ("I kept hitting the same error and stopped retrying. Try: "
                             "'write a simpler version' or split the task into smaller files.")
                break
            extra = (" You are repeating a failing action — CHANGE approach: simpler "
                     "content, fewer/escaped quotes, or split into smaller files."
                     if repeats == 1 else "")
            msgs += [{"role": "assistant", "content": json.dumps(act)},
                     {"role": "user", "content": "RESULT: " + res[:2000] + extra}]
        if final is None:
            final = "(step limit reached — say 'continue' to keep going)"
        out = ("\n".join(log) + ("\n\n" if log else "")) + final
        self.history += [{"role": "user", "content": text},
                         {"role": "assistant", "content": final}]
        self.store["history"] = self.history[-20:]
        self._save_chat(text)
        store_save(self.store)
        return out

    def _agent_write(self, rel: str, content: str) -> str:
        full = self._safe_path(rel)
        if full is None:
            return f"⛔ blocked: '{rel}' is outside the workspace"
        if not rel.strip():
            return "(write needs a path)"
        content = self._strip_fences(content) if content.lstrip().startswith("```") else content
        if rel.endswith(".py"):
            err = self._py_error(rel, content)
            for _ in range(2):                      # heal HERE: error + broken code → model
                if not err or not self.live:
                    break
                with Thinking(f"healing {rel}"):
                    content = self._strip_fences(self._chat([
                        {"role": "system", "content":
                         "Reply ONLY with the corrected, complete file content — no fences."},
                        {"role": "user", "content":
                         f"This {rel} has a syntax error ({err}). Fix it and return the "
                         f"full file:\n\n{content}"}]))
                err = self._py_error(rel, content)
            if err:
                return (f"✗ couldn't produce valid python for {rel} ({err}) — write a "
                        "SIMPLER version: shorter strings, no tricky quotes, smaller file")
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        self.actions += 1
        return f"wrote {rel} ({len(content.splitlines())} lines)"

    def _ws_filelist(self, limit: int = 25) -> str:
        return ", ".join(e["path"] for e in self.ws_tree(limit) if not e["dir"])

    def _ensure_venv(self) -> str:
        """A project .venv so pip installs work on macOS (PEP 668) — created on demand."""
        vbin = os.path.join(self.workspace(), ".venv", "bin")
        if not os.path.isdir(vbin):
            py = (self.toolchain.get("python") or {}).get("cmd", "python3")
            try:
                subprocess.run([py, "-m", "venv", ".venv"], cwd=self.workspace(),
                               capture_output=True, text=True, timeout=120)
            except Exception:
                return ""
        return vbin if os.path.isdir(vbin) else ""

    def _venv_route(self, cmd: str) -> str:
        """pip install … → into the project .venv (fixes macOS 'externally-managed-environment')."""
        if re.match(r"\s*(?:python3?\s+-m\s+)?pip3?\s+install\b", cmd, re.I):
            vbin = self._ensure_venv()
            if vbin:
                rest = re.sub(r"^\s*(?:python3?\s+-m\s+)?pip3?\s+install\b", "", cmd, 1, re.I)
                return f"{vbin}/pip install" + rest
        return cmd

    def _agent_run(self, cmd: str) -> str:
        cmd = self._venv_route(self._fix_lang_cmds(cmd))
        tok = cmd.split()
        ws = self.workspace()
        compound = ("&&" in cmd or ";" in cmd or cmd.strip().startswith("cd ")
                    or ".venv/bin/" in cmd)
        # "app.py" or a wrong absolute path → find the real file in the workspace
        if tok and tok[0].endswith(".py"):
            cand = tok[0]
            if not os.path.isfile(os.path.join(ws, cand)):
                base = os.path.basename(cand)
                hits = [e["path"] for e in self.ws_tree()
                        if not e["dir"] and os.path.basename(e["path"]) == base]
                if hits:
                    cand = hits[0]
            if os.path.isfile(os.path.join(ws, cand)):
                cmd = "python3 " + cand + (" " + " ".join(tok[1:]) if tok[1:] else "")
        checked = cmd if compound else self._dry_run(cmd)
        if not checked:
            return (f"can't run — '{cmd.split()[0] if cmd.split() else cmd}' not found. "
                    f"Files present: {self._ws_filelist() or '(none)'}")
        ctok = checked.split()
        if len(ctok) >= 2 and ctok[0] in ("python3", "python") and ctok[1].endswith(".py") \
                and not os.path.isfile(os.path.join(ws, ctok[1].lstrip("/"))) \
                and not os.path.isfile(ctok[1]):
            base = os.path.basename(ctok[1])
            hits = [e["path"] for e in self.ws_tree()
                    if not e["dir"] and os.path.basename(e["path"]) == base]
            if hits:
                ctok[1] = hits[0]
                checked = " ".join(ctok)
            else:
                return (f"can't run — {ctok[1]} doesn't exist. Files present: "
                        f"{self._ws_filelist() or '(none)'}")
        try:
            res = subprocess.run(checked, shell=True, cwd=self.workspace(),
                                 capture_output=True, text=True, timeout=60)
            self.actions += 1
            return f"$ {checked}\n" + (res.stdout or res.stderr or "(no output)")[:1200]
        except subprocess.TimeoutExpired:
            return "command timed out (60s)"

    # ── agent modes: plan / act / bypass ─────────────────────────────────────
    @property
    def agent_mode(self) -> str:
        return self.store.get("agent_mode", "act")

    def set_mode(self, mode: str) -> str:
        mode = mode.strip().lower()
        if mode not in ("plan", "act", "bypass"):
            return "modes: plan (propose only, nothing executes) · act (default) · bypass (no confirms)"
        self.store["agent_mode"] = mode
        store_save(self.store)
        if mode == "bypass":
            self._confirm = lambda _p="": "y"      # every y/N auto-approves
        elif self._confirm is not input:
            self._confirm = input
        return {"plan": "📋 plan mode — I'll propose, nothing executes until you switch to act",
                "act": "▶ act mode — actions execute (file ops consented per request)",
                "bypass": "⚡ bypass mode — no confirmations, everything auto-runs"}[mode]

    def do_plan(self, request: str) -> str:
        """Plan mode: outline exactly what WOULD happen — zero side effects."""
        if not self.live:
            return ("📋 plan (offline): I would interpret your request, list the files/"
                    "folders to create or edit inside the workspace, and wait for act mode.")
        with Thinking("planning (nothing will execute)"):
            out = self._chat([
                {"role": "system", "content":
                 f"PLAN ONLY — nothing will be executed. Workspace: {self.workspace()}. "
                 "Outline the concrete steps: every file you would create or edit (with "
                 "paths), every folder, every command. Short numbered list. Do NOT write "
                 "the actual code."},
                {"role": "user", "content": request}])
        return "📋 PLAN (nothing executed — switch to ▶ act to do it):\n" + out

    def _safe_path(self, p: str):
        """The workspace jail: in code mode, every file op must stay inside the workspace."""
        full = os.path.abspath(os.path.join(self.workspace(), os.path.expanduser(p)))
        if getattr(self, "view_mode", "chat") == "code" or self.active_mode() == "code":
            ws = self.workspace()
            if not (full == ws or full.startswith(ws + os.sep)):
                return None
        return full

    def do_edit_file(self, rel: str, instruction: str) -> str:
        """Edit an EXISTING file in the workspace: read → model rewrites → heal → write."""
        if not self.live:
            return "file editing needs a live model — install Ollama (./setup.sh)"
        full = self._safe_path(rel)
        if full is None:
            return f"⛔ blocked: '{rel}' is outside the workspace (code mode is jailed)"
        if not os.path.isfile(full):
            return f"no such file in the workspace: {rel} (try /tree)"
        with open(full, "r", encoding="utf-8", errors="ignore") as f:
            old = f.read(40000)
        if self.agent_mode == "plan":
            return self.do_plan(f"edit {rel}: {instruction}")
        with Thinking(f"editing {rel}"):
            new = self._strip_fences(self._chat([
                {"role": "system", "content":
                 "You edit code files. Reply ONLY with the COMPLETE new file content — no "
                 "fences, no commentary. Keep everything that shouldn't change."},
                {"role": "user", "content":
                 f"File {rel}:\n\n{old}\n\nEdit it as follows: {instruction}"}]))
            for _ in range(2):                      # same self-heal gate as generation
                err = self._py_error(rel, new)
                if not err:
                    break
                new = self._strip_fences(self._chat([
                    {"role": "system", "content": "Reply ONLY with the corrected full file."},
                    {"role": "user", "content":
                     f"This {rel} has a syntax error ({err}). Fix it:\n\n{new}"}]))
        import difflib
        changed = sum(1 for d in difflib.unified_diff(
            old.splitlines(), new.splitlines()) if d[:1] in "+-")
        with open(full, "w", encoding="utf-8") as f:
            f.write(new)
        self.actions += 1
        return f"edited {rel} (±{max(changed-2,0)} lines) — open it: /read {rel}"

    def refresh_toolchain(self) -> dict:
        self.toolchain = tcmod.detect()
        self.store["toolchain"] = self.toolchain
        store_save(self.store)
        return self.toolchain

    def _fix_lang_cmds(self, cmd: str) -> str:
        """Rewrite bare interpreter names ANYWHERE in the command to the detected ones
        (e.g. 'cd app && python main.py' → '… python3 main.py' on a python3-only Mac)."""
        py = (self.toolchain.get("python") or {}).get("cmd")
        if py and py != "python":
            cmd = re.sub(r"\bpython\b(?!3)", py, cmd)
            cmd = re.sub(r"\bpip\b(?!3)", "pip3" if py == "python3" else "pip", cmd)
        return cmd

    # ── code mode: workspace, file tree, file reading ────────────────────────
    def set_workspace(self, path: str) -> str:
        full = os.path.abspath(os.path.expanduser(path.strip() or "."))
        if not os.path.isdir(full):
            return f"not a folder: {full}"
        c = self.store["chats"].get(self.store.get("active_chat") or "")
        if c is not None and c.get("mode") == "code":
            c["ws"] = full                      # THIS session's folder
        self.store["workspace"] = full          # global fallback for new sessions
        store_save(self.store)
        os.chdir(full)              # builds/writes/actions now happen here
        self.scan_workspace()       # v2.0: proactive security sweep on selection
        msg = f"workspace set: {full} — builds, files and commands run here now"
        s = scanmod.summary(self.last_scan)
        if not s["clean"]:
            msg += (f"\n⚠ security: {s['total']} issue(s) found here "
                    f"({s['high']} high · {s['medium']} medium · {s['low']} low) — run /scan")
        return msg

    def workspace(self) -> str:
        c = self.store["chats"].get(self.store.get("active_chat") or "")
        if c and c.get("mode") == "code" and c.get("ws") and os.path.isdir(c["ws"]):
            return c["ws"]                      # per-session workspace wins
        ws = self.store.get("workspace")
        if ws and os.path.isdir(ws):
            return ws
        return os.getcwd()

    def ws_browse(self, path: str = "") -> dict:
        """Server-side folder browser: the web picker navigates real directories here."""
        full = os.path.abspath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(full):
            full = os.path.expanduser("~")
        try:
            dirs = sorted(d for d in os.listdir(full)
                          if os.path.isdir(os.path.join(full, d)) and not d.startswith("."))
        except OSError:
            dirs = []
        parent = os.path.dirname(full)
        return {"current": full, "parent": parent if parent != full else None,
                "dirs": dirs[:200], "home": os.path.expanduser("~")}

    def ws_tree(self, max_entries: int = 200) -> list:
        """A compact file tree of the workspace (depth 3, junk dirs skipped)."""
        from .indexer import SKIP_DIRS
        root = self.workspace()
        out = []
        for dirpath, dirs, files in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= 3:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for d in dirs:
                out.append({"path": os.path.join("" if rel == "." else rel, d),
                            "dir": True, "depth": depth})
            for f in sorted(files):
                if f.startswith("."):
                    continue
                out.append({"path": os.path.join("" if rel == "." else rel, f),
                            "dir": False, "depth": depth})
            if len(out) >= max_entries:
                break
        return out[:max_entries]

    def ws_read(self, rel: str) -> str:
        full = os.path.abspath(os.path.join(self.workspace(), rel))
        if not full.startswith(self.workspace()):
            return "(outside the workspace)"
        if not os.path.isfile(full):
            return f"(no such file: {rel})"
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read(20000)
        except OSError as e:
            return f"(can't read: {e})"
        return txt or "(empty file)"

    # ── profile, settings, memory tools, support bot ─────────────────────────
    def profile(self) -> dict:
        p = self.store.get("profile") or {}
        return {"name": p.get("name", ""), "role": p.get("role", ""),
                "prefs": p.get("prefs", ""), "theme": p.get("theme", "dark"),
                "onboarded": bool(p.get("name"))}

    def set_profile(self, **fields) -> dict:
        p = self.store.setdefault("profile", {})
        for k in ("name", "role", "prefs", "theme"):
            if fields.get(k) is not None:
                p[k] = str(fields[k]).strip()
        store_save(self.store)
        return self.profile()

    def import_memories(self, text: str) -> str:
        """Paste memories exported from other AI platforms — each line becomes a fact."""
        added = 0
        for line in text.splitlines():
            fact = line.strip().lstrip("-•* ").strip()
            if not fact or len(fact) < 4 or fact in self.store["facts"]:
                continue
            if added >= 100:
                break
            self.store["facts"].append(fact[:300])
            self.k.syscall("mem.put", key=f"fact#{len(self.store['facts'])-1}",
                           value=fact[:300], tags=["fact", "imported"])
            added += 1
        store_save(self.store)
        return f"imported {added} memories — I know you better now"

    def export_memories(self) -> str:
        return "\n".join(self.store["facts"]) or "(no memories yet)"

    def clear_memory(self, what: str) -> str:
        if what == "facts":
            self.store["facts"] = []
            self.store["vecs"] = {}
        elif what == "docs":
            self.store["docs"] = {}
            self.chunks = 0
        elif what == "chats":
            self.store["chats"] = {}
            self.store["active_chat"] = None
            self.store["history"] = []
            self.history = []
        store_save(self.store)
        return f"cleared {what}"

    def do_helpbot(self, q: str) -> str:
        """Support bot: answers about termind FROM the built-in docs (works offline too)."""
        if not self.live:
            return best_topic(q)
        with Thinking("checking the manual"):
            return self._chat([
                {"role": "system", "content":
                 "You are termind's support assistant. Answer the question using ONLY this "
                 "documentation — be concise and concrete:\n\n" + HELP_DOC},
                {"role": "user", "content": q}])

    def do_model(self, name: str = "") -> str:
        if not name:
            installed = list_models()
            rows = [("★ " if m.split(":")[0] == self.model.split(":")[0] else "  ") + m
                    for m in installed] or ["  (none pulled yet — try: /pull gemma3)"]
            guide = "\n".join(f"  /pull {n:<12} {s:>7} — {d}" for n, s, d in CATALOG
                              if n.split(":")[0] not in
                              {m.split(":")[0] for m in installed})
            return ("active: " + self.model + "\n" + "\n".join(rows)
                    + "\nswitch: /model <name> · sessions: /chats"
                    + ("\nget more brains (guided):\n" + guide if guide else "")
                    + "\nbring YOUR OWN model: /import <path.gguf> · "
                      "/pull hf.co/<user>/<repo> · remote server: OLLAMA_HOST=<url>")
        if not model_available(name):
            return f"'{name}' isn't pulled yet — run: /pull {name}"
        self.model = name
        self.store["model"] = name
        store_save(self.store)
        self.live = self.server and True
        return f"switched to {name} (saved — future sessions use it too)"

    def model_catalog(self) -> dict:
        """Everything the model manager UI needs: installed, curated catalog, pull state."""
        installed = list_models()
        bases = {m.split(":")[0] for m in installed}
        return {"installed": installed, "active": self.model,
                "catalog": [{"name": n, "size": s, "desc": d, "installed": n in bases}
                            for n, s, d in CATALOG],
                "pull": dict(self.pull), "server": self.server}

    def start_pull(self, name: str) -> str:
        """Background download with live progress (the web UI polls self.pull)."""
        if self.pull.get("status") == "pulling":
            return f"already downloading {self.pull.get('name')} — one at a time"
        if not self.server:
            return "Ollama isn't running — install/start it first (./setup.sh)"
        self.pull = {"status": "pulling", "name": name, "pct": 0, "stage": "starting"}

        def run():
            try:
                pull_stream(name, lambda pct, st: self.pull.update(
                    {"pct": pct if pct is not None else self.pull.get("pct", 0),
                     "stage": st or self.pull.get("stage", "")}))
                self.pull = {"status": "done", "name": name, "pct": 100}
            except Exception as e:
                self.pull = {"status": "error", "name": name, "error": str(e)[:140]}
        threading.Thread(target=run, daemon=True).start()
        return f"downloading {name} in the background — progress shows in ⚙ Models"

    def import_model(self, path: str, name: str = None) -> str:
        """Register the user's OWN model (a local .gguf fine-tune) with Ollama, in the
        background. After this it behaves like any catalog model: /model <name>, web picker…"""
        full = os.path.expanduser(path.strip())
        if not full.lower().endswith(".gguf") or not os.path.isfile(full):
            return ("import needs the path to a local .gguf file "
                    "(e.g. /import ~/models/my-finetune.gguf my-model)")
        if not shutil.which("ollama"):
            return "ollama isn't installed — run ./setup.sh first"
        if self.pull.get("status") == "pulling":
            return f"busy with {self.pull.get('name')} — one download/import at a time"
        name = re.sub(r"[^a-z0-9.-]+", "-",
                      (name or os.path.splitext(os.path.basename(full))[0]).lower()).strip("-")
        self.pull = {"status": "pulling", "name": name, "pct": 0,
                     "stage": "importing your gguf"}

        def run():
            import tempfile
            mf = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".Modelfile",
                                                 delete=False) as f:
                    f.write(f"FROM {full}\n")
                    mf = f.name
                r = subprocess.run(["ollama", "create", name, "-f", mf],
                                   capture_output=True, text=True, timeout=3600)
                if r.returncode != 0:
                    self.pull = {"status": "error", "name": name,
                                 "error": (r.stderr or "ollama create failed").strip()[:140]}
                else:
                    self.pull = {"status": "done", "name": name, "pct": 100}
            except Exception as e:
                self.pull = {"status": "error", "name": name, "error": str(e)[:140]}
            finally:
                if mf:
                    try:
                        os.unlink(mf)
                    except OSError:
                        pass
        threading.Thread(target=run, daemon=True).start()
        return (f"importing your model as '{name}' — when it's done, switch with: "
                f"/model {name}")

    def add_model(self, spec: str, name: str = None) -> str:
        """One entry point for 'bring your own model': a local .gguf path → import;
        anything else (hf.co/user/repo, ollama names) → pull."""
        spec = spec.strip()
        if not spec:
            return ("usage: a local .gguf path, or hf.co/<user>/<repo>, or any Ollama "
                    "model name")
        if spec.lower().endswith(".gguf"):
            return self.import_model(spec, name)
        return self.start_pull(spec)

    def do_pull(self, name: str) -> str:
        if not shutil.which("ollama"):
            return "ollama isn't installed — run ./setup.sh first"
        print(f"  {D}downloading {name} — Ollama will show progress…{N}")
        r = subprocess.run(["ollama", "pull", name], timeout=3600)
        if r.returncode != 0:
            return f"pull failed — check the model name ({name})"
        return f"{name} ready · switch to it:  /model {name}"

    def do_chat(self, text: str) -> str:
        msgs = self.chat_messages(text)
        tier = self.store.get("tier", "smart")
        with Thinking("neural core thinking"):
            if tier == "max" and self.frontier_ready():
                try:
                    reply = self._frontier(msgs, consent=text, why="max-tier chat")
                except Exception:
                    reply = self._chat(msgs) if self.live else offline_chat(msgs)
            elif tier == "smarter" and self.live:
                big = os.environ.get("TERMIND_BIG_MODEL")
                reply = (chat(msgs, model=big) if big else self._chat(msgs))
            else:
                reply = self._chat(msgs) if self.live else offline_chat(msgs)
        self.history += [{"role": "user", "content": text},
                         {"role": "assistant", "content": reply}]
        self.store["history"] = self.history[-20:]   # legacy field (kept for compat)
        self._save_chat(text)                        # persist into the active chat session
        note = ""
        if AUTO_FACT.search(text) and text not in self.store["facts"]:
            self.do_remember(text, auto=True)        # auto-memory: it learns you from chat
            note = f"\n{D}◆ auto-remembered this about you{N}"
        store_save(self.store)
        return reply + note

    def do_status(self) -> str:
        if self.live:
            brain = f"{self.model} (live, local)"
        elif getattr(self, "server", False):
            brain = f"server up, model missing (run: /pull {self.model})"
        else:
            brain = "offline brain (run ./setup.sh)"
        led = self.ledger.summary()
        esc = [e for e in self.ledger.entries
               if e.get("tool") == "escalate" and e.get("outcome") == "ok"]
        off = sum(int(e.get("bytes", 0)) for e in esc)
        off_str = (f"{off} bytes across {len(esc)} consented cloud escalation(s), all logged"
                   if esc else "0 bytes")
        return (f"brain: {brain} · facts remembered: {len(self.store['facts'])} · "
                f"indexed chunks: {self.chunks} · chat turns kept: {len(self.history)} · "
                f"actions run: {getattr(self, 'actions', 0)} · credits spent: {self.spent:.2f} · "
                f"denied by sandbox: {self.denied} · audit ledger: {led['count']} actions "
                f"({led['integrity']}) · data off-machine: {off_str}")

    def _ledger_report(self, line: str) -> str:
        """`/ledger` (recent + integrity) · `/ledger verify` · `/ledger export [path]`."""
        arg = line[len("/ledger"):].strip()
        led = self.ledger
        if arg.startswith("export"):
            dest = arg[len("export"):].strip() or os.path.join(
                os.environ.get("TERMIND_HOME", os.path.expanduser("~/.termind")),
                "ledger-export.json")
            try:
                with open(dest, "w") as f:
                    json.dump(led.export(), f, indent=2)
            except OSError as e:
                return f"couldn't write export: {e}"
            return f"exported {led.summary()['count']} actions → {dest}"
        if arg.startswith("verify"):
            v = led.verify()
            return (f"audit ledger VERIFIED — chain intact, {v['count']} actions, no tampering"
                    if v["ok"] else
                    f"⚠ audit ledger TAMPERED at entry #{v['broken_at']} of {v['count']}")
        s = led.summary()
        rows = [f"  {e['iso']}  {e['outcome']:<7} {e['tool']:<6} {e['target'][:46]}"
                for e in led.tail(12)]
        head = (f"audit ledger · {s['count']} actions "
                f"({s['ok']} ok · {s['fail']} fail · {s['blocked']} blocked) · "
                f"{s['bytes']} bytes written · integrity: {s['integrity']}")
        return head + ("\n" + "\n".join(rows) if rows else "\n  (no actions recorded yet)") \
            + "\nexport for a security review: /ledger export"

    # ── v2.0: database operations ────────────────────────────────────────────
    _SQL_LEAD = re.compile(r"^\s*(select|insert|update|delete|with|create|drop|alter|"
                           r"truncate|replace|pragma|explain)\b", re.I)

    def databases(self) -> list:
        return self.store.get("databases", [])

    def active_db(self):
        return self.store.get("active_db")

    def db_context(self) -> str:
        """The selected DB, for the bottom-dock context bar (alongside the folder)."""
        name = self.active_db()
        if not name:
            return ""
        rec = next((d for d in self.databases() if d["name"] == name), {})
        return f"{name} ({rec.get('engine', '?')})"

    def _database(self):
        name = self.active_db()
        if not name:
            return None
        if self._db is not None and self._db.name == name:
            return self._db
        rec = next((d for d in self.databases() if d["name"] == name), None)
        if not rec:
            return None
        self._db = dbmod.Database(rec["name"], rec["spec"])
        return self._db

    def _driver_hint(self, engine: str) -> str:
        mod = dbmod.DRIVERS.get(engine, "?")
        venv = os.path.join(self.workspace(), ".venv", "bin", "pip")
        return (f"{engine} needs the '{mod}' driver. termind keeps it ISOLATED in the workspace "
                f"venv — install with:  {venv} install {mod}")

    def db_add(self, name: str, spec: str) -> str:
        name, spec = name.strip(), spec.strip()
        if not name or not spec:
            return ("usage: /db add <name> <dsn>   e.g.  /db add app ./app.db   or "
                    "postgres://user:pass@host/db")
        engine, target = dbmod.parse_dsn(spec)
        self.store["databases"] = [d for d in self.databases() if d["name"] != name] + \
            [{"name": name, "spec": spec, "engine": engine}]
        self.store["active_db"] = name
        self._db = None
        store_save(self.store)
        if engine == "sqlite" and target not in (":memory:", ""):
            self.manifest.record("db", target, f"{name} (sqlite)")
        note = ""
        if engine != "sqlite" and not dbmod.engines_available().get(engine):
            note = "\n" + self._driver_hint(engine)
        return f"added '{name}' ({engine}) and selected it.{note}"

    def db_use(self, name: str) -> str:
        name = name.strip()
        if not any(d["name"] == name for d in self.databases()):
            return f"no database '{name}'. add one: /db add <name> <dsn>"
        self.store["active_db"] = name
        self._db = None
        store_save(self.store)
        return self.db_status()

    def db_status(self) -> str:
        name = self.active_db()
        if not name:
            return ("no database selected. add one in Settings → Databases, or "
                    "/db add <name> <dsn>")
        rec = next((d for d in self.databases() if d["name"] == name), {})
        eng = rec.get("engine", "?")
        try:
            tabs = self._database().tables()
            return (f"selected: {name} ({eng}) · {len(tabs)} table(s): "
                    f"{', '.join(tabs[:12]) or '(none)'}")
        except dbmod.DriverMissing:
            return f"selected: {name} ({eng}) — {self._driver_hint(eng)}"
        except Exception as e:
            return f"selected: {name} ({eng}) — couldn't connect: {e}"

    def db_schema(self, table: str = "") -> str:
        d = self._database()
        if not d:
            return "no database selected."
        try:
            sch = d.schema(table.strip() or None)
        except dbmod.DriverMissing:
            return self._driver_hint(d.engine)
        except Exception as e:
            return f"couldn't read schema: {e}"
        if not sch:
            return "(no tables)"
        return "\n".join(f"{t}({', '.join(c + ' ' + ty for c, ty in cols)})"
                         for t, cols in sch.items())

    def do_db(self, line: str) -> str:
        arg = line[3:].strip() if line.startswith("/db") else line.strip()
        if not arg or arg == "status":
            return self.db_status()
        if arg == "list":
            dbs = self.databases()
            if not dbs:
                return "no databases. add one: /db add <name> <dsn>"
            cur = self.active_db()
            return "\n".join(("● " if d["name"] == cur else "  ")
                             + f"{d['name']:<14} {d['engine']:<9} {d['spec']}" for d in dbs)
        if arg.startswith("add"):
            p = arg.split(None, 2)
            return self.db_add(p[1] if len(p) > 1 else "", p[2] if len(p) > 2 else "")
        if arg.startswith("use"):
            return self.db_use(arg[3:].strip())
        if arg.startswith("schema"):
            return self.db_schema(arg[6:].strip())
        if arg.startswith(("confirm", "cancel")):
            return self._confirm_sql(arg)
        if arg.startswith("query"):
            return self.do_db_query(arg[5:].strip())
        return self.do_db_query(arg)              # bare "/db <text>" → a query

    def do_db_query(self, text: str) -> str:
        d = self._database()
        if not d:
            return ("no database selected — add one in Settings → Databases, or "
                    "/db add <name> <dsn>")
        if not text.strip():
            return "usage: /db query <natural language or SQL>"
        if self._SQL_LEAD.match(text):
            sql = text.strip().rstrip(";")
        else:
            sql = self._nl_to_sql(d, text)
            if sql.startswith("✗"):
                return sql
        return self._run_sql(d, sql, consent=text)

    def _nl_to_sql(self, d, nl: str) -> str:
        if not self.live:
            return ("✗ natural-language → SQL needs a live local model — write the SQL "
                    "directly, or start Ollama.")
        try:
            sch = d.schema()
        except Exception:
            sch = {}
        schema_txt = "\n".join(f"{t}({', '.join(c for c, _ in cols)})"
                               for t, cols in sch.items()) or "(unknown schema)"
        self._set_state("Analyzing")
        raw = self._chat([
            {"role": "system", "content":
             "Translate the request into ONE valid SQL statement for this schema. Reply with "
             "SQL ONLY — no prose, no markdown fences.\nSchema:\n" + schema_txt},
            {"role": "user", "content": nl}])
        sql = self._strip_fences(raw).strip().rstrip(";")
        if not self._SQL_LEAD.match(sql):
            return f"✗ couldn't turn that into SQL. Model said: {raw[:160]}"
        return sql

    def _run_sql(self, d, sql: str, consent: str = "") -> str:
        cid = self.store.get("active_chat") or "default"
        v = d.verify(sql)
        if not v.get("ok"):
            return f"✗ invalid SQL: {v.get('error')}\n  {sql}"
        if dbmod.is_destructive(sql):
            try:
                pv = d.preview(sql)
            except dbmod.DriverMissing:
                return self._driver_hint(d.engine)
            self._pending_sql = (d.name, sql)
            aff = pv.get("affected")
            impact = (f"{aff} row(s) affected" if isinstance(aff, int) and aff >= 0
                      else "schema change (DDL)" if aff == -1 else "impact: review carefully")
            plan = ("\n  plan: " + " → ".join(pv.get("plan", [])[:3])) if pv.get("plan") else ""
            self.ledger.record(session=cid, tool="db-preview",
                               target=f"{d.name}: {sql[:80]}", outcome="ok",
                               consent=consent, detail=impact)
            return (f"⚠ DESTRUCTIVE on '{d.name}':\n  {sql}\n  preview: {impact}{plan}\n"
                    "  reply 'confirm' to run it, or 'cancel'. (Nothing has changed.)")
        self._set_state("Reading")
        try:
            res = d.run(sql)
        except dbmod.DriverMissing:
            return self._driver_hint(d.engine)
        except Exception as e:
            return f"✗ query failed: {e}\n  {sql}"
        self.ledger.record(session=cid, tool="db-read", target=f"{d.name}: {sql[:80]}",
                           outcome="ok", consent=consent)
        return f"$ {sql}\n" + self._fmt_rows(res)

    def _confirm_sql(self, text: str) -> str:
        if not self._pending_sql:
            return "nothing pending."
        name, sql = self._pending_sql
        if re.search(r"\b(cancel|no|abort)\b", text, re.I):
            self._pending_sql = None
            return f"cancelled — '{name}' is unchanged."
        d = self._database()
        if not d or d.name != name:
            self._pending_sql = None
            return "the pending query's database is no longer selected — cancelled."
        try:
            r = d.execute(sql)
        except Exception as e:
            self._pending_sql = None
            return f"✗ execution failed: {e}"
        self._pending_sql = None
        self.ledger.record(session=self.store.get("active_chat") or "default",
                           tool="db-write", target=f"{name}: {sql[:80]}", outcome="ok",
                           detail=f"{r['affected']} rows")
        return f"✓ executed on '{name}': {r['affected']} row(s) changed · logged in /ledger"

    @staticmethod
    def _fmt_rows(res: dict) -> str:
        cols, rows = res.get("columns", []), res.get("rows", [])
        if not cols:
            return "(no columns)"
        if not rows:
            return " | ".join(cols) + "\n(no rows)"
        w = [max(len(str(cols[i])), *(len(str(r[i])) for r in rows)) for i in range(len(cols))]
        line = lambda vals: " | ".join(str(v).ljust(w[i]) for i, v in enumerate(vals))
        out = [line(cols), "-+-".join("-" * x for x in w)] + [line(r) for r in rows]
        if res.get("truncated"):
            out.append(f"… (showing first {len(rows)})")
        return "\n".join(out)

    def _db_followup(self, text: str):
        """A bare 'confirm'/'cancel' resolves a pending destructive query."""
        if self._pending_sql and re.match(r"\s*(confirm|yes|run it|run|cancel|no|abort)\b",
                                           text, re.I):
            return self._confirm_sql(text)
        return None

    # ── v2.0: proactive security scanning ────────────────────────────────────
    def scan_workspace(self) -> list:
        try:
            self.last_scan = scanmod.scan_folder(self.workspace())
        except Exception:
            self.last_scan = []
        return self.last_scan

    def _scan_report(self) -> str:
        s = scanmod.summary(self.last_scan)
        if s["clean"]:
            return f"✓ security scan clean — no exposed secrets, dangerous scripts, or insecure deps."
        head = (f"⚠ security scan: {s['total']} issue(s) — {s['high']} high · "
                f"{s['medium']} medium · {s['low']} low")
        rows = [f"  [{f['severity']}] {f['kind']} — {f['file']}:{f['line']} ({f['snippet']})\n"
                f"     fix: {f['fix']}" for f in self.last_scan[:8]]
        more = f"\n  …and {s['total'] - 8} more" if s["total"] > 8 else ""
        return head + "\n" + "\n".join(rows) + more

    def do_scan(self) -> str:
        self.scan_workspace()
        return self._scan_report()

    # ── v2.2: CA workbench (local, audited) ──────────────────────────────────
    def do_ca(self, line: str) -> str:
        """CA workbench dispatcher. v2.2: /ca bank <statement> → Tally vouchers + ledger CSV."""
        arg = (line or "").strip()
        if not arg or arg in ("help", "?"):
            return CA_HELP
        parts = arg.split(None, 1)
        sub, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if sub == "bank":
            return self.do_ca_bank(rest)
        if sub == "scrutiny":
            return self.do_ca_scrutiny(rest)
        if sub == "gst":
            return self.do_ca_gst(rest)
        if sub == "notice":
            return self.do_ca_notice(rest)
        if sub in ("fs", "finstmt", "financials"):
            return self.do_ca_fs(rest)
        return f"unknown CA section '{sub}'.\n{CA_HELP}"

    def _ca_brain(self, fmt_json: bool = True):
        """The local model as a classify/draft brain — None when offline so callers fall back."""
        if not self.live:
            return None
        return lambda msgs: self._chat(msgs, fmt_json=fmt_json)

    def _ca_in_path(self, arg: str) -> str:
        """Resolve a statement path: as given, or relative to the workspace, or to cwd."""
        a = os.path.expanduser(arg.strip().strip('"').strip("'"))
        for cand in (a, os.path.join(self.workspace(), a), os.path.join(os.getcwd(), a)):
            if os.path.isfile(cand):
                return os.path.abspath(cand)
        return os.path.abspath(a)

    def do_ca_bank(self, arg: str) -> str:
        """Terminal entry: /ca bank <statement> → Tally XML + ledger CSV, with a text report."""
        if not arg:
            return ("usage: /ca bank <statement.csv|.xlsx|.pdf>\n"
                    "parses the statement, suggests a ledger head + voucher for each line "
                    "(rules first, the local model for the rest), and writes Tally import XML "
                    "+ a review CSV into your workspace. Nothing leaves the machine.")
        res = self._ca_bank_run(self._ca_in_path(arg), consent=f"/ca bank {arg}")
        if res.get("error"):
            return res["error"]
        return self._ca_bank_report(res["summary"], res["xml_path"], res["csv_path"])

    def _ca_bank_run(self, path: str, consent: str = "") -> dict:
        """The engine shared by terminal + web: parse → classify (local) → export, all audited.
        Returns {summary, xml_path, csv_path} or {error}."""
        if not os.path.isfile(path):
            return {"error": f"no such statement: {path}  (give a path to a .csv / .xlsx / .pdf)"}
        cid = self.store.get("active_chat") or "default"
        try:
            txns = cabank.parse_statement(path)
        except cabank.StatementError as e:
            return {"error": f"⚠ {e}"}
        except Exception as e:                       # never crash on a malformed file
            return {"error": f"⚠ couldn't read that statement: {e}"}
        if not txns:
            return {"error": "parsed it, but found no transactions — is it a bank-statement export?"}
        self.ledger.record(session=cid, tool="ca.bank.parse", target=os.path.basename(path),
                           outcome="ok", consent=consent, detail=f"{len(txns)} transactions")
        brain = None
        if self.live:
            brain = lambda msgs: self._chat(msgs, fmt_json=True)  # local model, leftovers only
        with Thinking(f"classifying {len(txns)} transactions"):
            classified = cabank.classify(txns, brain=brain)
        s = cabank.summary(classified)
        base = re.sub(r"[^\w.-]", "_", os.path.splitext(os.path.basename(path))[0]) or "statement"
        name = (self.profile() or {}).get("name", "") or ""
        xml = cabank.to_tally_xml(classified, company=name, bank_ledger="Bank")
        csv_out = cabank.to_csv(classified)
        xml_path = self._ca_write(f"{base}_tally.xml", xml)
        csv_path = self._ca_write(f"{base}_ledger.csv", csv_out)
        self.ledger.record(session=cid, tool="ca.bank.export", target=xml_path, outcome="ok",
                           consent=consent, bytes_written=len(xml.encode()),
                           detail=f"{s['transactions']} vouchers")
        self.actions += 1
        return {"summary": s, "xml_path": xml_path, "csv_path": csv_path,
                "xml_text": xml, "csv_text": csv_out}

    def _ca_save_upload(self, path: str = None, filename: str = None, content: str = None):
        """Resolve a CA input: save an uploaded file's text into the workspace, or resolve a path.
        Returns an absolute path, or an {ok:False,error} dict the API can return as-is."""
        if content is not None and filename:
            safe = re.sub(r"[^\w.-]", "_", os.path.basename(filename)) or "upload.csv"
            dest = os.path.join(self.workspace(), safe)
            try:
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manifest.record("ca-import", dest, safe)
            except OSError as e:
                return {"ok": False, "error": f"couldn't save the upload: {e}"}
            return dest
        if path:
            return self._ca_in_path(path)
        return {"ok": False, "error": "give a file path, or upload a file"}

    def ca_bank_api(self, path: str = None, filename: str = None, content: str = None) -> dict:
        """Web entry: accept a workspace path OR an uploaded file's text, run the engine, and
        return structured results the CA panel can render. Uploads land in the workspace."""
        path = self._ca_save_upload(path, filename, content)
        if isinstance(path, dict):
            return path
        res = self._ca_bank_run(path, consent=f"web: /ca bank {os.path.basename(path)}")
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        s = res["summary"]
        return {"ok": True, "summary": s,
                "ledgers": [{"head": h, **v} for h, v in s["ledgers"].items()],
                "xml": os.path.basename(res["xml_path"]),
                "csv": os.path.basename(res["csv_path"]),
                "xml_content": res["xml_text"], "csv_content": res["csv_text"],
                "workspace": self.workspace(),
                "report": self._ca_bank_report(s, res["xml_path"], res["csv_path"])}

    def _ca_write(self, name: str, content: str) -> str:
        out = os.path.join(self.workspace(), name)
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        self.manifest.record("ca-export", out, name)
        return out

    def _ca_bank_report(self, s: dict, xml_path: str, csv_path: str) -> str:
        top = list(s["ledgers"].items())[:6]
        rows = "\n".join(f"  {head[:30]:<30} {v['count']:>3} txn   ₹{v['amount']:,.2f}"
                         for head, v in top)
        model_note = f" · {s['by_model']} by the local model" if s["by_model"] else ""
        return (f"📒 bank statement → ledger  ·  {s['transactions']} transactions\n"
                f"   in ₹{s['total_in']:,.2f}   ·   out ₹{s['total_out']:,.2f}\n"
                f"   {s['auto_classified']} auto-classified by rules{model_note} · "
                f"{s['needs_review']} need a quick review\n"
                f"top ledgers:\n{rows}\n"
                f"→ Tally import XML : {xml_path}\n"
                f"→ review CSV       : {csv_path}\n"
                "import in Tally: Gateway → Import → Vouchers. Check the Suspense lines first. "
                "(parse + export are in /ledger — proof the data never left this machine.)")

    # ── v2.3: ledger scrutiny ────────────────────────────────────────────────
    def do_ca_scrutiny(self, arg: str) -> str:
        if not arg:
            return "usage: /ca scrutiny <ledger.csv|.xlsx|.pdf>  — anomaly pass over a ledger"
        res = self._ca_scrutiny_run(self._ca_in_path(arg), consent=f"/ca scrutiny {arg}")
        return res["error"] if res.get("error") else self._ca_scrutiny_report(res)

    def _ca_scrutiny_run(self, path: str, consent: str = "") -> dict:
        if not os.path.isfile(path):
            return {"error": f"no such ledger: {path}  (give a .csv / .xlsx / .pdf)"}
        cid = self.store.get("active_chat") or "default"
        try:
            txns = cabank.parse_statement(path)
        except cabank.StatementError as e:
            return {"error": f"⚠ {e}"}
        except Exception as e:
            return {"error": f"⚠ couldn't read that ledger: {e}"}
        if not txns:
            return {"error": "no transactions found — is this a ledger export?"}
        self.ledger.record(session=cid, tool="ca.scrutiny.parse", target=os.path.basename(path),
                           outcome="ok", consent=consent, detail=f"{len(txns)} entries")
        with Thinking(f"scrutinizing {len(txns)} entries"):
            flags = cascrutiny.scrutinize(txns)
            s = cascrutiny.summary(flags, txns)
            note = cascrutiny.narrative(flags, s, brain=self._ca_brain(fmt_json=False))
        base = re.sub(r"[^\w.-]", "_", os.path.splitext(os.path.basename(path))[0]) or "ledger"
        csv_text = cascrutiny.to_csv(flags)
        csv_path = self._ca_write(f"{base}_scrutiny.csv", csv_text)
        self.ledger.record(session=cid, tool="ca.scrutiny.report", target=csv_path, outcome="ok",
                           consent=consent, bytes_written=len(csv_text.encode()),
                           detail=f"{s['flags']} flags")
        self.actions += 1
        return {"summary": s, "flags": flags, "narrative": note,
                "csv_path": csv_path, "csv_text": csv_text}

    def _ca_scrutiny_report(self, res: dict) -> str:
        s, flags = res["summary"], res["flags"]
        if s["clean"]:
            return (f"🔍 ledger scrutiny — {s['transactions']} entries, no anomalies flagged.\n"
                    f"{res['narrative']}\n→ {res['csv_path']}")
        rows = "\n".join(f"  [{f.severity}] {f.kind} — {f.date} {f.narration[:32]} ₹{f.amount:,.0f}"
                         for f in flags[:10])
        more = f"\n  …and {s['flags'] - 10} more" if s["flags"] > 10 else ""
        return (f"🔍 ledger scrutiny — {s['transactions']} entries · {s['flags']} flag(s) "
                f"({s['high']} high · {s['medium']} medium · {s['low']} low)\n{rows}{more}\n"
                f"opinion: {res['narrative']}\n→ {res['csv_path']}")

    def ca_scrutiny_api(self, path=None, filename=None, content=None) -> dict:
        p = self._ca_save_upload(path, filename, content)
        if isinstance(p, dict):
            return p
        res = self._ca_scrutiny_run(p, consent=f"web: /ca scrutiny {os.path.basename(p)}")
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        s = res["summary"]
        return {"ok": True, "summary": s, "narrative": res["narrative"],
                "flags": [{"severity": f.severity, "kind": f.kind, "date": f.date,
                           "narration": f.narration, "amount": f.amount, "detail": f.detail}
                          for f in res["flags"][:100]],
                "csv": os.path.basename(res["csv_path"]), "csv_content": res["csv_text"],
                "report": self._ca_scrutiny_report(res)}

    # ── v2.4: GST reconciliation ─────────────────────────────────────────────
    def do_ca_gst(self, arg: str) -> str:
        bits = arg.split()
        if len(bits) < 2:
            return "usage: /ca gst <purchase-register> <gstr-2b>   (two files: books, then 2B)"
        res = self._ca_gst_run(self._ca_in_path(bits[0]), self._ca_in_path(bits[1]),
                               consent=f"/ca gst {arg}")
        return res["error"] if res.get("error") else self._ca_gst_report(res)

    def _ca_gst_run(self, books_path: str, portal_path: str, consent: str = "") -> dict:
        for p, label in ((books_path, "purchase register"), (portal_path, "GSTR-2B")):
            if not os.path.isfile(p):
                return {"error": f"no such {label}: {p}"}
        cid = self.store.get("active_chat") or "default"
        try:
            books = cagst.parse_invoices(books_path, source="books")
            portal = cagst.parse_invoices(portal_path, source="portal")
        except cabank.StatementError as e:
            return {"error": f"⚠ {e}"}
        except Exception as e:
            return {"error": f"⚠ couldn't read the GST files: {e}"}
        self.ledger.record(session=cid, tool="ca.gst.parse",
                           target=f"{os.path.basename(books_path)} + {os.path.basename(portal_path)}",
                           outcome="ok", consent=consent,
                           detail=f"{len(books)} books / {len(portal)} 2B")
        result = cagst.reconcile(books, portal)
        s = cagst.summary(result)
        csv_text = cagst.to_csv(result)
        csv_path = self._ca_write("gst_reconciliation.csv", csv_text)
        self.ledger.record(session=cid, tool="ca.gst.report", target=csv_path, outcome="ok",
                           consent=consent, bytes_written=len(csv_text.encode()),
                           detail=f"ITC at risk {s['itc_at_risk']}")
        self.actions += 1
        return {"summary": s, "result": result, "csv_path": csv_path, "csv_text": csv_text}

    def _ca_gst_report(self, res: dict) -> str:
        s = res["summary"]
        return (f"🧾 GST 2B reconciliation\n"
                f"   matched {s['matched']} · value mismatch {s['value_mismatch']} · "
                f"invoice-no typo? {s['probable_invoice_typo']}\n"
                f"   ⚠ in books, not in 2B: {s['in_books_not_2b']}  → ITC at risk "
                f"₹{s['itc_at_risk']:,.2f}\n"
                f"   in 2B, not in books: {s['in_2b_not_books']}  → ITC available unbooked "
                f"₹{s['itc_available_unbooked']:,.2f}\n→ {res['csv_path']}")

    def ca_gst_api(self, books=None, books_name=None, books_content=None,
                   portal=None, portal_name=None, portal_content=None) -> dict:
        bp = self._ca_save_upload(books, books_name, books_content)
        if isinstance(bp, dict):
            return bp
        pp = self._ca_save_upload(portal, portal_name, portal_content)
        if isinstance(pp, dict):
            return pp
        res = self._ca_gst_run(bp, pp, consent="web: /ca gst")
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "summary": res["summary"],
                "csv": os.path.basename(res["csv_path"]), "csv_content": res["csv_text"],
                "report": self._ca_gst_report(res)}

    # ── v2.5: notice-reply drafting ──────────────────────────────────────────
    def do_ca_notice(self, arg: str) -> str:
        if not arg:
            return "usage: /ca notice <notice.pdf|.txt>  — identify the notice + draft a reply"
        res = self._ca_notice_run(self._ca_in_path(arg), consent=f"/ca notice {arg}")
        return res["error"] if res.get("error") else self._ca_notice_report(res)

    def _ca_notice_run(self, path: str, facts: str = "", consent: str = "") -> dict:
        if not os.path.isfile(path):
            return {"error": f"no such notice file: {path}  (give a .pdf / .txt)"}
        cid = self.store.get("active_chat") or "default"
        try:
            with Thinking("reading the notice and drafting a reply"):
                out = canotice.draft_from_file(path, facts=facts,
                                               brain=self._ca_brain(fmt_json=False))
        except cabank.StatementError as e:
            return {"error": f"⚠ {e}"}
        except Exception as e:
            return {"error": f"⚠ couldn't read that notice: {e}"}
        n = out["notice"]
        self.ledger.record(session=cid, tool="ca.notice.read", target=os.path.basename(path),
                           outcome="ok", consent=consent, detail=f"{n.kind}")
        base = re.sub(r"[^\w.-]", "_", os.path.splitext(os.path.basename(path))[0]) or "notice"
        md_path = self._ca_write(f"{base}_reply.md", out["draft"])
        self.ledger.record(session=cid, tool="ca.notice.draft", target=md_path, outcome="ok",
                           consent=consent, bytes_written=len(out["draft"].encode()),
                           detail=f"by_model={out['by_model']}")
        self.actions += 1
        return {"notice": n, "draft": out["draft"], "by_model": out["by_model"], "md_path": md_path}

    def _ca_notice_report(self, res: dict) -> str:
        n = res["notice"]
        src = ("drafted by the local model" if res["by_model"]
               else "skeleton drafted offline — fill the figures before filing")
        issues = ", ".join(n.issues) or "see notice text"
        sec = f", {n.section}" if n.section else ""
        return (f"📑 notice reply — {n.kind} ({n.law}{sec})\n"
                f"   issues detected: {issues}\n   {src}\n→ {res['md_path']}\n"
                "review and edit before filing — this is a starting draft, not legal advice.")

    def ca_notice_api(self, path=None, filename=None, content=None, facts="") -> dict:
        p = self._ca_save_upload(path, filename, content)
        if isinstance(p, dict):
            return p
        res = self._ca_notice_run(p, facts=facts or "",
                                  consent=f"web: /ca notice {os.path.basename(p)}")
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        n = res["notice"]
        return {"ok": True, "by_model": res["by_model"], "draft": res["draft"],
                "notice": {"law": n.law, "kind": n.kind, "section": n.section,
                           "issues": n.issues, "amounts": n.amounts},
                "md": os.path.basename(res["md_path"]), "md_content": res["draft"],
                "report": self._ca_notice_report(res)}

    # ── v2.6: financial statements / Schedule III ────────────────────────────
    def do_ca_fs(self, arg: str) -> str:
        if not arg:
            return "usage: /ca fs <trial-balance.csv|.xlsx>  — Schedule III Balance Sheet + P&L"
        res = self._ca_fs_run(self._ca_in_path(arg), consent=f"/ca fs {arg}")
        return res["error"] if res.get("error") else self._ca_fs_report(res)

    def _ca_fs_run(self, path: str, consent: str = "") -> dict:
        if not os.path.isfile(path):
            return {"error": f"no such trial balance: {path}  (give a .csv / .xlsx)"}
        cid = self.store.get("active_chat") or "default"
        try:
            bals = cafinstmt.parse_trial_balance(path)
        except cabank.StatementError as e:
            return {"error": f"⚠ {e}"}
        except Exception as e:
            return {"error": f"⚠ couldn't read that trial balance: {e}"}
        if not bals:
            return {"error": "no ledger balances found — is this a trial balance?"}
        self.ledger.record(session=cid, tool="ca.fs.parse", target=os.path.basename(path),
                           outcome="ok", consent=consent, detail=f"{len(bals)} ledgers")
        with Thinking(f"grouping {len(bals)} ledgers into Schedule III"):
            mapped = cafinstmt.map_to_schedule3(bals, brain=self._ca_brain(fmt_json=True))
            st = cafinstmt.build_statements(mapped)
        text = cafinstmt.to_text(st)
        base = re.sub(r"[^\w.-]", "_", os.path.splitext(os.path.basename(path))[0]) or "tb"
        txt_path = self._ca_write(f"{base}_financials.txt", text)
        self.ledger.record(session=cid, tool="ca.fs.report", target=txt_path, outcome="ok",
                           consent=consent, bytes_written=len(text.encode()),
                           detail=f"PBT {st['pnl']['profit_before_tax']}")
        self.actions += 1
        return {"statements": st, "text": text, "txt_path": txt_path, "n": len(bals)}

    def _ca_fs_report(self, res: dict) -> str:
        st = res["statements"]
        p, b = st["pnl"], st["bs"]
        bal = ("✓ balanced" if b["balanced"]
               else f"⚠ NOT balanced (diff ₹{b['total_equity_liabilities'] - b['total_assets']:,.0f} "
                    "— check unmapped ledgers)")
        return (f"📊 financial statements (Schedule III) — {res['n']} ledgers\n"
                f"   total income ₹{p['total_income']:,.0f} · total expense "
                f"₹{p['total_expense']:,.0f} · PBT ₹{p['profit_before_tax']:,.0f}\n"
                f"   balance sheet total ₹{b['total_assets']:,.0f} · {bal}\n→ {res['txt_path']}")

    def ca_fs_api(self, path=None, filename=None, content=None) -> dict:
        p = self._ca_save_upload(path, filename, content)
        if isinstance(p, dict):
            return p
        res = self._ca_fs_run(p, consent=f"web: /ca fs {os.path.basename(p)}")
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "statements": res["statements"], "text": res["text"],
                "txt": os.path.basename(res["txt_path"]), "txt_content": res["text"],
                "report": self._ca_fs_report(res)}

    # ── v2.0: workspace lifecycle / clean uninstall ──────────────────────────
    def do_termind(self, line: str) -> str:
        arg = line.replace("/termind", "", 1).strip()
        if arg.startswith(("cleanup", "uninstall", "purge")):
            plan = self.manifest.cleanup_plan()
            rows = [f"  {'✓' if a['exists'] else '·'} {a['kind']:<9} {a['bytes']:>10} B  {a['path']}"
                    for a in plan["assets"]]
            mb = plan["total_bytes"] / 1e6
            return (f"termind uninstall plan — everything lives under {plan['home']}\n"
                    + (("\n".join(rows) + "\n") if rows else "")
                    + f"  · termind home (memory/ledger/manifest): {plan['home_bytes']} B\n"
                    f"total reclaimable: {mb:.1f} MB across {plan['count']} tracked asset(s) + "
                    "home.\nRemove the termind home and the assets above for a complete, clean "
                    "uninstall. (termind won't delete them for you — that's your call.)")
        return lcmod.isolation_summary(self.manifest)

    # ── v2.0: model tiers + activity state ───────────────────────────────────
    def set_tier(self, t: str) -> str:
        t = t.strip().lower()
        if t not in ("smart", "smarter", "max"):
            return ("tiers — smart (local) · smarter (deeper local) · max (frontier on consent). "
                    f"current: {self.store.get('tier', 'smart')}")
        self.store["tier"] = t
        store_save(self.store)
        return f"model tier → {t}."

    def _set_state(self, s: str):
        self._state = s

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
        if line.startswith("/reach"):
            return self.do_reach(line[6:].strip())
        if line.startswith("/img"):
            return self.do_img(line[4:].strip())
        if line.startswith("/edit"):
            return self.do_edit(line[5:].strip())
        if line.startswith("/chats"):
            rows = self.chats_list()
            if not rows:
                return "no chats yet — just start talking, or /chat new"
            return "\n".join(("★ " if c["active"] else "  ") + f"{i+1}. {c['title']}"
                             for i, c in enumerate(rows)) + "\nopen: /chat <number> · fresh: /chat new"
        if line.startswith("/chat"):
            arg = line[5:].strip()
            if arg == "new" or not arg:
                self.chat_new()
                return "fresh chat started (the old one is saved in /chats)"
            if arg.split()[0] == "rename":
                bits = arg.split(None, 2)
                rows = self.chats_list()
                try:
                    row = rows[int(bits[1]) - 1]
                    new = bits[2]
                except (ValueError, IndexError):
                    return "usage: /chat rename <number> <new title>"
                self.chat_rename(row["id"], new)
                return f"renamed to: {new.strip()[:60]}"
            if arg.split()[0] in ("delete", "del", "rm"):
                rows = self.chats_list()
                try:
                    row = rows[int(arg.split()[1]) - 1]
                except (ValueError, IndexError):
                    return "usage: /chat delete <number from /chats>"
                self.chat_delete(row["id"])
                return f"deleted: {row['title']}"
            rows = self.chats_list()
            try:
                cid = rows[int(arg) - 1]["id"]
            except (ValueError, IndexError):
                return "usage: /chat new · /chat <n> · /chat delete <n>"
            self.chat_open(cid)
            return f"resumed: {self.store['chats'][cid]['title']} ({len(self.history)} messages)"
        if line.startswith("/tools"):
            if "refresh" in line:
                self.refresh_toolchain()
            rows = [f"{k:<8} {v['cmd']:<10} {v['version']:<12} {v['path']}"
                    for k, v in self.toolchain.items() if not k.startswith("_")]
            return ("detected toolchains (auto):\n" + "\n".join(rows)
                    + "\nrefresh: /tools refresh") if rows else "none detected — /tools refresh"
        if line.startswith("/ledger"):
            return self._ledger_report(line)
        if line.startswith("/db"):
            return self.do_db(line)
        if line.startswith("/scan"):
            return self.do_scan()
        if line.startswith("/ca"):
            return self.do_ca(line[3:].strip())
        if line.startswith("/termind"):
            return self.do_termind(line)
        if line.startswith("/tier"):
            return self.set_tier(line[5:].strip())
        if line == "/mode" or line.startswith("/mode "):
            return self.set_mode(line[5:].strip() or "")
        if line.startswith("/ws"):
            return self.set_workspace(line[3:].strip() or ".")
        if line.startswith("/tree"):
            t = self.ws_tree()
            if not t:
                return f"(empty) workspace: {self.workspace()}"
            return f"workspace: {self.workspace()}\n" + "\n".join(
                "  " * e["depth"] + ("📁 " if e["dir"] else "· ") + os.path.basename(e["path"])
                for e in t[:60])
        if line.startswith("/read"):
            p = line[5:].strip()
            return self.ws_read(p) if p else "usage: /read <file in workspace>"
        if line.startswith("/profile"):
            p = self.profile()
            return (f"name: {p['name'] or '(not set)'} · role: {p['role'] or '-'} · "
                    f"prefs: {p['prefs'] or '-'} · theme: {p['theme']}"
                    "\nset it in the web UI (⚙ Settings) — it persists everywhere")
        if line.startswith("/guide"):
            q = line[6:].strip()
            from .helpdocs import DOC, best_topic as _bt
            return _bt(q) if q else DOC
        if line.startswith("/model"):
            return self.do_model(line[6:].strip())
        if line.startswith("/pull"):
            n = line[5:].strip()
            return self.do_pull(n) if n else "usage: /pull <model name>  e.g. /pull llama3.2"
        if line.startswith("/import"):
            parts = line[7:].strip().split(None, 1)
            if not parts:
                return ("usage: /import <path/to/model.gguf> [name] — bring your own "
                        "fine-tuned model. From Hugging Face: /pull hf.co/<user>/<repo>")
            return self.import_model(parts[0], parts[1] if len(parts) > 1 else None)
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
        return self.route(line)  # plain english: action if it sounds like one, else chat

    def handle_web(self, line: str, image: str = None, image_name: str = "image") -> str:
        """Same agent, driven from the web UI. There's no stdin, so a user's message IS the
        consent for the action it requested (file/project writes auto-approve; VS Code can't
        open from a server context, so build skips the editor step)."""
        prev = self._confirm
        self._confirm = lambda _p="": "y"   # the send itself is the y/N
        try:
            if image:
                # an attached image + an edit request = edit it, don't describe it
                if EDIT_HINT.search(line or ""):
                    self.last_image = (image_name, image)
                    return self.do_edit(line)
                return self.do_vision(line, image, image_name)
            pend = self._db_followup(line)           # confirm/cancel a pending destructive query
            if pend is not None:
                return pend
            follow = self._image_followups(line)
            if follow is not None:
                return follow
            code = self._code_route(line)
            if code is not None:
                return code
            if self.last_image and EDIT_HINT.search(line) and not line.startswith("/"):
                return self.do_edit(line)             # edit the previously sent image
            if line.strip().lower().startswith(("/build", "create", "make", "build", "new")) \
                    and "project" in line.lower() or line.strip().startswith("/build"):
                idea = line.split(None, 1)[1] if line.strip().startswith("/build") else line
                return self.do_build(idea, open_editor=False)
            return self.handle(line)
        finally:
            self._confirm = prev

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
        if s.startswith(("/model", "/pull", "/import")):
            return _panel("MODEL BAY", out.replace("\n", f"\n{PU}│{N} "), PU)
        if s.startswith(("/chats", "/chat")):
            return _panel("SESSIONS", out.replace("\n", f"\n{CY}│{N} "), CY)
        if s.startswith("/tools"):
            return _panel("TOOLCHAINS", out.replace("\n", f"\n{GR}│{N} "), GR)
        if s == "/mode" or s.startswith("/mode "):
            return _panel("AGENT MODE", out, YL)
        if s.startswith(("/ws", "/tree", "/read")):
            return _panel("WORKSPACE", out.replace("\n", f"\n{CY}│{N} "), CY)
        if s.startswith(("/guide", "/profile")):
            return _panel("HANDBOOK", out.replace("\n", f"\n{GR}│{N} "), GR)
        if s.startswith(("/img", "/edit")):
            return _panel("VISION", out.replace("\n", f"\n{PK}│{N} "), PK)
        if s.startswith("/do"):
            return _panel("OPERATOR", out.replace("\n", f"\n{YL}│{N} "), YL)
        if s.startswith(("/build", "/write", "/mkdir", "/code")):
            return _panel("BUILDER", out.replace("\n", f"\n{GR}│{N} "), GR)
        if s.startswith("/"):
            return f"{YL}{out}{N}"
        return _panel("NEURAL CORE", out, PK)


def run(session: "Session" = None, web_url: str = None) -> int:
    s = session or Session()
    print(BANNER)
    _boot(s.live, getattr(s, "server", False))
    if s.store["facts"] or s.store["docs"]:
        print(f"  {PU}◆{N} {D}memory restored:{N} {WH}{len(s.store['facts'])}{N}{D} facts · "
              f"{N}{WH}{len(s.store['docs'])}{N}{D} doc chunks from previous sessions{N}\n")
    if web_url:
        print(f"  {PK}◆{N} {D}web UI live (shares this brain):{N} {CY}{web_url}{N}\n")
    print(FEATURES)
    while True:
        try:
            line = input(f"{PK}{B}⟦{N}{CY}termind{N}{PK}{B}⟧{N} {GR}❯{N} ")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{D}link severed.{N}")
            return 0
        try:
            with s._lock:                       # don't clash with the web UI mid-call
                out = s.render(line)
        except SystemExit:
            print(f"{PU}◢ jacking out… session closed.{N}")
            return 0
        except Exception as e:  # the REPL never crashes
            out = f"{YL}⚠ error: {e}{N}"
        if out:
            print(f"\n{out}\n")
        sys.stdout.flush()
