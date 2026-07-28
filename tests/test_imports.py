from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gym_tracker.config import Settings
from gym_tracker.models import (
    ExerciseAlias,
    ExerciseVariant,
    Import,
    ImportMessageOccurrence,
    ParseResult,
    ParseReview,
    ParseRun,
    RawMessage,
    User,
    Workout,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import get_or_create_exercise, save_alias
from gym_tracker.repositories.dashboard import DashboardRepository
from gym_tracker.repositories.state import current_data_revision
from gym_tracker.schemas import ExercisePayload, SetPayload, WorkoutExtraction
from gym_tracker.services.llm_extractor import MockWorkoutExtractor
from gym_tracker.services.parser import parse_workout_message, split_whatsapp_messages
from gym_tracker.services.reviews import accept_review, reject_review
from gym_tracker.services.validation import ReviewReasonKind, classify_review_reason, llm_auto_accept_blockers
from gym_tracker.services.whatsapp_import import (
    _conflicting_message_indexes,
    _materialize_extraction,
    import_whatsapp_file,
)

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


def complete_proposal(
    raw_name: str = "Extensora",
    canonical_name: str = "Extensora",
    muscle_group: str = "Pernas",
    equipment: str = "Máquina",
    weight_kg: int = 40,
    reps: int = 10,
) -> WorkoutExtraction:
    return WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name=raw_name,
                canonical_name=canonical_name,
                muscle_group=muscle_group,
                equipment=equipment,
                sets=[SetPayload(weight_kg=weight_kg, reps=reps)],
            )
        ],
    )


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


