from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

import ashare_agent.webapp as webapp
from ashare_agent.webapp import AppServer, Jobs, command_plan


def test_command_plan_has_a_fixed_request_domain_and_rejects_nan_or_code_injection(tmp_path: Path):
    plan = command_plan(
        {"kind": "screen", "network": "direct", "top": 5, "symbols": "600000.SH"},
        tmp_path / "run",
    )
    assert plan[0][:2] == ["current-screen", "--network"]
    assert "600000.SH" in plan[0]

    with pytest.raises(ValueError):
        command_plan(
            {
                "kind": "screen",
                "network": "direct",
                "top": 5,
                "symbols": "600000.SH",
                "unexpected": "python -c exploit",
            },
            tmp_path / "run",
        )
    with pytest.raises(ValueError, match="无效"):
        command_plan(
            {"kind": "screen", "network": "direct", "symbols": "600000.SH", "min_amount": float("nan")},
            tmp_path / "run",
        )
    with pytest.raises(ValueError):
        command_plan(
            {"kind": "screen", "network": "direct", "symbols": "600000.SH; whoami"},
            tmp_path / "run",
        )


def test_paper_commands_are_fixed_and_reject_paths_or_replay(tmp_path):
    request = {"kind": "paper-check", "account_id": "paper-a", "snapshot_id": "snapshot-a"}
    command = command_plan(request, tmp_path)[0]
    assert command[:2] == ["paper-account", "check"]
    assert command[command.index("--id") + 1] == "paper-a"
    for update in ({"snapshot_id": "../outside"}, {"account_id": "C:\\other"},
                   {"snapshot_id": "--replay"}, {"replay": True}, {"snapshot_id": None}):
        with pytest.raises(ValueError):
            command_plan({**request, **update}, tmp_path)


