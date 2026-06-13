"""termind tests — REPL commands, real retrieval, and the sandbox/budget guarantees."""
import json

import pytest

from aion import Kernel
from termind.indexer import chunk_text, index_folder
from termind.repl import FEATURES, Session, _ask_agent
from aion import Capabilities


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "docs"            # own folder so TERMIND_HOME/memory.json isn't indexed
    d.mkdir()
    (d / "notes.md").write_text(
        "# Standup\n\nShipped the budget kernel today.\n\n## Action items\n\n"
        "Email the investor list and review the RAG pipeline.")
    (d / "arch.md").write_text(
        "The system runs on AION. Nothing leaves the machine, so it is private.")
    return str(d)


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


def test_lama_crop_window_math():
    from termind.inpaint import crop_window
    l, t, r, b = crop_window(2000, 1000, (1500, 100, 1700, 300))   # small box, big image
    assert r - l == b - t                                          # square
    assert l <= 1500 and r >= 1700 and t <= 100 and b >= 300       # contains the hole
    assert 0 <= l and r <= 2000 and 0 <= t and b <= 1000           # inside the image
    l, t, r, b = crop_window(400, 300, (10, 10, 390, 290))         # huge box, small image
    assert 0 <= l and r <= 400 and 0 <= t and b <= 300


def test_removal_falls_back_to_cv2_without_lama(tmp_path, monkeypatch):
    """No LaMa model in the test home → classical inpaint still works (no prompt hang)."""
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
    s._confirm = lambda _p="": "n"                                  # decline the download
    s.last_image = ("p.png", base64.b64encode(buf.getvalue()).decode())
    out = s.do_edit("remove the logo in the top right corner")
    assert "removed" in out


def test_lama_inpaint_live(tmp_path, monkeypatch):
    """Runs only when the real LaMa model is downloaded (~/.termind/models)."""
    import os
    home = os.path.expanduser("~/.termind")
    if not os.path.isfile(os.path.join(home, "models", "lama_fp32.onnx")):
        pytest.skip("LaMa model not downloaded")
    monkeypatch.setenv("TERMIND_HOME", home)
    import importlib
    import termind.inpaint as ip
    importlib.reload(ip)
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (640, 480), (90, 140, 90))
    d = ImageDraw.Draw(img)
    for y in range(0, 480, 16):                       # textured background
        d.line([(0, y), (640, y + 30)], fill=(70 + y % 60, 120, 80), width=3)
    d.rectangle([260, 180, 380, 300], fill=(0, 0, 0))  # the "object"
    out = ip.inpaint_bbox(img, (40.6, 37.5, 59.4, 62.5))
    px = out.getpixel((320, 240))
    assert sum(px) > 150                               # hole reconstructed, not black
    assert out.size == img.size


def test_chat_delete_and_terminal_command():
    s = _session()
    s.handle("first conversation here")
    s.handle("/chat new"); s.handle("second conversation here")
    assert len(s.chats_list()) == 2
    out = s.handle("/chat delete 2")                    # delete the older one
    assert "deleted" in out and len(s.chats_list()) == 1
    s.handle("/chat delete 1")                          # delete the ACTIVE one
    assert s.chats_list() == [] and s.history == []
    s2 = _session()
    assert s2.chats_list() == []                        # deletion persisted


def test_model_catalog_flags_installed(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "list_models", lambda: ["gemma3:latest", "moondream:latest"])
    cat = _session().model_catalog()
    flags = {c["name"]: c["installed"] for c in cat["catalog"]}
    assert flags["gemma3"] and flags["moondream"] and not flags["qwen2.5"]
    assert cat["pull"]["status"] == "idle"


def test_start_pull_state_machine(monkeypatch):
    import time as _t
    import termind.repl as r
    monkeypatch.setattr(r, "pull_stream",
                        lambda name, cb: (cb(50, "downloading"), cb(100, "success")))
    s = r.Session(live=True)
    s.server = True
    out = s.start_pull("qwen2.5")
    assert "background" in out
    for _ in range(50):
        if s.pull["status"] == "done":
            break
        _t.sleep(0.05)
    assert s.pull == {"status": "done", "name": "qwen2.5", "pct": 100}
    s.pull = {"status": "pulling", "name": "x"}
    assert "one at a time" in s.start_pull("y")


