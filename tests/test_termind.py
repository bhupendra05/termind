"""termind tests — REPL commands, real retrieval, and the sandbox/budget guarantees."""
import json

import pytest

from aion import Kernel
from termind.indexer import chunk_text, index_folder
from termind.repl import FEATURES, Session, _ask_agent
from aion import Capabilities


@pytest.fixture
def docs(tmp_path):
    (tmp_path / "notes.md").write_text(
        "# Standup\n\nShipped the budget kernel today.\n\n## Action items\n\n"
        "Email the investor list and review the RAG pipeline.")
    (tmp_path / "arch.md").write_text(
        "The system runs on AION. Nothing leaves the machine, so it is private.")
    return str(tmp_path)


def _session():
    return Session(live=False)  # offline brain — retrieval is still real


def test_index_and_ask_cites_source(docs):
    s = _session()
    out = s.do_index(docs)
    assert "2 files" in out
    ans = s.handle("/ask what are the action items?")
    assert ans.startswith("From your notes (") and "investor" in ans.lower()


def test_ask_no_match(docs):
    s = _session()
    s.do_index(docs)
    assert "Nothing relevant" in s.handle("/ask quantum chromodynamics")


def test_unknown_command_and_help():
    s = _session()
    assert "unknown command" in s.handle("/nope")
    assert s.handle("/help") == FEATURES


def test_status_reports_audit(docs):
    s = _session()
    s.do_index(docs)
    s.handle("/ask action items")
    st = s.handle("/status")
    assert "credits spent" in st and "0 bytes" in st


def test_ask_agent_is_sandboxed():
    """A rogue brain tries fs.write from inside /ask — the kernel must deny it."""
    k = Kernel()

    def rogue(msgs):
        n = sum(1 for m in msgs if m["role"] == "assistant")
        if n == 0:
            return json.dumps({"tool": "x", "args": {}})  # ignored shape; then try escape via search args
        return json.dumps({"final": "done"})

    def escape(sb, q, think):
        res = sb.syscall("fs.write", path="/pwn", data="x")
        return res

    pid = k.spawn("rogue", fn=escape, args=("q", rogue),
                  caps=Capabilities(["mem.search", "mem.get"]), budget=5.0)
    k.run()
    out = k.processes[pid].result
    assert out["ok"] is False and "permission denied" in out["error"]


def test_chunking_never_empty():
    assert chunk_text("") == []
    assert chunk_text("one\n\n\n\ntwo", size=3)


def test_index_folder_skips_binaries(tmp_path):
    (tmp_path / "a.md").write_text("hello world")
    (tmp_path / "b.png").write_bytes(b"\x89PNG")
    assert {e["source"] for e in index_folder(str(tmp_path))} == {"a.md"}


def test_server_up_but_no_model_is_not_live(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "ollama_available", lambda: True)
    monkeypatch.setattr(r, "model_available", lambda n=None: False)
    s = r.Session()
    assert s.server is True and s.live is False        # must NOT pretend to be online
    assert "model missing" in s.do_status()


def test_server_and_model_present_is_live(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "ollama_available", lambda: True)
    monkeypatch.setattr(r, "model_available", lambda n=None: True)
    assert r.Session().live is True


def test_memory_survives_restart():
    s1 = _session()
    s1.handle("/remember my name is Bhupendra and I build agent infrastructure")
    s2 = _session()                                    # simulates closing + reopening
    assert len(s2.store["facts"]) == 1
    assert "Bhupendra" in s2.handle("/recall who am I")


def test_indexed_docs_survive_restart(docs):
    s1 = _session()
    s1.do_index(docs)
    s2 = _session()
    assert s2.chunks > 0
    assert "investor" in s2.handle("/ask what are the action items?").lower()


def test_facts_injected_into_chat_system_prompt():
    s = _session()
    s.handle("/remember my name is Bhupendra")
    msgs = s.chat_messages("hello")
    assert "Bhupendra" in msgs[0]["content"] and msgs[0]["role"] == "system"


def test_auto_memory_learns_from_chat():
    s = _session()
    s.handle("i am Bhupendra and I build agent infrastructure")
    assert any("Bhupendra" in f for f in s.store["facts"])     # learned without /remember


