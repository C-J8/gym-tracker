import json
import time
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from gym_tracker.automation import (
    AlreadyRunning,
    ImportWorker,
    ProcessLock,
    is_running,
    read_json,
    request_stop,
    start,
    status,
    watch,
    write_json,
)
from gym_tracker.config import Settings
from gym_tracker.models import Base, Import, ParseResult, ParseReview, User, WorkoutSet
from gym_tracker.services.automatic_import import ImportInputError, SourceNotReady, import_snapshot, stable_snapshot

FIRST = b"01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"
SECOND = FIRST + b"\n02/01/2026 10:00 - Pessoa: Extensora 45kg/10rep"


@pytest.fixture
def configured(tmp_path):
    url = f"sqlite:///{(tmp_path / 'gym.sqlite').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(display_name="Teste"))
        session.commit()
    engine.dispose()
    source = tmp_path / "source.txt"
    source.write_bytes(FIRST)
    return Settings(
        database_url=url,
        gym_import_file=source,
        gym_tracker_user_id=None,
        parser_version="auto-test",
        openai_api_key=None,
        gym_import_settle_seconds=1,
        gym_backup_directory=tmp_path / "backups",
    )


def count_rows(settings, model):
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            return session.scalar(select(func.count()).select_from(model))
    finally:
        engine.dispose()


def test_stable_snapshot_uses_both_content_and_metadata(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(FIRST)
    assert stable_snapshot(source, 0, wait=lambda _: None) == FIRST
    with pytest.raises(SourceNotReady):
        stable_snapshot(source, 0, wait=lambda _: source.write_bytes(SECOND))


@pytest.mark.parametrize("content", [b"", b"\xff\xfe", b"arquivo sem cabecalho do WhatsApp"])
def test_invalid_source_cannot_be_imported(tmp_path, content):
    source = tmp_path / "source.txt"
    source.write_bytes(content)
    with pytest.raises(ImportInputError):
        stable_snapshot(source, 0, wait=lambda _: None)


def test_same_file_then_overlapping_export_is_idempotent(configured):
    first = import_snapshot(configured, configured.gym_import_file, FIRST)
    duplicate = import_snapshot(configured, configured.gym_import_file, FIRST)
    second = import_snapshot(configured, configured.gym_import_file, SECOND)
    assert first.sets_accepted == 1
    assert duplicate.duplicate_import
    assert second.messages_new == 1
    assert second.sets_accepted == 1
    assert count_rows(configured, WorkoutSet) == 2
    assert count_rows(configured, Import) == 2


def test_snapshot_is_not_reopened_during_import(configured):
    configured.gym_import_file.write_bytes(b"file changed after snapshot")
    report = import_snapshot(configured, configured.gym_import_file, FIRST)
    assert report.sets_accepted == 1


def test_automatic_import_never_calls_llm_and_keeps_reviews(configured, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("External LLM must never be called")

    monkeypatch.setattr("gym_tracker.services.llm_extractor.OpenAIWorkoutExtractor.extract", fail)
    configured.openai_api_key = "must-not-be-used"
    configured.llm_shadow_mode = False
    configured.llm_auto_accept = True
    report = import_snapshot(
        configured, configured.gym_import_file, b"01/01/2026 10:00 - Pessoa: Movimento X 10kg/10rep"
    )
    assert report.messages_pending_review == 1
    assert report.llm_proposals == 0
    assert count_rows(configured, WorkoutSet) == 0
    assert count_rows(configured, ParseReview) == 1


def test_multiple_users_require_explicit_selection(configured):
    engine = create_engine(configured.database_url)
    with Session(engine) as session:
        user = User(display_name="Outro")
        session.add(user)
        session.commit()
        selected = user.id
    engine.dispose()
    with pytest.raises(ImportInputError, match="GYM_TRACKER_USER_ID"):
        import_snapshot(configured, configured.gym_import_file, FIRST)
    assert count_rows(configured, Import) == 0
    configured.gym_tracker_user_id = selected
    assert import_snapshot(configured, configured.gym_import_file, FIRST).sets_accepted == 1


def test_invalid_user_is_not_created(configured):
    configured.gym_tracker_user_id = uuid.uuid4()
    with pytest.raises(ImportInputError, match="usuario existente"):
        import_snapshot(configured, configured.gym_import_file, FIRST)
    assert count_rows(configured, User) == 1


def test_import_failure_rolls_back_and_can_retry(configured, monkeypatch):
    from gym_tracker.services import automatic_import

    real_import = automatic_import.import_whatsapp_file

    def broken(*args, **kwargs):
        real_import(*args, **kwargs)
        raise RuntimeError("private raw content")

    monkeypatch.setattr(automatic_import, "import_whatsapp_file", broken)
    with pytest.raises(RuntimeError):
        import_snapshot(configured, configured.gym_import_file, FIRST)
    assert count_rows(configured, Import) == 0
    assert count_rows(configured, WorkoutSet) == 0
    monkeypatch.setattr(automatic_import, "import_whatsapp_file", real_import)
    assert import_snapshot(configured, configured.gym_import_file, FIRST).sets_accepted == 1


def test_new_parser_version_keeps_only_one_active_result(configured):
    import_snapshot(configured, configured.gym_import_file, FIRST)
    configured.parser_version = "auto-test-v2"
    report = import_snapshot(configured, configured.gym_import_file, FIRST)
    assert report.reprocessed
    engine = create_engine(configured.database_url)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active)) == 1
    engine.dispose()