def test_web_chat_delete_and_catalog(monkeypatch):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    monkeypatch.setattr(r, "list_models", lambda: ["gemma3:latest"])
    s = r.Session(live=False)
    s.handle("a chat to delete")
    cid = s.chats_list()[0]["id"]
    httpd, url = serve(s, port=8807, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/chat",
        data=json.dumps({"op": "delete", "id": cid}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert d["chats"] == []
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    c = json.loads(urllib.request.urlopen(url + "/api/catalog", timeout=3).read())
    assert any(x["name"] == "qwen2.5" for x in c["catalog"]) and "pull" in c
    httpd.server_close()


def test_import_rejects_non_gguf():
    out = _session().import_model("/tmp/not-a-model.txt")
    assert ".gguf" in out


def test_import_own_gguf_registers_with_ollama(tmp_path, monkeypatch):
    import time as _t
    import termind.repl as r
    gguf = tmp_path / "My Fine_Tune V2.gguf"
    gguf.write_bytes(b"GGUF fake")
    calls = {}
    monkeypatch.setattr(r.shutil, "which", lambda b: "/usr/local/bin/ollama")
    def fake_run(cmd, **k):
        calls["cmd"] = cmd
        class R: returncode = 0; stderr = ""
        return R()
    monkeypatch.setattr(r.subprocess, "run", fake_run)
    s = _session()
    out = s.import_model(str(gguf))
    assert "importing your model as 'my-fine-tune-v2'" in out      # name sanitized
    for _ in range(50):
        if s.pull["status"] == "done":
            break
        _t.sleep(0.05)
    assert s.pull["status"] == "done"
    assert calls["cmd"][:3] == ["ollama", "create", "my-fine-tune-v2"]


def test_add_model_routes_gguf_vs_pull(tmp_path, monkeypatch):
    import termind.repl as r
    s = _session()
    monkeypatch.setattr(r.Session, "import_model",
                        lambda self, p, n=None: f"IMPORT:{p}")
    monkeypatch.setattr(r.Session, "start_pull", lambda self, n: f"PULL:{n}")
    assert s.add_model("~/models/x.gguf").startswith("IMPORT:")
    assert s.add_model("hf.co/xyz/their-model") == "PULL:hf.co/xyz/their-model"
    assert "usage" in s.add_model("")


def test_terminal_import_command(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r.Session, "import_model",
                        lambda self, p, n=None: f"IMPORT:{p}|{n}")
    s = _session()
    assert s.handle("/import ~/m.gguf custom-name") == "IMPORT:~/m.gguf|custom-name"
    assert "usage" in s.handle("/import")


def test_web_import_endpoint(monkeypatch):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    monkeypatch.setattr(r.Session, "add_model",
                        lambda self, spec, name=None: f"ADDED:{spec}")
    s = r.Session(live=False)
    httpd, url = serve(s, port=8809, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/import",
        data=json.dumps({"spec": "hf.co/xyz/their-model"}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert d["reply"] == "ADDED:hf.co/xyz/their-model"
    httpd.server_close()


def test_profile_set_persists_and_feeds_prompt():
    s = _session()
    s.set_profile(name="XYZ", role="ml engineer", prefs="short answers", theme="cyber")
    s2 = _session()                                       # restart
    p = s2.profile()
    assert p["name"] == "XYZ" and p["theme"] == "cyber" and p["onboarded"]
    sysmsg = s2.chat_messages("hi")[0]["content"]
    assert "XYZ" in sysmsg and "short answers" in sysmsg


def test_onboarding_flag_when_no_name():
    assert _session().profile()["onboarded"] is False


def test_import_memories_dedupes_and_persists():
    s = _session()
    out = s.import_memories("- I love chai\n\n* I love chai\nworks at Acme Corp\nx")
    assert "imported 2" in out                            # dupe + too-short skipped
    s2 = _session()
    assert "I love chai" in s2.export_memories() and "Acme Corp" in s2.export_memories()


def test_clear_memory_kinds():
    s = _session()
    s.handle("/remember test fact about me")
    s.handle("hello there")
    assert s.clear_memory("facts") == "cleared facts" and s.store["facts"] == []
    s.clear_memory("chats")
    assert s.chats_list() == [] and s.history == []


def test_helpbot_answers_offline_with_gguf_criteria():
    s = _session()
    out = s.handle("how do i import my own model into termind?")
    assert "GGUF" in out                                  # support bot, grounded, offline


def test_helpbot_limitations_topic():
    out = _session().handle("what are your limitations?")
    assert "generative fill" in out or "Honest limits" in out


def test_web_profile_and_memory_endpoints():
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    httpd, url = serve(s, port=8811, open_browser=False)
    def post(path, body):
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=3).read())
    p = post("/api/profile", {"name": "XYZ", "theme": "light"})
    assert p["name"] == "XYZ" and p["theme"] == "light" and p["onboarded"]
    m = post("/api/memory", {"op": "import", "text": "likes momos\nbuilds agents"})
    assert m["facts"] == 2
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    h = json.loads(urllib.request.urlopen(url + "/api/help", timeout=3).read())
    assert "custom model criteria" in h["topics"]
    httpd.server_close()


def test_chat_rename_persists():
    s = _session()
    s.handle("a conversation about testing")
    cid = s.chats_list()[0]["id"]
    assert s.chat_rename(cid, "  My Renamed Chat  ")
    assert _session().chats_list()[0]["title"] == "My Renamed Chat"   # survives restart
    assert not s.chat_rename("nope", "x") and not s.chat_rename(cid, "  ")


def test_terminal_chat_rename_command():
    s = _session()
    s.handle("hello world chat")
    out = s.handle("/chat rename 1 Sprint Planning")
    assert "renamed to: Sprint Planning" in out
    assert s.chats_list()[0]["title"] == "Sprint Planning"
    assert "usage" in s.handle("/chat rename 99 X")


def test_web_chat_rename_endpoint():
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    s.handle("rename me please")
    cid = s.chats_list()[0]["id"]
    httpd, url = serve(s, port=8813, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/chat",
        data=json.dumps({"op": "rename", "id": cid, "title": "Q3 Roadmap"}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert d["chats"][0]["title"] == "Q3 Roadmap"
    httpd.server_close()


def test_workspace_set_tree_read(tmp_path):
    s = _session()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')")
    (tmp_path / "README.md").write_text("# my project")
    out = s.set_workspace(str(tmp_path))
    assert "workspace set" in out and s.workspace() == str(tmp_path)
    paths = {e["path"] for e in s.ws_tree()}
    assert "README.md" in paths and "src" in paths and "src/app.py" in paths
    assert s.ws_read("src/app.py") == "print('hi')"
    assert "outside the workspace" in s.ws_read("../../etc/passwd")
    s2 = _session()
    assert s2.workspace() == str(tmp_path)               # persists across restarts


def test_terminal_ws_and_tree_commands(tmp_path):
    s = _session()
    (tmp_path / "main.py").write_text("x = 1")
    s.handle(f"/ws {tmp_path}")
    out = s.handle("/tree")
    assert "main.py" in out and str(tmp_path) in out
    assert s.handle("/read main.py") == "x = 1"


def test_web_ws_endpoint(tmp_path):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    (tmp_path / "hello.txt").write_text("workspace works")
    httpd, url = serve(s, port=8815, open_browser=False)
    def post(body):
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        req = urllib.request.Request(url + "/api/ws", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert "workspace set" in post({"op": "set", "path": str(tmp_path)})["reply"]
    assert any(e["path"] == "hello.txt" for e in post({"op": "tree"})["tree"])
    assert post({"op": "read", "path": "hello.txt"})["content"] == "workspace works"
    httpd.server_close()


def test_motion_layer_served():
    from termind.web import PAGE
    assert all(k in PAGE for k in ("class=typing", "@keyframes bounce", "celebrate(",
                                   "@keyframes confl", "id=clk", "⌥ Code", "wsbar"))


def test_code_sessions_are_separate_from_chats():
    s = _session()
    s.view_mode = "chat"; s.handle("a normal chat message")
    s.view_mode = "code"; s.chat_new(mode="code"); s.handle("build something here")
    assert len(s.chats_list("chat")) == 1 and len(s.chats_list("code")) == 1
    assert len(s.chats_list()) == 2
    assert s.active_mode() == "code"
    s2 = _session()                                    # modes persist
    assert s2.chats_list("code")[0]["mode"] == "code"


def test_code_mode_in_system_prompt(tmp_path):
    s = _session()
    s.set_workspace(str(tmp_path))
    s.chat_new(mode="code")
    sysmsg = s.chat_messages("hi")[0]["content"]
    assert "CODE MODE" in sysmsg and str(tmp_path) in sysmsg
    s.chat_new(mode="chat")
    assert "CODE MODE" not in s.chat_messages("hi")[0]["content"]


def test_ws_browse_navigates_real_dirs(tmp_path):
    s = _session()
    root = tmp_path / "area"; root.mkdir()
    (root / "projects").mkdir()
    (root / "notes").mkdir()
    (root / ".hidden").mkdir()
    d = s.ws_browse(str(root))
    assert d["current"] == str(root)
    assert set(d["dirs"]) == {"projects", "notes"}      # hidden skipped
    assert d["parent"] and d["home"]
    assert s.ws_browse("/nope/missing")["current"]      # falls back to home, no crash


def test_web_browse_and_mode_endpoints(tmp_path):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    root = tmp_path / "area"; root.mkdir()
    (root / "sub").mkdir()
    httpd, url = serve(s, port=8817, open_browser=False)
    def post(path, body):
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=3).read())
    d = post("/api/ws", {"op": "browse", "path": str(root)})
    assert d["dirs"] == ["sub"]
    post("/api/chat", {"op": "new", "mode": "code"})
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    c = json.loads(urllib.request.urlopen(url + "/api/chats?mode=code", timeout=3).read())
    assert len(c["chats"]) == 1 and c["active_mode"] == "code"
    httpd.server_close()


def test_claude_style_ui_served():
    from termind.web import PAGE
    assert all(k in PAGE for k in ("vtabs", "⌥ Code", "choose folder", "fprow",
                                   "What are we building?", "data-view"))


def test_workspace_jail_blocks_escapes(tmp_path):
    s = _session()
    s.set_workspace(str(tmp_path))
    s.view_mode = "code"
    assert "⛔ blocked" in s.do_mkdir("../escape-attempt")
    assert "⛔ blocked" in s.do_mkdir("/tmp/absolute-escape")
    assert "created folder" in s.do_mkdir("inside/ok")          # inside is fine
    assert (tmp_path / "inside" / "ok").is_dir()


def test_edit_file_rewrites_and_heals(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "def add(a, b):\n    return a + b\n")
    s = r.Session(live=True)
    s.set_workspace(str(tmp_path))
    s.view_mode = "code"
    (tmp_path / "math.py").write_text("def add(a):\n    return a\n")
    out = s.do_edit_file("math.py", "take two args")
    assert "edited math.py" in out
    assert "a + b" in (tmp_path / "math.py").read_text()
    assert "⛔ blocked" in s.do_edit_file("../../etc/hosts", "x")
    assert "no such file" in s.do_edit_file("ghost.py", "x")


def test_edit_file_intent_routing(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r.Session, "do_edit_file",
                        lambda self, f, i: f"EDIT:{f}|{i}")
    s = _session()
    s.set_workspace(str(tmp_path))
    (tmp_path / "app.py").write_text("x=1")
    assert s.handle("fix app.py: handle the null case") == "EDIT:app.py|handle the null case"
    out = s.handle("fix nonexistent.py: whatever")        # no file → not routed to editor
    assert not out.startswith("EDIT:")


def test_plan_mode_executes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _session()
    s.set_workspace(str(tmp_path))
    s.view_mode = "code"
    s.set_mode("plan")
    out = s.handle("create a folder called zed")
    assert out.startswith("📋")
    assert not (tmp_path / "zed").exists()                 # NOTHING was created
    s.set_mode("act")
    s.handle("create a folder called zed")
    assert (tmp_path / "zed").is_dir()                     # act mode does it


def test_mode_persists_and_bypass_autoconfirms():
    s = _session()
    s.set_mode("bypass")
    assert s._confirm("anything?") == "y"
    s2 = _session()
    assert s2.agent_mode == "bypass"
    s2.set_mode("act")
    assert _session().agent_mode == "act"


def test_mode_ui_and_profile_chip_served():
    from termind.web import PAGE
    assert all(k in PAGE for k in ("data-m=plan", "data-m=act", "data-m=bypass",
                                   "mecard", "meav", "/api/mode"))


def test_per_session_workspaces(tmp_path):
    s = _session()
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir(); b.mkdir()
    s.chat_new(mode="code"); s.set_workspace(str(a))
    cid_a = s.store["active_chat"]
    s.chat_new(mode="code"); s.set_workspace(str(b))
    assert s.workspace() == str(b)
    s.chat_open(cid_a)
    assert s.workspace() == str(a)                       # follows the session
    s2 = _session()
    assert s2.workspace() == str(a)                      # persists with active chat


def test_code_agent_actually_executes(tmp_path, monkeypatch):
    """THE transcript bug: 'create a calculator website' must CREATE, not advise."""
    import json as _json
    import termind.repl as r
    seq = iter([
        _json.dumps({"tool": "mkdir", "path": "calculator"}),
        _json.dumps({"tool": "write", "path": "calculator/index.html",
                     "content": "<html><body>calc</body></html>"}),
        _json.dumps({"tool": "run", "cmd": "echo deployed"}),
        _json.dumps({"tool": "done", "say": "calculator website ready"}),
    ])
    monkeypatch.setattr(r, "chat", lambda *a, **k: next(seq))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("create a website for calculator")
    assert (tmp_path / "calculator" / "index.html").read_text().startswith("<html>")
    assert "mkdir" in out and "write" in out and "deployed" in out
    assert "calculator website ready" in out
    assert any("calculator" in m["content"] for m in s.history)   # saved to session


def test_code_agent_plan_mode_does_nothing(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "1. would create calculator/")
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    s.set_mode("plan")
    out = s.handle_web("create a website for calculator")
    assert out.startswith("📋") and not (tmp_path / "calculator").exists()
    s.set_mode("act")


def test_agent_write_rejects_broken_python_and_escapes(tmp_path):
    s = _session()
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    assert s._agent_write("x.py", "def broken(:").startswith("✗")
    assert not (tmp_path / "x.py").exists()
    assert "⛔" in s._agent_write("../evil.txt", "x")
    assert "wrote ok.py" in s._agent_write("ok.py", "a = 1")


def test_chats_api_reports_workspace(tmp_path):
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve
    s = r.Session(live=False)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path))
    httpd, url = serve(s, port=8819, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    d = json.loads(urllib.request.urlopen(url + "/api/chats?mode=code", timeout=3).read())
    assert d["workspace"] == str(tmp_path) and d["has_ws"] is True
    httpd.server_close()


def test_agent_write_heals_broken_python_inline(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "x = 'fixed'\n")   # the heal reply
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s._agent_write("app.py", "x = 'broken")                      # bad quote
    assert "wrote app.py" in out and (tmp_path / "app.py").read_text() == "x = 'fixed'\n"


def test_agent_write_gives_up_gracefully(tmp_path, monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "still 'broken")    # heal also fails
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s._agent_write("app.py", "x = 'broken")
    assert out.startswith("✗") and "SIMPLER" in out
    assert not (tmp_path / "app.py").exists()                          # nothing broken on disk


def test_loop_breaks_repeat_failure_spiral(tmp_path, monkeypatch):
    """THE 11x-REJECTED transcript: identical failures must stop the loop early."""
    import json as _json
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda msgs, **k: _json.dumps(
        {"tool": "write", "path": "../escape.py", "content": "x=1"}))  # always same ⛔
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("create something")
    assert out.count("✗") <= 3                                         # not 11 wasted steps
    assert "stopped retrying" in out
    assert "✓" not in out.split("\n\n")[0]                             # failures marked ✗


def test_prose_replies_dont_burn_the_loop(tmp_path, monkeypatch):
    """First transcript bug: 12 silent steps then 'step limit reached'."""
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: "Sure! I would make a calculator…")
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("create a calculator project here")
    assert "step limit" not in out                       # ends as conversation, not a stall