def test_chat_history_survives_restart():
    s1 = _session()
    s1.handle("hello there")
    s2 = _session()
    assert len(s2.history) >= 2                                 # conversation restored


def test_think_works_offline():
    out = _session().handle("/think why is the sky blue?")
    assert isinstance(out, str) and out                          # bottom rung never crashes


def test_do_requires_live_model():
    assert "needs a live model" in _session().handle("/do list my files")


def test_do_executes_only_on_yes(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: '{"cmd": "echo termind-ok", "why": "test"}')
    s = r.Session(live=True)
    assert "termind-ok" in s.do_action("say ok", confirm=lambda _: "y")     # ran with consent
    assert "aborted" in s.do_action("say ok", confirm=lambda _: "n")        # refused without


def test_bare_exit_actually_quits():
    import pytest as _pt
    for w in ("exit", "quit", "bye", "EXIT", "exit."):
        with _pt.raises(SystemExit):
            _session().handle(w)


def test_questions_are_not_auto_remembered():
    s = _session()
    s.handle("which model should i use for coding?")     # a question, not a self-fact
    s.handle("can i build this with python?")
    assert s.store["facts"] == []


def test_system_prompt_separates_agent_from_user():
    s = _session()
    s.handle("/remember my name is Bhupendra")
    sys_msg = s.chat_messages("who am i?")[0]["content"]
    assert "USER" in sys_msg and "Never confuse yourself" in sys_msg


def test_mkdir_creates_folder(tmp_path):
    s = _session()
    out = s.do_mkdir(str(tmp_path / "new" / "nested"))
    assert "created folder" in out and (tmp_path / "new" / "nested").is_dir()


def test_write_generates_file_with_consent(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "```python\nprint('hi')\n```")
    s = r.Session(live=True)
    f = str(tmp_path / "hello.py")
    out = s.do_write(f, "print hi", confirm=lambda _: "y")
    assert "wrote" in out and open(f).read() == "print('hi')\n"   # fences stripped
    assert "aborted" in s.do_write(f, "print hi", confirm=lambda _: "n")


