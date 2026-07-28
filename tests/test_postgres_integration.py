import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from gym_tracker.config import Settings, get_settings
from gym_tracker.models import Import, ImportMessageOccurrence, ParseResult, ParseRun, RawMessage, User, WorkoutSet
from gym_tracker.services.whatsapp_import import import_whatsapp_file

TEST_DATABASE_URL = os.getenv("GYM_TRACKER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="GYM_TRACKER_TEST_DATABASE_URL nao configurada")


def _reset_test_database(database_url: str) -> None:
    if not database_url.rsplit("/", 1)[-1].endswith("_test"):
        raise RuntimeError("testes PostgreSQL exigem um banco cujo nome termine em _test")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _migrate(database_url: str, revision: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), revision)


def test_postgres_overlapping_exports_and_versioned_results(tmp_path: Path, monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "head", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    old = "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"
    export_a = tmp_path / "a.txt"
    export_b = tmp_path / "b.txt"
    export_a.write_text(old, encoding="utf-8")
    export_b.write_text(f"{old}\n02/01/2026 10:00 - Pessoa: Flexora 30kg/8rep", encoding="utf-8")

    with Session(engine) as session:
        user = User(display_name="Pessoa de teste")
        session.add(user)
        session.commit()
        first = import_whatsapp_file(session, export_a, user.id, settings=Settings(parser_version="pg-v1"))
        session.commit()
        second = import_whatsapp_file(session, export_b, user.id, settings=Settings(parser_version="pg-v1"))
        session.commit()

        assert (first.sets_accepted, second.sets_accepted) == (1, 1)
        assert session.scalar(select(func.count()).select_from(Import)) == 2
        assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
        assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 3
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 2
        reused = session.scalar(select(RawMessage).where(RawMessage.raw_content == "Extensora 40kg/10rep"))
        assert reused is not None and len(reused.occurrences) == 2

        reprocessed = import_whatsapp_file(session, export_a, user.id, settings=Settings(parser_version="pg-v2"))
        session.commit()
        assert reprocessed.reprocessed is True
        assert session.scalar(select(func.count()).select_from(ParseRun)) == 3
        assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active.is_(True))) == 2
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3

    engine.dispose()
    get_settings.cache_clear()


def test_postgres_upgrade_from_0001_with_existing_data(monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "0001", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    ids = {name: uuid.uuid4() for name in ("user", "import", "raw", "workout", "exercise", "variant", "set")}
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, display_name) VALUES (:id, 'Pessoa')"),
            {"id": ids["user"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO imports
                    (id, user_id, source_filename, source_sha256, parser_version, status, metadata)
                VALUES (:id, :user_id, 'old.txt', :hash, 'v1', 'completed', CAST('{}' AS jsonb))
                """
            ),
            {"id": ids["import"], "user_id": ids["user"], "hash": "a" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO raw_messages
                    (id, import_id, source_index, sent_at, sender_raw, raw_content, content_sha256,
                     is_edited, is_deleted, parse_status)
                VALUES (:id, :import_id, 0, '2026-01-01 10:00:00-03', 'Pessoa',
                        'Extensora 40kg/10rep', :hash, false, false, 'accepted')
                """
            ),
            {"id": ids["raw"], "import_id": ids["import"], "hash": "b" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO workouts (id, user_id, workout_date, source)
                VALUES (:id, :user_id, '2026-01-01', 'whatsapp')
                """
            ),
            {"id": ids["workout"], "user_id": ids["user"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO exercises (id, canonical_name, muscle_group)
                VALUES (:id, 'Extensora', 'Pernas')
                """
            ),
            {"id": ids["exercise"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO exercise_variants (id, exercise_id, equipment, gym_or_location, load_basis)
                VALUES (:id, :exercise_id, 'Máquina', '', 'total')
                """
            ),
            {"id": ids["variant"], "exercise_id": ids["exercise"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO sets
                    (id, workout_id, exercise_variant_id, raw_message_id, set_number, weight_kg,
                     reps, parse_method, parser_version)
                VALUES (:id, :workout_id, :variant_id, :raw_id, 1, 40, 10, 'rule', 'v1')
                """
            ),
            {
                "id": ids["set"],
                "workout_id": ids["workout"],
                "variant_id": ids["variant"],
                "raw_id": ids["raw"],
            },
        )
    engine.dispose()

    _migrate(TEST_DATABASE_URL, "head", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM import_message_occurrences")) == 1
        assert connection.scalar(text("SELECT count(*) FROM parse_results WHERE is_active")) == 1
        assert connection.scalar(text("SELECT count(*) FROM sets WHERE parse_result_id IS NOT NULL")) == 1
    engine.dispose()
    get_settings.cache_clear()