def test_run_resolves_script_paths(tmp_path, monkeypatch):
    """Second transcript bug: 'run it' guessed a wrong path and failed."""
    import termind.repl as r
    s = _session()
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    (tmp_path / "calculator").mkdir()
    (tmp_path / "calculator" / "main.py").write_text("print('calc works')")
    out = s._agent_run("/Users/nope/wrong/path/main.py")     # wrong absolute guess
    assert "calc works" in out                               # resolved by basename
    out2 = s._agent_run("python3 /also/wrong/main.py")
    assert "calc works" in out2
    out3 = s._agent_run("python3 ghost.py")
    assert "Files present" in out3 and "main.py" in out3     # helpful listing back


def test_agent_sees_file_list(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    seen = {}
    def spy(msgs, **k):
        seen["sys"] = msgs[0]["content"]
        return _json.dumps({"tool": "done", "say": "ok"})
    monkeypatch.setattr(r, "chat", spy)
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    (tmp_path / "main.py").write_text("x=1")
    s.handle_web("what files do we have?")
    assert "main.py" in seen["sys"]                          # the agent KNOWS its files


def test_toolchain_detects_python():
    from termind.toolchain import detect, summary
    tc = detect()
    assert "python" in tc and tc["python"]["cmd"] in ("python3", "python")
    assert tc["python"]["version"][0].isdigit()
    assert "python→" in summary(tc)


def test_toolchain_cached_in_session():
    s = _session()
    assert s.toolchain.get("python")
    s2 = _session()
    assert s2.store["toolchain"].get("_detected_at")     # cached, not re-probed


def test_compound_cd_python_command_works(tmp_path):
    """THE transcript bug: 'cd calculator && python calculator.py' must run on a mac."""
    s = _session()
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    (tmp_path / "calculator").mkdir()
    (tmp_path / "calculator" / "calculator.py").write_text("print('calc ok')")
    out = s._agent_run("cd calculator && python calculator.py")
    assert "calc ok" in out and "not installed" not in out


def test_agent_prompt_carries_toolchain(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    seen = {}
    def spy(msgs, **k):
        seen["sys"] = msgs[0]["content"]
        return _json.dumps({"tool": "done", "say": "ok"})
    monkeypatch.setattr(r, "chat", spy)
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    s.handle_web("hello")
    assert "python→python" in seen["sys"]                # knows the exact interpreter


def test_clarifying_question_not_nudged(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "say", "say": "Which language should I use — Python or JavaScript?"}))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("create a calculator app")
    assert "Which language" in out and "step limit" not in out   # asks once, cleanly


