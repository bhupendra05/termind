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
                  offline_chat, ollama_available, parse_action)
from .store import load as store_load, save as store_save

# Auto-memory: only when a SENTENCE STARTS with a self-statement — "should i use X?" must
# not become a remembered "fact".
AUTO_FACT = re.compile(
    r"(?:^|[.!?]\s+)(i am|i'm|my name is|i work|i live|i like|i prefer|i build|call me)\b", re.I)

# Natural-language actions: "create a folder", "open vs code", "build me a tool…" — no slash
# needed. The hint gate keeps ordinary chat away from the intent classifier.
ACTION_HINT = re.compile(
    r"\b(create|make|build|scaffold|new|open|write|generate)\b[\s\S]*"
    r"\b(folder|directory|project|tool|app|file|script|code|vs ?code|editor)\b", re.I)
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
{CY}║{N}  {GR}◉{N} {WH}/chats{N} {D}» past conversations{N}  {GR}◉{N} {WH}/chat new{N} {D}» fresh chat{N}
{CY}║{N}  {GR}◉{N} {WH}/model [name]{N}    {D}»{N} list · switch your brain (any Ollama model)
{CY}║{N}  {GR}◉{N} {WH}/pull <name>{N}     {D}»{N} download a new model (llama3.2, qwen2.5…)
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

    # ── chat sessions: new chat, continue previous, switch ───────────────────
    def _new_chat_id(self) -> str:
        return f"{int(time.time() * 1000)}-{len(self.store['chats'])}"  # collision-proof

    def chat_new(self, title: str = "New chat") -> str:
        cid = self._new_chat_id()
        self.store["chats"][cid] = {"title": title, "ts": time.time(), "messages": []}
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
        return True

    def chats_list(self) -> list:
        items = sorted(self.store["chats"].items(), key=lambda kv: -kv[1].get("ts", 0))
        return [{"id": k, "title": v.get("title", "Chat"),
                 "active": k == self.store.get("active_chat")} for k, v in items]

    def _save_chat(self, first_user_text: str) -> None:
        cid = self.store.get("active_chat")
        if not cid or cid not in self.store["chats"]:
            # create the record inline — chat_new() would wipe the history we're saving
            cid = self._new_chat_id()
            self.store["chats"][cid] = {"title": "New chat", "ts": time.time(), "messages": []}
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
        full = os.path.expanduser(path)
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
        full = os.path.expanduser(file)
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
        root = os.path.expanduser(folder)
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

    def route(self, text: str) -> str:
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
        sys = ("You are termind, a private local AI agent running in the user's terminal. "
               "The HUMAN typing to you is your user — a separate person, not you. "
               "Be concise and direct. ALWAYS honor the user's stated preferences "
               "(answer length, tone, style) in every reply.")
        if self.store["facts"]:
            sys += (" Facts the USER has told you about THEMSELVES (when they ask 'who am I' "
                    "or about their identity, answer from these): "
                    + "; ".join(self.store["facts"])
                    + ". Never confuse yourself (termind, the agent) with the user.")
        # send only the recent turns — smaller context = faster local inference
        return [{"role": "system", "content": sys}] + self.history[-8:] + [
            {"role": "user", "content": text}]

    def do_model(self, name: str = "") -> str:
        if not name:
            installed = list_models()
            rows = [("★ " if m.split(":")[0] == self.model.split(":")[0] else "  ") + m
                    for m in installed] or ["  (none pulled yet — try: /pull gemma3)"]
            return ("active: " + self.model + "\n" + "\n".join(rows)
                    + "\nswitch: /model <name> · download: /pull <name>")
        if not model_available(name):
            return f"'{name}' isn't pulled yet — run: /pull {name}"
        self.model = name
        self.store["model"] = name
        store_save(self.store)
        self.live = self.server and True
        return f"switched to {name} (saved — future sessions use it too)"

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
            rows = self.chats_list()
            try:
                cid = rows[int(arg) - 1]["id"]
            except (ValueError, IndexError):
                return "usage: /chat new · /chat <number from /chats>"
            self.chat_open(cid)
            return f"resumed: {self.store['chats'][cid]['title']} ({len(self.history)} messages)"
        if line.startswith("/model"):
            return self.do_model(line[6:].strip())
        if line.startswith("/pull"):
            n = line[5:].strip()
            return self.do_pull(n) if n else "usage: /pull <model name>  e.g. /pull llama3.2"
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

    def handle_web(self, line: str) -> str:
        """Same agent, driven from the web UI. There's no stdin, so a user's message IS the
        consent for the action it requested (file/project writes auto-approve; VS Code can't
        open from a server context, so build skips the editor step)."""
        prev = self._confirm
        self._confirm = lambda _p="": "y"   # the send itself is the y/N
        try:
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
        if s.startswith(("/model", "/pull")):
            return _panel("MODEL BAY", out.replace("\n", f"\n{PU}│{N} "), PU)
        if s.startswith(("/chats", "/chat")):
            return _panel("SESSIONS", out.replace("\n", f"\n{CY}│{N} "), CY)
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