def test_build_scaffolds_project(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    plan = _json.dumps({"folder": str(tmp_path / "demo-app"),
                        "files": {"README.md": "# demo", "main.py": "print('go')"}})
    monkeypatch.setattr(r, "chat", lambda *a, **k: plan)
    s = r.Session(live=True)
    out = s.do_build("a demo app", confirm=lambda _: "y", open_editor=False)
    assert "built" in out
    assert (tmp_path / "demo-app" / "main.py").read_text().strip() == "print('go')"


def test_write_and_build_need_live_model():
    s = _session()
    assert "needs a live model" in s.do_write("x.py", "anything")
    assert "needs a live model" in s.do_build("anything")


def test_natural_language_creates_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _session()
    out = s.handle("create a folder called demo-zone")     # no slash, offline
    assert "created folder" in out and (tmp_path / "demo-zone").is_dir()


def test_natural_language_opens_vscode(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r.Session, "do_code", lambda self, p=".": f"CODE:{p}")
    assert _session().handle("open vs code").startswith("CODE:")


def test_natural_language_builds_project(monkeypatch):
    import json as _json
    import termind.repl as r
    monkeypatch.setattr(r, "chat",
                        lambda *a, **k: _json.dumps({"intent": "build_project"}))
    monkeypatch.setattr(r.Session, "do_build",
                        lambda self, idea, confirm=None, open_editor=True: f"BUILD:{idea}")
    s = r.Session(live=True)
    assert s.handle("create a new tool that tracks my expenses").startswith("BUILD:")


def test_plain_questions_still_chat():
    out = _session().handle("what is the capital of france?")   # no action words
    assert "offline brain" in out                                # routed to chat, not actions


def test_codegen_self_heals_broken_python(tmp_path, monkeypatch):
    import termind.repl as r
    replies = iter(['print("hi"',                       # broken (missing paren)
                    'print("hi")'])                     # the model's fix
    monkeypatch.setattr(r, "chat", lambda *a, **k: next(replies))
    s = r.Session(live=True)
    f = str(tmp_path / "ok.py")
    out = s.do_write(f, "print hi", confirm=lambda _: "y")
    assert "wrote" in out and open(f).read() == 'print("hi")\n'   # healed before writing


def test_py_error_detects_and_passes():
    from termind.repl import Session
    assert Session._py_error("x.py", "def broken(:") is not None
    assert Session._py_error("x.py", "a = 1") is None
    assert Session._py_error("notes.md", "anything (((") is None  # only checks .py


def test_folder_name_phrasing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _session()
    out = s.handle("can you create me a folder name termind1folder")
    assert "created folder" in out and (tmp_path / "termind1folder").is_dir()


def test_dry_run_swaps_python_for_python3(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r.shutil, "which",
                        lambda b: "/usr/bin/python3" if b == "python3" else None)
    assert r.Session._dry_run("python timer.py") == "python3 timer.py"
    assert r.Session._dry_run("ghostbin --x") == ""        # missing binary → skip, no crash


def test_dry_run_passes_existing_binaries():
    from termind.repl import Session
    assert Session._dry_run("echo hi").startswith("echo")


def test_thinking_spinner_is_safe_without_tty(capsys):
    from termind.repl import Thinking
    with Thinking("test"):
        pass                                            # piped: must not animate or crash
    assert capsys.readouterr().out == ""                # zero garbage when not a TTY


def test_model_switch_persists(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "model_available", lambda n=None: True)
    s = _session()
    out = s.handle("/model qwen2.5")
    assert "switched to qwen2.5" in out
    s2 = _session()
    assert s2.model == "qwen2.5"                       # choice survives restarts


def test_model_not_pulled_suggests_pull(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "model_available", lambda n=None: False)
    assert "/pull ghostmodel" in _session().handle("/model ghostmodel")


def test_model_list_shows_installed(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "list_models", lambda: ["gemma3:latest", "llama3.2:latest"])
    out = _session().handle("/model")
    assert "gemma3:latest" in out and "llama3.2:latest" in out and "active:" in out


def test_context_is_capped_for_speed():
    s = _session()
    s.history = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    msgs = s.chat_messages("latest")
    assert len(msgs) <= 10                              # system + last 8 + new message


def test_preferences_enforced_in_system_prompt():
    s = _session()
    assert "preferences" in s.chat_messages("hi")[0]["content"].lower()


def test_web_strip_ansi():
    from termind.web import _strip_ansi
    assert _strip_ansi("\033[36mhi\033[0m there") == "hi there"


def test_handle_web_chat_returns_plain_text():
    s = _session()
    out = s.handle_web("hello")
    assert isinstance(out, str) and "\033[" not in out      # web text has no panels/ansi from chat


def test_web_server_state_and_send(monkeypatch):
    import json, urllib.request
    import termind.repl as r
    from termind.web import serve
    monkeypatch.setattr(r, "model_available", lambda n=None: True)
    monkeypatch.setattr(r, "chat", lambda *a, **k: "hello from the model")
    s = r.Session(live=True)
    httpd, url = serve(s, port=8799, open_browser=False)
    import threading
    threading.Thread(target=httpd.handle_request, daemon=True).start()  # state
    st = json.loads(urllib.request.urlopen(url + "/api/state", timeout=3).read())
    assert "model" in st and "live" in st and "version" in st
    threading.Thread(target=httpd.handle_request, daemon=True).start()  # send
    req = urllib.request.Request(url + "/api/send", data=json.dumps({"text": "hi"}).encode(),
                                 headers={"Content-Type": "application/json"})
    rep = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert "hello from the model" in rep["reply"]
    httpd.server_close()


def test_chat_sessions_new_open_and_persist():
    s = _session()
    s.handle("hello first chat")                       # auto-creates + titles a chat
    first = s.chats_list()[0]
    assert first["title"].startswith("hello first")
    s.handle("/chat new")
    s.handle("now a second chat")
    assert len(s.chats_list()) == 2
    out = s.handle("/chat 2")                          # open the older one by index
    assert "resumed" in out and any("first chat" in m["content"] for m in s.history)
    s2 = _session()                                    # restart: active chat restored
    assert len(s2.chats_list()) == 2 and s2.history


def test_web_chats_api(monkeypatch):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    s.handle("remember this conversation")
    httpd, url = serve(s, port=8801, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    d = json.loads(urllib.request.urlopen(url + "/api/chats", timeout=3).read())
    assert d["chats"] and d["messages"]
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/chat", data=json.dumps({"op": "new"}).encode(),
                                 headers={"Content-Type": "application/json"})
    d2 = json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert d2["messages"] == [] and len(d2["chats"]) == 2
    httpd.server_close()


def test_vision_sends_image_to_model(monkeypatch):
    import termind.repl as r
    seen = {}
    def fake_chat(msgs, **k):
        seen["images"] = msgs[-1].get("images")
        return "a neon terminal screenshot"
    monkeypatch.setattr(r, "chat", fake_chat)
    s = r.Session(live=True)
    out = s.do_vision("what is this?", "QkFTRTY0", "shot.png")
    assert out == "a neon terminal screenshot"
    assert seen["images"] == ["QkFTRTY0"]                      # Ollama multimodal format
    assert any("[sent image: shot.png]" in m["content"] for m in s.history)


def test_vision_needs_live_model():
    assert "needs a live model" in _session().do_vision("x", "QkFTRTY0")


def test_edit_requires_image_first():
    assert "no image yet" in _session().handle("/edit grayscale")


def test_edit_grayscale_and_resize(tmp_path, monkeypatch):
    PIL = pytest.importorskip("PIL")
    import base64, io
    from PIL import Image
    monkeypatch.chdir(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (100, 60), (255, 0, 0)).save(buf, "PNG")
    s = _session()
    s.last_image = ("red.png", base64.b64encode(buf.getvalue()).decode())
    out = s.handle("/edit resize 50%")
    assert "saved:" in out and (tmp_path / "red_edited.png").exists()
    from PIL import Image as I2
    assert I2.open(tmp_path / "red_edited.png").size == (50, 30)
    assert "saved:" in s.handle("/edit grayscale")             # chains on the edited image


def test_web_send_image(monkeypatch):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    monkeypatch.setattr(r, "chat", lambda msgs, **k: "I see a red square")
    s = r.Session(live=True)
    httpd, url = serve(s, port=8803, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/send",
        data=json.dumps({"text": "", "image": "QkFTRTY0", "image_name": "sq.png"}).encode(),
        headers={"Content-Type": "application/json"})
    rep = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert "red square" in rep["reply"]
    httpd.server_close()


def _png_b64(color=(255, 0, 0), size=(80, 60)):
    import base64, io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_edit_multi_op_natural_language_offline(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.chdir(tmp_path)
    s = _session()
    s.last_image = ("x.png", _png_b64())
    out = s.handle("/edit make it brighter and grayscale, then flip")  # offline keyword plan
    assert "applied" in out and "brightness" in out and "grayscale" in out and "flip" in out
    assert (tmp_path / "x_edited.png").exists()


def test_edit_plan_via_model(monkeypatch, tmp_path):
    pytest.importorskip("PIL")
    import json as _json
    import termind.repl as r
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"ops": [{"op": "sepia", "val": None}, {"op": "rotate", "val": 180}]}))
    s = r.Session(live=True)
    s.last_image = ("y.png", _png_b64((0, 0, 255)))
    out = s.do_edit("give it an old warm vintage look upside down")
    assert "sepia" in out and "rotate 180" in out


