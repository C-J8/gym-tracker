import json
import sqlite3
import subprocess

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from gym_tracker.backup import BackupError, backup_database, postgres_dump_command
from gym_tracker.config import Settings
from gym_tracker.models import Base, Import, User, WorkoutSet
from gym_tracker.services.automatic_import import ImportInputError, import_snapshot

CONTENT = b"01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "test.sqlite"
    engine = create_engine(f"sqlite:///{source.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(display_name="Synthetic backup test"))
        session.commit()
    engine.dispose()
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{source.as_posix()}",
        gym_backup_directory=tmp_path / "backups",
        gym_tracker_user_id=None,
        openai_api_key=None,
    )


def count(url, model):
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            return session.scalar(select(func.count()).select_from(model))
    finally:
        engine.dispose()


def test_new_import_and_new_parser_back_up_before_writing(settings, tmp_path):
    source = tmp_path / "source.txt"
    import_snapshot(settings, source, CONTENT)
    backups = list(settings.gym_backup_directory.glob("*.sqlite"))
    assert len(backups) == 1
    saved_url = f"sqlite:///{backups[0].as_posix()}"
    assert count(saved_url, User) == 1
    assert count(saved_url, Import) == 0
    assert count(saved_url, WorkoutSet) == 0
    assert count(settings.database_url, WorkoutSet) == 1
    assert import_snapshot(settings, source, CONTENT).duplicate_import
    assert len(list(settings.gym_backup_directory.glob("*.sqlite"))) == 1
    settings.parser_version = "new-version"
    import_snapshot(settings, source, CONTENT)
    assert len(list(settings.gym_backup_directory.glob("*.sqlite"))) == 2


def test_failed_backup_blocks_import_and_can_retry(settings, tmp_path, monkeypatch):
    from gym_tracker.services import automatic_import

    original = automatic_import.backup_database

    def fail(*_):
        raise BackupError("Backup falhou. Nenhum dado sera importado.")

    monkeypatch.setattr(automatic_import, "backup_database", fail)
    with pytest.raises(ImportInputError, match="Backup falhou"):
        import_snapshot(settings, tmp_path / "source.txt", CONTENT)
    assert count(settings.database_url, Import) == 0
    assert count(settings.database_url, WorkoutSet) == 0
    monkeypatch.setattr(automatic_import, "backup_database", original)
    assert import_snapshot(settings, tmp_path / "source.txt", CONTENT).sets_accepted == 1


def test_sqlite_backup_includes_committed_wal_and_preserves_older_backups(settings):
    source = settings.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE wal_probe(value TEXT)")
        connection.execute("INSERT INTO wal_probe VALUES ('committed')")
        connection.commit()
        first = backup_database(settings)
        second = backup_database(settings)
        assert first != second
        with sqlite3.connect(first) as copy:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert copy.execute("SELECT value FROM wal_probe").fetchone()[0] == "committed"
    assert first.exists()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://user:secret@remote:5434/gym",
        "postgresql+psycopg://user:secret@localhost:5434/gym?host=remote",
        "postgresql+psycopg://user:secret@localhost:5434/--unsafe",
    ],
)
def test_postgres_backup_rejects_remote_and_ambiguous_targets(url):
    with pytest.raises(BackupError, match="Backup bloqueado"):
        postgres_dump_command(Settings(_env_file=None, database_url=url))


def test_container_must_match_the_database_port(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}).encode(),
        ),
    )
    settings = Settings(
        _env_file=None, gym_backup_container="test-db", database_url="postgresql://user:secret@localhost:5434/gym"
    )
    with pytest.raises(BackupError, match="porta local"):
        postgres_dump_command(settings)


def test_dump_command_does_not_expose_credentials(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5434"}]}).encode(),
        ),
    )
    settings = Settings(
        _env_file=None, gym_backup_container="test-db", database_url="postgresql://user:secret@localhost:5434/gym"
    )
    command, env = postgres_dump_command(settings)
    assert command[:4] == ["docker", "exec", "test-db", "pg_dump"]
    assert "secret" not in " ".join(command)
    assert "PGPASSWORD" not in env


def test_failed_dump_never_publishes_final_backup(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("gym_tracker.backup.postgres_dump_command", lambda _: (["docker", "exec"], {}))

    def broken(*_args, **kwargs):
        kwargs["stdout"].write(b"partial")
        raise subprocess.CalledProcessError(1, "docker", stderr=b"SECRET")

    monkeypatch.setattr(subprocess, "run", broken)
    settings.database_url = "postgresql://user:SECRET@localhost:5434/gym"
    with pytest.raises(BackupError) as error:
        backup_database(settings, tmp_path / "dumps")
    assert "SECRET" not in str(error.value)
    assert list((tmp_path / "dumps").glob("*.partial"))
    assert not list((tmp_path / "dumps").glob("*.dump"))
