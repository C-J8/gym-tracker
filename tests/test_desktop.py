import os
import socket
import time
import uuid
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gym_tracker.automation import (
    read_json,
    request_stop,
)
from gym_tracker.automation import (
    start as start_importer,
)
from gym_tracker.automation import (
    status as importer_status,
)
from gym_tracker.desktop import control, dashboard_healthy, start
from gym_tracker.models import Base, User


def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("Desktop did not reach the expected state")


@pytest.fixture
def desktop_environment(tmp_path, monkeypatch):
    database = tmp_path / "test.sqlite"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(display_name="Teste"))
        session.commit()
    engine.dispose()
    (tmp_path / "source.txt").write_text("01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep", encoding="utf-8")
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('Teste')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    monkeypatch.setenv("GYM_IMPORT_FILE", str(tmp_path / "source.txt"))
    monkeypatch.setenv("GYM_IMPORT_SETTLE_SECONDS", "1")
    monkeypatch.setenv("GYM_TRACKER_USER_ID", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GYM_BACKUP_DIRECTORY", str(tmp_path / "backups"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return tmp_path / ".gym-tracker", port


def test_desktop_reuses_processes_and_stops_its_importer(desktop_environment):
    runtime, port = desktop_environment
    try:
        first = start(runtime, port)
        assert first["phase"] == "ready"
        assert first["importer_owned"]
        assert dashboard_healthy(port)
        assert start(runtime, port)["token"] == first["token"]
        wait_until(lambda: importer_status(runtime).get("phase") == "imported")
        assert control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)
        assert not importer_status(runtime)["running"]
        assert not dashboard_healthy(port)
    finally:
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)


def test_desktop_preserves_manually_started_importer(desktop_environment):
    runtime, port = desktop_environment
    manual = start_importer(runtime)
    try:
        first = start(runtime, port)
        assert not first["importer_owned"]
        assert control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)
        assert importer_status(runtime)["run_id"] == manual["run_id"]
        assert importer_status(runtime)["running"]
        # A stale owner cannot stop a replacement worker.
        request_stop(runtime, expected_run_id=uuid.uuid4().hex)
        assert importer_status(runtime)["running"]
    finally:
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)
        request_stop(runtime, expected_run_id=manual["run_id"])
        wait_until(lambda: not importer_status(runtime)["running"])


def test_desktop_refuses_an_occupied_port(desktop_environment):
    runtime, port = desktop_environment
    with socket.socket() as occupied:
        if os.name == "nt":
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", port))
        occupied.listen()
        with pytest.raises(RuntimeError, match="nao iniciou"):
            start(runtime, port)
        assert not importer_status(runtime)["running"]
        assert control(runtime) is None


def test_overview_is_authenticated_readonly_and_does_not_export_messages(desktop_environment):
    runtime, port = desktop_environment
    (runtime.parent / "source.txt").write_bytes(b"01/01/2026 10:00 - PRIVATE_NAME: PRIVATE_MOVE 10kg/10rep")
    try:
        start(runtime, port)
        wait_until(lambda: importer_status(runtime).get("phase") == "imported")
        state = read_json(runtime / "desktop.json")
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{state['control_port']}/overview", timeout=3)
        assert error.value.code == 403
        before = importer_status(runtime)["checked_at"]
        overview = control(runtime, "overview")
        assert overview["database"] == "connected"
        assert overview["pending_reviews"] == 1
        assert overview["importer"]["running"]
        assert overview["importer"]["checked_at"] == before
        assert overview["last_backup_at"]
        assert "PRIVATE_NAME" not in str(overview)
        assert "PRIVATE_MOVE" not in str(overview)
        assert "source" not in overview["importer"]
        assert "pid" not in overview["importer"]
    finally:
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)