def test_minute_and_zero_seconds_reuse_message_and_preserve_occurrence_precision(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    minute = write_export(tmp_path / "minute.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    second = write_export(tmp_path / "second.txt", "01/01/2026 10:00:00 - Pessoa: Extensora 40kg/10rep")

    import_whatsapp_file(session, minute, user.id, settings=settings())
    session.commit()
    report = import_whatsapp_file(session, second, user.id, settings=settings())
    session.commit()

    assert report.messages_new == 0
    assert report.messages_reused == 1
    assert report.conflicting_messages == 0
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 1
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1
    occurrences = list(session.scalars(select(ImportMessageOccurrence).order_by(ImportMessageOccurrence.created_at)))
    assert [item.timestamp_precision for item in occurrences] == ["minute", "second"]


def test_comparable_timestamps_with_different_content_create_review(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    original = write_export(tmp_path / "minute-original.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    changed = write_export(tmp_path / "second-changed.txt", "01/01/2026 10:00:00 - Pessoa: Extensora 45kg/8rep")

    import_whatsapp_file(session, original, user.id, settings=settings())
    session.commit()
    report = import_whatsapp_file(session, changed, user.id, settings=settings())

    assert report.conflicting_messages == 1
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1


def test_minute_and_nonzero_seconds_preserve_ambiguity_for_review(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    minute = write_export(tmp_path / "minute-ambiguous.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    second = write_export(tmp_path / "second-ambiguous.txt", "01/01/2026 10:00:30 - Pessoa: Extensora 40kg/10rep")

    import_whatsapp_file(session, minute, user.id, settings=settings())
    session.commit()
    report = import_whatsapp_file(session, second, user.id, settings=settings())

    assert report.conflicting_messages == 1
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(RawMessage)) == 2
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1


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


def test_user_alias_equipment_overrides_catalog_default_for_all_sets(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    exercise = get_or_create_exercise(session, "Puxada alta", "Costas")
    save_alias(session, "puxada alta", exercise, "Cabo", user.id)
    session.commit()
    source = write_export(
        tmp_path / "alias-equipment.txt",
        "01/01/2026 10:00 - Pessoa: Puxada alta 40kg/10rep ... 45kg/8rep",
    )

    report = import_whatsapp_file(session, source, user.id, settings=settings())
    variants = list(
        session.scalars(
            select(ExerciseVariant).join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id).distinct()
        )
    )

    assert report.sets_accepted == 2
    assert report.messages_pending_review == 0
    assert [item.equipment for item in variants] == ["Cabo"]


def test_explicit_equipment_overrides_user_alias(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    exercise = get_or_create_exercise(session, "Puxada alta", "Costas")
    save_alias(session, "puxada alta", exercise, "Cabo", user.id)
    session.commit()
    source = write_export(
        tmp_path / "explicit-equipment.txt",
        "01/01/2026 10:00 - Pessoa: Puxada alta máquina 40kg/10rep",
    )

    report = import_whatsapp_file(session, source, user.id, settings=settings())
    variant = session.scalar(
        select(ExerciseVariant).join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id)
    )

    assert report.sets_accepted == 1
    assert variant is not None and variant.equipment == "Máquina"


@pytest.mark.parametrize("raw_name", ["Bíceps hack", "Bíceps zottman"])
def test_alias_overrides_catalog_halter_default(
    session: Session,
    user: User,
    tmp_path: Path,
    raw_name: str,
) -> None:
    exercise = get_or_create_exercise(session, raw_name, "Bíceps")
    save_alias(session, raw_name, exercise, "Cabo", user.id)
    session.commit()
    source = write_export(
        tmp_path / f"{raw_name}.txt",
        f"01/01/2026 10:00 - Pessoa: {raw_name} 10kg/10rep ... 12kg/8rep",
    )

    report = import_whatsapp_file(session, source, user.id, settings=settings())
    variants = list(
        session.scalars(
            select(ExerciseVariant).join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id).distinct()
        )
    )

    assert report.sets_accepted == 2
    assert [item.equipment for item in variants] == ["Cabo"]


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [("com halter", "Halter"), ("na máquina", "Máquina")],
)
def test_equipment_written_with_technique_name_overrides_alias(
    session: Session,
    user: User,
    tmp_path: Path,
    suffix: str,
    expected: str,
) -> None:
    exercise = get_or_create_exercise(session, "Bíceps hack", "Bíceps")
    save_alias(session, "bíceps hack", exercise, "Cabo", user.id)
    session.commit()
    source = write_export(
        tmp_path / f"explicit-{expected}.txt",
        f"01/01/2026 10:00 - Pessoa: Bíceps hack {suffix} 10kg/10rep",
    )

    report = import_whatsapp_file(session, source, user.id, settings=settings())
    variant = session.scalar(
        select(ExerciseVariant).join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id)
    )

    assert report.sets_accepted == 1
    assert variant is not None and variant.equipment == expected


def test_alias_overrides_block_context_for_technique_name(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    exercise = get_or_create_exercise(session, "Bíceps hack", "Bíceps")
    save_alias(session, "bíceps hack", exercise, "Cabo", user.id)
    session.commit()
    source = write_export(
        tmp_path / "alias-context.txt",
        "01/01/2026 10:00 - Pessoa: Máquina\nBíceps hack 10kg/10rep",
    )

    report = import_whatsapp_file(session, source, user.id, settings=settings())
    variant = session.scalar(
        select(ExerciseVariant).join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id)
    )

    assert report.sets_accepted == 1
    assert variant is not None and variant.equipment == "Cabo"


def test_explicit_block_context_is_used_after_alias_lookup(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(
        tmp_path / "context-equipment.txt",
        "01/01/2026 10:00 - Pessoa: Cabo\nPuxada alta 40kg/10rep",
    )

    report = import_whatsapp_file(session, source, user.id, settings=settings())
    variant = session.scalar(
        select(ExerciseVariant).join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id)
    )

    assert report.sets_accepted == 1
    assert report.messages_pending_review == 0
    assert variant is not None and variant.equipment == "Cabo"


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
    assert report.messages_accepted == 1
    assert session.scalar(select(func.count()).select_from(Workout)) == 1
    assert session.scalar(select(func.count()).select_from(ParseReview)) == 0


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    [
        ("Extensora 40/10rep", "carga sem unidade"),
        ("Extensora 40kg", "repeticoes ausentes"),
    ],
    ids=["missing-load-unit", "missing-repetitions"],
)
def test_llm_cannot_fill_mandatory_facts_missing_from_source(
    session: Session,
    user: User,
    tmp_path: Path,
    message: str,
    expected_reason: str,
) -> None:
    source = write_export(tmp_path / f"{expected_reason}.txt", f"01/01/2026 10:00 - Pessoa: {message}")
    proposal = complete_proposal()
    extractor = MockWorkoutExtractor(result=proposal)

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=extractor,
    )
    review = session.scalar(select(ParseReview))

    assert report.messages_accepted == 0
    assert report.messages_pending_review == 1
    assert report.sets_accepted == 0
    assert session.scalar(select(func.count()).select_from(Workout)) == 0
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert session.scalar(select(func.count()).select_from(ParseReview)) == 1
    assert review is not None and expected_reason in review.reason
    assert review.proposed_payload["llm"] == proposal.model_dump(mode="json")
    assert extractor.calls == 1


def test_unclassified_review_reason_blocks_llm_auto_acceptance() -> None:
    reason = "novo motivo ainda sem classificacao"

    assert classify_review_reason(reason) == ReviewReasonKind.UNKNOWN
    assert llm_auto_accept_blockers([reason]) == [reason]


