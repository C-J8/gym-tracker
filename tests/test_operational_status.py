import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from gym_tracker.config import Settings
from gym_tracker.models import Base, ParseReview, User
from gym_tracker.operational_status import last_automatic_backup, operational_database_status
from gym_tracker.services.automatic_import import import_snapshot


@pytest.fixture
def configured(tmp_path):
    url = f"sqlite:///{(tmp_path / 'status.sqlite').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(display_name="SECRET person")
        session.add(user)
        session.commit()
        user_id = user.id
    engine.dispose()
    return Settings(
        _env_file=None,
        database_url=url,
        gym_tracker_user_id=user_id,
        gym_backup_directory=tmp_path / "backups",
        openai_api_key=None,
    )


def test_status_reads_actual_pending_reviews_after_human_update(configured, tmp_path):
    import_snapshot(configured, tmp_path / "input.txt", b"01/01/2026 10:00 - SECRET: Movimento X 10kg/10rep")
    assert operational_database_status(configured) == {"database": "connected", "pending_reviews": 1}
    engine = create_engine(configured.database_url)
    with Session(engine) as session:
        review = session.scalar(select(ParseReview))
        review.status = "rejected"
        session.commit()
    assert operational_database_status(configured) == {"database": "connected", "pending_reviews": 0}
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ParseReview)) == 1
    engine.dispose()


def test_status_uses_a_readonly_sqlite_connection(configured, monkeypatch):
    from gym_tracker import operational_status

    engines = []
    original = operational_status.create_engine

    def record(*args, **kwargs):
        engine = original(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(operational_status, "create_engine", record)
    assert operational_database_status(configured)["database"] == "connected"
    with pytest.raises(OperationalError, match="readonly"), engines[0].begin() as connection:
        connection.exec_driver_sql("INSERT INTO users(id, display_name) VALUES ('bad', 'must not be written')")


def test_user_selection_is_required_and_does_not_create_users(configured):
    engine = create_engine(configured.database_url)
    with Session(engine) as session:
        session.add(User(display_name="Another user"))
        session.commit()
    engine.dispose()
    configured.gym_tracker_user_id = None
    assert operational_database_status(configured) == {"database": "user_required", "pending_reviews": None}
    configured.gym_tracker_user_id = uuid.uuid4()
    assert operational_database_status(configured)["database"] == "user_required"


def test_pending_reviews_are_scoped_to_selected_user(configured, tmp_path):
    engine = create_engine(configured.database_url)
    with Session(engine) as session:
        other = User(display_name="Other")
        session.add(other)
        session.commit()
        other_id = other.id
    engine.dispose()
    other_settings = configured.model_copy(update={"gym_tracker_user_id": other_id})
    import_snapshot(other_settings, tmp_path / "input.txt", b"01/01/2026 10:00 - Other: Movimento X 10kg/10rep")
    assert operational_database_status(configured)["pending_reviews"] == 0
    assert operational_database_status(other_settings)["pending_reviews"] == 1


def test_missing_database_is_not_created_and_errors_are_private(tmp_path):
    missing = tmp_path / "missing.sqlite"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{missing.as_posix()}")
    assert operational_database_status(settings) == {"database": "unavailable", "pending_reviews": None}
    assert not missing.exists()


def test_only_completed_nonempty_backups_are_reported(configured):
    directory = configured.gym_backup_directory
    assert last_automatic_backup(configured) is None
    directory.mkdir()
    (directory / "gym-test.dump.partial").write_bytes(b"partial")
    (directory / "gym-empty.dump").write_bytes(b"")
    assert last_automatic_backup(configured) is None
    (directory / "gym-complete.dump").write_bytes(b"archive")
    assert last_automatic_backup(configured) is not None