def test_settings_sections_and_toolchain_api():
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve, PAGE
    assert all(k in PAGE for k in ("snavi", "data-s=tools", "Toolchains",
                                   "re-detect", "data-s=about"))
    s = r.Session(live=False)
    httpd, url = serve(s, port=8821, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    d = json.loads(urllib.request.urlopen(url + "/api/toolchain", timeout=3).read())
    assert "python" in d["toolchain"]
    httpd.server_close()


def test_ask_tool_offers_clickable_options(tmp_path, monkeypatch):
    """v0.19: the agent can PICK-list — sets last_options + lists them in the reply."""
    import json as _json
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "ask", "say": "Which language do you want?",
         "options": ["Python", "JavaScript / Node", "HTML/CSS/JS (web)"]}))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("build me a calculator")
    assert s.last_options == ["Python", "JavaScript / Node", "HTML/CSS/JS (web)"]
    assert "Which language" in out and "1. Python" in out          # terminal sees choices too
    assert "step limit" not in out and not (tmp_path / "calculator").exists()


def test_ask_options_clear_on_next_turn(tmp_path, monkeypatch):
    """Stale quick-replies must never leak into a later chat/build turn."""
    import json as _json
    import termind.repl as r
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "ask", "say": "CLI or web?", "options": ["CLI", "Web"]}))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    s.handle_web("make a thing")
    assert s.last_options == ["CLI", "Web"]
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "done", "say": "all set"}))
    s.handle_web("Web")                                           # they picked → fresh turn
    assert s.last_options == []                                   # cleared, no leak


