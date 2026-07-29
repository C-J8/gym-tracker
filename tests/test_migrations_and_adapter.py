import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gym_tracker.config import Settings, get_settings
from gym_tracker.data_adapter import DataBackendError, data_signature, load_dashboard_data
from gym_tracker.models import Base, User
from gym_tracker.repositories.state import bump_data_revision
from gym_tracker.services.whatsapp_import import import_whatsapp_file


def test_migrations_apply_from_zero(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert {
        "users",
        "imports",
        "raw_messages",
        "import_message_occurrences",
        "parse_runs",
        "parse_results",
        "workouts",
        "exercises",
        "sets",
        "parse_reviews",
        "data_revisions",
    } <= tables
    assert revision == "0002"
    get_settings.cache_clear()


def test_upgrade_from_previous_schema_preserves_and_activates_existing_data(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "upgrade.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "0001")
    ids = {
        name: f"{index:032x}"
        for index, name in enumerate(("user", "import", "raw", "workout", "exercise", "variant", "set"), 1)
    }
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO users (id, display_name) VALUES (?, ?)", (ids["user"], "Pessoa"))
        connection.execute(
            """
            INSERT INTO imports (id, user_id, source_filename, source_sha256, parser_version, status, metadata)
            VALUES (?, ?, 'old.txt', ?, 'v1', 'completed', '{}')
            """,
            (ids["import"], ids["user"], "a" * 64),
        )
        connection.execute(
            """
            INSERT INTO raw_messages
                (id, import_id, source_index, sent_at, sender_raw, raw_content, content_sha256,
                 is_edited, is_deleted, parse_status)
            VALUES (?, ?, 0, '2026-01-01 10:00:00', 'Pessoa', 'Extensora 40kg/10rep', ?,
                    0, 0, 'accepted')
            """,
            (ids["raw"], ids["import"], "b" * 64),
        )
        connection.execute(
            "INSERT INTO workouts (id, user_id, workout_date, source) VALUES (?, ?, '2026-01-01', 'whatsapp')",
            (ids["workout"], ids["user"]),
        )
        connection.execute(
            "INSERT INTO exercises (id, canonical_name, muscle_group) VALUES (?, 'Extensora', 'Pernas')",
            (ids["exercise"],),
        )
        connection.execute(
            """
            INSERT INTO exercise_variants (id, exercise_id, equipment, gym_or_location, load_basis)
            VALUES (?, ?, 'Máquina', '', 'total')
            """,
            (ids["variant"], ids["exercise"]),
        )
        connection.execute(
            """
            INSERT INTO sets
                (id, workout_id, exercise_variant_id, raw_message_id, set_number, weight_kg, reps,
                 parse_method, parser_version)
            VALUES (?, ?, ?, ?, 1, 40, 10, 'rule', 'v1')
            """,
            (ids["set"], ids["workout"], ids["variant"], ids["raw"]),
        )
        connection.commit()

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM import_message_occurrences").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM parse_runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM parse_results WHERE is_active = 1").fetchone()[0] == 1
        assert connection.execute("SELECT parse_result_id IS NOT NULL FROM sets").fetchone()[0] == 1
    get_settings.cache_clear()


def test_csv_adapter_cannot_feed_dashboard(tmp_path: Path) -> None:
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text("data,grupo_muscular\n2026-01-01,Peito\n", encoding="utf-8")

    with pytest.raises(DataBackendError, match="backend CSV foi desativado"):
        load_dashboard_data(Settings(data_backend="csv", legacy_csv_path=csv_path))


def test_postgres_signature_changes_with_data_revision(tmp_path: Path) -> None:
    database = tmp_path / "signature.sqlite"
    url = f"sqlite:///{database.as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=url, data_backend="postgres")
    first = data_signature(settings)
    with Session(engine) as session:
        bump_data_revision(session)
        session.commit()
    second = data_signature(settings)
    assert first != second


def test_import_changes_dashboard_signature_without_restart(tmp_path: Path) -> None:
    database = tmp_path / "import-signature.sqlite"
    url = f"sqlite:///{database.as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=url, data_backend="postgres", parser_version="cache-test")
    source = tmp_path / "cache.txt"
    source.write_text("01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep", encoding="utf-8")
    with Session(engine) as session:
        user = User(display_name="Pessoa")
        session.add(user)
        session.commit()
        before = data_signature(settings)
        import_whatsapp_file(session, source, user.id, settings=settings)
        session.commit()
        after = data_signature(settings)
    assert before != after
    assert len(load_dashboard_data(settings)) == 1


def test_streamlit_app_starts_without_csv_backend(tmp_path: Path, monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    database = tmp_path / "streamlit.sqlite"
    Base.metadata.create_all(create_engine(f"sqlite:///{database.as_posix()}"))
    monkeypatch.setenv("DATA_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    app = AppTest.from_file("app.py").run(timeout=30)
    assert not app.exception
    get_settings.cache_clear()
