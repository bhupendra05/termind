"""termind web — a local Claude-style chat UI over the SAME agent Session.

Pure standard library (http.server). Shares memory, model choice, and every command with the
terminal REPL: whatever you /remember or build in one shows up in the other. Nothing leaves the
machine — the server binds to localhost only.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .llm import list_models
from .repl import Session
from . import db as dbmod
from . import scan as scanmod
from . import lifecycle as lcmod


def _strip_ansi(t: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", t)


class _Handler(BaseHTTPRequestHandler):
    session: Session = None

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            s = self.session
            return self._send(200, json.dumps({
                "model": s.model, "live": s.live,
                "models": list_models() or [s.model],
                "facts": len(s.store["facts"]), "chunks": s.chunks,
                "version": __import__("termind").__version__,
                "agent_mode": s.agent_mode,
                "db": s.db_context(),                          # selected database (bottom dock)
                "tier": s.store.get("tier", "smart"),
                "state": getattr(s, "_state", "idle"),
                "workspace": s.workspace(),                    # bottom bar folder chip
            }))
        if self.path.startswith("/api/chats"):
            s = self.session
            mode = None
            if "mode=" in self.path:
                mode = self.path.split("mode=")[1].split("&")[0] or None
            return self._send(200, json.dumps({
                "chats": s.chats_list(mode),
                "active_mode": s.active_mode(),
                "workspace": s.workspace(),
                "has_ws": bool((s.store["chats"].get(s.store.get("active_chat") or "")
                                or {}).get("ws")),
                "messages": s.history,            # messages of the active chat
            }))
        if self.path == "/api/catalog":
            return self._send(200, json.dumps(self.session.model_catalog()))
        if self.path == "/api/profile":
            return self._send(200, json.dumps(self.session.profile()))
        if self.path == "/api/toolchain":
            return self._send(200, json.dumps({"toolchain": self.session.toolchain}))
        if self.path == "/api/help":
            from .helpdocs import TOPICS
            return self._send(200, json.dumps({"topics": TOPICS}))
        if self.path == "/api/ledger" or self.path.startswith("/api/ledger?"):
            led = self.session.ledger
            if self.path.endswith("full=1"):                 # full artifact for download
                return self._send(200, json.dumps(led.export()))
            return self._send(200, json.dumps({              # compact view for the panel
                "summary": led.summary(), "integrity": led.verify(),
                "entries": led.tail(50)}))
        if self.path == "/api/db":
            s = self.session
            return self._send(200, json.dumps({
                "databases": s.databases(), "active": s.active_db(),
                "context": s.db_context(), "status": s.db_status(),
                "engines": dbmod.engines_available()}))
        if self.path == "/api/scan":
            s = self.session
            return self._send(200, json.dumps({
                "workspace": s.workspace(), "summary": scanmod.summary(s.last_scan),
                "findings": s.last_scan[:50]}))
        if self.path == "/api/lifecycle":
            s = self.session
            return self._send(200, json.dumps({
                "plan": s.manifest.cleanup_plan(), "models": lcmod.ollama_models_dir(),
                "summary": lcmod.isolation_summary(s.manifest)}))
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        if self.path == "/api/send":
            text = (req.get("text") or "").strip()
            image = req.get("image") or None          # base64 (no data: prefix)
            if not text and not image:
                return self._send(200, json.dumps({"reply": ""}))
            with self.session._lock:  # one model call at a time → consistent shared memory
                try:
                    self.session.view_mode = str(req.get("mode") or "chat")
                    # web has no stdin, so consent-gated actions auto-approve in the UI flow
                    out = self.session.handle_web(text, image=image,
                                                  image_name=req.get("image_name") or "image")
                except SystemExit:
                    out = "__EXIT__"
                except Exception as e:
                    out = f"error: {e}"
            resp = {"reply": _strip_ansi(out)}
            if self.session.last_options:       # clarifying question → clickable quick-replies
                resp["options"] = self.session.last_options
            # an edit happened or the user asked to see it → return the actual image
            if self.session.last_image and (out.startswith("applied")
                                            or out.startswith("here's the current image")):
                resp["image"] = self.session.last_image[1]
            return self._send(200, json.dumps(resp))
        if self.path == "/api/chat":
            s = self.session
            with s._lock:
                if req.get("op") == "new":
                    s.chat_new(mode=str(req.get("mode") or "chat"))
                elif req.get("op") == "open":
                    s.chat_open(str(req.get("id", "")))
                elif req.get("op") == "delete":
                    s.chat_delete(str(req.get("id", "")))
                elif req.get("op") == "rename":
                    s.chat_rename(str(req.get("id", "")), str(req.get("title", "")))
            return self._send(200, json.dumps({
                "chats": s.chats_list(str(req.get("mode")) if req.get("mode") else None),
                "workspace": s.workspace(),
                "has_ws": bool((s.store["chats"].get(s.store.get("active_chat") or "")
                                or {}).get("ws")),
                "messages": s.history}))
        if self.path == "/api/pull":
            out = self.session.start_pull(str(req.get("model", "")).strip())
            return self._send(200, json.dumps({"reply": out,
                                               "pull": dict(self.session.pull)}))
        if self.path == "/api/import":
            out = self.session.add_model(str(req.get("spec", "")),
                                         (req.get("name") or None))
            return self._send(200, json.dumps({"reply": out,
                                               "pull": dict(self.session.pull)}))
        if self.path == "/api/profile":
            return self._send(200, json.dumps(self.session.set_profile(
                name=req.get("name"), role=req.get("role"),
                prefs=req.get("prefs"), theme=req.get("theme"))))
        if self.path == "/api/memory":
            s = self.session
            op = req.get("op")
            with s._lock:
                if op == "import":
                    out = s.import_memories(str(req.get("text", "")))
                elif op == "export":
                    out = s.export_memories()
                elif op == "clear":
                    out = s.clear_memory(str(req.get("what", "")))
                else:
                    out = "unknown op"
            return self._send(200, json.dumps({"reply": out,
                                               "facts": len(s.store["facts"])}))
        if self.path == "/api/ws":
            s = self.session
            op = req.get("op")
            with s._lock:
                if op == "browse":
                    return self._send(200, json.dumps(
                        s.ws_browse(str(req.get("path", "")))))
                if op == "set":
                    return self._send(200, json.dumps(
                        {"reply": s.set_workspace(str(req.get("path", "."))),
                         "workspace": s.workspace()}))
                if op == "tree":
                    return self._send(200, json.dumps(
                        {"workspace": s.workspace(), "tree": s.ws_tree()}))
                if op == "read":
                    return self._send(200, json.dumps(
                        {"path": req.get("path", ""),
                         "content": s.ws_read(str(req.get("path", "")))}))
            return self._send(400, json.dumps({"error": "bad op"}))
        if self.path == "/api/toolchain":
            return self._send(200, json.dumps(
                {"toolchain": self.session.refresh_toolchain()}))
        if self.path == "/api/mode":
            out = self.session.set_mode(str(req.get("mode", "")))
            return self._send(200, json.dumps({"reply": out,
                                               "agent_mode": self.session.agent_mode}))
        if self.path == "/api/model":
            name = (req.get("model") or "").strip()
            with self.session._lock:
                out = _strip_ansi(self.session.do_model(name))
            return self._send(200, json.dumps({"reply": out, "model": self.session.model}))
        if self.path == "/api/db":
            s, op = self.session, str(req.get("op", ""))
            with s._lock:
                if op == "add":
                    out = s.db_add(str(req.get("name", "")), str(req.get("spec", "")))
                elif op == "use":
                    out = s.db_use(str(req.get("name", "")))
                elif op == "schema":
                    out = s.db_schema(str(req.get("table", "")))
                elif op == "confirm":
                    out = s._confirm_sql(str(req.get("answer", "confirm")))
                else:                                  # op == "query"
                    out = s.do_db_query(str(req.get("text", "")))
            return self._send(200, json.dumps({
                "reply": _strip_ansi(out), "databases": s.databases(), "active": s.active_db(),
                "context": s.db_context(), "pending": bool(s._pending_sql)}))
        if self.path == "/api/scan":
            s = self.session
            with s._lock:
                s.scan_workspace()
            return self._send(200, json.dumps({
                "summary": scanmod.summary(s.last_scan), "findings": s.last_scan[:50],
                "workspace": s.workspace()}))
        if self.path == "/api/tier":
            out = self.session.set_tier(str(req.get("tier", "")))
            return self._send(200, json.dumps({"reply": out,
                                               "tier": self.session.store.get("tier", "smart")}))
        if self.path == "/api/ca":
            s, op = self.session, str(req.get("op", ""))
            path = req.get("path") or None
            fn = req.get("filename") or None
            content = req.get("content")
            with s._lock:
                if op == "bank":
                    out = s.ca_bank_api(path=path, filename=fn, content=content)
                elif op == "scrutiny":
                    out = s.ca_scrutiny_api(path=path, filename=fn, content=content)
                elif op == "gst":
                    out = s.ca_gst_api(books=req.get("books") or None,
                                       books_name=req.get("books_name") or None,
                                       books_content=req.get("books_content"),
                                       portal=req.get("portal") or None,
                                       portal_name=req.get("portal_name") or None,
                                       portal_content=req.get("portal_content"))
                elif op == "notice":
                    out = s.ca_notice_api(path=path, filename=fn, content=content,
                                          facts=req.get("facts") or "")
                elif op == "fs":
                    out = s.ca_fs_api(path=path, filename=fn, content=content)
                else:
                    out = {"ok": False, "error": "unknown CA section"}
            return self._send(200, json.dumps(out))
        return self._send(404, json.dumps({"error": "not found"}))


def serve(session: Session, host="127.0.0.1", port=8765, open_browser=True):
    _Handler.session = session
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return httpd, url


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>termind</title>
<style>
:root{--bg:#262624;--side:#1f1e1d;--card:#30302e;--bubble:#393937;--line:#3d3c39;
--ink:#ececec;--dim:#9b9a96;--clay:#d97757;--clay2:#c4633f;--green:#7bbf7e;
--shadow:0 12px 40px rgba(0,0,0,.45);--ring:rgba(217,119,87,.28)}
[data-theme=light]{--bg:#f6f5f0;--side:#edebe4;--card:#ffffff;--bubble:#e8e6de;--line:#d9d6cb;
--ink:#2a2a26;--dim:#8a887e;--clay:#c45f3c;--clay2:#a94e2f;--green:#4d9a55;
--shadow:0 12px 36px rgba(60,50,30,.14);--ring:rgba(196,95,60,.22)}
[data-theme=cyber]{--bg:#05010f;--side:#0c0220;--card:#150a2b;--bubble:#231046;--line:#37205e;
--ink:#e9eaff;--dim:#8385b3;--clay:#05d9e8;--clay2:#ff2a6d;--green:#3ef58b;
--shadow:0 14px 44px rgba(5,217,232,.10);--ring:rgba(5,217,232,.25)}
*{box-sizing:border-box}
::selection{background:var(--clay);color:#fff}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px;border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:var(--dim);border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-track{background:transparent}
body{margin:0;font-family:Inter,ui-sans-serif,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink);height:100vh;display:flex;overflow:hidden;
font-feature-settings:'cv11','ss01';-webkit-font-smoothing:antialiased}
/* ───────── sidebar ───────── */
aside{width:256px;background:var(--side);border-right:1px solid var(--line);display:flex;
flex-direction:column;padding:14px 12px;gap:8px}
.brand{display:flex;align-items:center;gap:9px;padding:6px 8px 14px;font-weight:700;
letter-spacing:.4px;color:var(--ink);font-size:15.5px}
.brand::before{content:'▲';color:var(--clay);font-size:17px}
.brand small{color:var(--dim);font-weight:500;font-size:10.5px;border:1px solid var(--line);
border-radius:20px;padding:2px 8px;letter-spacing:.6px}
.newchat{display:flex;align-items:center;justify-content:center;gap:8px;background:transparent;
color:var(--ink);border:1px solid var(--line);border-radius:11px;padding:10px 12px;
font-size:13.5px;font-weight:600;cursor:pointer;font-family:inherit;
transition:all .18s ease}
.newchat:hover{border-color:var(--clay);color:var(--clay);background:var(--card)}
.label{color:var(--dim);font-size:10.5px;padding:12px 8px 4px;text-transform:uppercase;
letter-spacing:1.4px;font-weight:600}
#chats{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:2px;padding-right:2px}
.chat-it{display:flex;align-items:center;gap:6px;padding:9px 10px 9px 12px;border-radius:9px;
font-size:13px;color:var(--ink);cursor:pointer;border-left:2px solid transparent;
transition:background .15s ease}
.chat-it:hover{background:var(--card)}
.chat-it.active{background:var(--card);border-left-color:var(--clay)}
.chat-it .tt{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-it .del{visibility:hidden;color:var(--dim);padding:1px 5px;border-radius:6px;flex:none;
font-size:11px;transition:all .15s}
.chat-it:hover .del{visibility:visible}.chat-it .del:hover{color:#fff;background:var(--clay)}
#mecard{display:flex;align-items:center;gap:10px;padding:10px;border-radius:12px;
border:1px solid var(--line);cursor:pointer;transition:all .15s;background:var(--card)}
#mecard:hover{border-color:var(--clay)}
.meav{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;flex:none;
background:linear-gradient(135deg,var(--clay),var(--clay2));color:#fff;font-weight:700;
font-size:14px;box-shadow:0 2px 8px var(--ring)}
.men{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mes{font-size:10.5px;color:var(--dim)}
.foot{color:var(--dim);font-size:10.5px;padding:10px 8px 2px;border-top:1px solid var(--line);
line-height:1.9;letter-spacing:.3px}
/* ───────── main ───────── */
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:flex;align-items:center;gap:10px;padding:13px 22px;border-bottom:1px solid var(--line);
background:color-mix(in srgb,var(--bg) 86%,transparent);backdrop-filter:blur(10px)}
#title{font-size:13.5px;font-weight:600;color:var(--dim);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;letter-spacing:.2px}
.spacer{flex:1}
#core{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px;
border:1px solid var(--line);border-radius:20px;padding:5px 12px;background:var(--card)}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.live{background:var(--green);box-shadow:0 0 8px var(--green)}
.off{background:var(--clay);box-shadow:0 0 8px var(--clay)}
select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:9px;
padding:7px 11px;font-family:inherit;font-size:12.5px;cursor:pointer;outline:none;
transition:border-color .15s}
select:hover,select:focus{border-color:var(--clay)}
#log{flex:1;overflow-y:auto;padding:30px 0 14px}
.wrap{max-width:780px;margin:0 auto;padding:0 26px}
/* ───────── messages ───────── */
.msg{margin:22px 0;animation:fade .28s cubic-bezier(.2,.8,.25,1);line-height:1.65;font-size:14.5px}
@keyframes fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.you{display:flex;justify-content:flex-end}
.you .body{background:var(--bubble);border-radius:18px 18px 4px 18px;padding:11px 17px;
max-width:78%;white-space:pre-wrap;box-shadow:0 1px 2px rgba(0,0,0,.12)}
.bot .who{display:flex;align-items:center;gap:8px;color:var(--clay);font-size:12px;
font-weight:700;margin-bottom:7px;letter-spacing:.4px}
.bot .who::before{content:'▲';display:grid;place-items:center;width:22px;height:22px;
border-radius:7px;background:linear-gradient(135deg,var(--clay),var(--clay2));color:#fff;
font-size:11px;box-shadow:0 2px 6px var(--ring)}
.bot .body{white-space:pre-wrap;color:var(--ink);padding-left:30px}
pre{background:color-mix(in srgb,var(--side) 70%,#000 8%);border:1px solid var(--line);
border-radius:11px;padding:14px;overflow-x:auto;font-size:12.5px;line-height:1.55;
font-family:'JetBrains Mono',ui-monospace,Menlo,monospace}
code{background:color-mix(in srgb,var(--side) 70%,#000 8%);padding:2px 6px;border-radius:6px;
font-size:12.5px;font-family:'JetBrains Mono',ui-monospace,Menlo,monospace;border:1px solid var(--line)}
.think{color:var(--dim);font-style:italic}
.think::after{content:'';display:inline-block;width:10px;animation:dots 1.2s steps(4) infinite}
@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
/* greeting */
.greet{color:var(--dim);text-align:center;margin-top:13vh;animation:fade .4s ease}
.greet h1{color:var(--ink);font-weight:700;font-size:30px;margin:0 0 8px;letter-spacing:-.4px}
.greet h1::before{content:'▲';display:block;font-size:30px;color:var(--clay);margin-bottom:14px;
text-shadow:0 4px 18px var(--ring)}
.chips{margin-top:24px;display:flex;flex-wrap:wrap;gap:9px;justify-content:center;max-width:560px;
margin-left:auto;margin-right:auto}
.chip{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:9px 15px;
color:var(--ink);font-size:13px;cursor:pointer;transition:all .16s ease}
.chip:hover{border-color:var(--clay);color:var(--clay);transform:translateY(-1px);
box-shadow:0 4px 12px var(--ring)}
/* clarifying quick-reply chips (Claude-style clickable choices under a bot bubble) */
.quick{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.qchip{border:1px solid var(--clay);background:var(--card);border-radius:11px;padding:8px 14px;
color:var(--clay);font-size:13px;font-weight:600;cursor:pointer;transition:all .16s ease;
animation:fade .25s ease}
.qchip:hover{background:var(--clay);color:#fff;transform:translateY(-1px);
box-shadow:0 5px 14px var(--ring)}
/* ───────── composer ───────── */
footer{padding:10px 22px 20px}
.inbar{max-width:780px;margin:0 auto;display:flex;gap:10px;align-items:flex-end;
background:var(--card);border:1px solid var(--line);border-radius:18px;padding:12px 14px;
transition:border-color .18s,box-shadow .18s;box-shadow:0 4px 18px rgba(0,0,0,.10)}
.inbar:focus-within{border-color:var(--clay);box-shadow:0 0 0 3px var(--ring)}
textarea{flex:1;background:transparent;border:0;color:var(--ink);font-family:inherit;
font-size:14.5px;resize:none;outline:none;max-height:180px;line-height:1.55;padding:4px 2px}
textarea::placeholder{color:var(--dim)}
button.send{background:linear-gradient(135deg,var(--clay),var(--clay2));color:#fff;border:0;
border-radius:11px;width:38px;height:38px;font-size:16px;cursor:pointer;flex:none;
transition:transform .12s,box-shadow .18s;box-shadow:0 3px 10px var(--ring)}
button.send:hover{transform:translateY(-1px);box-shadow:0 6px 16px var(--ring)}
button.send:active{transform:none}
button.send:disabled{opacity:.35;cursor:default;transform:none;box-shadow:none}
.hint{max-width:780px;margin:9px auto 0;color:var(--dim);font-size:11px;text-align:center;
letter-spacing:.3px}
/* image chip */
#imgchip img{box-shadow:0 2px 8px rgba(0,0,0,.25)}
/* ───────── overlays & panels ───────── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);backdrop-filter:blur(6px);
display:none;align-items:center;justify-content:center;z-index:50}
.overlay.open{display:flex;animation:fade .2s ease}
.panel{background:var(--side);border:1px solid var(--line);border-radius:18px;width:560px;
max-width:92vw;max-height:82vh;overflow-y:auto;padding:24px;box-shadow:var(--shadow);
animation:pop .26s cubic-bezier(.2,.9,.3,1.2)}
@keyframes pop{from{opacity:0;transform:scale(.96) translateY(10px)}to{opacity:1;transform:none}}
.panel h2{margin:0 0 5px;font-size:17px;font-weight:700;letter-spacing:-.2px}
.panel .sub{color:var(--dim);font-size:12.5px;margin-bottom:14px;line-height:1.5}
.mrow{display:flex;align-items:center;gap:12px;padding:11px 10px;border-radius:11px;
border:1px solid transparent;transition:all .15s}
.mrow:hover{background:var(--card);border-color:var(--line)}
.mrow .nm{font-weight:600;font-size:13.5px}
.mrow .ds{color:var(--dim);font-size:12px;margin-top:1px}
.mrow .sz{color:var(--dim);font-size:11.5px;flex:none;width:52px;text-align:right;
font-family:'JetBrains Mono',monospace}
.mbtn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:9px;
padding:7px 13px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:inherit;flex:none;
transition:all .15s ease}
.mbtn:hover{border-color:var(--clay);color:var(--clay)}
.mbtn.active{border-color:var(--green);color:var(--green)}
.bar{height:5px;border-radius:4px;background:var(--card);overflow:hidden;margin-top:8px}
.bar>div{height:100%;background:linear-gradient(90deg,var(--clay),var(--clay2));width:0%;
transition:width .45s ease;border-radius:4px}
.warnbar{max-width:780px;margin:12px auto 0;padding:11px 16px;border:1px solid var(--clay);
border-radius:12px;color:var(--ink);font-size:13px;background:color-mix(in srgb,var(--clay) 9%,transparent);
display:none;animation:fade .3s ease}
.warnbar b{color:var(--clay);cursor:pointer}
/* settings inputs */
.sin{width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 13px;color:var(--ink);font-family:inherit;font-size:13px;outline:none;
margin:4px 0;resize:vertical;transition:border-color .15s,box-shadow .15s}
.sin:focus{border-color:var(--clay);box-shadow:0 0 0 3px var(--ring)}
.tbtn.on{border-color:var(--clay);color:var(--clay);background:color-mix(in srgb,var(--clay) 10%,var(--card))}
.htop{padding:10px 13px;border:1px solid var(--line);border-radius:10px;margin:5px 0;
cursor:pointer;font-size:13px;transition:border-color .15s}
.htop:hover{border-color:var(--clay)}
.htop .hb{display:none;color:var(--dim);font-size:12.5px;white-space:pre-wrap;margin-top:8px;
line-height:1.6}
.htop.open{border-color:var(--clay)}.htop.open .hb{display:block}
#mspec{flex:1;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:9px 12px;color:var(--ink);font-family:inherit;font-size:12.5px;outline:none;
transition:border-color .15s}
#mspec:focus{border-color:var(--clay)}
.spanel{display:flex;gap:0;padding:0;width:640px;overflow:hidden}
.snav{width:172px;background:color-mix(in srgb,var(--bg) 55%,var(--side));padding:16px 10px;
border-right:1px solid var(--line);display:flex;flex-direction:column;gap:3px;flex:none}
.snavh{font-weight:700;font-size:15px;padding:2px 10px 12px}
.snavi{text-align:left;border:0;background:transparent;color:var(--dim);border-radius:9px;
padding:9px 11px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;
transition:all .15s}
.snavi:hover{background:var(--card);color:var(--ink)}
.snavi.on{background:var(--card);color:var(--clay)}
.sbody{flex:1;padding:20px 22px;overflow-y:auto;max-height:78vh}
.spane{display:none;animation:fade .25s ease}
.spane.on{display:block}
.trow{display:flex;gap:10px;padding:8px 10px;border-radius:8px;font-size:12.5px;
font-family:'JetBrains Mono',monospace;align-items:center}
.trow:hover{background:var(--card)}
.trow .tl{width:70px;font-weight:700;color:var(--clay)}
.trow .tv{color:var(--green);width:80px}
.trow .tp{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* audit ledger */
.abadge{display:inline-block;padding:4px 11px;border-radius:8px;color:#fff;font-weight:700;
font-size:12px;letter-spacing:.2px}
.ameta{display:block;margin-top:7px;color:var(--dim);font-size:12px}
.arow{display:flex;gap:10px;padding:6px 8px;border-radius:7px;align-items:center;
font-family:'JetBrains Mono',monospace;font-size:12px;border-bottom:1px solid var(--line)}
.arow:hover{background:var(--card)}
.arow .ao{width:62px;font-weight:700;text-transform:uppercase;font-size:10.5px}
.arow .at{width:52px;color:var(--clay)}
.arow .atg{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.obpanel{width:480px;padding:34px 36px 28px}
.obmark{width:58px;height:58px;margin:0 auto 16px;border-radius:17px;display:grid;
place-items:center;font-size:26px;color:#fff;
background:linear-gradient(135deg,var(--clay),var(--clay2));
box-shadow:0 10px 30px var(--ring);animation:bob 3s ease-in-out infinite}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.obpanel .sin{text-align:center;margin:5px 0;animation:fade .5s ease backwards}
.obpanel .sin:nth-of-type(1){animation-delay:.05s}
.obpanel .sin:nth-of-type(2){animation-delay:.12s}
.obpanel .sin:nth-of-type(3){animation-delay:.19s}
#obgo{font-size:14px;border-radius:12px;
background:linear-gradient(135deg,var(--clay),var(--clay2))!important;
box-shadow:0 6px 18px var(--ring);transition:transform .15s,box-shadow .2s}
#obgo:hover{transform:translateY(-2px);box-shadow:0 10px 26px var(--ring);color:#fff}
.vtabs{display:flex;gap:6px;padding:0 0 10px}
.vt{flex:1;border:1px solid var(--line);background:transparent;color:var(--dim);border-radius:10px;
padding:8px 0;font-size:12.5px;font-weight:700;cursor:pointer;font-family:inherit;
letter-spacing:.3px;transition:all .18s}
.vt:hover{color:var(--ink);border-color:var(--dim)}
.vt.on{background:var(--card);color:var(--clay);border-color:var(--clay)}
.wspathpill{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 11px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw}
.modes{display:flex;gap:4px;margin-left:auto}
.mode{border:1px solid var(--line);background:transparent;color:var(--dim);border-radius:8px;
padding:6px 10px;font-size:11.5px;font-weight:700;cursor:pointer;font-family:inherit;
transition:all .15s;letter-spacing:.2px}
.mode:hover{color:var(--ink);border-color:var(--dim)}
.mode.on{color:var(--clay);border-color:var(--clay);background:color-mix(in srgb,var(--clay) 9%,transparent)}
.fprow{display:flex;align-items:center;gap:8px;padding:8px 11px;border-radius:8px;cursor:pointer;
font-family:'JetBrains Mono',monospace;font-size:12.5px;transition:background .12s}
.fprow:hover{background:var(--card);color:var(--clay)}
body[data-view=code] #log{font-size:13.5px}
body[data-view=code] .bot .body,body[data-view=code] .you .body{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:13px}
body[data-view=code] header{border-bottom-color:color-mix(in srgb,var(--clay) 35%,var(--line))}
/* ── code mode ── */
@keyframes slidedown{from{opacity:0;transform:translateY(-8px)}to{opacity:1}}
/* ── bottom context bar (always visible, below input) ── */
#wsbar{display:flex;align-items:center;gap:7px;padding:5px 0 4px;flex-wrap:wrap;
max-width:780px;margin:0 auto 6px}
.ctxbtn{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:9px;
border:1px solid var(--line);background:var(--card);color:var(--dim);font-size:12px;
font-family:inherit;cursor:pointer;transition:all .15s;white-space:nowrap}
.ctxbtn:hover{border-color:var(--clay);color:var(--clay)}
.ctxpill{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:9px;
border:1px solid var(--line);background:var(--card);color:var(--dim);font-size:12px;
white-space:nowrap;font-family:inherit}
#ctxtier{cursor:pointer;transition:all .15s}
#ctxtier:hover{border-color:var(--clay);color:var(--clay)}
#wscodeonly{display:none;align-items:center;gap:4px}
#wscodeonly.on{display:inline-flex}
#wspath{flex:1;background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:7px 11px;color:var(--ink);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none}
#wspath:focus{border-color:var(--clay)}
#wstreebox{display:none;max-height:200px;overflow-y:auto;border-bottom:1px solid var(--line);
padding:8px 22px;background:var(--side);font-family:'JetBrains Mono',monospace;font-size:12px}
#wstreebox.on{display:block;animation:slidedown .25s ease}
.tre{padding:2.5px 6px;border-radius:6px;cursor:pointer;color:var(--dim);white-space:pre}
.tre:hover{background:var(--card);color:var(--clay)}
.tre.dir{color:var(--ink);font-weight:600}
/* ── clock ── */
#clk{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--dim);
border:1px solid var(--line);border-radius:20px;padding:5px 12px;background:var(--card);
min-width:74px;text-align:center}
[data-theme=cyber] #clk{color:var(--clay);text-shadow:0 0 8px var(--ring)}
/* ── motion layer ── */
.mbtn,.newchat,.chip,.send{will-change:transform}
.mbtn:active,.chip:active,.newchat:active{transform:scale(.95)}
button.send:active{transform:scale(.88)}
.you .body{animation:slideL .3s cubic-bezier(.2,.8,.3,1.1)}
@keyframes slideL{from{opacity:0;transform:translateX(18px)}to{opacity:1;transform:none}}
.bot{animation:slideR .32s cubic-bezier(.2,.8,.3,1.1)}
@keyframes slideR{from{opacity:0;transform:translateX(-14px)}to{opacity:1;transform:none}}
.bot .who::before{animation:avpop .4s cubic-bezier(.3,1.6,.4,1)}
@keyframes avpop{from{transform:scale(0) rotate(-90deg)}to{transform:none}}
.typing{display:inline-flex;gap:4px;align-items:center;padding:4px 0}
.typing i{width:7px;height:7px;border-radius:50%;background:var(--clay);display:block;
animation:bounce 1.2s ease-in-out infinite}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:.5}30%{transform:translateY(-6px);opacity:1}}
.done-glow .body{animation:donepulse 1s ease}
@keyframes donepulse{0%{box-shadow:0 0 0 0 rgba(123,191,126,.55)}100%{box-shadow:0 0 0 14px rgba(123,191,126,0)}}
.conf{position:absolute;width:7px;height:7px;border-radius:2px;pointer-events:none;
animation:confl .9s ease-out forwards;z-index:60}
@keyframes confl{0%{opacity:1;transform:translate(0,0) rotate(0)}
100%{opacity:0;transform:translate(var(--dx),var(--dy)) rotate(540deg)}}
.mrow{animation:fade .35s ease backwards}
.mrow:nth-child(1){animation-delay:.03s}.mrow:nth-child(2){animation-delay:.08s}
.mrow:nth-child(3){animation-delay:.13s}.mrow:nth-child(4){animation-delay:.18s}
.htop{animation:fade .35s ease backwards}
.htop:nth-child(1){animation-delay:.03s}.htop:nth-child(2){animation-delay:.07s}
.htop:nth-child(3){animation-delay:.11s}.htop:nth-child(4){animation-delay:.15s}
.chat-it{animation:fade .25s ease}
@media(max-width:720px){aside{display:none}}
</style></head><body>
<aside>
  <div class=brand>termind <small id=ver></small></div>
  <div class=vtabs>
    <button class="vt on" id=vchat>💬 Chat</button>
    <button class=vt id=vcode>⌥ Code</button>
  </div>
  <button class=newchat id=new>✚&nbsp; New chat</button>
  <div class=label>Chats</div>
  <div id=chats></div>
  <div id=mecard title="profile & settings">
    <span class=meav id=meav>?</span>
    <span style="min-width:0"><div class=men id=men>set up profile</div>
    <div class=mes>profile · settings</div></span>
  </div>
  <div class=foot>private · $0/query · sandboxed on AION · localhost only</div>
</aside>
<main>
<header>
  <span id=title>New chat</span><span class=spacer></span>
  <span id=core><span class="dot off"></span>…</span>
  <select id=model title="quick switch" style="display:none"></select>
  <button class=mbtn id=mopen>⚙ Models</button>
  <span id=clk title="local time"></span>
</header>
<div class=overlay id=fp><div class=panel style="width:480px">
  <h2>📂 choose a workspace folder</h2>
  <div class=sub>navigating YOUR real folders (server-side — that's why this works)</div>
  <div id=fpcur class=wspathpill style="display:block;margin-bottom:10px"></div>
  <div id=fplist style="max-height:300px;overflow-y:auto"></div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
    <button class=mbtn id=fphome>🏠 home</button>
    <button class=mbtn id=fpsel style="background:var(--clay);color:#fff;border:0">✓ use this folder</button>
    <button class=mbtn id=fpclose>cancel</button>
  </div>
</div></div>
<div id=wstreebox></div>
<div class=warnbar id=warn>No local model is running — click <b id=warnopen>⚙ Models</b> for one-click guided setup. Chat works in limited offline mode until then.</div>
<div id=log><div class=wrap id=stream></div></div>
<div class=overlay id=ob><div class="panel obpanel" style="text-align:center">
  <div class=obmark>▲</div>
  <h2 style="font-size:24px;letter-spacing:-.4px">welcome to termind</h2>
  <div class=sub style="font-size:13.5px">your private local AI — chats, builds, sees & remembers.<br>everything stays on this machine. let's set you up.</div>
  <input id=obname class=sin placeholder="what should I call you?" style="text-align:center">
  <input id=obrole class=sin placeholder="what do you do? (optional)" style="text-align:center">
  <input id=obprefs class=sin placeholder="how do you like answers? e.g. short and direct (optional)" style="text-align:center">
  <div class=sub style="margin:12px 0 6px">pick a look</div>
  <div style="display:flex;gap:8px;justify-content:center">
    <button class="mbtn tbtn" data-t=dark>🌙 dark</button>
    <button class="mbtn tbtn" data-t=light>☀️ light</button>
    <button class="mbtn tbtn" data-t=cyber>🌆 cyberpunk</button>
  </div>
  <button class=mbtn id=obgo style="margin-top:16px;background:var(--clay);color:#fff;border:0;padding:10px 26px">Start →</button>
</div></div>
<div class=overlay id=ss><div class="panel spanel">
  <div class=snav>
    <div class=snavh>Settings</div>
    <button class="snavi on" data-s=profile>👤 Profile</button>
    <button class=snavi data-s=appearance>🎨 Appearance</button>
    <button class=snavi data-s=memory>🧠 Memory</button>
    <button class=snavi data-s=tools>🧰 Toolchains</button>
    <button class=snavi data-s=data>🗄 Databases</button>
    <button class=snavi data-s=ca>🧮 CA workbench</button>
    <button class=snavi data-s=security>🛡 Security</button>
    <button class=snavi data-s=audit>🔒 Audit</button>
    <button class=snavi data-s=help>📖 Help</button>
    <button class=snavi data-s=about>▲ About</button>
  </div>
  <div class=sbody>
  <section class="spane on" data-s=profile>
    <h2>Profile</h2>
    <div class=sub>fed to the model so every reply fits you · stored locally only</div>
    <input id=sname class=sin placeholder="your name">
    <input id=srole class=sin placeholder="your role (fed to the model)">
    <input id=sprefs class=sin placeholder="answer style, e.g. short and direct">
    <button class=mbtn id=ssave style="background:var(--clay);color:#fff;border:0;margin-top:8px">Save profile</button>
  </section>
  <section class=spane data-s=appearance>
    <h2>Appearance</h2>
    <div class=sub>one click · remembered everywhere</div>
    <div style="display:flex;gap:8px">
      <button class="mbtn tbtn" data-t=dark>🌙 dark</button>
      <button class="mbtn tbtn" data-t=light>☀️ light</button>
      <button class="mbtn tbtn" data-t=cyber>🌆 cyberpunk</button>
    </div>
  </section>
  <section class=spane data-s=memory>
    <h2>Memory</h2>
    <div class=sub>your facts are portable — bring them from anywhere, take them anywhere</div>
    <textarea id=smem class=sin rows=3 placeholder="paste memories exported from ChatGPT/Claude — one per line — and click import"></textarea>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class=mbtn id=smemimp>⬆ import</button>
      <button class=mbtn id=smemexp>⬇ export mine</button>
      <button class=mbtn id=smemclr>🗑 clear facts</button>
      <button class=mbtn id=schatclr>🗑 clear all chats</button>
    </div>
    <div class=ds id=smemout style="padding:6px 2px"></div>
  </section>
  <section class=spane data-s=tools>
    <h2>Toolchains</h2>
    <div class=sub>auto-detected languages on THIS machine — the code agent uses these exact commands</div>
    <div id=stools></div>
    <button class=mbtn id=stoolref style="margin-top:8px">↻ re-detect</button>
  </section>
  <section class=spane data-s=data>
    <h2>Databases</h2>
    <div class=sub>connect a database, then query it in plain language or SQL. termind verifies every query and shows an impact preview before anything destructive runs. SQLite needs zero dependencies; other engines install their driver into the isolated workspace venv.</div>
    <div id=sdblist style="margin:8px 0"></div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <input id=sdbname class=sin style="flex:0 0 110px" placeholder="name">
      <input id=sdbspec class=sin style="flex:1" placeholder="./app.db  or  postgres://user:pass@host/db">
      <button class=mbtn id=sdbadd style="background:var(--clay);color:#fff;border:0">+ add</button>
    </div>
    <div style="display:flex;gap:6px;margin-top:8px">
      <input id=sdbq class=sin style="flex:1" placeholder="ask in English, or write SQL…">
      <button class=mbtn id=sdbrun>run</button>
    </div>
    <pre id=sdbout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:12px;background:var(--card);border-radius:8px;padding:10px;margin-top:8px;max-height:240px;overflow:auto"></pre>
  </section>
  <section class=spane data-s=ca>
    <h2>CA workbench</h2>
    <div class=sub>built for chartered accountants who can't put client data in cloud tools. Every step runs on THIS machine with the local model, and each parse + export is sealed into the audit ledger — your DPDP "data never left the device" proof. Section 1 of the roadmap is live; scrutiny, GST, notices and financial statements follow.</div>
    <div class=catab style="font-weight:600;margin:12px 0 6px">📒 Bank statement → Tally</div>
    <div class=sub>upload a bank statement (CSV / Excel / PDF) or name one in your workspace. termind classifies every line to a ledger head + voucher (rules first, the local model for the rest) and writes ready-to-import Tally vouchers.</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0">
      <input type=file id=cafile accept=".csv,.txt,.tsv,.xlsx,.xls,.pdf" class=sin style="flex:1;padding:7px">
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <input id=capath class=sin style="flex:1" placeholder="…or a file already in your workspace, e.g. hdfc_apr.csv">
      <button class=mbtn id=carun style="background:var(--clay);color:#fff;border:0">→ convert to Tally</button>
    </div>
    <div id=cabadge style="margin:10px 0"></div>
    <div id=caledgers></div>
    <div id=cadl style="display:flex;gap:8px;margin-top:10px"></div>
    <pre id=caout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);margin-top:8px;background:var(--card);border-radius:8px;padding:10px;max-height:220px;overflow:auto"></pre>

    <div class=catab style="font-weight:600;margin:20px 0 6px;border-top:1px solid var(--line);padding-top:16px">🔍 Ledger scrutiny</div>
    <div class=sub>flag round numbers, duplicates, weekend entries, unusual spikes, missing narrations, and possible personal expenses — a first-pass review, locally.</div>
    <div style="display:flex;gap:6px;margin:8px 0;flex-wrap:wrap">
      <input type=file id=scrfile accept=".csv,.txt,.tsv,.xlsx,.xls,.pdf" class=sin style="flex:1;padding:7px">
      <input id=scrpath class=sin style="flex:1" placeholder="…or a workspace file (xlsx/pdf)">
      <button class=mbtn id=scrrun style="background:var(--clay);color:#fff;border:0">→ scrutinize</button>
    </div>
    <div id=scrbadge style="margin:8px 0"></div>
    <div id=scrdl style="display:flex;gap:8px;margin:6px 0"></div>
    <pre id=scrout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);background:var(--card);border-radius:8px;padding:10px;max-height:220px;overflow:auto"></pre>

    <div class=catab style="font-weight:600;margin:20px 0 6px;border-top:1px solid var(--line);padding-top:16px">🧾 GST 2B reconciliation</div>
    <div class=sub>match the purchase register against GSTR-2B → ITC at risk, ITC available unbooked, value mismatches, and probable invoice-number typos.</div>
    <div style="display:flex;gap:6px;margin:8px 0;flex-wrap:wrap">
      <label class=sub style="flex:0 0 100%;margin:0">purchase register (your books)</label>
      <input type=file id=gstbooks accept=".csv,.xlsx,.xls" class=sin style="flex:1;padding:7px">
      <input id=gstbookspath class=sin style="flex:1" placeholder="…or workspace file">
      <label class=sub style="flex:0 0 100%;margin:6px 0 0">GSTR-2B (from the portal)</label>
      <input type=file id=gstportal accept=".csv,.xlsx,.xls" class=sin style="flex:1;padding:7px">
      <input id=gstportalpath class=sin style="flex:1" placeholder="…or workspace file">
      <button class=mbtn id=gstrun style="background:var(--clay);color:#fff;border:0;flex:0 0 100%;margin-top:6px">→ reconcile</button>
    </div>
    <div id=gstbadge style="margin:8px 0"></div>
    <div id=gstdl style="display:flex;gap:8px;margin:6px 0"></div>
    <pre id=gstout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);background:var(--card);border-radius:8px;padding:10px;max-height:220px;overflow:auto"></pre>

    <div class=catab style="font-weight:600;margin:20px 0 6px;border-top:1px solid var(--line);padding-top:16px">📑 Notice reply</div>
    <div class=sub>paste a GST / Income-Tax notice; termind identifies the section and drafts a point-wise reply (the local model writes the body — never pasted to the cloud). Add known facts to ground it.</div>
    <textarea id=nottext class=sin style="width:100%;min-height:80px;padding:8px;font-family:inherit" placeholder="paste the notice text here (or use a workspace path below)"></textarea>
    <div style="display:flex;gap:6px;margin:8px 0;flex-wrap:wrap">
      <input id=notpath class=sin style="flex:1" placeholder="…or a workspace file (.pdf/.txt)">
      <input id=notfacts class=sin style="flex:1" placeholder="facts to use, e.g. income reconciles to 26AS">
      <button class=mbtn id=notrun style="background:var(--clay);color:#fff;border:0">→ draft reply</button>
    </div>
    <div id=notbadge style="margin:8px 0"></div>
    <div id=notdl style="display:flex;gap:8px;margin:6px 0"></div>
    <pre id=notout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);background:var(--card);border-radius:8px;padding:10px;max-height:260px;overflow:auto"></pre>

    <div class=catab style="font-weight:600;margin:20px 0 6px;border-top:1px solid var(--line);padding-top:16px">📊 Financial statements (Schedule III)</div>
    <div class=sub>turn a trial balance into a grouped Balance Sheet + Statement of Profit &amp; Loss per Schedule III, with totals and a balance check.</div>
    <div style="display:flex;gap:6px;margin:8px 0;flex-wrap:wrap">
      <input type=file id=fsfile accept=".csv,.xlsx,.xls" class=sin style="flex:1;padding:7px">
      <input id=fspath class=sin style="flex:1" placeholder="…or a workspace file (xlsx)">
      <button class=mbtn id=fsrun style="background:var(--clay);color:#fff;border:0">→ build statements</button>
    </div>
    <div id=fsbadge style="margin:8px 0"></div>
    <div id=fsdl style="display:flex;gap:8px;margin:6px 0"></div>
    <pre id=fsout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);background:var(--card);border-radius:8px;padding:10px;max-height:300px;overflow:auto"></pre>
  </section>
  <section class=spane data-s=security>
    <h2>Security</h2>
    <div class=sub>termind sweeps every folder you select for exposed secrets, dangerous scripts, and insecure dependencies — locally, offline. It also keeps the workspace isolated so termind uninstalls clean.</div>
    <div id=ssecbadge style="margin:10px 0"></div>
    <div id=ssecrows></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class=mbtn id=ssecscan>↻ rescan folder</button>
      <button class=mbtn id=ssecclean>🧹 uninstall plan</button>
    </div>
    <pre id=ssecclout style="white-space:pre-wrap;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--dim);margin-top:8px"></pre>
  </section>
  <section class=spane data-s=audit>
    <h2>Audit ledger</h2>
    <div class=sub>a tamper-evident, append-only record of every action the code agent took on this machine — who authorized it, what it touched, whether it was blocked. Hand the export to a security reviewer.</div>
    <div id=sauditbadge style="margin:10px 0"></div>
    <div id=sauditrows style="max-height:260px;overflow:auto"></div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class=mbtn id=sauditverify>✓ verify integrity</button>
      <button class=mbtn id=sauditexp style="background:var(--clay);color:#fff;border:0">⬇ export signed ledger</button>
    </div>
  </section>
  <section class=spane data-s=help>
    <h2>Help & workflows</h2>
    <div id=shelp></div>
    <div class=ds style="padding:6px 2px">or just ask in chat: <b style="color:var(--clay);cursor:pointer" id=shask>"what are termind's limitations?"</b></div>
  </section>
  <section class=spane data-s=about>
    <h2>About termind</h2>
    <div class=sub id=sabout></div>
    <div class=ds>a local AI agent — terminal + web, one brain.<br>private · $0/query · sandboxed on AION · localhost only.<br><br>github.com/bhupendra05/termind</div>
  </section>
  <div style="text-align:right;margin-top:10px"><button class=mbtn id=sclose>Close</button></div>
  </div>
</div></div>
<div class=overlay id=mm><div class=panel>
  <h2>⚙ Models</h2>
  <div class=sub>your local brains — switch instantly, or download new ones (all run on YOUR machine, $0)</div>
  <div id=minstalled></div>
  <div class=sub style="margin-top:14px">get more models <span style="opacity:.7">· one click, guided, downloads via Ollama</span></div>
  <div id=mcatalog></div>
  <div class=sub style="margin-top:16px">bring YOUR OWN model</div>
  <div class=mrow style="gap:8px">
    <input id=mspec placeholder="~/models/my-finetune.gguf  ·  or  hf.co/you/your-model">
    <button class=mbtn id=madd>＋ add</button>
  </div>
  <div class=ds style="padding:2px 8px 0;line-height:1.6">
    🧬 your local fine-tune: paste the path to its <b>.gguf</b> file<br>
    🤗 from Hugging Face: paste <b>hf.co/&lt;user&gt;/&lt;repo&gt;</b> (GGUF repos work directly)<br>
    🌐 model on another machine: launch with <b>OLLAMA_HOST=http://that-host:11434 termind</b>
  </div>
  <div id=mpull style="display:none"><div class=sub id=mpulltxt></div><div class=bar><div id=mpullbar></div></div></div>
  <div style="text-align:right;margin-top:14px"><button class=mbtn id=mclose>Close</button></div>
</div></div>
<footer>
  <div id=wsbar>
    <button class=ctxbtn id=wsbrowse>📂</button>
    <span id=wscur class=ctxpill>no folder</span>
    <span id=dbcur class=ctxpill style="display:none"></span>
    <span id=wscodeonly>
      <span class=modes>
        <button class=mode data-m=plan title="propose only — nothing executes">📋 plan</button>
        <button class=mode data-m=act title="actions execute">▶ act</button>
        <button class=mode data-m=bypass title="no confirmations, auto-run everything">⚡ bypass</button>
      </span>
      <button class=ctxbtn id=wstree>📁 files</button>
    </span>
    <span style=flex:1></span>
    <button class=ctxbtn id=ctxmodbtn title="click to open Models">⊕ <span id=ctxmod>—</span></button>
    <span class=ctxpill id=ctxtier title="click to cycle tier: smart → smarter → max">smart</span>
    <span class=ctxpill>🔒 ask</span>
  </div>
  <div id=imgchip style="max-width:760px;margin:0 auto 6px;display:none;align-items:center;gap:8px;color:var(--dim);font-size:12px">
    <img id=imgprev style="height:42px;border-radius:8px;border:1px solid var(--line)"/>
    <span id=imgname></span>
    <span style="cursor:pointer;color:var(--clay)" id=imgx>✕</span>
  </div>
  <div class=inbar>
    <button class=send id=att title="attach image" style="background:var(--card);color:var(--dim);border:1px solid var(--line)">📎</button>
    <input type=file id=file accept="image/*" style="display:none">
    <textarea id=in rows=1 placeholder="Message termind…  (📎 to add an image)"></textarea>
    <button class=send id=go>↑</button>
  </div>
  <div class=hint>Enter to send · Shift+Enter for newline · shares its brain with your terminal</div>
</footer>
</main>
<script>
const stream=document.getElementById('stream'),log=document.getElementById('log'),
inp=document.getElementById('in'),go=document.getElementById('go'),sel=document.getElementById('model'),
core=document.getElementById('core'),ver=document.getElementById('ver'),
chatsEl=document.getElementById('chats'),titleEl=document.getElementById('title');
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(s){s=esc(s);s=s.replace(/```(\w*)\n?([\s\S]*?)```/g,(m,l,c)=>'<pre>'+c.trim()+'</pre>');
return s.replace(/`([^`]+)`/g,'<code>$1</code>')}
function add(who,txt){
 if(stream.querySelector('.greet'))stream.innerHTML='';
 const m=document.createElement('div');m.className='msg '+who;
 m.innerHTML=who=='you'?'<div class=body>'+fmt(txt)+'</div>'
 :'<div class=who>termind</div><div class=body>'+fmt(txt)+'</div>';
 stream.appendChild(m);log.scrollTop=log.scrollHeight;return m.querySelector('.body')}
function greet(){
 if(view=='code'){stream.innerHTML='<div class=greet><h1>What are we building?</h1>'+
 '<div>code mode — files, folders and builds land in your workspace</div><div class=chips>'+
 '<span class=chip>build a small REST API here</span>'+
 '<span class=chip>create a folder called utils</span>'+
 '<span class=chip>write a Makefile for this project</span>'+
 '<span class=chip>/tree</span></div></div>';return}
 stream.innerHTML='<div class=greet><h1>How can I help?</h1>'+
'<div>your private local agent — it remembers you</div><div class=chips>'+
'<span class=chip>who am i?</span><span class=chip>/status</span>'+
'<span class=chip>create a new project: a python dice roller</span>'+
'<span class=chip>/think design a caching layer</span></div>'+
'<div style=margin-top:10px;font-size:12px>📎 attach an image — your local model can see it</div></div>'}
function renderChats(d){chatsEl.innerHTML='';
 d.chats.forEach(c=>{const e=document.createElement('div');
 e.className='chat-it'+(c.active?' active':'');
 e.innerHTML='<span class=tt></span><span class=del title="rename chat">✎</span><span class=del title="delete chat">✕</span>';
 e.querySelector('.tt').textContent=(c.ws?'📂 ':'')+c.title;
 if(c.ws)e.title=c.ws;
 if(c.active)titleEl.textContent=c.title;
 e.querySelector('.tt').onclick=()=>chatOp({op:'open',id:c.id});
 const rn=(ev)=>{ev.stopPropagation();const t=prompt('Rename chat:',c.title);
  if(t&&t.trim())chatOp({op:'rename',id:c.id,title:t.trim()})};
 e.querySelectorAll('.del')[0].onclick=rn;
 e.querySelector('.tt').ondblclick=rn;
 e.querySelectorAll('.del')[1].onclick=(ev)=>{ev.stopPropagation();
  if(confirm('Delete "'+c.title+'"? This cannot be undone.'))chatOp({op:'delete',id:c.id})};
 chatsEl.appendChild(e)});}
function renderMsgs(ms){stream.innerHTML='';if(!ms.length){greet();return}
 ms.forEach(m=>add(m.role=='user'?'you':'bot',m.content))}
async function chatOp(body){body.mode=view;const d=await (await fetch('/api/chat',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 renderChats(d);renderMsgs(d.messages);if(body.op=='new')titleEl.textContent='New chat';
 if(d.workspace)wscur.textContent=d.workspace;
 if(view=='code'&&body.op=='new'&&!d.has_ws){fp.classList.add('open');browse('')}
 if(view=='code')refreshWs()}
async function loadChats(){const d=await (await fetch('/api/chats?mode='+view)).json();
 renderChats(d);renderMsgs(d.messages);if(d.workspace)wscur.textContent=d.workspace}
async function state(){const s=await (await fetch('/api/state')).json();
 ver.textContent='v'+s.version;
 core.innerHTML='<span class="dot '+(s.live?'live':'off')+'"></span>'+(s.live?s.model:'offline');
 document.querySelectorAll('.mode').forEach(b=>b.classList.toggle('on',b.dataset.m==s.agent_mode));
 document.getElementById('warn').style.display=s.live?'none':'block';
 sel.innerHTML='';s.models.forEach(m=>{const o=document.createElement('option');o.value=m;
  o.textContent=m;if(m.split(':')[0]==s.model.split(':')[0])o.selected=true;sel.appendChild(o)});
 const dc=document.getElementById('dbcur');
 if(dc){if(s.db){dc.textContent='🗄 '+s.db;dc.style.display='inline-flex'}else{dc.style.display='none'}}
 const tc=document.getElementById('ctxtier');if(tc)tc.textContent=s.tier||'smart';
 const mc=document.getElementById('ctxmod');if(mc)mc.textContent=(s.model||'—').split(':')[0];
 const wsc=document.getElementById('wscur');if(wsc&&wsc.textContent==='no folder'&&s.workspace)wsc.textContent=s.workspace}
sel.onchange=async()=>{const b=await (await fetch('/api/model',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({model:sel.value})})).json();
 add('bot',b.reply);state()};
document.getElementById('ctxmodbtn').onclick=()=>{mm.classList.add('open');renderModels()};
document.getElementById('ctxtier').onclick=async()=>{
 const tiers=['smart','smarter','max'];
 const cur=document.getElementById('ctxtier').textContent.trim();
 const next=tiers[(tiers.indexOf(cur)+1)%tiers.length];
 const r=await (await fetch('/api/tier',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({tier:next})})).json();
 add('bot',r.reply);state()}
document.getElementById('new').onclick=()=>chatOp({op:'new'});
let view='chat';
let busy=false,img=null,imgName='',imgURL='';
const att=document.getElementById('att'),file=document.getElementById('file'),
chip=document.getElementById('imgchip'),prev=document.getElementById('imgprev'),
iname=document.getElementById('imgname');
att.onclick=()=>file.click();
file.onchange=()=>{const f=file.files[0];if(!f)return;const rd=new FileReader();
 rd.onload=()=>{imgURL=rd.result;img=imgURL.split(',')[1];imgName=f.name;
 prev.src=imgURL;iname.textContent=f.name;chip.style.display='flex'};rd.readAsDataURL(f)};
document.getElementById('imgx').onclick=()=>{img=null;imgName='';imgURL='';chip.style.display='none';file.value=''};
async function send(t){if(busy||(!t.trim()&&!img))return;busy=true;go.disabled=true;
 const body=add('you',t||'(image)');
 if(imgURL){const im=document.createElement('img');im.src=imgURL;
  im.style.cssText='display:block;max-width:220px;border-radius:10px;margin-top:8px';
  body.appendChild(im)}
 inp.value='';inp.style.height='auto';
 const b=add('bot','');b.innerHTML='<span class=typing><i></i><i></i><i></i></span>';
 let r;
 try{r=await (await fetch('/api/send',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,image:img,image_name:imgName,mode:view})})).json();
  img=null;imgName='';imgURL='';chip.style.display='none';file.value='';
  b.innerHTML=r.reply=='__EXIT__'?'<span class=think>session closed.</span>':fmt(r.reply||'(no output)');
  if(r.image){const im=document.createElement('img');im.src='data:image/png;base64,'+r.image;
   im.style.cssText='display:block;max-width:320px;border-radius:10px;margin-top:10px;background:repeating-conic-gradient(#444 0 25%,#555 0 50%) 0 0/16px 16px';
   b.appendChild(im)}
  if(r.options&&r.options.length){const q=document.createElement('div');q.className='quick';
   r.options.forEach(o=>{const c=document.createElement('button');c.className='qchip';c.textContent=o;
    c.onclick=()=>{q.remove();send(o)};q.appendChild(c)});b.appendChild(q)}}
 catch(e){b.innerHTML='<span class=think>error: '+e+'</span>'}
 busy=false;go.disabled=false;inp.focus();
 if(r&&/^(applied|built|created|wrote|imported|removed|saved|switched|renamed|workspace set)/.test(r.reply||'')){
  const m=b.closest('.msg');m.classList.add('done-glow');celebrate(b)}
 const d=await (await fetch('/api/chats')).json();renderChats(d);
 log.scrollTop=log.scrollHeight}
function celebrate(el){const r=el.getBoundingClientRect();
 const cols=['#d97757','#7bbf7e','#a64dff','#05d9e8','#ffd700'];
 for(let i=0;i<14;i++){const p=document.createElement('span');p.className='conf';
  p.style.left=(r.left+20+Math.random()*120)+'px';p.style.top=(r.top+8)+'px';
  p.style.background=cols[i%cols.length];
  p.style.setProperty('--dx',(Math.random()*140-70)+'px');
  p.style.setProperty('--dy',(40+Math.random()*90)+'px');
  document.body.appendChild(p);setTimeout(()=>p.remove(),950)}}
go.onclick=()=>send(inp.value);
inp.addEventListener('keydown',e=>{if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();send(inp.value)}});
inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=inp.scrollHeight+'px'});
document.addEventListener('click',e=>{if(e.target.classList.contains('chip'))send(e.target.textContent)});
const mm=document.getElementById('mm'),mi=document.getElementById('minstalled'),
mc=document.getElementById('mcatalog'),mp=document.getElementById('mpull'),
mpt=document.getElementById('mpulltxt'),mpb=document.getElementById('mpullbar');
let mpoll=null;
function row(html){const d=document.createElement('div');d.className='mrow';d.innerHTML=html;return d}
async function renderModels(){const c=await (await fetch('/api/catalog')).json();
 mi.innerHTML='';c.installed.forEach(m=>{const act=m.split(':')[0]==c.active.split(':')[0];
  const e=row('<div style="flex:1"><div class=nm>'+m+'</div></div>'+
   '<button class="mbtn'+(act?' active':'')+'">'+(act?'✓ active':'use')+'</button>');
  e.querySelector('button').onclick=async()=>{await fetch('/api/model',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({model:m})});
   state();renderModels()};mi.appendChild(e)});
 if(!c.installed.length)mi.appendChild(row('<div class=ds>nothing installed yet — pick one below ⤵</div>'));
 mc.innerHTML='';c.catalog.filter(x=>!x.installed).forEach(x=>{
  const e=row('<div style="flex:1"><div class=nm>'+x.name+'</div><div class=ds>'+x.desc+'</div></div>'+
   '<span class=sz>'+x.size+'</span><button class=mbtn>⬇ download</button>');
  e.querySelector('button').onclick=async()=>{await fetch('/api/pull',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({model:x.name})});
   pollPull()};mc.appendChild(e)});
 if(c.pull&&c.pull.status=='pulling')pollPull();else mp.style.display='none'}
function pollPull(){mp.style.display='block';if(mpoll)return;
 mpoll=setInterval(async()=>{const c=await (await fetch('/api/catalog')).json();const p=c.pull;
  if(p.status=='pulling'){mpt.textContent='downloading '+p.name+' — '+(p.pct||0)+'% ('+(p.stage||'')+')';
   mpb.style.width=(p.pct||0)+'%'}
  else{clearInterval(mpoll);mpoll=null;
   mpt.textContent=p.status=='done'?p.name+' ready — click "use" to switch':'failed: '+(p.error||'');
   mpb.style.width='100%';state();renderModels()}},1500)}
document.getElementById('madd').onclick=async()=>{
 const spec=document.getElementById('mspec').value.trim();if(!spec)return;
 const r=await (await fetch('/api/import',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({spec:spec})})).json();
 document.getElementById('mspec').value='';
 mpt.textContent=r.reply;mp.style.display='block';
 if(r.pull&&r.pull.status=='pulling')pollPull();};
document.getElementById('mspec').addEventListener('keydown',e=>{
 if(e.key=='Enter')document.getElementById('madd').click()});
document.getElementById('mopen').onclick=()=>{mm.classList.add('open');renderModels()};
document.getElementById('mclose').onclick=()=>mm.classList.remove('open');
mm.onclick=(e)=>{if(e.target===mm)mm.classList.remove('open')};
document.getElementById('warnopen').onclick=()=>{mm.classList.add('open');renderModels()};
const ss=document.getElementById('ss'),ob=document.getElementById('ob');
let prof={};
function applyTheme(t){document.body.dataset.theme=(t&&t!=='dark')?t:'';
 document.querySelectorAll('.tbtn').forEach(b=>b.classList.toggle('on',b.dataset.t==(t||'dark')))}
document.querySelectorAll('.tbtn').forEach(b=>b.onclick=()=>{prof.theme=b.dataset.t;
 applyTheme(prof.theme);fetch('/api/profile',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:prof.theme})})});
async function loadProfile(){prof=await (await fetch('/api/profile')).json();
 applyTheme(prof.theme);
 document.getElementById('meav').textContent=(prof.name||'?')[0].toUpperCase();
 document.getElementById('men').textContent=prof.name||'set up profile';
 document.getElementById('sname').value=prof.name||'';
 document.getElementById('srole').value=prof.role||'';
 document.getElementById('sprefs').value=prof.prefs||'';
 if(!prof.onboarded)ob.classList.add('open')}
document.getElementById('obgo').onclick=async()=>{
 const nm=document.getElementById('obname').value.trim()||'friend';
 await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({name:nm,role:document.getElementById('obrole').value,
  prefs:document.getElementById('obprefs').value,theme:prof.theme||'dark'})});
 ob.classList.remove('open');loadProfile();
 add('bot','welcome aboard, '+nm+' — I will remember you. Try the chips above, or ask me anything.')};
document.getElementById('mecard').onclick=async()=>{ss.classList.add('open');
 const h=await (await fetch('/api/help')).json();const sh=document.getElementById('shelp');
 sh.innerHTML='';Object.entries(h.topics).forEach(([k,v])=>{const d=document.createElement('div');
  d.className='htop';d.innerHTML='<b>'+k+'</b><div class=hb></div>';
  d.querySelector('.hb').textContent=v;
  d.onclick=()=>d.classList.toggle('open');sh.appendChild(d)})};
document.querySelectorAll('.snavi').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.snavi').forEach(x=>x.classList.toggle('on',x===b));
 document.querySelectorAll('.spane').forEach(p=>p.classList.toggle('on',p.dataset.s==b.dataset.s));
 if(b.dataset.s=='tools')loadTools();
 if(b.dataset.s=='audit')loadAudit();
 if(b.dataset.s=='data')loadDb();
 if(b.dataset.s=='ca')loadCa();
 if(b.dataset.s=='security')loadSec();
 if(b.dataset.s=='about')document.getElementById('sabout').textContent='version '+ver.textContent;});
async function loadTools(refresh){const d=await (refresh
 ?await fetch('/api/toolchain',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
 :await fetch('/api/toolchain')).json();
 const el=document.getElementById('stools');el.innerHTML='';
 Object.entries(d.toolchain).filter(([k])=>!k.startsWith('_')).forEach(([k,v])=>{
  const r=document.createElement('div');r.className='trow';
  r.innerHTML='<span class=tl></span><span class=tv></span><span class=tp></span>';
  r.querySelector('.tl').textContent=k;
  r.querySelector('.tv').textContent=v.cmd+' '+v.version;
  r.querySelector('.tp').textContent=v.path;el.appendChild(r)})}
document.getElementById('stoolref').onclick=()=>loadTools(true);
async function loadDb(){const d=await (await fetch('/api/db')).json();
 const el=document.getElementById('sdblist');el.innerHTML='';
 if(!d.databases.length){el.innerHTML='<div class=ds>no databases yet — add one below.</div>'}
 d.databases.forEach(db=>{const r=document.createElement('div');r.className='trow';
  const on=db.name==d.active;
  r.innerHTML='<span class=tl style="color:'+(on?'var(--clay)':'var(--dim)')+'">'+(on?'● ':'') +db.name+'</span><span class=tv>'+db.engine+'</span><span class=tp></span>';
  r.querySelector('.tp').textContent=db.spec;
  r.style.cursor='pointer';r.onclick=async()=>{await fetch('/api/db',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'use',name:db.name})});loadDb();state()};
  el.appendChild(r)})}
async function dbApi(op,extra){return (await (await fetch('/api/db',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({op:op},extra||{}))})).json())}
document.getElementById('sdbadd').onclick=async()=>{const r=await dbApi('add',{name:document.getElementById('sdbname').value,spec:document.getElementById('sdbspec').value});
 document.getElementById('sdbout').textContent=r.reply;loadDb();state()};
document.getElementById('sdbrun').onclick=async()=>{const t=document.getElementById('sdbq').value;
 const r=await dbApi('query',{text:t});const out=document.getElementById('sdbout');out.textContent=r.reply;
 if(r.pending){const c=document.createElement('div');c.className='quick';
  ['confirm','cancel'].forEach(a=>{const b=document.createElement('button');b.className='qchip';b.textContent=a;
   b.onclick=async()=>{const rr=await dbApi('confirm',{answer:a});out.textContent=r.reply+'\n\n'+rr.reply;loadDb()};c.appendChild(b)});
  out.parentNode.insertBefore(c,out.nextSibling)}}
function loadCa(){['caout','scrout','gstout','notout','fsout'].forEach(i=>{const e=document.getElementById(i);if(e)e.textContent=''});
 ['cabadge','caledgers','cadl','scrbadge','scrdl','gstbadge','gstdl','notbadge','notdl','fsbadge','fsdl'].forEach(i=>{const e=document.getElementById(i);if(e)e.innerHTML=''})}
function caDownload(name,content,mime){const b=new Blob([content],{type:mime});
 const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download=name;
 document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u)}
async function caApi(body){return (await (await fetch('/api/ca',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json())}
function caErr(id,d){document.getElementById(id).innerHTML='<span class=abadge style="background:#a3262d">⚠ '+(d.error||'failed')+'</span>'}
function caDLbtns(id,files){const dl=document.getElementById(id);dl.innerHTML='';
 files.forEach((f,i)=>{if(!f.content)return;const b=document.createElement('button');b.className='mbtn';
  if(i===0){b.style.cssText='background:var(--clay);color:#fff;border:0'}
  b.textContent='⬇ '+f.name;b.onclick=()=>caDownload(f.name,f.content,f.mime||'text/plain');dl.appendChild(b)})}
async function caGather(op,fileId,pathId,extra){const body=Object.assign({op:op},extra||{});
 const f=fileId&&document.getElementById(fileId).files[0];
 if(f){body.filename=f.name;body.content=await f.text()}
 else if(pathId&&document.getElementById(pathId).value.trim()){body.path=document.getElementById(pathId).value.trim()}
 else return null;
 return caApi(body)}
function caRender(d){if(!d.ok){caErr('cabadge',d);return}
 const s=d.summary;
 document.getElementById('cabadge').innerHTML='<span class=abadge style="background:var(--ok,#1f7a4d)">✓ '+s.transactions+' VOUCHERS</span>'+
  '<span class=ameta>in ₹'+s.total_in.toLocaleString('en-IN')+' · out ₹'+s.total_out.toLocaleString('en-IN')+
  ' · '+s.auto_classified+' by rules'+(s.by_model?' · '+s.by_model+' by local model':'')+' · '+s.needs_review+' to review</span>';
 const box=document.getElementById('caledgers');box.innerHTML='';
 (d.ledgers||[]).slice(0,8).forEach(l=>{const r=document.createElement('div');r.className='trow';
  r.innerHTML='<span class=tl></span><span class=tv></span><span class=tp style="text-align:right"></span>';
  r.querySelector('.tl').textContent=l.head;r.querySelector('.tv').textContent=l.count+' txn';
  r.querySelector('.tp').textContent='₹'+Number(l.amount).toLocaleString('en-IN');box.appendChild(r)});
 caDLbtns('cadl',[{name:d.xml,content:d.xml_content,mime:'application/xml'},{name:d.csv,content:d.csv_content,mime:'text/csv'}]);
 document.getElementById('caout').textContent=d.report||''}
document.getElementById('carun').onclick=async()=>{const out=document.getElementById('caout');
 out.textContent='working — locally, nothing leaves this machine…';
 const d=await caGather('bank','cafile','capath');
 if(!d){out.textContent='choose a statement file, or type a workspace path.';return}caRender(d)};
document.getElementById('scrrun').onclick=async()=>{const out=document.getElementById('scrout');
 out.textContent='scrutinizing — on-device…';
 const d=await caGather('scrutiny','scrfile','scrpath');
 if(!d){out.textContent='choose a ledger file, or a workspace path.';return}
 if(!d.ok){caErr('scrbadge',d);out.textContent='';return}const s=d.summary;
 document.getElementById('scrbadge').innerHTML='<span class=abadge style="background:'+(s.clean?'var(--ok,#1f7a4d)':'#a3262d')+'">'+(s.clean?'✓ CLEAN':'⚠ '+s.flags+' FLAG(S)')+'</span><span class=ameta>'+s.high+' high · '+s.medium+' medium · '+s.low+' low · '+s.transactions+' entries</span>';
 caDLbtns('scrdl',[{name:d.csv,content:d.csv_content,mime:'text/csv'}]);out.textContent=d.report||''};
document.getElementById('gstrun').onclick=async()=>{const out=document.getElementById('gstout');
 out.textContent='reconciling — on-device…';const body={op:'gst'};
 const fb=document.getElementById('gstbooks').files[0],fp=document.getElementById('gstportal').files[0];
 if(fb){body.books_name=fb.name;body.books_content=await fb.text()}else if(document.getElementById('gstbookspath').value.trim()){body.books=document.getElementById('gstbookspath').value.trim()}
 if(fp){body.portal_name=fp.name;body.portal_content=await fp.text()}else if(document.getElementById('gstportalpath').value.trim()){body.portal=document.getElementById('gstportalpath').value.trim()}
 if((!body.books_content&&!body.books)||(!body.portal_content&&!body.portal)){out.textContent='give both files: the purchase register and the GSTR-2B.';return}
 const d=await caApi(body);if(!d.ok){caErr('gstbadge',d);out.textContent='';return}const s=d.summary;
 document.getElementById('gstbadge').innerHTML='<span class=abadge style="background:'+(s.itc_at_risk>0?'#a3262d':'var(--ok,#1f7a4d)')+'">ITC at risk ₹'+s.itc_at_risk.toLocaleString('en-IN')+'</span><span class=ameta>'+s.matched+' matched · '+s.in_books_not_2b+' not in 2B · '+s.in_2b_not_books+' unbooked · '+s.value_mismatch+' value · '+s.probable_invoice_typo+' typo?</span>';
 caDLbtns('gstdl',[{name:d.csv,content:d.csv_content,mime:'text/csv'}]);out.textContent=d.report||''};
document.getElementById('notrun').onclick=async()=>{const out=document.getElementById('notout');
 out.textContent='drafting — on-device…';const body={op:'notice',facts:document.getElementById('notfacts').value.trim()};
 const txt=document.getElementById('nottext').value.trim();
 if(txt){body.filename='notice.txt';body.content=txt}
 else if(document.getElementById('notpath').value.trim()){body.path=document.getElementById('notpath').value.trim()}
 else{out.textContent='paste the notice text, or give a workspace path.';return}
 const d=await caApi(body);if(!d.ok){caErr('notbadge',d);out.textContent='';return}const n=d.notice;
 document.getElementById('notbadge').innerHTML='<span class=abadge style="background:var(--clay)">'+n.kind+'</span><span class=ameta>'+n.law+(n.section?' · '+n.section:'')+(d.by_model?' · drafted by local model':' · offline skeleton')+'</span>';
 caDLbtns('notdl',[{name:d.md,content:d.md_content,mime:'text/markdown'}]);out.textContent=d.draft||''};
document.getElementById('fsrun').onclick=async()=>{const out=document.getElementById('fsout');
 out.textContent='building statements — on-device…';
 const d=await caGather('fs','fsfile','fspath');
 if(!d){out.textContent='choose a trial balance file, or a workspace path.';return}
 if(!d.ok){caErr('fsbadge',d);out.textContent='';return}const b=d.statements.bs,p=d.statements.pnl;
 document.getElementById('fsbadge').innerHTML='<span class=abadge style="background:'+(b.balanced?'var(--ok,#1f7a4d)':'#a3262d')+'">'+(b.balanced?'✓ BALANCED':'⚠ NOT BALANCED')+'</span><span class=ameta>PBT ₹'+p.profit_before_tax.toLocaleString('en-IN')+' · BS total ₹'+b.total_assets.toLocaleString('en-IN')+'</span>';
 caDLbtns('fsdl',[{name:d.txt,content:d.txt_content,mime:'text/plain'}]);out.textContent=d.text||''};
async function loadSec(){const d=await (await fetch('/api/scan')).json();renderSec(d)}
function renderSec(d){const s=d.summary,ok=s.clean;
 document.getElementById('ssecbadge').innerHTML='<span class=abadge style="background:'+(ok?'var(--ok,#1f7a4d)':'#a3262d')+'">'+(ok?'✓ CLEAN':'⚠ '+s.total+' ISSUE(S)')+'</span><span class=ameta>'+s.high+' high · '+s.medium+' medium · '+s.low+' low · '+(d.workspace||'')+'</span>';
 const box=document.getElementById('ssecrows');box.innerHTML='';
 (d.findings||[]).forEach(f=>{const r=document.createElement('div');r.className='arow';
  const col=f.severity=='high'?'#a3262d':f.severity=='medium'?'#b8860b':'var(--dim)';
  r.innerHTML='<span class=ao style="color:'+col+'">'+f.severity+'</span><span class=at></span><span class=atg></span>';
  r.querySelector('.at').textContent=f.kind;r.querySelector('.atg').textContent=f.file+':'+f.line+' — fix: '+f.fix;
  r.title=f.snippet;box.appendChild(r)})}
document.getElementById('ssecscan').onclick=async()=>{const d=await (await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();renderSec(d)};
document.getElementById('ssecclean').onclick=async()=>{const d=await (await fetch('/api/lifecycle')).json();
 const p=d.plan;let t=d.summary+'\\n\\nfull uninstall would remove:\\n';
 p.assets.forEach(a=>{t+='  '+(a.exists?'✓':'·')+' '+a.kind+'  '+a.path+'\\n'});
 t+='  · termind home: '+p.home+'\\n'+d.models.note;
 document.getElementById('ssecclout').textContent=t};
async function loadAudit(){const d=await (await fetch('/api/ledger')).json();
 const s=d.summary,ok=d.integrity.ok;
 const badge=document.getElementById('sauditbadge');
 badge.innerHTML='<span class=abadge style="background:'+(ok?'var(--ok,#1f7a4d)':'#a3262d')+'">'
  +(ok?'✓ VERIFIED · chain intact':'⚠ TAMPERED @ #'+d.integrity.broken_at)+'</span>'
  +'<span class=ameta>'+s.count+' actions · '+s.ok+' ok · '+s.fail+' fail · '+s.blocked+' blocked · '+s.bytes+' bytes written</span>';
 const box=document.getElementById('sauditrows');box.innerHTML='';
 if(!d.entries.length){box.innerHTML='<div class=ds style="padding:8px 2px">no actions recorded yet — build something in Code mode.</div>';return}
 d.entries.slice().reverse().forEach(e=>{const r=document.createElement('div');r.className='arow';
  const col=e.outcome=='ok'?'var(--ok,#1f7a4d)':e.outcome=='blocked'?'#a3262d':'#b8860b';
  r.innerHTML='<span class=ao style="color:'+col+'">'+e.outcome+'</span>'
   +'<span class=at></span><span class=atg></span>';
  r.querySelector('.at').textContent=e.tool;
  r.querySelector('.atg').textContent=e.target;
  r.title=e.iso+(e.consent?'  ·  authorized by: "'+e.consent+'"':'');box.appendChild(r)})}
document.getElementById('sauditverify').onclick=async()=>{await loadAudit();
 const v=(await (await fetch('/api/ledger')).json()).integrity;
 add('bot',v.ok?'🔒 audit ledger verified — hash chain intact across '+v.count+' actions, no tampering.':'⚠ audit ledger TAMPERED at entry #'+v.broken_at+'.')};
document.getElementById('sauditexp').onclick=async()=>{
 const full=await (await fetch('/api/ledger?full=1')).json();
 const blob=new Blob([JSON.stringify(full,null,2)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);
 a.download='termind-agent-ledger.json';a.click();URL.revokeObjectURL(a.href)};
document.getElementById('sclose').onclick=()=>ss.classList.remove('open');
ss.onclick=(e)=>{if(e.target===ss)ss.classList.remove('open')};
document.getElementById('ssave').onclick=async()=>{
 await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({name:document.getElementById('sname').value,
   role:document.getElementById('srole').value,
   prefs:document.getElementById('sprefs').value})});
 ss.classList.remove('open');loadProfile()};
document.getElementById('smemimp').onclick=async()=>{
 const r=await (await fetch('/api/memory',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({op:'import',text:document.getElementById('smem').value})})).json();
 document.getElementById('smemout').textContent=r.reply;document.getElementById('smem').value=''};
document.getElementById('smemexp').onclick=async()=>{
 const r=await (await fetch('/api/memory',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'export'})})).json();
 document.getElementById('smem').value=r.reply;
 document.getElementById('smemout').textContent='your memories ⤴ (copy them anywhere)'};
document.getElementById('smemclr').onclick=async()=>{if(!confirm('Forget all facts about you?'))return;
 const r=await (await fetch('/api/memory',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'clear',what:'facts'})})).json();
 document.getElementById('smemout').textContent=r.reply};
document.getElementById('schatclr').onclick=async()=>{if(!confirm('Delete ALL conversations?'))return;
 await fetch('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({op:'clear',what:'chats'})});loadChats();
 document.getElementById('smemout').textContent='all chats cleared'};
document.getElementById('shask').onclick=()=>{ss.classList.remove('open');
 send("what are termind's limitations?")};
/* clock */
const clk=document.getElementById('clk');
setInterval(()=>{const d=new Date();
 clk.textContent=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
 clk.title=d.toDateString()},1000);
/* bottom bar */
const wsbar=document.getElementById('wsbar'),wsbox=document.getElementById('wstreebox'),
wscur=document.getElementById('wscur');
function setView(v){view=v;document.body.dataset.view=v;
 document.getElementById('vchat').classList.toggle('on',v=='chat');
 document.getElementById('vcode').classList.toggle('on',v=='code');
 document.getElementById('wscodeonly').classList.toggle('on',v=='code');
 if(v!='code')wsbox.classList.remove('on');else refreshWs();
 loadChats()}
document.querySelectorAll('.mode').forEach(b=>b.onclick=async()=>{
 const r=await (await fetch('/api/mode',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:b.dataset.m})})).json();
 document.querySelectorAll('.mode').forEach(x=>x.classList.toggle('on',x.dataset.m==r.agent_mode));
 add('bot',r.reply)});
document.getElementById('vchat').onclick=()=>setView('chat');
document.getElementById('vcode').onclick=()=>setView('code');
/* folder picker */
const fp=document.getElementById('fp'),fpl=document.getElementById('fplist'),
fpc=document.getElementById('fpcur');let fppath='';
async function browse(p){const d=await (await fetch('/api/ws',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'browse',path:p||''})})).json();
 fppath=d.current;fpc.textContent=d.current;fpl.innerHTML='';
 if(d.parent){const up=document.createElement('div');up.className='fprow';
  up.textContent='⬆ ..';up.onclick=()=>browse(d.parent);fpl.appendChild(up)}
 d.dirs.forEach(n=>{const e=document.createElement('div');e.className='fprow';
  e.textContent='📁 '+n;e.onclick=()=>browse(fppath+'/'+n);fpl.appendChild(e)})}
document.getElementById('wsbrowse').onclick=()=>{fp.classList.add('open');browse(wscur.textContent.includes('/')?wscur.textContent:'')};
document.getElementById('fphome').onclick=()=>browse('~');
document.getElementById('fpclose').onclick=()=>fp.classList.remove('open');
fp.onclick=(e)=>{if(e.target===fp)fp.classList.remove('open')};
document.getElementById('fpsel').onclick=async()=>{fp.classList.remove('open');
 const r=await (await fetch('/api/ws',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({op:'set',path:fppath})})).json();
 wscur.textContent=r.workspace;add('bot',r.reply);
 celebrate(document.querySelector('#stream .msg:last-child .body')||document.body);refreshWs()};
async function refreshWs(){const d=await (await fetch('/api/ws',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'tree'})})).json();
 wscur.textContent=d.workspace;renderTree(d.tree)}
function renderTree(t){wsbox.innerHTML='';t.forEach(e=>{const d=document.createElement('div');
 d.className='tre'+(e.dir?' dir':'');
 d.textContent='  '.repeat(e.depth)+(e.dir?'📁 ':'· ')+e.path.split('/').pop();
 if(!e.dir)d.onclick=async()=>{const r=await (await fetch('/api/ws',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'read',path:e.path})})).json();
  add('bot','`'+r.path+'`\n```\n'+r.content.slice(0,4000)+'\n```');wsbox.classList.remove('on')};
 wsbox.appendChild(d)})}

document.getElementById('wstree').onclick=()=>{wsbox.classList.toggle('on');
 if(wsbox.classList.contains('on'))refreshWs()};

state();loadChats();loadProfile();inp.focus();
</script></body></html>"""
