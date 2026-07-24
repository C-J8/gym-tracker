from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gym_tracker.config import Settings
from gym_tracker.models import (
    Import,
    ParseReview,
    RawMessage,
    User,
    Workout,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import (
    get_or_create_exercise,
    get_or_create_variant,
    save_alias,
)
from gym_tracker.schemas import ExercisePayload, SetPayload, WorkoutExtraction
from gym_tracker.services.llm_extractor import MockWorkoutExtractor
from gym_tracker.services.parser import parse_workout_message, split_whatsapp_messages
from gym_tracker.services.reviews import accept_review, reject_review
from gym_tracker.services.whatsapp_import import _conflicting_message_indexes, import_whatsapp_file

FIXTURE = Path("tests/fixtures/whatsapp_anonymized.txt")


def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        data_backend="csv",
        openai_api_key=None,
        parser_version="test",
    )


def test_import_is_idempotent_and_creates_reviews(session: Session, user: User) -> None:
    first = import_whatsapp_file(session, FIXTURE, user.id, settings=settings())
    session.commit()
    second = import_whatsapp_file(session, FIXTURE, user.id, settings=settings())

    assert first.sets_accepted > 0
    assert first.messages_pending_review >= 2
    assert second.duplicate_import is True
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == first.sets_accepted
    assert session.scalar(select(func.count()).select_from(ParseReview)) == first.messages_pending_review


def test_known_user_alias_is_used_before_llm(session: Session, user: User, tmp_path: Path) -> None:
    exercise = get_or_create_exercise(session, "Rosca personalizada", "Bíceps")
    save_alias(session, "mov x", exercise, "Halter", user.id)
    session.commit()
    source = tmp_path / "alias.txt"
    source.write_text("01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep", encoding="utf-8")
    extractor = MockWorkoutExtractor(error=AssertionError("LLM nao deveria ser chamada"))

    report = import_whatsapp_file(session, source, user.id, settings=settings(), extractor=extractor)
    assert report.sets_accepted == 1
    assert extractor.calls == 0


def test_valid_llm_proposal_stays_in_shadow_mode(session: Session, user: User, tmp_path: Path) -> None:
    source = tmp_path / "unknown.txt"
    source.write_text("01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep", encoding="utf-8")
    proposal = WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name="Mov X",
                canonical_name="Rosca X",
                muscle_group="Bíceps",
                equipment="Halter",
                sets=[SetPayload(weight_kg=10, reps=10)],
            )
        ],
    )
    extractor = MockWorkoutExtractor(result=proposal)
    report = import_whatsapp_file(session, source, user.id, settings=settings(), extractor=extractor)

    review = session.scalar(select(ParseReview))
    assert report.llm_proposals == 1
    assert report.sets_accepted == 0
    assert review is not None
    assert review.proposed_payload["llm_model"] == "mock-model"


def test_accept_and_reject_review(session: Session, user: User, tmp_path: Path) -> None:
    source = tmp_path / "reviews.txt"
    source.write_text(
        "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep\n02/01/2026 10:00 - Pessoa: Mov Y 12kg/8rep",
        encoding="utf-8",
    )
    import_whatsapp_file(session, source, user.id, settings=settings())
    reviews = list(session.scalars(select(ParseReview).order_by(ParseReview.created_at)))
    payload = {
        "workout_date": "2026-01-01",
        "workout_type": None,
        "notes": None,
        "exercises": [
            {
                "raw_name": "Mov X",
                "canonical_name": "Rosca X",
                "muscle_group": "Bíceps",
                "equipment": "Halter",
                "load_basis": "por_halter",
                "sets": [{"weight_kg": 10, "reps": 10, "rpe": None, "rir": None}],
                "uncertain_fields": [],
                "needs_review": False,
            }
        ],
        "unconsumed_text": [],
        "needs_review": False,
    }
    accept_review(session, reviews[0].id, payload, alias_raw="mov x")
    reject_review(session, reviews[1].id)
    assert reviews[0].status == "accepted"
    assert reviews[1].status == "rejected"
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1


def test_database_constraints_reject_invalid_reps(session: Session, user: User) -> None:
    record = Import(
        user_id=user.id,
        source_filename="constraint.txt",
        source_sha256="b" * 64,
        parser_version="test",
        status="processing",
        metadata_={},
    )
    raw = RawMessage(
        import_record=record,
        source_index=0,
        sent_at=datetime(2026, 1, 1, 10),
        sender_raw="Pessoa",
        raw_content="x",
        content_sha256="a" * 64,
    )
    workout = Workout(user_id=user.id, workout_date=date(2026, 1, 1), source="test")
    exercise = get_or_create_exercise(session, "Teste", "Outro")
    variant = get_or_create_variant(session, exercise, "Máquina", "total")
    session.add_all(
        [
            raw,
            workout,
            WorkoutSet(
                workout=workout,
                exercise_variant=variant,
                raw_message=raw,
                set_number=1,
                weight_kg=10,
                reps=0,
                parse_method="rule",
                parser_version="test",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_message_is_not_materialized_twice(session: Session, user: User, tmp_path: Path) -> None:
    source = tmp_path / "duplicate.txt"
    line = "01/01/2026 10:00 - Pessoa: Supino reto 50kg/10rep"
    source.write_text(f"{line}\n{line}", encoding="utf-8")
    report = import_whatsapp_file(session, source, user.id, settings=settings())
    assert report.duplicate_messages == 1
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1


def test_single_shared_set_does_not_make_distinct_workouts_conflict() -> None:
    text = (
        "01/01/2026 10:00 - Pessoa: Supino reto 50kg/10rep\nLateral 3kg/10rep\n"
        "01/01/2026 18:00 - Pessoa: Extensora 40kg/10rep\nLateral 3kg/10rep"
    )
    messages = split_whatsapp_messages(text)
    outcomes = [parse_workout_message(message) for message in messages]
    conflicts, duplicates = _conflicting_message_indexes(messages, outcomes)
    assert conflicts == set()
    assert duplicates == 0


def test_edited_subset_is_a_normalized_conflict() -> None:
    text = (
        "01/01/2026 10:00 - Pessoa: Supino reto 50kg/10rep\nPuxada alta 40kg/10rep\n"
        "01/01/2026 10:10 - Pessoa: Supino reto 50kg/10rep\nPuxada alta 40kg/10rep\nLateral 3kg/10rep"
    )
    messages = split_whatsapp_messages(text)
    outcomes = [parse_workout_message(message) for message in messages]
    conflicts, duplicates = _conflicting_message_indexes(messages, outcomes)
    assert conflicts == {0, 1}
    assert duplicates == 2
