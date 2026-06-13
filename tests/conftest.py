import pytest


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    """Every test gets its own TERMIND_HOME so suites never touch ~/.termind, and runs with
    no ambient cloud key so frontier-escalation behavior is deterministic (tests that exercise
    it set ANTHROPIC_API_KEY themselves)."""
    monkeypatch.setenv("TERMIND_HOME", str(tmp_path / "termind-home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