@pytest.fixture
def running_server(tmp_path: Path):
    jobs = Jobs(tmp_path)
    server = AppServer(("127.0.0.1", 0), jobs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, jobs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_server_state_exposes_csrf_and_rejects_missing_or_bad_post_auth(running_server):
    server, jobs = running_server
    base = f"http://127.0.0.1:{server.server_port}"
    with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
        state_response = client.get("/api/state")
        assert state_response.status_code == 200
        state = state_response.json()
        assert state["app"] == "ashare-workbench"
        csrf = state["csrf"]

        payload = {"kind": "doctor", "network": "direct"}
        responses = [
            client.post("/api/run", json=payload, headers={"X-CSRF-Token": csrf}),
            client.post(
                "/api/run",
                json=payload,
                headers={"Origin": "http://evil.invalid", "X-CSRF-Token": csrf},
            ),
            client.post(
                "/api/run",
                json=payload,
                headers={"Origin": server.origin, "X-CSRF-Token": "wrong"},
            ),
        ]
        assert [response.status_code for response in responses] == [403, 403, 403]
        assert not jobs.jobs


def test_planner_api_state_auth_save_and_invalid_position(running_server):
    server, _ = running_server
    base = f"http://127.0.0.1:{server.server_port}"
    with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
        state_response = client.get("/api/planner/state")
        assert state_response.status_code == 200
        state = state_response.json()
        assert state["profile"]["capital"] is None
        assert state["planner_api_version"] == 3
        assert state["account_check"]["status"] == "unconfirmed"
        assert state["profile"]["cash"] is None
        assert state["active_job"] is None
        csrf = state["csrf"]
        headers = {"Origin": server.origin, "X-CSRF-Token": csrf}

        unauthenticated = client.post(
            "/api/planner/profile", json={"capital": "100000", "cash": "100000"}
        )
        assert unauthenticated.status_code == 403

        saved = client.post(
            "/api/planner/profile",
            json={"capital": "100000", "cash": "100000"},
            headers=headers,
        )
        assert saved.status_code == 200
        assert saved.json()["capital"] == "100000"

        invalid_position = client.post(
            "/api/planner/position",
            json={
                "ts_code": "600000.SH",
                "quantity": 100,
                "cost_price": "not-a-number",
                "buy_date": "2026-09-01",
            },
            headers=headers,
        )
        assert invalid_position.status_code == 400


def test_planner_api_rejects_profile_changes_during_active_job_and_missing_version(running_server):
    server, jobs = running_server
    jobs.active = "active-planner"
    jobs.jobs[jobs.active] = {"id": jobs.active, "kind": "planner", "status": "running", "logs": ""}
    (jobs.folder / jobs.active).mkdir(parents=True)
    base = f"http://127.0.0.1:{server.server_port}"
    with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
        state = client.get("/api/planner/state")
        assert state.status_code == 200
        headers = {"Origin": server.origin, "X-CSRF-Token": state.json()["csrf"]}

        blocked = client.post(
            "/api/planner/profile",
            json={"capital": "100000", "cash": "100000"},
            headers=headers,
        )
        assert blocked.status_code == 400
        assert "运行期间" in blocked.json()["error"]

        missing = client.get("/api/planner/version/does-not-exist")
        assert missing.status_code == 404


def test_local_server_does_not_expose_env_or_allow_report_path_traversal(running_server):
    server, _ = running_server
    base = f"http://127.0.0.1:{server.server_port}"
    with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
        traversal = client.get("/reports/no-job/%2e%2e/%2e%2e/.env")
        direct_env = client.get("/.env")
        api_traversal = client.get("/api/%2e%2e/.env")

    assert traversal.status_code == 404
    assert direct_env.status_code == 404
    assert api_traversal.status_code == 404


def test_account_confirmation_http_cas_and_client_claim_rejection(running_server, monkeypatch):
    from datetime import datetime

    import ashare_agent.planner as planner

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-09-10T16:30:00+08:00").astimezone(tz)

    monkeypatch.setattr(planner, "datetime", Clock)
    server, _ = running_server
    with httpx.Client(base_url=server.origin, trust_env=False, timeout=2) as client:
        state = client.get("/api/planner/state").json()
        headers = {"Origin": server.origin, "X-CSRF-Token": state["csrf"]}
        assert client.post("/api/planner/profile", json={"capital": "100000", "cash": "100000"}, headers=headers).status_code == 200
        state = client.get("/api/planner/state").json()
        body = {"as_of": "2026-09-10T15:30:00+08:00", "positions_complete": True,
                "frozen_cash": "0", "other_assets": "0", "expected_account_revision": state["account_revision"]}
        assert client.post("/api/planner/account-confirmation", json=body).status_code == 403
        assert client.post("/api/planner/account-confirmation", json={**body, "recorded_at": body["as_of"]}, headers=headers).status_code == 400
        result = client.post("/api/planner/account-confirmation", json=body, headers=headers)
        assert result.status_code == 200
        assert result.json()["positions_count"] == 0
        assert client.get("/api/planner/state").json()["account_check"]["status"] == "confirmed"
        assert client.post("/api/planner/account-confirmation", json=body, headers=headers).status_code == 400


def test_paper_web_virtual_creation_state_and_strict_post_fields(running_server):
    server, jobs = running_server
    with httpx.Client(base_url=server.origin, trust_env=False, timeout=2) as client:
        page = client.get("/paper")
        assert page.status_code == 200 and "模拟账户" in page.text
        state = client.get("/api/paper/state").json()
        assert state["paper_api_version"] == 1 and state["accounts"] == []
        headers = {"Origin": server.origin, "X-CSRF-Token": state["csrf"]}
        payload = {"name": "HTTP虚拟账户", "initial_cash": "12345.67"}
        assert client.post("/api/paper/create", json=payload).status_code == 403
        assert client.post("/api/paper/create", json={**payload, "positions": []}, headers=headers).status_code == 400
        response = client.post("/api/paper/create", json=payload, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["cash"] == "12345.67" and result["status"] == "waiting_data"
        ident = result["account_id"]
        assert client.get("/api/paper/accounts/" + ident).json()["metrics"]["total_return"] is None
        assert client.get("/api/paper/state").json()["accounts"][0]["account_id"] == ident
        assert not list(jobs.root.rglob("*.sqlite"))
        assert client.get("/api/paper/accounts/invalid-id").status_code == 404


def test_paper_cli_create_and_list_share_the_virtual_workspace(tmp_path, monkeypatch):
    from ashare_agent.cli import execute, parser

    monkeypatch.chdir(tmp_path)
    result = execute(parser().parse_args(["paper-account", "create", "--name", "CLI fixture", "--initial-cash", "90000"]))
    listed = execute(parser().parse_args(["paper-account", "list"]))
    assert listed["accounts"][0]["account_id"] == result["account_id"]
    assert result["cash"] == "90000.00"
    assert not list(tmp_path.rglob("*.sqlite"))


def test_launcher_starts_new_backend_if_old_version_owns_default_port(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["workbench", "--no-browser"])
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"app": "ashare-workbench"}))
    monkeypatch.setattr(webapp, "Jobs", lambda: object())
    addresses = []

    class Server:
        origin = "http://127.0.0.1:12345"

        def serve_forever(self):
            addresses.append("served")

        def server_close(self):
            addresses.append("closed")

    def create(address, jobs):
        addresses.append(address)
        if address[1] == 8765:
            raise OSError("fixture old backend still running")
        return Server()

    monkeypatch.setattr(webapp, "AppServer", create)
    webapp.main()
    assert addresses == [("127.0.0.1", 8765), ("127.0.0.1", 0), "served", "closed"]