def test_edit_ops_brightness_and_crop():
    pytest.importorskip("PIL")
    import base64, io
    from PIL import Image
    from termind.repl import Session
    img = Image.new("RGB", (100, 40), (10, 10, 10))
    out = Session._apply_edit(img, "brightness", 300)
    assert out.getpixel((0, 0))[0] > 10                      # brighter
    sq = Session._apply_edit(img, "crop", None)
    assert sq.size == (40, 40)                               # square crop


def test_edit_remove_background_live(tmp_path, monkeypatch):
    pytest.importorskip("rembg")
    monkeypatch.chdir(tmp_path)
    s = _session()
    s.last_image = ("circ.png", _png_b64((255, 0, 0), (64, 64)))
    out = s.handle("/edit remove background")
    assert "applied rembg" in out and (tmp_path / "circ_edited.png").exists()


def test_attached_image_with_edit_request_edits_not_describes(tmp_path, monkeypatch):
    """THE screenshot bug: image + 'remove background' must EDIT, not describe."""
    pytest.importorskip("rembg")
    monkeypatch.chdir(tmp_path)
    s = _session()
    out = s.handle_web("remove background", image=_png_b64((0, 200, 0), (48, 48)),
                       image_name="logo.png")
    assert out.startswith("applied rembg") and (tmp_path / "logo_edited.png").exists()


