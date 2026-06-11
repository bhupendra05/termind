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
:root{--bg:#05010f;--panel:#0d0221;--cyan:#05d9e8;--pink:#ff2a6d;--purple:#a64dff;
--green:#3ef58b;--ink:#e7e9ff;--dim:#7b7da6}
*{box-sizing:border-box}
body{margin:0;font-family:'JetBrains Mono',ui-monospace,Menlo,monospace;background:
radial-gradient(1200px 600px at 70% -10%,#1a0930,transparent),var(--bg);color:var(--ink);
height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;border-bottom:1px solid #2a1750;
background:linear-gradient(90deg,rgba(255,42,109,.08),transparent)}
.logo{font-weight:800;letter-spacing:3px;background:linear-gradient(90deg,var(--cyan),var(--pink),
var(--purple));-webkit-background-clip:text;background-clip:text;color:transparent;font-size:20px}
.tag{color:var(--dim);font-size:12px}
.spacer{flex:1}
select{background:#160a2b;color:var(--cyan);border:1px solid #3a1f63;border-radius:8px;
padding:7px 10px;font-family:inherit;font-size:12px;cursor:pointer}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;
box-shadow:0 0 8px currentColor}
.live{color:var(--green)}.off{color:var(--pink)}
#log{flex:1;overflow-y:auto;padding:26px 0}
.wrap{max-width:820px;margin:0 auto;padding:0 22px}
.msg{display:flex;gap:12px;margin:16px 0;animation:fade .25s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
.av{width:30px;height:30px;border-radius:8px;flex:none;display:grid;place-items:center;
font-size:13px;font-weight:700}
.you .av{background:#241047;color:var(--cyan);border:1px solid var(--cyan)}
.bot .av{background:linear-gradient(135deg,var(--pink),var(--purple));color:#fff}
.body{padding-top:4px;white-space:pre-wrap;line-height:1.55;font-size:14px}
.bot .body{color:#dfe1ff}
.body code,pre{background:#160a2b;border:1px solid #2a1750;border-radius:8px}
pre{padding:12px;overflow-x:auto;border-left:2px solid var(--cyan)}
code{padding:2px 5px;border-radius:5px;font-size:13px}
.think{color:var(--pink)}
footer{border-top:1px solid #2a1750;padding:16px 22px;background:rgba(13,2,33,.6)}
.inbar{max-width:820px;margin:0 auto;display:flex;gap:10px;align-items:flex-end;
background:#120730;border:1px solid #3a1f63;border-radius:14px;padding:10px 12px}
textarea{flex:1;background:transparent;border:0;color:var(--ink);font-family:inherit;font-size:14px;
resize:none;outline:none;max-height:160px;line-height:1.5}
button.send{background:linear-gradient(135deg,var(--cyan),var(--purple));color:#05010f;border:0;
border-radius:10px;padding:9px 16px;font-weight:700;cursor:pointer;font-family:inherit}
button.send:disabled{opacity:.4;cursor:default}
.hint{max-width:820px;margin:8px auto 0;color:var(--dim);font-size:11px;text-align:center}
.chip{display:inline-block;border:1px solid #3a1f63;border-radius:20px;padding:3px 10px;margin:3px;
color:var(--cyan);font-size:12px;cursor:pointer}.chip:hover{background:#1a0d38}
</style></head><body>
<header>
  <span class=logo>▲ TERMIND</span><span class=tag id=ver>web</span>
  <span class=spacer></span>
  <span id=core class=tag><span class="dot off"></span>…</span>
  <select id=model title="active model"></select>
</header>
<div id=log><div class=wrap id=stream>
  <div class="msg bot"><div class=av>▲</div><div class=body>Local agent online. I share memory and your model with the terminal. Try the chips below, or just talk.
  <div style=margin-top:10px>
    <span class=chip>who am i?</span>
    <span class=chip>create a new project: a python dice roller</span>
    <span class=chip>/think design a caching layer for an agent OS</span>
    <span class=chip>/status</span>
  </div></div></div>
</div></div>
<footer>
  <div class=inbar>
    <textarea id=in rows=1 placeholder="message termind…  (Enter to send, Shift+Enter for newline)"></textarea>
    <button class=send id=go>Send</button>
  </div>
  <div class=hint>private · $0/query · sandboxed on AION · localhost only · shares brain with your terminal</div>
</footer>
<script>
const stream=document.getElementById('stream'),log=document.getElementById('log'),
inp=document.getElementById('in'),go=document.getElementById('go'),
sel=document.getElementById('model'),core=document.getElementById('core'),ver=document.getElementById('ver');
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(s){s=esc(s);s=s.replace(/```([\s\S]*?)```/g,(m,c)=>'<pre>'+c.trim()+'</pre>');
return s.replace(/`([^`]+)`/g,'<code>$1</code>')}
function add(who,txt){const m=document.createElement('div');m.className='msg '+(who=='you'?'you':'bot');
m.innerHTML='<div class=av>'+(who=='you'?'›':'▲')+'</div><div class=body>'+fmt(txt)+'</div>';
stream.appendChild(m);log.scrollTop=log.scrollHeight;return m.querySelector('.body')}
async function state(){const s=await (await fetch('/api/state')).json();
ver.textContent='v'+s.version+' web';core.innerHTML='<span class="dot '+(s.live?'live':'off')+'"></span>'+
(s.live?s.model+' · local':'offline brain');sel.innerHTML='';
s.models.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=m;
if(m.split(':')[0]==s.model.split(':')[0])o.selected=true;sel.appendChild(o)})}
sel.onchange=async()=>{const b=await (await fetch('/api/model',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({model:sel.value})})).json();
add('bot',b.reply);state()}
let busy=false;
async function send(t){if(busy||!t.trim())return;busy=true;go.disabled=true;add('you',t);
inp.value='';inp.style.height='auto';
const b=add('bot','');b.innerHTML='<span class=think>⠿ thinking…</span>';
try{const r=await (await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({text:t})})).json();
if(r.reply=='__EXIT__'){b.innerHTML='<span class=think>session closed — you can close this tab.</span>';}
else{b.innerHTML=fmt(r.reply||'(no output)')}}
catch(e){b.innerHTML='<span class=think>error: '+e+'</span>'}
busy=false;go.disabled=false;inp.focus();state();log.scrollTop=log.scrollHeight}
go.onclick=()=>send(inp.value);
inp.addEventListener('keydown',e=>{if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();send(inp.value)}});
inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=inp.scrollHeight+'px'});
document.addEventListener('click',e=>{if(e.target.classList.contains('chip'))send(e.target.textContent)});
state();inp.focus();
</script></body></html>"""
