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
    monkeypatch.setattr(r, "model_available", lambda: False)
    s = r.Session()
    assert s.server is True and s.live is False        # must NOT pretend to be online
    assert "model missing" in s.do_status()


def test_server_and_model_present_is_live(monkeypatch):
    import termind.repl as r
    monkeypatch.setattr(r, "ollama_available", lambda: True)
    monkeypatch.setattr(r, "model_available", lambda: True)
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
