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
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        if self.path == "/api/send":
            text = (req.get("text") or "").strip()
            if not text:
                return self._send(200, json.dumps({"reply": ""}))
            with self.session._lock:  # one model call at a time → consistent shared memory
                try:
                    # web has no stdin, so consent-gated actions auto-approve in the UI flow
                    out = self.session.handle_web(text)
                except SystemExit:
                    out = "__EXIT__"
                except Exception as e:
                    out = f"error: {e}"
            return self._send(200, json.dumps({"reply": _strip_ansi(out)}))
        if self.path == "/api/chat":
            s = self.session
            with s._lock:
                if req.get("op") == "new":
                    s.chat_new()
                elif req.get("op") == "open":
                    s.chat_open(str(req.get("id", "")))
            return self._send(200, json.dumps({"chats": s.chats_list(),
                                               "messages": s.history}))
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
--ink:#ececec;--dim:#9b9a96;--clay:#d97757;--clay2:#c4633f;--green:#7bbf7e}
*{box-sizing:border-box}
body{margin:0;font-family:ui-sans-serif,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink);height:100vh;display:flex;overflow:hidden}
/* ── sidebar ── */
aside{width:250px;background:var(--side);border-right:1px solid var(--line);display:flex;
flex-direction:column;padding:12px;gap:8px}
.brand{display:flex;align-items:center;gap:8px;padding:6px 8px 12px;font-weight:700;
letter-spacing:.5px;color:var(--clay);font-size:16px}
.brand small{color:var(--dim);font-weight:400}
.newchat{display:flex;align-items:center;gap:8px;background:transparent;color:var(--ink);
border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:13.5px;cursor:pointer;
font-family:inherit;transition:background .15s}
.newchat:hover{background:var(--card)}
.label{color:var(--dim);font-size:11px;padding:10px 8px 2px;text-transform:uppercase;letter-spacing:1px}
#chats{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:2px}
.chat-it{padding:8px 10px;border-radius:8px;font-size:13px;color:var(--ink);cursor:pointer;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border:1px solid transparent}
.chat-it:hover{background:var(--card)}
.chat-it.active{background:var(--card);border-color:var(--line)}
.foot{color:var(--dim);font-size:11px;padding:8px;border-top:1px solid var(--line);line-height:1.5}
/* ── main ── */
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--line)}
#title{font-size:14px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spacer{flex:1}
#core{font-size:12px;color:var(--dim)}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px}
.live{background:var(--green)}.off{background:var(--clay)}
select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;
padding:6px 10px;font-family:inherit;font-size:12.5px;cursor:pointer}
#log{flex:1;overflow-y:auto;padding:28px 0}
.wrap{max-width:760px;margin:0 auto;padding:0 24px}
.msg{margin:18px 0;animation:fade .2s ease;line-height:1.6;font-size:15px}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
.you{display:flex;justify-content:flex-end}
.you .body{background:var(--bubble);border-radius:16px;padding:10px 16px;max-width:85%;
white-space:pre-wrap}
.bot .body{white-space:pre-wrap;color:var(--ink)}
.bot .who{color:var(--clay);font-size:12.5px;font-weight:600;margin-bottom:4px}
pre{background:#1d1c1a;border:1px solid var(--line);border-radius:10px;padding:12px;
overflow-x:auto;font-size:13px;font-family:ui-monospace,Menlo,monospace}
code{background:#1d1c1a;padding:2px 6px;border-radius:6px;font-size:13px;
font-family:ui-monospace,Menlo,monospace}
.think{color:var(--dim);font-style:italic}
.greet{color:var(--dim);text-align:center;margin-top:14vh}
.greet h1{color:var(--ink);font-weight:600;font-size:26px;margin:0 0 6px}
.chips{margin-top:18px;display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.chip{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:8px 14px;
color:var(--ink);font-size:13px;cursor:pointer}.chip:hover{border-color:var(--clay)}
footer{padding:14px 20px 18px}
.inbar{max-width:760px;margin:0 auto;display:flex;gap:10px;align-items:flex-end;
background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px 14px;
transition:border-color .15s}
.inbar:focus-within{border-color:var(--clay)}
textarea{flex:1;background:transparent;border:0;color:var(--ink);font-family:inherit;
font-size:15px;resize:none;outline:none;max-height:180px;line-height:1.5}
button.send{background:var(--clay);color:#fff;border:0;border-radius:10px;width:36px;height:36px;
font-size:16px;cursor:pointer;flex:none}
button.send:hover{background:var(--clay2)}button.send:disabled{opacity:.4;cursor:default}
.hint{max-width:760px;margin:8px auto 0;color:var(--dim);font-size:11px;text-align:center}
@media(max-width:720px){aside{display:none}}
</style></head><body>
<aside>
  <div class=brand>▲ termind <small id=ver></small></div>
  <button class=newchat id=new>✚&nbsp; New chat</button>
  <div class=label>Chats</div>
  <div id=chats></div>
  <div class=foot>private · $0/query<br>sandboxed on AION<br>localhost only</div>
</aside>
<main>
<header>
  <span id=title>New chat</span><span class=spacer></span>
  <span id=core><span class="dot off"></span>…</span>
  <select id=model title="model"></select>
</header>
<div id=log><div class=wrap id=stream></div></div>
<footer>
  <div class=inbar>
    <textarea id=in rows=1 placeholder="Message termind…"></textarea>
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
'<span class=chip>/think design a caching layer</span></div></div>'}
function renderChats(d){chatsEl.innerHTML='';
 d.chats.forEach(c=>{const e=document.createElement('div');
 e.className='chat-it'+(c.active?' active':'');e.textContent=c.title;
 if(c.active)titleEl.textContent=c.title;
 e.onclick=()=>chatOp({op:'open',id:c.id});chatsEl.appendChild(e)});}
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
 sel.innerHTML='';s.models.forEach(m=>{const o=document.createElement('option');o.value=m;
 o.textContent=m;if(m.split(':')[0]==s.model.split(':')[0])o.selected=true;sel.appendChild(o)})}
sel.onchange=async()=>{const b=await (await fetch('/api/model',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({model:sel.value})})).json();
 add('bot',b.reply);state()}
document.getElementById('new').onclick=()=>chatOp({op:'new'});
let busy=false;
async function send(t){if(busy||!t.trim())return;busy=true;go.disabled=true;add('you',t);
 inp.value='';inp.style.height='auto';
 const b=add('bot','');b.innerHTML='<span class=think>thinking…</span>';
 try{const r=await (await fetch('/api/send',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})).json();
  b.innerHTML=r.reply=='__EXIT__'?'<span class=think>session closed.</span>':fmt(r.reply||'(no output)');}
 catch(e){b.innerHTML='<span class=think>error: '+e+'</span>'}
 busy=false;go.disabled=false;inp.focus();
 const d=await (await fetch('/api/chats')).json();renderChats(d);
 log.scrollTop=log.scrollHeight}
go.onclick=()=>send(inp.value);
inp.addEventListener('keydown',e=>{if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();send(inp.value)}});
inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=inp.scrollHeight+'px'});
document.addEventListener('click',e=>{if(e.target.classList.contains('chip'))send(e.target.textContent)});
state();loadChats();inp.focus();
</script></body></html>"""