def test_os_lock_releases_without_deleting_lockfile(tmp_path):
    lock = tmp_path / "worker.lock"
    assert not is_running(tmp_path)
    with ProcessLock(lock):
        assert is_running(tmp_path)
        with pytest.raises(AlreadyRunning), ProcessLock(lock):
            pass
    assert lock.exists()
    assert not is_running(tmp_path)
    with ProcessLock(lock):
        assert is_running(tmp_path)


def test_stop_request_is_bound_to_current_run(tmp_path):
    with ProcessLock(tmp_path / "worker.lock"):
        write_json(tmp_path / "status.json", {"run_id": "new"})
        request_stop(tmp_path)
        assert read_json(tmp_path / "stop.json") == {"run_id": "new"}
    assert request_stop(tmp_path)["running"] is False


def test_worker_reports_safe_error_and_recovers(configured, tmp_path, monkeypatch):
    from gym_tracker import automation

    worker = ImportWorker(configured, tmp_path / "runtime", "worker")
    monkeypatch.setattr(worker, "wait", lambda _: None)
    real_import = automation.import_snapshot
    try:
        monkeypatch.setattr(automation, "import_snapshot", lambda *_: (_ for _ in ()).throw(RuntimeError("SECRET")))
        worker.cycle()
        assert status(worker.runtime)["phase"] == "error"
        assert "SECRET" not in (worker.runtime / "status.json").read_text()
        assert "SECRET" not in (worker.runtime / "events.log").read_text()
        monkeypatch.setattr(automation, "import_snapshot", real_import)
        worker.cycle()
        assert status(worker.runtime)["phase"] == "imported"
        imported_at = status(worker.runtime)["last_import_at"]
        worker.cycle()
        assert status(worker.runtime)["phase"] == "unchanged"
        assert status(worker.runtime)["last_import_at"] == imported_at
        assert "Pessoa" not in (worker.runtime / "events.log").read_text()
    finally:
        worker.close()


def test_missing_file_waits_without_writing_database(configured, tmp_path):
    configured.gym_import_file = tmp_path / "missing.txt"
    worker = ImportWorker(configured, tmp_path / "runtime", "worker")
    try:
        worker.cycle()
        assert status(worker.runtime)["phase"] == "waiting"
        assert count_rows(configured, Import) == 0
    finally:
        worker.close()


def test_stop_during_settle_does_not_import(configured, tmp_path, monkeypatch):
    worker = ImportWorker(configured, tmp_path / "runtime", "new")
    monkeypatch.setattr(worker, "wait", lambda _: write_json(worker.runtime / "stop.json", {"run_id": "new"}))
    try:
        worker.cycle()
        assert count_rows(configured, Import) == 0
    finally:
        worker.close()