def test_pip_install_routes_to_project_venv(tmp_path):
    """v0.19 PEP-668 fix: pip installs go into a project .venv, never system python."""
    s = _session()
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    routed = s._venv_route("pip3 install -r requirements.txt")
    assert routed.endswith("/pip install -r requirements.txt")
    assert ".venv/bin/pip install" in routed
    assert (tmp_path / ".venv" / "bin" / "pip").exists()          # venv really created
    # variants all normalize to the same venv pip
    assert s._venv_route("pip install flask").endswith("/pip install flask")
    assert s._venv_route("python3 -m pip install flask").endswith("/pip install flask")
    # non-pip commands are untouched
    assert s._venv_route("python3 app.py") == "python3 app.py"


def test_send_api_returns_clickable_options(tmp_path, monkeypatch):
    """The web /api/send response carries options so the UI can render quick-reply chips."""
    import json, threading, urllib.request
    import termind.repl as r
    from termind.web import serve, PAGE
    assert "qchip" in PAGE and "r.options" in PAGE                # UI wiring present
    monkeypatch.setattr(r, "chat", lambda *a, **k: json.dumps(
        {"tool": "ask", "say": "Which language?", "options": ["Python", "Go"]}))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    httpd, url = serve(s, port=8822, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    req = urllib.request.Request(url + "/api/send",
        data=json.dumps({"text": "build a calculator", "mode": "code"}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert d["options"] == ["Python", "Go"]
    httpd.server_close()


# ───────────────────────── v0.20 — Agent Action Ledger ─────────────────────────
def test_ledger_records_hash_chains_and_survives_reload():
    from termind.ledger import Ledger
    led = Ledger()
    a = led.record(session="s1", tool="write", target="app.py", outcome="ok",
                   consent="build a calculator", bytes_written=42)
    b = led.record(session="s1", tool="run", target="python app.py", outcome="ok")
    assert b["prev"] == a["hash"]                       # chained
    assert led.verify()["ok"] and led.verify()["count"] == 2
    again = Ledger()                                    # reloaded from the JSONL on disk
    assert again.verify()["ok"] and again.summary()["count"] == 2
    assert again.entries[0]["consent"] == "build a calculator"   # consent captured


def test_ledger_detects_tampering():
    from termind.ledger import Ledger
    led = Ledger()
    led.record(session="s", tool="write", target="a.py", outcome="ok")
    led.record(session="s", tool="write", target="b.py", outcome="ok")
    led.entries[0]["target"] = "evil.py"               # someone edited a past entry
    v = led.verify()
    assert v["ok"] is False and v["broken_at"] == 0    # the chain catches it


def test_ledger_chain_verifies_without_the_key():
    """A reviewer with only the exported file (no install key) can still prove no tampering."""
    import os
    from termind.ledger import Ledger
    led = Ledger()
    led.record(session="s", tool="run", target="ls", outcome="ok")
    os.remove(os.path.join(os.environ["TERMIND_HOME"], "ledger.key"))
    v = Ledger().verify()
    assert v["ok"] and v["chain_ok"] and v["sig_ok"] is None   # chain proven, sig skipped


def test_ledger_export_is_self_describing():
    from termind.ledger import Ledger
    led = Ledger()
    led.record(session="s", tool="mkdir", target="proj", outcome="ok")
    ex = led.export()
    assert ex["artifact"] == "agent-action-ledger" and ex["integrity"]["ok"]
    assert ex["summary"]["count"] == 1 and len(ex["entries"]) == 1


def test_code_agent_writes_every_action_to_the_ledger(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    seq = iter([
        _json.dumps({"tool": "mkdir", "path": "calc"}),
        _json.dumps({"tool": "write", "path": "calc/app.py", "content": "print(1)\n"}),
        _json.dumps({"tool": "write", "path": "../escape.py", "content": "x=1"}),  # blocked
        _json.dumps({"tool": "done", "say": "done"}),
    ])
    monkeypatch.setattr(r, "chat", lambda *a, **k: next(seq))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    s.handle_web("build a calculator")
    outs = [e["outcome"] for e in s.ledger.entries]
    assert "ok" in outs and "blocked" in outs           # both the write and the jail-block logged
    assert all(e["consent"] == "build a calculator" for e in s.ledger.entries)
    assert s.ledger.verify()["ok"]                       # tamper-evident chain holds
    assert "audit ledger" in s.do_status()               # surfaced in /status


def test_ledger_command_and_export(tmp_path):
    s = _session()
    s.ledger.record(session="s", tool="run", target="pytest", outcome="ok")
    assert "audit ledger" in s.handle("/ledger")
    assert "VERIFIED" in s.handle("/ledger verify")
    dest = tmp_path / "led.json"
    out = s.handle(f"/ledger export {dest}")
    assert "exported" in out and dest.exists()
    assert json.loads(dest.read_text())["artifact"] == "agent-action-ledger"


def test_ledger_api_and_settings_panel(tmp_path):
    import threading, urllib.request
    import termind.repl as r
    from termind.web import serve, PAGE
    assert all(k in PAGE for k in ("data-s=audit", "loadAudit", "termind-agent-ledger.json"))
    s = r.Session(live=False)
    s.ledger.record(session="s", tool="write", target="x.py", outcome="ok", bytes_written=10)
    httpd, url = serve(s, port=8823, open_browser=False)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    d = json.loads(urllib.request.urlopen(url + "/api/ledger", timeout=3).read())
    assert d["summary"]["count"] == 1 and d["integrity"]["ok"] and len(d["entries"]) == 1
    httpd.server_close()
    httpd2, url2 = serve(s, port=8824, open_browser=False)
    threading.Thread(target=httpd2.handle_request, daemon=True).start()
    full = json.loads(urllib.request.urlopen(url2 + "/api/ledger?full=1", timeout=3).read())
    assert full["artifact"] == "agent-action-ledger"
    httpd2.server_close()


# ───────────────────── v0.21 — Consent Escalation (frontier-on-consent) ─────────────────────
def test_reach_stays_local_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = _session()
    out = s.do_reach("what is 2+2")
    assert "100% local" in out
    assert not any(e["tool"] == "escalate" for e in s.ledger.entries)   # nothing left the machine
    assert "data off-machine: 0 bytes" in s.do_status()


def test_reach_escalates_and_logs_every_byte(monkeypatch):
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(r, "claude_chat", lambda msgs: "the frontier answer")
    s = _session()
    out = s.do_reach("design a B-tree")
    assert "the frontier answer" in out
    esc = [e for e in s.ledger.entries if e["tool"] == "escalate"]
    assert len(esc) == 1 and esc[0]["outcome"] == "ok" and esc[0]["bytes"] > 0
    assert esc[0]["consent"] == "design a B-tree"          # the authorizing message is recorded
    assert "cloud:" in esc[0]["target"]
    assert s.ledger.verify()["ok"]                          # the escalation is in the sealed chain
    off = s.do_status().split("data off-machine:")[1]
    assert "0 bytes" not in off and "consented cloud escalation" in off   # status is truthful now


def test_think_cloud_rung_is_now_audited(monkeypatch):
    """The pre-existing /think -> Claude path used to leave the machine UNLOGGED; now it's sealed."""
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("TERMIND_BIG_MODEL", raising=False)
    monkeypatch.setattr(r, "claude_chat", lambda msgs: "cloud reasoning")
    s = _session()
    assert "cloud reasoning" in s.do_think("prove it")
    assert any(e["tool"] == "escalate" for e in s.ledger.entries)


def test_frontier_failure_is_logged_and_falls_back(monkeypatch):
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    def boom(msgs):
        raise RuntimeError("network down")
    monkeypatch.setattr(r, "claude_chat", boom)
    s = _session()
    out = s.do_reach("hello")
    assert "unreachable" in out and "staying local" in out
    esc = [e for e in s.ledger.entries if e["tool"] == "escalate"]
    assert esc and esc[0]["outcome"] == "fail"             # the attempt itself is on the record


def test_reach_over_web_routes_and_logs(monkeypatch):
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(r, "claude_chat", lambda msgs: "web frontier answer")
    s = r.Session(live=False)
    out = s.handle_web("/reach explain quicksort")
    assert "web frontier answer" in out
    esc = [e for e in s.ledger.entries if e["tool"] == "escalate"]
    assert len(esc) == 1 and esc[0]["bytes"] > 0           # the web path is audited too


# ──────────────── v0.22 — Escalate-in-the-loop (stuck local → frontier, on consent) ────────────────
def test_stuck_local_offers_escalation_when_key_present(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "write", "path": "../escape.py", "content": "x=1"}))   # always jail-blocked
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("build a thing")
    assert "escalate" in out.lower()                           # offers the frontier instead of giving up
    assert s.last_options == ["⤴ Escalate this step to Claude", "Keep it local"]
    assert s._stuck_task == "build a thing"


def test_stuck_without_key_just_gives_up_locally(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "write", "path": "../escape.py", "content": "x=1"}))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    out = s.handle_web("build a thing")
    assert "stopped retrying" in out and not s.last_options    # no key → no cloud chip


