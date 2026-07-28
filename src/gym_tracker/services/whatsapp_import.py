import hashlib
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gym_tracker.config import Settings, get_settings
from gym_tracker.models import (
    Import,
    ImportMessageOccurrence,
    ImportStatus,
    ParseMethod,
    ParseResult,
    ParseResultStatus,
    ParseReview,
    ParseRun,
    ParseRunStatus,
    ParseStatus,
    RawMessage,
    User,
    Workout,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import (
    find_alias,
    find_confirmed_variant,
    get_or_create_exercise,
    get_or_create_variant,
)
from gym_tracker.repositories.state import bump_data_revision
from gym_tracker.schemas import ParseOutcome, QualityReport, WhatsAppMessage, WorkoutExtraction
from gym_tracker.services.llm_extractor import OpenAIWorkoutExtractor, WorkoutExtractor
from gym_tracker.services.loads import validate_materializable_load
from gym_tracker.services.normalization import normalize_message_content, normalize_text
from gym_tracker.services.parser import normalized_set_signatures, parse_workout_message, split_whatsapp_messages
from gym_tracker.services.validation import count_reasons, review_reasons


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _timestamp_key(value: datetime) -> str:
    return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()


def _message_identity(
    user_id: uuid.UUID,
    message: WhatsAppMessage,
    normalized_content_sha256: str,
    occurrence_ordinal: int,
) -> str:
    material = "\n".join(
        (
            str(user_id),
            normalize_text(message.sender_raw),
            _timestamp_key(message.sent_at),
            message.timestamp_precision,
            normalized_content_sha256,
            str(message.is_edited),
            str(occurrence_ordinal),
        )
    )
    return _sha256_text(material)


def _resolve_database_context(session: Session, user_id: uuid.UUID, outcome: ParseOutcome) -> ParseOutcome:
    if outcome.extraction is None:
        return outcome
    remaining_reasons = list(outcome.reasons)
    for exercise in outcome.extraction.exercises:
        alias = find_alias(session, exercise.raw_name, user_id)
        if exercise.canonical_name is None and alias is not None:
            exercise.canonical_name = alias.canonical_name
            exercise.muscle_group = alias.muscle_group
        if exercise.equipment is None and alias is not None and alias.equipment is not None:
            exercise.equipment = alias.equipment
            exercise.load_basis = alias.load_basis
        if exercise.equipment is None and exercise.canonical_name:
            confirmed = find_confirmed_variant(session, exercise.canonical_name, user_id)
            if confirmed is not None:
                exercise.equipment, exercise.load_basis = confirmed

        if exercise.canonical_name:
            remaining_reasons = [
                reason
                for reason in remaining_reasons
                if not reason.startswith(f"exercicio desconhecido: {exercise.raw_name}")
            ]
            exercise.uncertain_fields = [
                field for field in exercise.uncertain_fields if field not in {"canonical_name", "muscle_group"}
            ]
        if exercise.equipment:
            remaining_reasons = [
                reason
                for reason in remaining_reasons
                if not reason.startswith(f"equipamento nao determinado: {exercise.raw_name}")
            ]
            exercise.uncertain_fields = [field for field in exercise.uncertain_fields if field != "equipment"]
        exercise.needs_review = bool(exercise.uncertain_fields)

    outcome.reasons = remaining_reasons
    outcome.extraction.needs_review = bool(
        remaining_reasons or any(item.needs_review for item in outcome.extraction.exercises)
    )
    return outcome


def _conflicting_message_indexes(messages: list[WhatsAppMessage], outcomes: list[ParseOutcome]) -> tuple[set[int], int]:
    grouped: dict[tuple[object, ...], list[tuple[int, set[tuple[str, str, str, str]]]]] = defaultdict(list)
    for position, (message, outcome) in enumerate(zip(messages, outcomes, strict=True)):
        signatures = normalized_set_signatures(outcome)
        if signatures:
            grouped[(message.sent_at.date(), normalize_text(message.sender_raw))].append((position, signatures))

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
    parse_result: ParseResult,
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
            validate_materializable_load(item.weight_kg)
            variant_set_counters[variant.id] += 1
            session.add(
                WorkoutSet(
                    workout=workout,
                    exercise_variant=variant,
                    raw_message=raw_message,
                    parse_result=parse_result,
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


def _activate_result(session: Session, result: ParseResult) -> None:
    previous = session.scalar(
        select(ParseResult)
        .where(
            ParseResult.raw_message_id == result.raw_message_id,
            ParseResult.is_active.is_(True),
            ParseResult.id != result.id,
        )
        .with_for_update()
    )
    if previous is not None:
        previous.is_active = False
        session.flush([previous])
    result.status = ParseResultStatus.ACCEPTED.value
    result.is_active = True
    result.activated_at = datetime.now(UTC)
    result.raw_message.parse_status = ParseStatus.ACCEPTED.value
    session.flush()


def _existing_result_for_version(
    session: Session,
    raw_message_id: uuid.UUID,
    parser_version: str,
) -> ParseResult | None:
    return session.scalar(
        select(ParseResult)
        .join(ParseRun)
        .where(
            ParseResult.raw_message_id == raw_message_id,
            ParseRun.parser_version == parser_version,
        )
        .order_by(ParseResult.created_at)
    )


def _has_content_conflict(session: Session, message: RawMessage) -> bool:
    return (
        session.scalar(
            select(RawMessage.id).where(
                RawMessage.user_id == message.user_id,
                RawMessage.sender_normalized == message.sender_normalized,
                RawMessage.sent_at == message.sent_at,
                RawMessage.timestamp_precision == message.timestamp_precision,
                RawMessage.occurrence_ordinal == message.occurrence_ordinal,
                RawMessage.normalized_content_sha256 != message.normalized_content_sha256,
                RawMessage.id != message.id,
            )
        )
        is not None
    )


def _ingest_messages(
    session: Session,
    import_record: Import,
    user_id: uuid.UUID,
    messages: list[WhatsAppMessage],
) -> tuple[list[RawMessage], set[uuid.UUID], int]:
    canonical_messages: list[RawMessage] = []
    new_ids: set[uuid.UUID] = set()
    exact_occurrences: dict[tuple[object, ...], int] = defaultdict(int)
    conflict_count = 0

    for message in messages:
        normalized_content_sha256 = _sha256_text(normalize_message_content(message.raw_content))
        exact_key = (
            normalize_text(message.sender_raw),
            _timestamp_key(message.sent_at),
            message.timestamp_precision,
            normalized_content_sha256,
            message.is_edited,
        )
        occurrence_ordinal = exact_occurrences[exact_key]
        exact_occurrences[exact_key] += 1
        identity_sha256 = _message_identity(user_id, message, normalized_content_sha256, occurrence_ordinal)
        raw_message = session.scalar(
            select(RawMessage).where(
                RawMessage.user_id == user_id,
                RawMessage.identity_sha256 == identity_sha256,
            )
        )
        if raw_message is None:
            raw_message = RawMessage(
                first_import=import_record,
                user_id=user_id,
                source_index=message.source_index,
                source_offset=message.source_offset,
                sent_at=message.sent_at,
                timestamp_precision=message.timestamp_precision,
                sender_raw=message.sender_raw,
                sender_normalized=normalize_text(message.sender_raw),
                raw_content=message.raw_content,
                content_sha256=message.content_sha256,
                normalized_content_sha256=normalized_content_sha256,
                identity_sha256=identity_sha256,
                occurrence_ordinal=occurrence_ordinal,
                is_edited=message.is_edited,
                is_deleted=message.is_deleted,
                parse_status=ParseStatus.SKIPPED.value,
            )
            session.add(raw_message)
            session.flush()
            new_ids.add(raw_message.id)

        session.add(
            ImportMessageOccurrence(
                import_record=import_record,
                raw_message=raw_message,
                source_index=message.source_index,
                source_offset=message.source_offset,
                occurrence_ordinal=occurrence_ordinal,
            )
        )
        canonical_messages.append(raw_message)
        if _has_content_conflict(session, raw_message):
            conflict_count += 1
    session.flush()
    return canonical_messages, new_ids, conflict_count


def _messages_from_occurrences(import_record: Import) -> list[RawMessage]:
    return [
        occurrence.raw_message
        for occurrence in sorted(import_record.message_occurrences, key=lambda item: item.source_index)
    ]


def _to_message(raw: RawMessage, occurrence: ImportMessageOccurrence | None = None) -> WhatsAppMessage:
    sent_at = raw.sent_at.astimezone(ZoneInfo("America/Sao_Paulo")) if raw.sent_at.tzinfo else raw.sent_at
    return WhatsAppMessage(
        source_index=occurrence.source_index if occurrence else raw.source_index,
        source_offset=occurrence.source_offset if occurrence else raw.source_offset,
        sent_at=sent_at,
        timestamp_precision=raw.timestamp_precision,
        sender_raw=raw.sender_raw,
        raw_content=raw.raw_content,
        content_sha256=raw.content_sha256,
        is_edited=raw.is_edited,
        is_deleted=raw.is_deleted,
    )


def import_whatsapp_file(
    session: Session,
    file_path: Path,
    user_id: uuid.UUID,
    settings: Settings | None = None,
    extractor: WorkoutExtractor | None = None,
) -> QualityReport:
    settings = settings or get_settings()
    if session.get(User, user_id) is None:
        raise ValueError(f"usuario nao encontrado: {user_id}")

    source_bytes = file_path.read_bytes()
    source_hash = sha256_bytes(source_bytes)
    import_record = session.scalar(
        select(Import)
        .options(
            selectinload(Import.message_occurrences).selectinload(ImportMessageOccurrence.raw_message),
            selectinload(Import.parse_runs),
        )
        .where(Import.user_id == user_id, Import.source_sha256 == source_hash)
    )
    is_new_import = import_record is None
    if import_record is None:
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
    else:
        previous_run = next(
            (run for run in import_record.parse_runs if run.parser_version == settings.parser_version),
            None,
        )
        if previous_run is not None:
            quality = (previous_run.metadata_ or {}).get("quality", {})
            return QualityReport(
                import_id=str(import_record.id),
                parse_run_id=str(previous_run.id),
                duplicate_import=True,
                **quality,
            )

    extractor = extractor or OpenAIWorkoutExtractor(settings)
    parse_run = ParseRun(
        import_record=import_record,
        parser_version=settings.parser_version,
        prompt_version=extractor.prompt_version if extractor else None,
        llm_model=extractor.model if extractor else None,
        llm_shadow_mode=settings.llm_shadow_mode,
        status=ParseRunStatus.PROCESSING.value,
        metadata_={},
    )
    session.add(parse_run)
    session.flush()

    if is_new_import:
        parsed_messages = split_whatsapp_messages(source_bytes.decode("utf-8-sig"))
        raw_messages, new_ids, identity_conflicts = _ingest_messages(session, import_record, user_id, parsed_messages)
        occurrences = sorted(import_record.message_occurrences, key=lambda item: item.source_index)
    else:
        occurrences = sorted(import_record.message_occurrences, key=lambda item: item.source_index)
        raw_messages = _messages_from_occurrences(import_record)
        new_ids = set()
        identity_conflicts = sum(_has_content_conflict(session, raw) for raw in raw_messages)

    messages = [_to_message(raw, occurrence) for raw, occurrence in zip(raw_messages, occurrences, strict=True)]
    outcomes = [_resolve_database_context(session, user_id, parse_workout_message(message)) for message in messages]
    normalized_conflicts, normalized_duplicate_count = _conflicting_message_indexes(messages, outcomes)
    report = QualityReport(
        import_id=str(import_record.id),
        parse_run_id=str(parse_run.id),
        reprocessed=not is_new_import,
        messages_total=len(messages),
        messages_new=len(new_ids),
        messages_reused=len(messages) - len(new_ids),
        conflicting_messages=identity_conflicts,
        normalized_duplicates=normalized_duplicate_count,
    )
    all_reasons: list[str] = []

    for position, (raw_message, message, outcome) in enumerate(zip(raw_messages, messages, outcomes, strict=True)):
        previous_for_version = _existing_result_for_version(session, raw_message.id, settings.parser_version)
        if previous_for_version is not None:
            session.add(
                ParseResult(
                    parse_run=parse_run,
                    raw_message=raw_message,
                    status=ParseResultStatus.REUSED.value,
                    parse_method=previous_for_version.parse_method,
                    payload={"reused_result_id": str(previous_for_version.id)},
                    reasons=[],
                )
            )
            continue

        reasons = review_reasons(outcome)
        if position in normalized_conflicts:
            reasons.append("duplicacao normalizada ou conflito temporal")
        if _has_content_conflict(session, raw_message):
            reasons.append("possivel edicao: mesmo remetente e horario com conteudo diferente")
        reasons = list(dict.fromkeys(reasons))

        result = ParseResult(
            parse_run=parse_run,
            raw_message=raw_message,
            status=ParseResultStatus.SKIPPED.value,
            parse_method=ParseMethod.RULE.value,
            payload=outcome.extraction.model_dump(mode="json") if outcome.extraction else None,
            reasons=reasons,
        )
        session.add(result)
        session.flush()
        if outcome.extraction is None:
            raw_message.parse_status = ParseStatus.SKIPPED.value
            report.messages_skipped += 1
            continue

        llm_proposal = None
        if reasons and extractor.available:
            try:
                llm_proposal = extractor.extract(message.raw_content, message.sent_at.date().isoformat())
                report.llm_proposals += 1
            except Exception as error:
                reasons.append(f"falha no extrator LLM: {type(error).__name__}")

        can_auto_accept_llm = (
            llm_proposal is not None
            and not settings.llm_shadow_mode
            and settings.llm_auto_accept
            and not review_reasons(ParseOutcome(extraction=llm_proposal, parse_method=ParseMethod.LLM.value))
        )
        if reasons and not can_auto_accept_llm:
            result.status = ParseResultStatus.REVIEW.value
            result.reasons = reasons
            result.payload = _payload_for_review(outcome, llm_proposal, extractor)
            raw_message.parse_status = ParseStatus.REVIEW.value
            session.add(
                ParseReview(
                    raw_message=raw_message,
                    parse_result=result,
                    reason="; ".join(reasons),
                    proposed_payload=result.payload,
                )
            )
            report.messages_pending_review += 1
            all_reasons.extend(reasons)
            continue

        accepted_extraction = llm_proposal if can_auto_accept_llm else outcome.extraction
        parse_method = ParseMethod.LLM.value if can_auto_accept_llm else ParseMethod.RULE.value
        result.parse_method = parse_method
        result.payload = accepted_extraction.model_dump(mode="json")
        report.sets_accepted += _materialize_extraction(
            session,
            user_id,
            raw_message,
            result,
            accepted_extraction,
            settings.parser_version,
            parse_method=parse_method,
            llm_model=extractor.model if can_auto_accept_llm else None,
        )
        _activate_result(session, result)
        report.messages_accepted += 1

    report.reasons = count_reasons(all_reasons)
    parse_run.status = (
        ParseRunStatus.COMPLETED_WITH_REVIEWS.value
        if report.messages_pending_review
        else ParseRunStatus.COMPLETED.value
    )
    parse_run.completed_at = datetime.now(UTC)
    quality_payload = report.model_dump(exclude={"import_id", "parse_run_id", "duplicate_import", "reprocessed"})
    parse_run.metadata_ = {"quality": quality_payload}
    import_record.status = (
        ImportStatus.COMPLETED_WITH_REVIEWS.value if report.messages_pending_review else ImportStatus.COMPLETED.value
    )
    import_record.metadata_ = {
        **(import_record.metadata_ or {}),
        "latest_parse_run_id": str(parse_run.id),
        "quality": quality_payload,
    }
    bump_data_revision(session)
    session.flush()
    return report
