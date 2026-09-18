"""SYNTHETIC compatibility checks; no real study, account, or external service."""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import ashare_agent.webapp as webapp
from test_intraday import quote
from test_webapp import running_server  # noqa: F401


def test_fixture_accepts_overrides_without_duplicate_keyword_error():
    q = quote(previous_close="12.00", bid="11.99", name="SYNTHETIC")
    assert q["previous_close"] == "12.00"
    assert q["bid"] == "11.99"
    assert q["name"] == "SYNTHETIC"
    assert quote()["previous_close"] == "10.00"


def test_local_redundant_argument_removal_is_retained():
    source = Path(__file__).with_name("test_intraday.py").read_text(encoding="utf-8")
    assert 'q = quote("9.00", "9.00", limit_state="possible_lower_limit")' in source
    assert 'q = quote("9.00", "9.00", previous_close="10.00", limit_state=' not in source


@pytest.mark.parametrize("existing", [7, 8, 9])
def test_launcher_does_not_reuse_legacy_backend(monkeypatch, tmp_path, existing):
    assert webapp.BACKEND_VERSION == 9
    events = []
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    monkeypatch.setattr(webapp.os, "chdir", lambda path: None)
    monkeypatch.setattr(webapp.sys, "argv", ["webapp", "--no-browser"])
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: SimpleNamespace(
        json=lambda: {"app": "ashare-workbench", "backend_version": existing}))
    monkeypatch.setattr(webapp, "Jobs", lambda: events.append("jobs") or object())

    class SyntheticServer:
        origin = "http://127.0.0.1:0"
        def __init__(self, *a, **kw):
            events.append("server")
        def serve_forever(self):
            events.append("serve")
        def server_close(self):
            events.append("close")

    monkeypatch.setattr(webapp, "AppServer", SyntheticServer)
    webapp.main()
    assert events == ([] if existing == 9 else ["jobs", "server", "serve", "close"])
    assert not (tmp_path / "runtime").exists()


def test_research_page_and_readonly_state_do_not_register_real_study(running_server):
    server, jobs = running_server
    before = {p.relative_to(jobs.root).as_posix(): p.read_bytes()
              for p in jobs.root.rglob("*") if p.is_file()}
    with httpx.Client(base_url=server.origin, trust_env=False) as client:
        assert client.get("/research").status_code == 200
        response = client.get("/api/research/state")
        assert response.status_code == 200
        assert response.json()["status"] == "not_initialized"
        state = client.get("/api/state").json()
        assert state["backend_version"] == 9
    after = {p.relative_to(jobs.root).as_posix(): p.read_bytes()
             for p in jobs.root.rglob("*") if p.is_file()}
    assert before == after
    assert not (jobs.root / "runtime/research-v2").exists()


def test_readme_last_section_uses_new_export_path_only():
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    section = readme[readme.rindex("# 一键 AI 复核"):]
    assert "runtime/research-v2/outbox/<batch_id>/" in section
    assert "runtime/daily-lab/overlay-results/" not in section
    assert "初始化会锚定真实研究阶段" in section
