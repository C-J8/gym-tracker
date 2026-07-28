import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gym_tracker.config import Settings, get_settings
from gym_tracker.models import Import, ImportMessageOccurrence, ParseResult, ParseRun, RawMessage, User, WorkoutSet
from gym_tracker.repositories.dashboard import DashboardRepository
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


def _downgrade(database_url: str, revision: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    command.downgrade(Config("alembic.ini"), revision)


def _seed_overlapping_exports_at_0001(database_url: str) -> dict[str, uuid.UUID]:
    names = (
        "user",
        "import_a",
        "import_b",
        "raw_a",
        "raw_b_duplicate",
        "raw_b_new",
        "workout_a",
        "workout_b_duplicate",
        "workout_b_new",
        "exercise_old",
        "exercise_new",
        "variant_old",
        "variant_new",
        "set_a",
        "set_b_duplicate",
        "set_b_new",
        "review",
    )
    ids = {name: uuid.uuid4() for name in names}
    engine = create_engine(database_url)
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
                VALUES
                    (:a, :user_id, 'export-a.txt', :hash_a, 'v1', 'completed', CAST('{}' AS jsonb)),
                    (:b, :user_id, 'export-b.txt', :hash_b, 'v1', 'completed', CAST('{}' AS jsonb))
                """
            ),
            {
                "a": ids["import_a"],
                "b": ids["import_b"],
                "user_id": ids["user"],
                "hash_a": "a" * 64,
                "hash_b": "b" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO raw_messages
                    (id, import_id, source_index, sent_at, sender_raw, raw_content, content_sha256,
                     is_edited, is_deleted, parse_status)
                VALUES
                    (:raw_a, :import_a, 0, '2026-01-01 10:00:00-03', 'Pessoa',
                     'Extensora 40kg/10rep', :old_hash_a, false, false, 'accepted'),
                    (:raw_b_duplicate, :import_b, 0, '2026-01-01 10:00:00-03', 'Pessoa',
                     'Extensora 40kg/10rep', :old_hash_b, false, false, 'accepted'),
                    (:raw_b_new, :import_b, 1, '2026-01-02 10:00:00-03', 'Pessoa',
                     'Flexora 30kg/8rep', :new_hash, false, false, 'accepted')
                """
            ),
            {
                "raw_a": ids["raw_a"],
                "raw_b_duplicate": ids["raw_b_duplicate"],
                "raw_b_new": ids["raw_b_new"],
                "import_a": ids["import_a"],
                "import_b": ids["import_b"],
                "old_hash_a": "c" * 64,
                "old_hash_b": "d" * 64,
                "new_hash": "e" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO workouts (id, user_id, workout_date, source)
                VALUES
                    (:a, :user_id, '2026-01-01', 'whatsapp'),
                    (:b_duplicate, :user_id, '2026-01-01', 'whatsapp'),
                    (:b_new, :user_id, '2026-01-02', 'whatsapp')
                """
            ),
            {
                "a": ids["workout_a"],
                "b_duplicate": ids["workout_b_duplicate"],
                "b_new": ids["workout_b_new"],
                "user_id": ids["user"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO exercises (id, canonical_name, muscle_group)
                VALUES
                    (:old, 'Extensora', 'Pernas'),
                    (:new, 'Flexora', 'Pernas')
                """
            ),
            {"old": ids["exercise_old"], "new": ids["exercise_new"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO exercise_variants (id, exercise_id, equipment, gym_or_location, load_basis)
                VALUES
                    (:old, :exercise_old, 'Máquina', '', 'total'),
                    (:new, :exercise_new, 'Máquina', '', 'total')
                """
            ),
            {
                "old": ids["variant_old"],
                "new": ids["variant_new"],
                "exercise_old": ids["exercise_old"],
                "exercise_new": ids["exercise_new"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO sets
                    (id, workout_id, exercise_variant_id, raw_message_id, set_number, weight_kg,
                     reps, parse_method, parser_version)
                VALUES
                    (:set_a, :workout_a, :variant_old, :raw_a, 1, 40, 10, 'rule', 'v1'),
                    (:set_b_duplicate, :workout_b_duplicate, :variant_old, :raw_b_duplicate,
                     1, 40, 10, 'rule', 'v1'),
                    (:set_b_new, :workout_b_new, :variant_new, :raw_b_new, 1, 30, 8, 'rule', 'v1')
                """
            ),
            {
                "set_a": ids["set_a"],
                "set_b_duplicate": ids["set_b_duplicate"],
                "set_b_new": ids["set_b_new"],
                "workout_a": ids["workout_a"],
                "workout_b_duplicate": ids["workout_b_duplicate"],
                "workout_b_new": ids["workout_b_new"],
                "variant_old": ids["variant_old"],
                "variant_new": ids["variant_new"],
                "raw_a": ids["raw_a"],
                "raw_b_duplicate": ids["raw_b_duplicate"],
                "raw_b_new": ids["raw_b_new"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO parse_reviews (id, raw_message_id, reason, status)
                VALUES (:id, :raw_message_id, 'revisão legada', 'pending')
                """
            ),
            {"id": ids["review"], "raw_message_id": ids["raw_b_duplicate"]},
        )
    engine.dispose()
    return ids


def _seed_repeated_occurrences_at_0001(database_url: str) -> dict[str, uuid.UUID]:
    ids = {
        name: uuid.uuid4()
        for name in (
            "user",
            "import_a",
            "import_b",
            "exercise_old",
            "exercise_new",
            "variant_old",
            "variant_new",
            "review",
        )
    }
    message_ids = [uuid.uuid4() for _ in range(5)]
    workout_ids = [uuid.uuid4() for _ in range(5)]
    set_ids = [uuid.uuid4() for _ in range(5)]
    ids.update({f"message_{index}": value for index, value in enumerate(message_ids)})
    engine = create_engine(database_url)
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
                VALUES
                    (:a, :user_id, 'export-a.txt', :hash_a, 'v1', 'completed', CAST('{}' AS jsonb)),
                    (:b, :user_id, 'export-b.txt', :hash_b, 'v1', 'completed', CAST('{}' AS jsonb))
                """
            ),
            {
                "a": ids["import_a"],
                "b": ids["import_b"],
                "user_id": ids["user"],
                "hash_a": "1" * 64,
                "hash_b": "2" * 64,
            },
        )
        raw_rows = [
            {
                "id": message_ids[0],
                "import_id": ids["import_a"],
                "source_index": 0,
                "sent_at": "2026-01-01 10:00:00-03",
                "content": "Extensora 40kg/10rep",
                "hash": "3" * 64,
            },
            {
                "id": message_ids[1],
                "import_id": ids["import_a"],
                "source_index": 1,
                "sent_at": "2026-01-01 10:00:00-03",
                "content": "Extensora   40kg/10rep",
                "hash": "4" * 64,
            },
            {
                "id": message_ids[2],
                "import_id": ids["import_b"],
                "source_index": 0,
                "sent_at": "2026-01-01 10:00:00-03",
                "content": "Extensora 40kg/10rep",
                "hash": "5" * 64,
            },
            {
                "id": message_ids[3],
                "import_id": ids["import_b"],
                "source_index": 1,
                "sent_at": "2026-01-01 10:00:00-03",
                "content": "Extensora  40kg/10rep",
                "hash": "6" * 64,
            },
            {
                "id": message_ids[4],
                "import_id": ids["import_b"],
                "source_index": 2,
                "sent_at": "2026-01-02 10:00:00-03",
                "content": "Flexora 30kg/8rep",
                "hash": "7" * 64,
            },
        ]
        connection.execute(
            text(
                """
                INSERT INTO raw_messages
                    (id, import_id, source_index, sent_at, sender_raw, raw_content, content_sha256,
                     is_edited, is_deleted, parse_status)
                VALUES
                    (:id, :import_id, :source_index, CAST(:sent_at AS timestamptz), 'Pessoa',
                     :content, :hash, false, false, 'accepted')
                """
            ),
            raw_rows,
        )
        connection.execute(
            text(
                """
                INSERT INTO exercises (id, canonical_name, muscle_group)
                VALUES (:old, 'Extensora', 'Pernas'), (:new, 'Flexora', 'Pernas')
                """
            ),
            {"old": ids["exercise_old"], "new": ids["exercise_new"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO exercise_variants (id, exercise_id, equipment, gym_or_location, load_basis)
                VALUES
                    (:old, :exercise_old, 'Máquina', '', 'total'),
                    (:new, :exercise_new, 'Máquina', '', 'total')
                """
            ),
            {
                "old": ids["variant_old"],
                "new": ids["variant_new"],
                "exercise_old": ids["exercise_old"],
                "exercise_new": ids["exercise_new"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO workouts (id, user_id, workout_date, source)
                VALUES (:id, :user_id, CAST(:workout_date AS date), 'whatsapp')
                """
            ),
            [
                {
                    "id": workout_ids[index],
                    "user_id": ids["user"],
                    "workout_date": "2026-01-02" if index == 4 else "2026-01-01",
                }
                for index in range(5)
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO sets
                    (id, workout_id, exercise_variant_id, raw_message_id, set_number, weight_kg,
                     reps, parse_method, parser_version)
                VALUES
                    (:id, :workout_id, :variant_id, :raw_message_id, 1, :weight, :reps, 'rule', 'v1')
                """
            ),
            [
                {
                    "id": set_ids[index],
                    "workout_id": workout_ids[index],
                    "variant_id": ids["variant_new"] if index == 4 else ids["variant_old"],
                    "raw_message_id": message_ids[index],
                    "weight": 30 if index == 4 else 40,
                    "reps": 8 if index == 4 else 10,
                }
                for index in range(5)
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO parse_reviews (id, raw_message_id, reason, status)
                VALUES (:id, :raw_message_id, 'revisão legada', 'pending')
                """
            ),
            {"id": ids["review"], "raw_message_id": message_ids[3]},
        )
    engine.dispose()
    return ids


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


def test_postgres_upgrade_and_downgrade_overlapping_0001_exports(monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "0001", monkeypatch)
    ids = _seed_overlapping_exports_at_0001(TEST_DATABASE_URL)

    _migrate(TEST_DATABASE_URL, "head", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Import)) == 2
        assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
        assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 3
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 2
        assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active.is_(True))) == 2
        old_message = session.scalar(select(RawMessage).where(RawMessage.raw_content == "Extensora 40kg/10rep"))
        assert old_message is not None
        assert {item.import_id for item in old_message.occurrences} == {ids["import_a"], ids["import_b"]}
        assert len(DashboardRepository(session).workout_dataframe(ids["user"])) == 2
        review_link = session.execute(
            text("SELECT raw_message_id, parse_result_id FROM parse_reviews WHERE id = :id"),
            {"id": ids["review"]},
        ).one()
        assert review_link.raw_message_id == old_message.id
        assert review_link.parse_result_id is not None
    engine.dispose()

    _downgrade(TEST_DATABASE_URL, "0001", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        assert connection.scalar(text("SELECT count(*) FROM raw_messages")) == 2
        assert connection.scalar(text("SELECT count(*) FROM sets")) == 2
        assert connection.scalar(text("SELECT count(*) FROM parse_reviews")) == 1
    engine.dispose()
    get_settings.cache_clear()


def test_postgres_migrates_repeated_occurrences_and_runtime_reuses_them(tmp_path: Path, monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "0001", monkeypatch)
    ids = _seed_repeated_occurrences_at_0001(TEST_DATABASE_URL)

    _migrate(TEST_DATABASE_URL, "head", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Import)) == 2
        assert session.scalar(select(func.count()).select_from(RawMessage)) == 3
        assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 5
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3
        assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active.is_(True))) == 3
        assert len(DashboardRepository(session).workout_dataframe(ids["user"])) == 3
        occurrences = list(
            session.scalars(
                select(ImportMessageOccurrence).order_by(
                    ImportMessageOccurrence.import_id,
                    ImportMessageOccurrence.source_index,
                )
            )
        )
        assert sorted(item.occurrence_ordinal for item in occurrences if item.source_index < 2) == [0, 0, 1, 1]
        review_link = session.execute(
            text("SELECT raw_message_id, parse_result_id FROM parse_reviews WHERE id = :id"),
            {"id": ids["review"]},
        ).one()
        assert review_link.parse_result_id is not None

        export_c = tmp_path / "export-c.txt"
        export_c.write_text(
            "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep\n"
            "01/01/2026 10:00 - Pessoa: Extensora   40kg/10rep\n"
            "02/01/2026 10:00 - Pessoa: Flexora 30kg/8rep",
            encoding="utf-8",
        )
        report = import_whatsapp_file(session, export_c, ids["user"], settings=Settings(parser_version="v1"))
        session.commit()
        assert report.messages_new == 0
        assert report.messages_reused == 3
        assert session.scalar(select(func.count()).select_from(RawMessage)) == 3
        assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 8
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3
    engine.dispose()

    _downgrade(TEST_DATABASE_URL, "0001", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        assert connection.scalar(text("SELECT count(*) FROM raw_messages")) == 3
        assert connection.scalar(text("SELECT count(*) FROM sets")) == 3
    engine.dispose()
    get_settings.cache_clear()


def test_postgres_legacy_identity_remains_isolated_per_user(monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "0001", monkeypatch)
    ids = _seed_overlapping_exports_at_0001(TEST_DATABASE_URL)
    second = {name: uuid.uuid4() for name in ("user", "import", "raw", "workout", "set")}
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, display_name) VALUES (:id, 'Outra pessoa')"),
            {"id": second["user"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO imports
                    (id, user_id, source_filename, source_sha256, parser_version, status, metadata)
                VALUES (:id, :user_id, 'other.txt', :hash, 'v1', 'completed', CAST('{}' AS jsonb))
                """
            ),
            {"id": second["import"], "user_id": second["user"], "hash": "8" * 64},
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
            {"id": second["raw"], "import_id": second["import"], "hash": "9" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO workouts (id, user_id, workout_date, source)
                VALUES (:id, :user_id, '2026-01-01', 'whatsapp')
                """
            ),
            {"id": second["workout"], "user_id": second["user"]},
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
                "id": second["set"],
                "workout_id": second["workout"],
                "variant_id": ids["variant_old"],
                "raw_id": second["raw"],
            },
        )
    engine.dispose()

    _migrate(TEST_DATABASE_URL, "head", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with Session(engine) as session:
        messages = list(session.scalars(select(RawMessage).where(RawMessage.raw_content == "Extensora 40kg/10rep")))
        assert len(messages) == 2
        assert {item.user_id for item in messages} == {ids["user"], second["user"]}
        assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active.is_(True))) == 3
    engine.dispose()
    get_settings.cache_clear()


