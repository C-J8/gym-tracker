import csv
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.config import get_settings
from gym_tracker.models import Import, ImportStatus, ParseMethod, ParseStatus, RawMessage, User, Workout, WorkoutSet
from gym_tracker.repositories.catalog import get_or_create_exercise, get_or_create_variant
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
    report = QualityReport(import_id=str(record.id), messages_total=len(rows))
    workouts: dict[date, Workout] = {}
    signatures: set[tuple] = set()
    series_counters: dict[tuple[date, str, str], int] = defaultdict(int)

    for source_index, row in enumerate(rows):
        workout_date = date.fromisoformat(row["data"])
        signature = tuple(row[column] for column in sorted(REQUIRED_COLUMNS))
        sent_at = datetime.combine(workout_date, time(), ZoneInfo("America/Sao_Paulo"))
        raw_content = json.dumps(row, ensure_ascii=False, sort_keys=True)
        raw = RawMessage(
            import_record=record,
            source_index=source_index,
            sent_at=sent_at,
            sender_raw="legacy-csv",
            raw_content=raw_content,
            content_sha256=hashlib.sha256(f"{source_index}:{raw_content}".encode()).hexdigest(),
            parse_status=ParseStatus.ACCEPTED.value,
        )
        session.add(raw)
        if signature in signatures:
            raw.parse_status = ParseStatus.DUPLICATE.value
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
    record.metadata_ = {"quality": report.model_dump(exclude={"import_id", "duplicate_import"})}
    session.flush()
    return report
