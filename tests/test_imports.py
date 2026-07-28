from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gym_tracker.config import Settings
from gym_tracker.models import (
    Import,
    ImportMessageOccurrence,
    ParseResult,
    ParseReview,
    ParseRun,
    RawMessage,
    User,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import get_or_create_exercise, save_alias
from gym_tracker.repositories.dashboard import DashboardRepository
from gym_tracker.repositories.state import current_data_revision
from gym_tracker.schemas import ExercisePayload, SetPayload, WorkoutExtraction
from gym_tracker.services.llm_extractor import MockWorkoutExtractor
from gym_tracker.services.parser import parse_workout_message, split_whatsapp_messages
from gym_tracker.services.reviews import accept_review, reject_review
from gym_tracker.services.whatsapp_import import _conflicting_message_indexes, import_whatsapp_file

FIXTURE = Path("tests/fixtures/whatsapp_anonymized.txt")


def settings(version: str = "test", **overrides) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        data_backend="csv",
        openai_api_key=None,
        parser_version=version,
        **overrides,
    )


def write_export(path: Path, *messages: str) -> Path:
    path.write_text("\n".join(messages), encoding="utf-8")
    return path


def test_same_file_and_version_is_an_explicit_noop(session: Session, user: User) -> None:
    first = import_whatsapp_file(session, FIXTURE, user.id, settings=settings())
    session.commit()
    second = import_whatsapp_file(session, FIXTURE, user.id, settings=settings())

    assert first.messages_total > 0
    assert first.messages_pending_review >= 2
    assert second.duplicate_import is True
    assert second.import_id == first.import_id
    assert second.parse_run_id == first.parse_run_id
    assert session.scalar(select(func.count()).select_from(ParseRun)) == 1