def test_intraday_http_confirmation_and_start_stop_outside_session(running_server, monkeypatch):
    from datetime import datetime

    from ashare_agent import intraday

    monkeypatch.setattr(intraday, "now", lambda: datetime.fromisoformat("2026-09-11T09:00:00+08:00"))
    server, _ = running_server
    with httpx.Client(base_url=server.origin, trust_env=False, timeout=3) as client:
        assert client.get("/intraday").status_code == 200
        state = client.get("/api/intraday/state").json()
        assert state["intraday_api_version"] == 1 and state["running"] is False
        headers = {"Origin": server.origin, "X-CSRF-Token": state["csrf"]}
        client.post("/api/planner/profile", json={"capital": "100000", "cash": "100000"}, headers=headers)
        state = client.get("/api/intraday/state").json()
        payload = {"expected_account_revision": state["account"]["revision"], "positions_complete": True,
                   "corporate_actions_checked": True, "frozen_cash": "0", "other_assets": "0", "sellable_quantities": {}}
        assert client.post("/api/intraday/start", json=payload).status_code == 403
        assert client.post("/api/intraday/start", json={**payload, "positions_complete": "true"}, headers=headers).status_code == 400
        assert client.post("/api/intraday/start", json={**payload, "recorded_at": "fake"}, headers=headers).status_code == 400
        started = client.post("/api/intraday/start", json=payload, headers=headers)
        assert started.status_code == 200, started.text
        state = client.get("/api/intraday/state").json()
        assert state["running"] is True
        assert state["market_status"] != "连续竞价"
        assert client.post("/api/intraday/stop", json={}, headers=headers).status_code == 200
        server.monitor.thread.join(timeout=2)
        assert client.get("/api/intraday/state").json()["running"] is False


class _FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def test_jobs_cancel_only_targets_the_active_job(tmp_path: Path):
    jobs = Jobs(tmp_path)
    active = "active"
    other = "other"
    jobs.jobs[active] = {"id": active, "status": "running", "logs": ""}
    jobs.jobs[other] = {"id": other, "status": "running", "logs": ""}
    process = _FakeProcess()
    jobs.active = active
    jobs.process = process

    with pytest.raises(ValueError, match="已结束或不存在"):
        jobs.cancel(other)
    assert not process.terminated
    assert other not in jobs.cancelled

    jobs.cancel(active)
    assert process.terminated
    assert active in jobs.cancelled
    assert "已请求停止" in jobs.jobs[active]["logs"]


def test_jobs_persist_recovery_gate_for_legacy_running_record(tmp_path: Path):
    output = tmp_path / "runtime" / "web-runs" / "legacy"
    output.mkdir(parents=True)
    (output / "job.json").write_text(
        json.dumps({"id": "legacy", "status": "running", "logs": ""}), encoding="utf-8"
    )

    jobs = Jobs(tmp_path)
    assert jobs.jobs["legacy"]["status"] == "failed"
    assert jobs.jobs["legacy"]["recovery_required"] is True
    with pytest.raises(RuntimeError, match="安全阻止"):
        jobs.start({"kind": "doctor", "network": "direct"})
    jobs.close()

    restarted = Jobs(tmp_path)
    assert restarted.recovery_blocked is True
    assert restarted.jobs["legacy"]["recovery_required"] is True
    restarted.close()


def test_appserver_bind_failure_does_not_close_unbound_jobs(tmp_path: Path, monkeypatch):
    first_jobs = Jobs(tmp_path / "first")
    first = AppServer(("127.0.0.1", 0), first_jobs)
    second_jobs = Jobs(tmp_path / "second")

    def fail_bind(_server):
        raise OSError("occupied")

    monkeypatch.setattr(webapp.ThreadingHTTPServer, "server_bind", fail_bind)
    try:
        with pytest.raises(OSError):
            AppServer(("127.0.0.1", 0), second_jobs)
        assert second_jobs.closed is False
    finally:
        second_jobs.close()
        first.server_close()


def test_cancelled_job_does_not_expose_report_or_result(running_server):
    server, jobs = running_server
    ident = "cancelled"
    output = jobs.folder / ident
    output.mkdir()
    (output / "report.html").write_text("private", encoding="utf-8")
    (output / "result.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    jobs.jobs[ident] = {
        "id": ident,
        "kind": "screen",
        "status": "cancelled",
        "logs": "",
        "error": "cancelled",
    }

    snapshot = jobs.snapshot(ident)
    assert snapshot["report_url"] is None
    assert snapshot["result"] is None
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        response = client.get(f"/reports/{ident}/report.html")
    assert response.status_code == 404