def test_postgres_downgrade_preserves_legitimate_identical_messages(tmp_path: Path, monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "head", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    source = tmp_path / "identical.txt"
    line = "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"
    source.write_text(f"{line}\n{line}", encoding="utf-8")
    with Session(engine) as session:
        user = User(display_name="Pessoa de teste")
        session.add(user)
        session.commit()
        report = import_whatsapp_file(session, source, user.id, settings=Settings(parser_version="pg-v1"))
        session.commit()
        assert report.sets_accepted == 2
        assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
        assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 2
    engine.dispose()

    _downgrade(TEST_DATABASE_URL, "0001", monkeypatch)
    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        assert connection.scalar(text("SELECT count(*) FROM raw_messages")) == 2
        assert connection.scalar(text("SELECT count(DISTINCT content_sha256) FROM raw_messages")) == 2
        assert connection.scalar(text("SELECT count(*) FROM sets")) == 2
    engine.dispose()
    get_settings.cache_clear()


def test_postgres_failed_upgrade_rolls_back_completely(monkeypatch) -> None:
    assert TEST_DATABASE_URL is not None
    _reset_test_database(TEST_DATABASE_URL)
    _migrate(TEST_DATABASE_URL, "0001", monkeypatch)
    _seed_overlapping_exports_at_0001(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("UPDATE sets SET weight_kg = 600 WHERE id = (SELECT id FROM sets LIMIT 1)"))
    engine.dispose()

    with pytest.raises(IntegrityError):
        _migrate(TEST_DATABASE_URL, "head", monkeypatch)

    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        assert (
            connection.scalar(
                text(
                    """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'raw_messages' AND column_name = 'identity_sha256'
                """
                )
            )
            == 0
        )
        assert connection.scalar(text("SELECT count(*) FROM raw_messages")) == 3
        assert connection.scalar(text("SELECT count(*) FROM sets")) == 3
    engine.dispose()
    get_settings.cache_clear()