def test_overlapping_exports_reuse_message_without_duplicate_sets(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    old = "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"
    new = "02/01/2026 10:00 - Pessoa: Flexora 30kg/8rep"
    export_a = write_export(tmp_path / "a.txt", old)
    export_b = write_export(tmp_path / "b.txt", old, new)

    first = import_whatsapp_file(session, export_a, user.id, settings=settings())
    session.commit()
    second = import_whatsapp_file(session, export_b, user.id, settings=settings())
    session.commit()

    assert first.sets_accepted == 1
    assert second.sets_accepted == 1
    assert second.messages_new == 1
    assert second.messages_reused == 1
    assert session.scalar(select(func.count()).select_from(Import)) == 2
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
    assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 3
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 2
    old_message = session.scalar(select(RawMessage).where(RawMessage.raw_content == "Extensora 40kg/10rep"))
    assert old_message is not None
    assert len(old_message.occurrences) == 2


def test_same_file_new_parser_version_reprocesses_and_only_new_result_is_active(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(tmp_path / "versioned.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    first = import_whatsapp_file(session, source, user.id, settings=settings("v1"))
    session.commit()
    second = import_whatsapp_file(session, source, user.id, settings=settings("v2"))
    session.commit()

    assert first.import_id == second.import_id
    assert first.parse_run_id != second.parse_run_id
    assert second.reprocessed is True
    assert session.scalar(select(func.count()).select_from(ParseRun)) == 2
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 2
    assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active.is_(True))) == 1
    assert len(DashboardRepository(session).workout_dataframe(user.id)) == 1


def test_same_text_on_different_dates_is_legitimate(session: Session, user: User, tmp_path: Path) -> None:
    source = write_export(
        tmp_path / "dates.txt",
        "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep",
        "02/01/2026 10:00 - Pessoa: Extensora 40kg/10rep",
    )
    report = import_whatsapp_file(session, source, user.id, settings=settings())
    assert report.sets_accepted == 2
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2


def test_same_message_for_different_users_never_collides(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    second_user = User(display_name="Outra pessoa")
    session.add(second_user)
    session.commit()
    source = write_export(tmp_path / "users.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")

    import_whatsapp_file(session, source, user.id, settings=settings())
    import_whatsapp_file(session, source, second_user.id, settings=settings())
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 2


def test_identical_occurrences_in_same_minute_are_preserved(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    line = "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"
    source = write_export(tmp_path / "identical.txt", line, line)
    report = import_whatsapp_file(session, source, user.id, settings=settings())

    assert report.sets_accepted == 2
    messages = list(session.scalars(select(RawMessage).order_by(RawMessage.occurrence_ordinal)))
    assert [message.occurrence_ordinal for message in messages] == [0, 1]
    assert session.scalar(select(func.count()).select_from(ImportMessageOccurrence)) == 2


def test_possible_edit_creates_review_and_preserves_previous_active_result(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    original = write_export(tmp_path / "original.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    edited = write_export(tmp_path / "edited.txt", "01/01/2026 10:00 - Pessoa: Extensora 45kg/8rep")
    import_whatsapp_file(session, original, user.id, settings=settings())
    session.commit()
    report = import_whatsapp_file(session, edited, user.id, settings=settings())

    assert report.conflicting_messages == 1
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
    assert session.scalar(select(func.count()).select_from(ParseResult).where(ParseResult.is_active.is_(True))) == 1
    assert len(DashboardRepository(session).workout_dataframe(user.id)) == 1


def test_known_user_alias_resolves_exercise_and_equipment_before_llm(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    exercise = get_or_create_exercise(session, "Rosca personalizada", "Bíceps")
    save_alias(session, "mov x", exercise, "Halter", user.id)
    session.commit()
    source = write_export(tmp_path / "alias.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
    extractor = MockWorkoutExtractor(error=AssertionError("LLM nao deveria ser chamada"))

    report = import_whatsapp_file(session, source, user.id, settings=settings(), extractor=extractor)
    assert report.sets_accepted == 1
    assert extractor.calls == 0


def test_single_confirmed_variant_can_resolve_missing_equipment(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    explicit = write_export(
        tmp_path / "explicit.txt",
        "01/01/2026 10:00 - Pessoa: Supino reto halter 20kg/10rep",
    )
    implicit = write_export(
        tmp_path / "implicit.txt",
        "02/01/2026 10:00 - Pessoa: Supino reto 22kg/8rep",
    )
    import_whatsapp_file(session, explicit, user.id, settings=settings())
    session.commit()
    report = import_whatsapp_file(session, implicit, user.id, settings=settings())

    assert report.sets_accepted == 1
    assert session.scalar(select(func.count()).select_from(ParseReview)) == 0


def test_unknown_equipment_goes_to_review_without_materialization(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(tmp_path / "equipment.txt", "01/01/2026 10:00 - Pessoa: Supino reto 50kg/10rep")
    report = import_whatsapp_file(session, source, user.id, settings=settings())
    review = session.scalar(select(ParseReview))

    assert report.sets_accepted == 0
    assert report.messages_pending_review == 1
    assert review is not None and "equipamento nao determinado" in review.reason


def test_valid_llm_proposal_stays_in_shadow_mode(session: Session, user: User, tmp_path: Path) -> None:
    source = write_export(tmp_path / "unknown.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
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
    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=True, llm_auto_accept=True),
        extractor=extractor,
    )

    review = session.scalar(select(ParseReview))
    assert report.llm_proposals == 1
    assert report.sets_accepted == 0
    assert review is not None
    assert review.proposed_payload["llm_model"] == "mock-model"


def test_llm_requires_explicit_non_shadow_auto_accept(session: Session, user: User, tmp_path: Path) -> None:
    source = write_export(tmp_path / "llm.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
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

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=extractor,
    )
    assert report.sets_accepted == 1
    assert session.scalar(select(func.count()).select_from(ParseReview)) == 0


def test_non_shadow_without_auto_accept_still_requires_review(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(tmp_path / "llm-review.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
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
    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=False),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    assert report.sets_accepted == 0
    assert report.messages_pending_review == 1


def test_accept_and_reject_review_are_single_use(session: Session, user: User, tmp_path: Path) -> None:
    source = write_export(
        tmp_path / "reviews.txt",
        "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep",
        "02/01/2026 10:00 - Pessoa: Mov Y 12kg/8rep",
    )
    import_whatsapp_file(session, source, user.id, settings=settings())
    reviews = list(session.scalars(select(ParseReview).order_by(ParseReview.created_at)))
    payload = WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name="Mov X",
                canonical_name="Rosca X",
                muscle_group="Bíceps",
                equipment="Halter",
                load_basis="por_halter",
                sets=[SetPayload(weight_kg=10, reps=10)],
            )
        ],
    ).model_dump(mode="json")
    revision_before = current_data_revision(session)
    accept_review(session, reviews[0].id, payload, alias_raw="mov x")
    reject_review(session, reviews[1].id)

    assert reviews[0].status == "accepted"
    assert reviews[1].status == "rejected"
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1
    assert current_data_revision(session) > revision_before
    with pytest.raises(ValueError, match="finalizado"):
        reject_review(session, reviews[1].id)


def test_database_constraints_reject_invalid_reps(session: Session, user: User, tmp_path: Path) -> None:
    source = write_export(tmp_path / "constraint.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    import_whatsapp_file(session, source, user.id, settings=settings())
    item = session.scalar(select(WorkoutSet))
    assert item is not None
    item.reps = 0
    with pytest.raises(IntegrityError):
        session.flush()


def test_activation_failure_rolls_back_and_keeps_previous_result(
    session: Session,
    user: User,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = write_export(tmp_path / "rollback.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    import_whatsapp_file(session, source, user.id, settings=settings("v1"))
    session.commit()
    old_result = session.scalar(select(ParseResult).where(ParseResult.is_active.is_(True)))
    assert old_result is not None

    def fail_after_deactivation(db_session: Session, result: ParseResult) -> None:
        active = db_session.scalar(select(ParseResult).where(ParseResult.is_active.is_(True)).with_for_update())
        active.is_active = False
        db_session.flush()
        raise RuntimeError("falha simulada")

    monkeypatch.setattr("gym_tracker.services.whatsapp_import._activate_result", fail_after_deactivation)
    with pytest.raises(RuntimeError, match="simulada"), session.begin_nested():
        import_whatsapp_file(session, source, user.id, settings=settings("v2"))

    session.expire_all()
    assert session.get(ParseResult, old_result.id).is_active is True
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1


def test_single_shared_set_does_not_make_distinct_workouts_conflict() -> None:
    text = (
        "01/01/2026 10:00 - Pessoa: Supino reto halter 50kg/10rep\nLateral halter 3kg/10rep\n"
        "01/01/2026 18:00 - Pessoa: Extensora 40kg/10rep\nLateral halter 3kg/10rep"
    )
    messages = split_whatsapp_messages(text)
    outcomes = [parse_workout_message(message) for message in messages]
    conflicts, duplicates = _conflicting_message_indexes(messages, outcomes)
    assert conflicts == set()
    assert duplicates == 0


def test_edited_subset_is_a_normalized_conflict() -> None:
    text = (
        "01/01/2026 10:00 - Pessoa: Supino reto halter 50kg/10rep\nPuxada alta 40kg/10rep\n"
        "01/01/2026 10:10 - Pessoa: Supino reto halter 50kg/10rep\n"
        "Puxada alta 40kg/10rep\nLateral halter 3kg/10rep"
    )
    messages = split_whatsapp_messages(text)
    outcomes = [parse_workout_message(message) for message in messages]
    conflicts, duplicates = _conflicting_message_indexes(messages, outcomes)
    assert conflicts == {0, 1}
    assert duplicates == 2
