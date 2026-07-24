import hashlib
import uuid
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.config import Settings, get_settings
from gym_tracker.models import (
    Import,
    ImportStatus,
    ParseMethod,
    ParseReview,
    ParseStatus,
    RawMessage,
    User,
    Workout,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import find_alias, get_or_create_exercise, get_or_create_variant
from gym_tracker.schemas import ParseOutcome, QualityReport, WhatsAppMessage, WorkoutExtraction
from gym_tracker.services.llm_extractor import OpenAIWorkoutExtractor, WorkoutExtractor
from gym_tracker.services.parser import normalized_set_signatures, parse_workout_message, split_whatsapp_messages
from gym_tracker.services.validation import count_reasons, review_reasons


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _resolve_database_aliases(session: Session, user_id: uuid.UUID, outcome: ParseOutcome) -> ParseOutcome:
    if outcome.extraction is None:
        return outcome
    remaining_reasons = list(outcome.reasons)
    for exercise in outcome.extraction.exercises:
        if exercise.canonical_name:
            continue
        match = find_alias(session, exercise.raw_name, user_id)
        if match is None:
            continue
        exercise.canonical_name = match.canonical_name
        exercise.muscle_group = match.muscle_group
        exercise.equipment = match.equipment
        exercise.load_basis = match.load_basis
        exercise.needs_review = False
        exercise.uncertain_fields = []
        remaining_reasons = [
            reason for reason in remaining_reasons if reason != f"exercicio desconhecido: {exercise.raw_name}"
        ]
    outcome.reasons = remaining_reasons
    outcome.extraction.needs_review = bool(remaining_reasons)
    return outcome


def _conflicting_message_indexes(messages: list[WhatsAppMessage], outcomes: list[ParseOutcome]) -> tuple[set[int], int]:
    grouped: dict[tuple[object, ...], list[tuple[int, set[tuple[str, str, str, str]]]]] = defaultdict(list)
    for position, (message, outcome) in enumerate(zip(messages, outcomes, strict=True)):
        signatures = normalized_set_signatures(outcome)
        if signatures:
            grouped[(message.sent_at.date(), message.sender_raw)].append((position, signatures))

    conflicts: set[int] = set()
    duplicates = 0
    for candidates in grouped.values():
        for left_index, (left_position, left) in enumerate(candidates):
            for right_position, right in candidates[left_index + 1 :]:
                overlap = left & right
                smaller_size = min(len(left), len(right))
                is_substantial = len(overlap) >= 2 and len(overlap) / smaller_size >= 0.8
                if is_substantial:
                    conflicts.update((left_position, right_position))
                    duplicates += len(overlap)
    return conflicts, duplicates


def _payload_for_review(
    deterministic: ParseOutcome,
    llm_proposal: WorkoutExtraction | None,
    extractor: WorkoutExtractor | None,
) -> dict:
    payload: dict = {
        "rule": deterministic.extraction.model_dump(mode="json") if deterministic.extraction else None,
    }
    if llm_proposal is not None:
        payload["llm"] = llm_proposal.model_dump(mode="json")
        payload["llm_model"] = extractor.model if extractor else None
        payload["prompt_version"] = extractor.prompt_version if extractor else None
    return payload


def _materialize_extraction(
    session: Session,
    user_id: uuid.UUID,
    raw_message: RawMessage,
    extraction: WorkoutExtraction,
    parser_version: str,
    parse_method: str = ParseMethod.RULE.value,
    llm_model: str | None = None,
) -> int:
    workout = Workout(
        user_id=user_id,
        workout_date=extraction.workout_date,
        workout_type=extraction.workout_type,
        notes=extraction.notes,
        source="whatsapp",
    )
    session.add(workout)
    session.flush()
    created = 0
    variant_set_counters: dict[uuid.UUID, int] = defaultdict(int)
    for exercise_payload in extraction.exercises:
        if not exercise_payload.canonical_name or not exercise_payload.muscle_group or not exercise_payload.equipment:
            raise ValueError(f"extracao incompleta para {exercise_payload.raw_name}")
        exercise = get_or_create_exercise(session, exercise_payload.canonical_name, exercise_payload.muscle_group)
        variant = get_or_create_variant(
            session,
            exercise,
            exercise_payload.equipment,
            exercise_payload.load_basis,
        )
        for item in exercise_payload.sets:
            variant_set_counters[variant.id] += 1
            session.add(
                WorkoutSet(
                    workout=workout,
                    exercise_variant=variant,
                    raw_message=raw_message,
                    set_number=variant_set_counters[variant.id],
                    weight_kg=item.weight_kg,
                    reps=item.reps,
                    rpe=item.rpe,
                    rir=item.rir,
                    parse_method=parse_method,
                    parser_version=parser_version,
                    llm_model=llm_model,
                )
            )
            created += 1
    session.flush()
    return created


def import_whatsapp_file(
    session: Session,
    file_path: Path,
    user_id: uuid.UUID,
    settings: Settings | None = None,
    extractor: WorkoutExtractor | None = None,
) -> QualityReport:
    settings = settings or get_settings()
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"usuario nao encontrado: {user_id}")

    source_bytes = file_path.read_bytes()
    source_hash = sha256_bytes(source_bytes)
    previous = session.scalar(select(Import).where(Import.user_id == user_id, Import.source_sha256 == source_hash))
    if previous is not None:
        metadata = previous.metadata_ or {}
        return QualityReport(import_id=str(previous.id), duplicate_import=True, **metadata.get("quality", {}))

    import_record = Import(
        user_id=user_id,
        source_filename=file_path.name,
        source_sha256=source_hash,
        parser_version=settings.parser_version,
        status=ImportStatus.PROCESSING.value,
        metadata_={},
    )
    session.add(import_record)
    session.flush()

    text = source_bytes.decode("utf-8-sig")
    messages = split_whatsapp_messages(text)
    outcomes = [_resolve_database_aliases(session, user_id, parse_workout_message(message)) for message in messages]
    conflicts, normalized_duplicate_count = _conflicting_message_indexes(messages, outcomes)
    extractor = extractor or OpenAIWorkoutExtractor(settings)
    report = QualityReport(
        import_id=str(import_record.id),
        messages_total=len(messages),
        normalized_duplicates=normalized_duplicate_count,
    )
    all_reasons: list[str] = []
    seen_hashes: set[str] = set()

    for position, (message, outcome) in enumerate(zip(messages, outcomes, strict=True)):
        if message.content_sha256 in seen_hashes:
            report.duplicate_messages += 1
            continue
        seen_hashes.add(message.content_sha256)
        raw_message = RawMessage(
            import_record=import_record,
            source_index=message.source_index,
            sent_at=message.sent_at,
            sender_raw=message.sender_raw,
            raw_content=message.raw_content,
            content_sha256=message.content_sha256,
            is_edited=message.is_edited,
            is_deleted=message.is_deleted,
            parse_status=ParseStatus.SKIPPED.value,
        )
        session.add(raw_message)
        session.flush()

        reasons = review_reasons(outcome)
        if position in conflicts:
            reasons.append("duplicacao normalizada ou conflito temporal")
        reasons = list(dict.fromkeys(reasons))
        if outcome.extraction is None:
            raw_message.parse_status = ParseStatus.SKIPPED.value
            report.messages_skipped += 1
            continue

        if reasons:
            llm_proposal = None
            if extractor.available:
                try:
                    llm_proposal = extractor.extract(message.raw_content, message.sent_at.date().isoformat())
                    report.llm_proposals += 1
                except Exception as error:  # a falha da LLM nunca derruba a importacao deterministica
                    reasons.append(f"falha no extrator LLM: {type(error).__name__}")
            raw_message.parse_status = ParseStatus.REVIEW.value
            session.add(
                ParseReview(
                    raw_message=raw_message,
                    reason="; ".join(reasons),
                    proposed_payload=_payload_for_review(outcome, llm_proposal, extractor),
                )
            )
            report.messages_pending_review += 1
            all_reasons.extend(reasons)
            continue

        report.sets_accepted += _materialize_extraction(
            session, user_id, raw_message, outcome.extraction, settings.parser_version
        )
        raw_message.parse_status = ParseStatus.ACCEPTED.value
        report.messages_accepted += 1

    report.reasons = count_reasons(all_reasons)
    import_record.status = (
        ImportStatus.COMPLETED_WITH_REVIEWS.value if report.messages_pending_review else ImportStatus.COMPLETED.value
    )
    import_record.metadata_ = {"quality": report.model_dump(exclude={"import_id", "duplicate_import"})}
    session.flush()
    return report
