import csv
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.config import get_settings
from gym_tracker.models import (
    Import,
    ImportMessageOccurrence,
    ImportStatus,
    ParseMethod,
    ParseResult,
    ParseResultStatus,
    ParseRun,
    ParseRunStatus,
    ParseStatus,
    RawMessage,
    User,
    Workout,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import get_or_create_exercise, get_or_create_variant
from gym_tracker.repositories.state import bump_data_revision
from gym_tracker.schemas import QualityReport

REQUIRED_COLUMNS = {"data", "grupo_muscular", "exercicio", "tipo", "peso_kg", "serie", "repeticoes"}


def import_legacy_csv(session: Session, file_path: Path, user_id: uuid.UUID) -> QualityReport:
    if session.get(User, user_id) is None:
        raise ValueError(f"usuario nao encontrado: {user_id}")
    content = file_path.read_bytes()
    source_hash = hashlib.sha256(content).hexdigest()
    previous = session.scalar(select(Import).where(Import.user_id == user_id, Import.source_sha256 == source_hash))
    if previous:
        return QualityReport(import_id=str(previous.id), duplicate_import=True, **previous.metadata_.get("quality", {}))

    rows = list(csv.DictReader(content.decode("utf-8-sig").splitlines()))
    if not rows or not REQUIRED_COLUMNS.issubset(rows[0]):
        raise ValueError(f"CSV deve conter as colunas: {', '.join(sorted(REQUIRED_COLUMNS))}")

    settings = get_settings()
    record = Import(
        user_id=user_id,
        source_filename=file_path.name,
        source_sha256=source_hash,
        parser_version=f"legacy-csv/{settings.parser_version}",
        status=ImportStatus.PROCESSING.value,
        metadata_={},
    )
    session.add(record)
    session.flush()
    parse_run = ParseRun(
        import_record=record,
        parser_version=f"legacy-csv/{settings.parser_version}",
        llm_shadow_mode=True,
        status=ParseRunStatus.PROCESSING.value,
        metadata_={},
    )
    session.add(parse_run)
    session.flush()
    report = QualityReport(import_id=str(record.id), parse_run_id=str(parse_run.id), messages_total=len(rows))
    workouts: dict[date, Workout] = {}
    signatures: set[tuple] = set()
    series_counters: dict[tuple[date, str, str], int] = defaultdict(int)

    for source_index, row in enumerate(rows):
        workout_date = date.fromisoformat(row["data"])
        signature = tuple(row[column] for column in sorted(REQUIRED_COLUMNS))
        sent_at = datetime.combine(workout_date, time(), ZoneInfo("America/Sao_Paulo"))
        raw_content = json.dumps(row, ensure_ascii=False, sort_keys=True)
        identity_hash = hashlib.sha256(f"{user_id}:{source_index}:{raw_content}".encode()).hexdigest()
        raw = RawMessage(
            first_import=record,
            user_id=user_id,
            source_index=source_index,
            source_offset=source_index,
            sent_at=sent_at,
            timestamp_precision="minute",
            sender_raw="legacy-csv",
            sender_normalized="legacy-csv",
            raw_content=raw_content,
            content_sha256=identity_hash,
            normalized_content_sha256=hashlib.sha256(raw_content.encode()).hexdigest(),
            identity_sha256=identity_hash,
            occurrence_ordinal=0,
            parse_status=ParseStatus.ACCEPTED.value,
        )
        session.add(raw)
        session.flush()
        session.add(
            ImportMessageOccurrence(
                import_record=record,
                raw_message=raw,
                source_index=source_index,
                source_offset=source_index,
                occurrence_ordinal=0,
            )
        )
        result = ParseResult(
            parse_run=parse_run,
            raw_message=raw,
            status=ParseResultStatus.ACCEPTED.value,
            parse_method=ParseMethod.RULE.value,
            payload=row,
            reasons=[],
            is_active=True,
            activated_at=datetime.now(UTC),
        )
        session.add(result)
        session.flush()
        if signature in signatures:
            raw.parse_status = ParseStatus.DUPLICATE.value
            result.status = ParseResultStatus.REUSED.value
            result.is_active = False
            result.activated_at = None
            report.duplicate_messages += 1
            report.normalized_duplicates += 1
            continue
        signatures.add(signature)
        workout = workouts.get(workout_date)
        if workout is None:
            workout = Workout(user_id=user_id, workout_date=workout_date, source="legacy_csv")
            workouts[workout_date] = workout
            session.add(workout)
        exercise = get_or_create_exercise(session, row["exercicio"], row["grupo_muscular"])
        variant = get_or_create_variant(session, exercise, row["tipo"], "total")
        key = (workout_date, row["exercicio"], row["tipo"])
        series_counters[key] += 1
        session.add(
            WorkoutSet(
                workout=workout,
                exercise_variant=variant,
                raw_message=raw,
                parse_result=result,
                set_number=series_counters[key],
                weight_kg=Decimal(row["peso_kg"].replace(",", ".")),
                reps=int(row["repeticoes"]),
                parse_method=ParseMethod.RULE.value,
                parser_version=f"legacy-csv/{settings.parser_version}",
            )
        )
        report.messages_accepted += 1
        report.sets_accepted += 1

    record.status = ImportStatus.COMPLETED.value
    parse_run.status = ParseRunStatus.COMPLETED.value
    parse_run.completed_at = datetime.now(UTC)
    quality = report.model_dump(exclude={"import_id", "parse_run_id", "duplicate_import"})
    parse_run.metadata_ = {"quality": quality}
    record.metadata_ = {"quality": quality, "latest_parse_run_id": str(parse_run.id)}
    bump_data_revision(session)
    session.flush()
    return report
