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
        self.pull = {"status": "idle"}  # background model download state (web progress bar)
        # toolchain: detect once, cache for a week (what languages this machine speaks)
        tc = self.store.get("toolchain") or {}
        if not tc or time.time() - tc.get("_detected_at", 0) > 7 * 86400:
            tc = tcmod.detect()
            self.store["toolchain"] = tc
            store_save(self.store)
        self.toolchain = tc
        self.ledger = Ledger()       # tamper-evident audit log of every code-agent action

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

    def do_think(self, q: str) -> str:
        """Escalation ladder for hard questions: big local model → Claude → deep local CoT."""
        with Thinking("deep thinking"):
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
        """System prompt carries the remembered facts — the model knows who it's talking to."""
        p = self.profile()
        sys = ("You are termind, a private local AI agent running in the user's terminal. "
               "The HUMAN typing to you is your user — a separate person, not you. "
               "Be concise and direct. ALWAYS honor the user's stated preferences "
               "(answer length, tone, style) in every reply. You are NOT text-only: you can "
               "SEE images the user attaches, and the app EDITS images on request (background "
               "removal, brightness, contrast, crop, rotate, resize, sepia, blur…). If asked "
               "whether you can edit an image, say yes and ask what edit they want. NEVER claim "
               "you performed an action (edit, save, send) — the app does actions and reports "
               "them itself; if asked to do one, tell the user to phrase it as a request like "
               "'remove the logo' and the app will handle it.")
        if self.active_mode() == "code":
            sys += (f" CODE MODE is active. Workspace: {self.workspace()}. You have elevated "
                    "file powers here: the app creates folders/files, scaffolds projects, and "
                    "runs commands IN THIS WORKSPACE when the user asks.")
        if p["name"]:
            sys += (f" Your user's profile — name: {p['name']}"
                    + (f", role: {p['role']}" if p['role'] else "")
                    + (f", answer-style preference: {p['prefs']}" if p['prefs'] else "")
                    + ". Address them naturally by name when it fits.")
        if self.store["facts"]:
            sys += (" Facts the USER has told you about THEMSELVES (when they ask 'who am I' "
                    "or about their identity, answer from these): "
                    + "; ".join(self.store["facts"])
                    + ". Never confuse yourself (termind, the agent) with the user.")
        # send only the recent turns — smaller context = faster local inference
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
        "You are termind's CODE AGENT working inside the workspace: {ws}\n"
        "You ACT by replying with EXACTLY ONE JSON object per turn — never prose, never "
        "shell-command advice:\n"
        '{{"tool":"mkdir","path":"<relative folder>"}}\n'
        '{{"tool":"write","path":"<relative file>","content":"<FULL file content>"}}\n'
        '{{"tool":"read","path":"<relative file>"}}\n'
        '{{"tool":"run","cmd":"<shell command, runs inside the workspace>"}}\n'
        '{{"tool":"done","say":"<short summary for the user>"}}  when the task is complete\n'
        '{{"tool":"ask","say":"<question>","options":["<choice>","<choice>"]}}  to make the '
        "user PICK from clickable choices\n"
        '{{"tool":"say","say":"<reply>"}}  only for pure conversation\n'
        "Multi-file tasks: ONE tool call per turn, then wait for the RESULT. A build task is "
        "NOT done until every file is WRITTEN with the write tool — after mkdir, immediately "
        "write the files. If the user says 'yes', 'do it', 'run it', or picks an option — "
        "CONTINUE with tools.\n"
        "NEW PROJECT with no language chosen yet: your FIRST reply MUST be an ask tool offering "
        "languages, e.g. options [\"Python\",\"JavaScript / Node\",\"HTML/CSS/JS (web)\",\"Go\"]. "
        "Then ask scope (CLI / web / GUI) the same way. After they pick, build without asking "
        "again.\n"
        "Python packages: just run 'pip install <pkg>' or 'pip install -r requirements.txt' — "
        "termind creates and uses a project .venv for you, so never tell the user to install "
        "anything.\n"
        "Installed toolchains on THIS machine (use these exact commands): {tools}")

    def do_code_agent(self, text: str) -> str:
        """Code-session messages go through a real act-observe loop: the model calls
        tools, termind EXECUTES them, results feed back. No more command-printing."""
        self.last_options = []       # fresh turn → clear any prior quick-replies
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
        for _ in range(12):
            with Thinking("code agent working"):
                act = parse_action(self._chat(msgs, fmt_json=True))
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
        return f"workspace set: {full} — builds, files and commands run here now"

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
        with Thinking("neural core thinking"):
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
        return (f"brain: {brain} · facts remembered: {len(self.store['facts'])} · "
                f"indexed chunks: {self.chunks} · chat turns kept: {len(self.history)} · "
                f"actions run: {getattr(self, 'actions', 0)} · credits spent: {self.spent:.2f} · "
                f"denied by sandbox: {self.denied} · audit ledger: {led['count']} actions "
                f"({led['integrity']}) · data off-machine: 0 bytes")

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
