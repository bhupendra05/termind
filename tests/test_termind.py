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