def test_edit_phrase_after_image_routes_to_edit(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.chdir(tmp_path)
    s = _session()
    s.last_image = ("p.png", _png_b64())
    out = s.handle("make it black and white")          # plain chat phrasing, no slash
    assert out.startswith("applied") and "grayscale" in out


def test_capability_awareness_in_system_prompt():
    sysmsg = _session().chat_messages("can you edit images?")[0]["content"]
    assert "NOT text-only" in sysmsg and "EDITS images" in sysmsg


def test_attached_image_with_question_still_describes(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "a green square")
    s = r.Session(live=True)
    out = s.handle_web("what is in this image?", image=_png_b64((0, 200, 0)),
                       image_name="sq.png")
    assert out == "a green square"                     # questions still go to vision


def test_object_removal_parsing():
    s = _session()
    assert s._parse_edit_ops("remove the gemini logo from my image") == \
        [("removeobj", "gemini logo")]
    assert s._parse_edit_ops("erase the watermark") == [("removeobj", "watermark")]
    assert s._parse_edit_ops("remove background")[0][0] == "rembg"   # bg still → rembg


def test_edit_hint_matches_object_removal():
    from termind.repl import EDIT_HINT
    assert EDIT_HINT.search("remove the gemini logo from my image")
    assert EDIT_HINT.search("erase the text in the corner")


def test_object_removal_inpaints_only_that_region(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    import base64, io, json as _json
    import termind.repl as r
    from PIL import Image, ImageDraw
    monkeypatch.chdir(tmp_path)
    img = Image.new("RGB", (100, 100), (200, 30, 30))           # red field…
    ImageDraw.Draw(img).rectangle([35, 35, 65, 65], fill=(0, 0, 0))  # …with a black "logo"
    buf = io.BytesIO(); img.save(buf, "PNG")
    def vlm(msgs, **k):                                          # dispatch like a real VLM
        sysmsg = msgs[0]["content"]
        user = msgs[-1]["content"]
        if "contain" in user:                                    # quadrant descent: center obj
            return _json.dumps({"present": True})                # in all quads → falls through
        if "still visible" in user:
            return _json.dumps({"present": False})
        if "present" in sysmsg:
            return _json.dumps({"present": True})
        return _json.dumps({"found": True, "x1": 35, "y1": 35, "x2": 65, "y2": 65})
    monkeypatch.setattr(r, "chat", vlm)
    s = r.Session(live=True)
    s.last_image = ("ad.png", base64.b64encode(buf.getvalue()).decode())
    out = s.do_edit("remove the black logo")
    assert "removed 'black logo'" in out
    from PIL import Image as I2
    res = I2.open(tmp_path / "ad_edited.png").convert("RGB")
    cx = res.getpixel((50, 50))
    assert cx[0] > 120 and cx[1] < 90                            # center is red-ish, not black
    assert res.getpixel((5, 5))[0] > 150                         # corners untouched


def test_object_removal_not_found(monkeypatch, tmp_path):
    pytest.importorskip("cv2")
    import json as _json
    import termind.repl as r
    monkeypatch.chdir(tmp_path)
    def vlm(msgs, **k):
        sysmsg = msgs[0]["content"]
        if "present" in sysmsg:
            return _json.dumps({"present": False})
        return _json.dumps({"found": False})
    monkeypatch.setattr(r, "chat", vlm)
    s = r.Session(live=True)
    s.last_image = ("x.png", _png_b64())
    assert "couldn't locate" in s.do_edit("remove the unicorn")


def test_quadrant_descent_when_bbox_lies(tmp_path, monkeypatch):
    """Direct bbox is WRONG → quadrant yes/no search finds the true region."""
    pytest.importorskip("cv2")
    import base64, io, json as _json
    import termind.repl as r
    from PIL import Image, ImageDraw
    monkeypatch.chdir(tmp_path)
    img = Image.new("RGB", (200, 200), (200, 30, 30))
    ImageDraw.Draw(img).rectangle([150, 0, 200, 50], fill=(0, 0, 0))  # logo in D1 (top-right)
    buf = io.BytesIO(); img.save(buf, "PNG")
    import re as _re
    def vlm(msgs, **k):
        user = msgs[-1]["content"]
        if "contain" in user:        # answer truthfully based on the crop's actual pixels
            import io as _io
            from PIL import Image as _I
            crop = _I.open(_io.BytesIO(base64.b64decode(msgs[-1]["images"][0]))).convert("RGB")
            dark = any(sum(crop.getpixel((x, y))) < 90
                       for x in range(0, crop.width, 7) for y in range(0, crop.height, 7))
            return _json.dumps({"present": dark})
        if "still visible" in user:
            return _json.dumps({"present": False})
        if "present" in msgs[0]["content"]:
            return _json.dumps({"present": True})
        return _json.dumps({"found": True, "x1": 5, "y1": 60, "x2": 25, "y2": 90})  # wrong box
    monkeypatch.setattr(r, "chat", vlm)
    s = r.Session(live=True)
    s.last_image = ("p.png", base64.b64encode(buf.getvalue()).decode())
    out = s.do_edit("remove the black logo")
    assert "removed" in out
    from PIL import Image as I2
    res = I2.open(tmp_path / "p_edited.png").convert("RGB")
    assert res.getpixel((180, 20))[0] > 100              # logo region reconstructed (red-ish)


def test_user_position_words_win(tmp_path, monkeypatch):
    """'in the top right corner' must be used directly — zero VLM geometry involved."""
    pytest.importorskip("cv2")
    import base64, io, json as _json
    import termind.repl as r
    from PIL import Image, ImageDraw
    monkeypatch.chdir(tmp_path)
    img = Image.new("RGB", (200, 200), (200, 30, 30))
    ImageDraw.Draw(img).rectangle([150, 0, 200, 50], fill=(0, 0, 0))
    buf = io.BytesIO(); img.save(buf, "PNG")
    monkeypatch.setattr(r, "chat", lambda msgs, **k: _json.dumps({"present": False}))
    s = r.Session(live=True)
    s.last_image = ("p.png", base64.b64encode(buf.getvalue()).decode())
    out = s.do_edit("remove the logo in the top right corner")
    assert "removed" in out
    from PIL import Image as I2
    assert I2.open(tmp_path / "p_edited.png").convert("RGB").getpixel((180, 20))[0] > 100


def test_position_followup_resumes_failed_removal(tmp_path, monkeypatch):
    """THE screenshot flow: locate fails → user replies 'right bottom corner .' → erased."""
    pytest.importorskip("cv2")
    import base64, io, json as _json
    import termind.repl as r
    from PIL import Image, ImageDraw
    monkeypatch.chdir(tmp_path)
    img = Image.new("RGB", (200, 200), (200, 30, 30))
    ImageDraw.Draw(img).rectangle([150, 150, 200, 200], fill=(0, 0, 0))  # logo bottom-right
    buf = io.BytesIO(); img.save(buf, "PNG")
    def vlm(msgs, **k):                                  # locate always fails / nothing visible
        return _json.dumps({"present": False, "found": False})
    monkeypatch.setattr(r, "chat", vlm)
    s = r.Session(live=True)
    s.last_image = ("ad.png", base64.b64encode(buf.getvalue()).decode())
    out1 = s.handle_web("remove the gemini logo from my image")
    assert "couldn't locate" in out1 and s._pending_remove == "gemini logo"
    out2 = s.handle_web("right bottom corner .")
    assert "removed" in out2 and s._pending_remove is None
    from PIL import Image as I2
    assert I2.open(tmp_path / "ad_edited.png").convert("RGB").getpixel((180, 180))[0] > 100


def test_send_me_image_returns_real_image():
    s = _session()
    s.last_image = ("pic.png", _png_b64())
    out = s.handle_web("then send me image")
    assert out.startswith("here's the current image (pic.png)")


def test_no_fabricated_actions_in_system_prompt():
    assert "NEVER claim you performed an action" in _session().chat_messages("x")[0]["content"]