def test_llm_semantic_normalization_cannot_change_explicit_set_values(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(tmp_path / "changed-values.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
    proposal = complete_proposal(
        raw_name="Mov X",
        canonical_name="Rosca X",
        muscle_group="Bíceps",
        equipment="Halter",
        weight_kg=12,
        reps=8,
    )

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    review = session.scalar(select(ParseReview))

    assert report.messages_accepted == 0
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(Workout)) == 0
    assert review is not None and "proposta diverge dos valores da origem" in review.reason


def test_incomplete_exercise_blocks_entire_multi_exercise_message(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(
        tmp_path / "mixed-source.txt",
        "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep\nFlexora 30kg",
    )
    proposal = WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name="Extensora",
                canonical_name="Extensora",
                muscle_group="Pernas",
                equipment="Máquina",
                sets=[SetPayload(weight_kg=40, reps=10)],
            ),
            ExercisePayload(
                raw_name="Flexora",
                canonical_name="Flexora",
                muscle_group="Pernas",
                equipment="Máquina",
                sets=[SetPayload(weight_kg=30, reps=8)],
            ),
        ],
    )

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    review = session.scalar(select(ParseReview))

    assert report.messages_accepted == 0
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(Workout)) == 0
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert review is not None and "repeticoes ausentes" in review.reason
    assert len(review.proposed_payload["llm"]["exercises"]) == 2


def test_empty_llm_extraction_goes_to_review_without_empty_workout(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(tmp_path / "empty-llm.txt", "01/01/2026 10:00 - Pessoa: Mov X")
    proposal = WorkoutExtraction(workout_date="2026-01-01", exercises=[])
    extractor = MockWorkoutExtractor(result=proposal)

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=extractor,
    )
    review = session.scalar(select(ParseReview))

    assert report.messages_accepted == 0
    assert report.messages_pending_review == 1
    assert report.sets_accepted == 0
    assert session.scalar(select(func.count()).select_from(Workout)) == 0
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert session.scalar(select(func.count()).select_from(ParseReview)) == 1
    assert review is not None
    assert review.proposed_payload["llm"]["exercises"] == []
    assert "proposta sem exercicios" in review.reason
    assert extractor.calls == 1


@pytest.mark.parametrize(
    "proposal",
    [
        WorkoutExtraction.model_construct(
            workout_date=date(2026, 1, 1),
            exercises=[
                ExercisePayload.model_construct(
                    raw_name="Extensora",
                    canonical_name="Extensora",
                    muscle_group="Pernas",
                    equipment="Máquina",
                    sets=[],
                )
            ],
        ),
        WorkoutExtraction.model_construct(
            workout_date=date(2026, 1, 1),
            exercises=[
                complete_proposal().exercises[0],
                ExercisePayload.model_construct(
                    raw_name="Flexora",
                    canonical_name="Flexora",
                    muscle_group="Pernas",
                    equipment="Máquina",
                    sets=[],
                ),
            ],
        ),
        WorkoutExtraction.model_construct(
            workout_date=date(2026, 1, 1),
            exercises=[
                ExercisePayload.model_construct(
                    raw_name="Extensora",
                    canonical_name="Extensora",
                    muscle_group="Pernas",
                    equipment="Máquina",
                    sets=[
                        SetPayload.model_construct(
                            weight_kg=Decimal("0.05"),
                            reps=10,
                            rpe=None,
                            rir=None,
                        )
                    ],
                )
            ],
        ),
    ],
    ids=["exercise-without-sets", "valid-and-empty-exercises", "no-materializable-sets"],
)
def test_nonmaterializable_llm_extraction_blocks_entire_message(
    session: Session,
    user: User,
    tmp_path: Path,
    proposal: WorkoutExtraction,
) -> None:
    source = write_export(tmp_path / "invalid-llm.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    review = session.scalar(select(ParseReview))

    assert report.messages_accepted == 0
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(Workout)) == 0
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert review is not None and review.proposed_payload["llm"]["exercises"]


