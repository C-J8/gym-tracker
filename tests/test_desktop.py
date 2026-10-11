import os
import socket
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gym_tracker.automation import (
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