def test_reopening_recovers_stopped_importer_without_restarting_dashboard(desktop_environment):
    runtime, port = desktop_environment
    try:
        first = start(runtime, port)
        worker = importer_status(runtime)
        request_stop(runtime, expected_run_id=worker["run_id"])
        wait_until(lambda: not importer_status(runtime)["running"])
        current = control(runtime)
        assert current["phase"] == "degraded"
        assert not current["importer"]["running"]
        assert dashboard_healthy(port)
        recovered = start(runtime, port)
        assert recovered["token"] == first["token"]
        assert recovered["importer"]["running"]
        assert recovered["importer_owned"]
        assert recovered["importer"]["run_id"] != worker["run_id"]
    finally:
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)
        assert not importer_status(runtime)["running"]


def test_desktop_reports_import_error_without_hiding_dashboard(desktop_environment):
    runtime, port = desktop_environment
    (runtime.parent / "source.txt").write_bytes(b"invalid export")
    try:
        start(runtime, port)
        wait_until(lambda: importer_status(runtime).get("phase") == "error")
        current = control(runtime)
        assert current["phase"] == "degraded"
        assert current["importer"]["running"]
        assert current["importer"]["phase"] == "error"
        assert dashboard_healthy(port)
    finally:
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)


def test_desktop_preserves_manual_replacement_after_its_worker_stops(desktop_environment):
    runtime, port = desktop_environment
    manual = None
    try:
        start(runtime, port)
        owned = importer_status(runtime)["run_id"]
        request_stop(runtime, expected_run_id=owned)
        wait_until(lambda: not importer_status(runtime)["running"])
        manual = start_importer(runtime)
        assert start(runtime, port)["importer"]["run_id"] == manual["run_id"]
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)
        assert importer_status(runtime)["running"]
        assert importer_status(runtime)["run_id"] == manual["run_id"]
    finally:
        control(runtime, "stop")
        wait_until(lambda: control(runtime) is None)
        if manual:
            request_stop(runtime, expected_run_id=manual["run_id"])
            wait_until(lambda: not importer_status(runtime)["running"])


def test_reopen_waits_for_supervisor_ownership_even_if_worker_already_has_lock(tmp_path, monkeypatch):
    from gym_tracker import desktop

    starting = {"port": 8501, "phase": "starting", "importer_owned": False, "importer": {"running": True}}
    ready = {**starting, "phase": "ready", "importer_owned": True}
    observations = iter([starting, starting, ready])

    def observed(_runtime, action="status"):
        return starting if action == "ensure" else next(observations)

    monkeypatch.setattr(desktop, "control", observed)
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    assert desktop.start(tmp_path, 8501) == ready


def test_supervisor_does_not_rewrite_discovery_descriptor_after_ready(desktop_environment, monkeypatch):
    from gym_tracker import desktop

    runtime, port = desktop_environment
    writes = []
    original = desktop.write_json

    def once_only(path, value):
        if path.name == "desktop.json":
            writes.append(path)
            if len(writes) > 1:
                raise PermissionError("Simulated Windows read handle")
        original(path, value)

    class FinishedDashboard:
        def poll(self):
            return 0

    monkeypatch.setattr(desktop, "write_json", once_only)
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *_args, **_kwargs: FinishedDashboard())
    monkeypatch.setattr(desktop, "dashboard_healthy", lambda _: True)
    monkeypatch.setattr(desktop, "start_importer", lambda *_args, **_kwargs: {"run_id": "manual"})
    desktop.watch(runtime, port, str(uuid.uuid4()))
    assert len(writes) == 1


def test_supervisor_failure_diagnostic_does_not_log_exception_text(tmp_path, monkeypatch, capsys):
    from gym_tracker import desktop

    def broken(*_args, **_kwargs):
        raise PermissionError("SECRET credentials or message")

    runtime = tmp_path / "runtime"
    monkeypatch.setattr(desktop, "watch", broken)
    monkeypatch.setattr(desktop.sys, "argv", ["desktop", "watch", "--runtime-dir", str(runtime)])
    with pytest.raises(SystemExit) as error:
        desktop.main()
    assert error.value.code == 1
    diagnostic = read_json(runtime / "desktop-error.json")
    assert diagnostic["kind"] == "PermissionError"
    assert set(diagnostic) == {"at", "kind", "errno"}
    assert "SECRET" not in str(diagnostic)
    assert "SECRET" not in capsys.readouterr().out
