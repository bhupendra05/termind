import pytest


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    """Every test gets its own TERMIND_HOME so suites never touch ~/.termind."""
    monkeypatch.setenv("TERMIND_HOME", str(tmp_path / "termind-home"))
