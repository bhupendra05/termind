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
            }))
        if self.path == "/api/chats":
            s = self.session
            return self._send(200, json.dumps({
                "chats": s.chats_list(),
                "messages": s.history,            # messages of the active chat
            }))
        if self.path == "/api/catalog":
            return self._send(200, json.dumps(self.session.model_catalog()))
        if self.path == "/api/profile":
            return self._send(200, json.dumps(self.session.profile()))
        if self.path == "/api/help":
            from .helpdocs import TOPICS
            return self._send(200, json.dumps({"topics": TOPICS}))
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
                    # web has no stdin, so consent-gated actions auto-approve in the UI flow
                    out = self.session.handle_web(text, image=image,
                                                  image_name=req.get("image_name") or "image")
                except SystemExit:
                    out = "__EXIT__"
                except Exception as e:
                    out = f"error: {e}"
            resp = {"reply": _strip_ansi(out)}
            # an edit happened or the user asked to see it → return the actual image
            if self.session.last_image and (out.startswith("applied")
                                            or out.startswith("here's the current image")):
                resp["image"] = self.session.last_image[1]
            return self._send(200, json.dumps(resp))
        if self.path == "/api/chat":
            s = self.session
            with s._lock:
                if req.get("op") == "new":
                    s.chat_new()
                elif req.get("op") == "open":
                    s.chat_open(str(req.get("id", "")))
                elif req.get("op") == "delete":
                    s.chat_delete(str(req.get("id", "")))
                elif req.get("op") == "rename":
                    s.chat_rename(str(req.get("id", "")), str(req.get("title", "")))
            return self._send(200, json.dumps({"chats": s.chats_list(),
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
        if self.path == "/api/model":
            name = (req.get("model") or "").strip()
            with self.session._lock:
                out = _strip_ansi(self.session.do_model(name))
            return self._send(200, json.dumps({"reply": out, "model": self.session.model}))
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
/* ── code mode ── */
#wsbar{display:none;align-items:center;gap:9px;padding:9px 22px;border-bottom:1px solid var(--line);
background:color-mix(in srgb,var(--clay) 5%,var(--bg));animation:slidedown .3s ease}
#wsbar.on{display:flex}
@keyframes slidedown{from{opacity:0;transform:translateY(-8px)}to{opacity:1}}
.wslab{font-size:10.5px;font-weight:700;letter-spacing:1.4px;color:var(--clay)}
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
  <button class=newchat id=new>✚&nbsp; New chat</button>
  <div class=label>Chats</div>
  <div id=chats></div>
  <div class=foot>private · $0/query<br>sandboxed on AION<br>localhost only</div>
</aside>
<main>
<header>
  <span id=title>New chat</span><span class=spacer></span>
  <span id=core><span class="dot off"></span>…</span>
  <select id=model title="quick switch"></select>
  <button class=mbtn id=mopen>⚙ Models</button>
  <button class=mbtn id=sopen>☰ Settings</button>
  <button class=mbtn id=copen>⌥ Code</button>
  <span id=clk title="local time"></span>
</header>
<div id=wsbar>
  <span class=wslab>⌥ CODE MODE</span>
  <input id=wspath placeholder="workspace folder, e.g. ~/Developer/my-app">
  <button class=mbtn id=wsset>set</button>
  <button class=mbtn id=wstree>📁 files</button>
  <span class=ds id=wscur></span>
</div>
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
<div class=overlay id=ss><div class=panel>
  <h2>☰ Settings</h2>
  <div class=sub>everything is stored locally · nothing leaves your machine</div>
  <div class=label style="padding-left:0">profile</div>
  <input id=sname class=sin placeholder="your name">
  <input id=srole class=sin placeholder="your role (fed to the model)">
  <input id=sprefs class=sin placeholder="answer style, e.g. short and direct">
  <div class=label style="padding-left:0;margin-top:10px">theme</div>
  <div style="display:flex;gap:8px">
    <button class="mbtn tbtn" data-t=dark>🌙 dark</button>
    <button class="mbtn tbtn" data-t=light>☀️ light</button>
    <button class="mbtn tbtn" data-t=cyber>🌆 cyberpunk</button>
  </div>
  <div class=label style="padding-left:0;margin-top:14px">memory</div>
  <textarea id=smem class=sin rows=3 placeholder="paste memories exported from ChatGPT/Claude — one per line — and click import"></textarea>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <button class=mbtn id=smemimp>⬆ import</button>
    <button class=mbtn id=smemexp>⬇ export mine</button>
    <button class=mbtn id=smemclr>🗑 clear facts</button>
    <button class=mbtn id=schatclr>🗑 clear all chats</button>
  </div>
  <div class=ds id=smemout style="padding:6px 2px"></div>
  <div class=label style="padding-left:0;margin-top:14px">help & workflows</div>
  <div id=shelp></div>
  <div class=ds style="padding:6px 2px">or just ask in chat: <b style="color:var(--clay);cursor:pointer" id=shask>"what are termind's limitations?"</b></div>
  <div style="text-align:right;margin-top:14px">
    <button class=mbtn id=ssave style="background:var(--clay);color:#fff;border:0">Save</button>
    <button class=mbtn id=sclose>Close</button>
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
function greet(){stream.innerHTML='<div class=greet><h1>How can I help?</h1>'+
'<div>your private local agent — it remembers you</div><div class=chips>'+
'<span class=chip>who am i?</span><span class=chip>/status</span>'+
'<span class=chip>create a new project: a python dice roller</span>'+
'<span class=chip>/think design a caching layer</span></div>'+
'<div style=margin-top:10px;font-size:12px>📎 attach an image — your local model can see it</div></div>'}
function renderChats(d){chatsEl.innerHTML='';
 d.chats.forEach(c=>{const e=document.createElement('div');
 e.className='chat-it'+(c.active?' active':'');
 e.innerHTML='<span class=tt></span><span class=del title="rename chat">✎</span><span class=del title="delete chat">✕</span>';
 e.querySelector('.tt').textContent=c.title;
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
async function chatOp(body){const d=await (await fetch('/api/chat',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 renderChats(d);renderMsgs(d.messages);if(body.op=='new')titleEl.textContent='New chat'}
async function loadChats(){const d=await (await fetch('/api/chats')).json();
 renderChats(d);renderMsgs(d.messages)}
async function state(){const s=await (await fetch('/api/state')).json();
 ver.textContent='v'+s.version;
 core.innerHTML='<span class="dot '+(s.live?'live':'off')+'"></span>'+(s.live?s.model:'offline');
document.getElementById('warn').style.display=s.live?'none':'block';
 sel.innerHTML='';s.models.forEach(m=>{const o=document.createElement('option');o.value=m;
 o.textContent=m;if(m.split(':')[0]==s.model.split(':')[0])o.selected=true;sel.appendChild(o)})}
sel.onchange=async()=>{const b=await (await fetch('/api/model',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({model:sel.value})})).json();
 add('bot',b.reply);state()}
document.getElementById('new').onclick=()=>chatOp({op:'new'});
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
 try{const r=await (await fetch('/api/send',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,image:img,image_name:imgName})})).json();
  img=null;imgName='';imgURL='';chip.style.display='none';file.value='';
  b.innerHTML=r.reply=='__EXIT__'?'<span class=think>session closed.</span>':fmt(r.reply||'(no output)');
  if(r.image){const im=document.createElement('img');im.src='data:image/png;base64,'+r.image;
   im.style.cssText='display:block;max-width:320px;border-radius:10px;margin-top:10px;background:repeating-conic-gradient(#444 0 25%,#555 0 50%) 0 0/16px 16px';
   b.appendChild(im)}}
 catch(e){b.innerHTML='<span class=think>error: '+e+'</span>'}
 busy=false;go.disabled=false;inp.focus();
 if(/^(applied|built|created|wrote|imported|removed|saved|switched|renamed|workspace set)/.test(r.reply||'')){
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
document.getElementById('sopen').onclick=async()=>{ss.classList.add('open');
 const h=await (await fetch('/api/help')).json();const sh=document.getElementById('shelp');
 sh.innerHTML='';Object.entries(h.topics).forEach(([k,v])=>{const d=document.createElement('div');
  d.className='htop';d.innerHTML='<b>'+k+'</b><div class=hb></div>';
  d.querySelector('.hb').textContent=v;
  d.onclick=()=>d.classList.toggle('open');sh.appendChild(d)})};
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
/* code mode */
const wsbar=document.getElementById('wsbar'),wsbox=document.getElementById('wstreebox'),
wspath=document.getElementById('wspath'),wscur=document.getElementById('wscur');
document.getElementById('copen').onclick=()=>{wsbar.classList.toggle('on');
 if(!wsbar.classList.contains('on'))wsbox.classList.remove('on');
 else refreshWs()};
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
document.getElementById('wsset').onclick=async()=>{const r=await (await fetch('/api/ws',
 {method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({op:'set',path:wspath.value||'.'})})).json();
 add('bot',r.reply);celebrate(document.querySelector('#stream .msg:last-child .body')||document.body);
 refreshWs()};
document.getElementById('wstree').onclick=()=>{wsbox.classList.toggle('on');
 if(wsbox.classList.contains('on'))refreshWs()};
wspath.addEventListener('keydown',e=>{if(e.key=='Enter')document.getElementById('wsset').click()});
state();loadChats();loadProfile();inp.focus();
</script></body></html>"""