def test_stale_stop_does_not_stop_new_run(configured, tmp_path):
    worker = ImportWorker(configured, tmp_path / "runtime", "new")
    try:
        write_json(worker.runtime / "stop.json", {"run_id": "old"})
        assert not worker.stopping()
    finally:
        worker.close()


def test_once_records_completion_and_releases_lock(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(ImportWorker, "wait", lambda *_: None)
    watch(configured, tmp_path / "runtime", "once", once=True)
    result = status(tmp_path / "runtime")
    assert result["phase"] == "stopped"
    assert result["last_cycle_phase"] == "imported"
    assert result["running"] is False


def test_empty_user_configuration_is_allowed():
    assert Settings(gym_tracker_user_id="").gym_tracker_user_id is None


def test_status_does_not_trust_stale_pid(tmp_path):
    write_json(tmp_path / "status.json", {"pid": 1, "phase": "imported"})
    assert status(tmp_path)["running"] is False
    assert json.loads((tmp_path / "status.json").read_text())["pid"] == 1


def test_background_process_imports_changes_and_stops(configured, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", configured.database_url)
    monkeypatch.setenv("GYM_IMPORT_FILE", str(configured.gym_import_file))
    monkeypatch.setenv("GYM_TRACKER_USER_ID", "")
    monkeypatch.setenv("GYM_IMPORT_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("GYM_IMPORT_SETTLE_SECONDS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("PARSER_VERSION", "background-test")
    monkeypatch.setenv("GYM_BACKUP_DIRECTORY", str(tmp_path / "backups"))
    runtime = tmp_path / "background"

    def wait_until(predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        pytest.fail(f"Monitor did not reach expected state: {status(runtime)}")

    try:
        first = start(runtime)
        assert first["running"]
        assert start(runtime)["run_id"] == first["run_id"]
        wait_until(lambda: status(runtime).get("phase") == "imported")
        assert count_rows(configured, WorkoutSet) == 1
        # A malformed replacement must not destroy the previous accepted data.
        configured.gym_import_file.write_bytes(b"invalid export")
        wait_until(lambda: status(runtime).get("phase") == "error")
        assert count_rows(configured, WorkoutSet) == 1
        configured.gym_import_file.write_bytes(SECOND)
        wait_until(lambda: status(runtime).get("phase") == "imported")
        assert count_rows(configured, WorkoutSet) == 2
        request_stop(runtime)
        wait_until(lambda: not is_running(runtime), timeout=5)
        second = start(runtime)
        assert second["run_id"] != first["run_id"]
        wait_until(lambda: status(runtime).get("phase") == "unchanged")
        assert count_rows(configured, WorkoutSet) == 2
    finally:
        request_stop(runtime)
        wait_until(lambda: not is_running(runtime), timeout=5)


def test_env_user_is_loaded_from_dotenv(tmp_path):
    user_id = uuid.uuid4()
    env = tmp_path / ".env"
    env.write_text(f"GYM_TRACKER_USER_ID={user_id}\n", encoding="utf-8")
    assert Settings(_env_file=env).gym_tracker_user_id == user_id


def test_dashboard_detects_committed_import_without_restarting(configured, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from gym_tracker.config import get_settings

    monkeypatch.setenv("DATABASE_URL", configured.database_url)
    monkeypatch.setenv("DATA_BACKEND", "postgres")
    monkeypatch.setenv("GYM_TRACKER_USER_ID", "")
    get_settings.cache_clear()
    try:
        import_snapshot(configured, configured.gym_import_file, FIRST)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        assert not app.exception
        first_signature = app.session_state["gym_data_signature"]
        import_snapshot(configured, configured.gym_import_file, SECOND)
        # The timer invokes the same fragment on an active browser session.
        app.run(timeout=30)
        assert not app.exception
        assert app.session_state["gym_data_signature"] != first_signature
    finally:
        get_settings.cache_clear()