def test_escalation_runs_frontier_brain_and_logs_every_step(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(    # local model is hopeless
        {"tool": "write", "path": "../escape.py", "content": "x=1"}))
    fseq = iter([                                                  # frontier model is competent
        _json.dumps({"tool": "write", "path": "app.py", "content": "print('hi')\n"}),
        _json.dumps({"tool": "done", "say": "built it with the frontier model"}),
    ])
    monkeypatch.setattr(r, "claude_chat", lambda msgs: next(fseq))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    s.handle_web("build a hello app")                             # gets stuck → offers escalation
    assert s._stuck_task == "build a hello app"
    out = s.handle_web("⤴ Escalate this step to Claude")          # user consents
    assert "built it with the frontier model" in out
    assert (tmp_path / "app.py").read_text().startswith("print")  # frontier actually built it
    esc = [e for e in s.ledger.entries if e["tool"] == "escalate"]
    assert esc and all(e["consent"] == "build a hello app" for e in esc)  # original task authorized it
    assert s.ledger.verify()["ok"] and s._stuck_task is None
    assert "consented cloud escalation" in s.do_status()          # off-machine bytes now truthful


def test_keep_it_local_declines_escalation(tmp_path, monkeypatch):
    import json as _json
    import termind.repl as r
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(r, "chat", lambda *a, **k: _json.dumps(
        {"tool": "write", "path": "../escape.py", "content": "x=1"}))
    s = r.Session(live=True)
    s.chat_new(mode="code"); s.set_workspace(str(tmp_path)); s.view_mode = "code"
    s.handle_web("build a thing")
    out = s.handle_web("Keep it local")
    assert "Staying local" in out and s._stuck_task is None
    assert not any(e["tool"] == "escalate" for e in s.ledger.entries)   # nothing left the machine