def test_empty_llm_reprocessing_keeps_previous_accepted_result_active(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    exercise = get_or_create_exercise(session, "Rosca X", "Bíceps")
    alias = save_alias(session, "mov x", exercise, "Halter", user.id)
    session.commit()
    source = write_export(tmp_path / "empty-reprocessing.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
    first = import_whatsapp_file(session, source, user.id, settings=settings("v1"))
    session.commit()
    session.delete(session.get(ExerciseAlias, alias.id))
    session.commit()
    proposal = WorkoutExtraction(workout_date="2026-01-01", exercises=[])

    second = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings("v2", llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    active_result = session.scalar(select(ParseResult).where(ParseResult.is_active.is_(True)))
    review = session.scalar(select(ParseReview))

    assert first.messages_accepted == 1
    assert second.messages_accepted == 0
    assert second.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(Workout)) == 1
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1
    assert active_result is not None and active_result.parse_run.parser_version == "v1"
    assert review is not None and review.proposed_payload["llm"]["exercises"] == []


def test_direct_materialization_rejects_empty_extraction_before_creating_workout(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(tmp_path / "direct-empty.txt", "01/01/2026 10:00 - Pessoa: Mov X")
    proposal = WorkoutExtraction(workout_date="2026-01-01", exercises=[])
    import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    raw_message = session.scalar(select(RawMessage))
    result = session.scalar(select(ParseResult))
    assert raw_message is not None and result is not None

    with pytest.raises(ValueError, match="proposta sem exercicios"):
        _materialize_extraction(session, user.id, raw_message, result, proposal, "test")

    assert session.scalar(select(func.count()).select_from(Workout)) == 0
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


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


def test_uncertain_llm_proposal_never_replaces_previous_active_result(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    exercise = get_or_create_exercise(session, "Rosca X", "Bíceps")
    alias = save_alias(session, "mov x", exercise, "Halter", user.id)
    session.commit()
    source = write_export(tmp_path / "uncertain-llm.txt", "01/01/2026 10:00 - Pessoa: Mov X 10kg/10rep")
    first = import_whatsapp_file(session, source, user.id, settings=settings("v1"))
    session.commit()
    session.delete(session.get(ExerciseAlias, alias.id))
    session.commit()
    proposal = WorkoutExtraction(
        workout_date="2026-01-01",
        needs_review=True,
        exercises=[
            ExercisePayload(
                raw_name="Mov X",
                canonical_name="Rosca X",
                muscle_group="Bíceps",
                equipment="Halter",
                sets=[SetPayload(weight_kg=10, reps=10)],
                uncertain_fields=["equipment"],
                needs_review=True,
            )
        ],
    )

    second = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings("v2", llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    review = session.scalar(select(ParseReview))
    active_result = session.scalar(select(ParseResult).where(ParseResult.is_active.is_(True)))

    assert first.sets_accepted == 1
    assert second.sets_accepted == 0
    assert second.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1
    assert session.scalar(select(func.count()).select_from(ParseReview)) == 1
    assert review is not None
    assert "exercicio desconhecido" in review.reason
    assert review.proposed_payload["llm"]["needs_review"] is True
    assert review.proposed_payload["llm"]["exercises"][0]["uncertain_fields"] == ["equipment"]
    assert active_result is not None and active_result.parse_run.parser_version == "v1"


def test_valid_llm_proposal_cannot_override_source_identity_conflict(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    original = write_export(tmp_path / "llm-original.txt", "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep")
    changed = write_export(tmp_path / "llm-changed.txt", "01/01/2026 10:00:00 - Pessoa: Extensora 45kg/8rep")
    import_whatsapp_file(session, original, user.id, settings=settings("v1"))
    session.commit()
    proposal = WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name="Extensora",
                canonical_name="Extensora",
                muscle_group="Pernas",
                equipment="Máquina",
                sets=[SetPayload(weight_kg=45, reps=8)],
            )
        ],
    )

    report = import_whatsapp_file(
        session,
        changed,
        user.id,
        settings=settings("v2", llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    review = session.scalar(select(ParseReview))
    active_result = session.scalar(select(ParseResult).where(ParseResult.is_active.is_(True)))

    assert report.conflicting_messages == 1
    assert report.sets_accepted == 0
    assert report.messages_pending_review == 1
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 1
    assert len(DashboardRepository(session).workout_dataframe(user.id)) == 1
    assert review is not None and "possivel edicao" in review.reason
    assert review.proposed_payload["llm"]["exercises"][0]["equipment"] == "Máquina"
    assert active_result is not None and active_result.parse_run.parser_version == "v1"


def test_valid_llm_proposal_cannot_override_normalized_temporal_conflict(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_export(
        tmp_path / "normalized-conflict.txt",
        "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep\nFlexora 30kg/8rep",
        "01/01/2026 10:10 - Pessoa: Extensora 40kg/10rep\nFlexora 30kg/8rep",
    )
    proposal = WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name="Extensora",
                canonical_name="Extensora",
                muscle_group="Pernas",
                equipment="Máquina",
                sets=[SetPayload(weight_kg=40, reps=10)],
            )
        ],
    )

    report = import_whatsapp_file(
        session,
        source,
        user.id,
        settings=settings(llm_shadow_mode=False, llm_auto_accept=True),
        extractor=MockWorkoutExtractor(result=proposal),
    )
    reviews = list(session.scalars(select(ParseReview)))

    assert report.normalized_duplicates == 2
    assert report.sets_accepted == 0
    assert report.messages_pending_review == 2
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert len(reviews) == 2
    assert all("duplicacao normalizada ou conflito temporal" in item.reason for item in reviews)


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
