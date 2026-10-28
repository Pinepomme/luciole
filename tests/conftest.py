import pytest


@pytest.fixture(autouse=True)
def luciole_home(tmp_path_factory, monkeypatch):
    """Each test has its own ~/.luciole: it never sees an engine running on this machine."""
    home = tmp_path_factory.mktemp("luciole-home")
    monkeypatch.setenv("LUCIOLE_HOME", str(home))
    monkeypatch.delenv("LUCIOLE_LIVE_PORT", raising=False)
    return home
