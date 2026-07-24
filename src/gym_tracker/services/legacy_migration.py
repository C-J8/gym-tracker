import csv
import json
from collections import Counter
from pathlib import Path

from gym_tracker.schemas import QualityReport
from gym_tracker.services.parser import parse_workout_message, split_whatsapp_messages
from gym_tracker.services.validation import count_reasons, review_reasons
from gym_tracker.services.whatsapp_import import _conflicting_message_indexes

CSV_COLUMNS = ["data", "grupo_muscular", "exercicio", "tipo", "peso_kg", "serie", "repeticoes"]


def compare_legacy_sources(whatsapp_file: Path, legacy_csv: Path, output_dir: Path) -> dict:
    messages = split_whatsapp_messages(whatsapp_file.read_text(encoding="utf-8-sig"))
    outcomes = [parse_workout_message(message) for message in messages]
    conflicts, duplicate_count = _conflicting_message_indexes(messages, outcomes)
    rows: list[dict] = []
    pending: list[dict] = []
    reasons_all: list[str] = []
    accepted_messages = 0
    skipped_messages = 0

    for position, (message, outcome) in enumerate(zip(messages, outcomes, strict=True)):
        reasons = review_reasons(outcome)
        if position in conflicts:
            reasons.append("duplicacao normalizada ou conflito temporal")
        reasons = list(dict.fromkeys(reasons))
        if outcome.extraction is None:
            skipped_messages += 1
            continue
        if reasons:
            pending.append(
                {
                    "source_index": message.source_index,
                    "sent_at": message.sent_at.isoformat(),
                    "reasons": reasons,
                    "raw_content": message.raw_content,
                    "proposal": outcome.extraction.model_dump(mode="json"),
                }
            )
            reasons_all.extend(reasons)
            continue
        accepted_messages += 1
        for exercise in outcome.extraction.exercises:
            for set_number, item in enumerate(exercise.sets, start=1):
                rows.append(
                    {
                        "data": outcome.extraction.workout_date.isoformat(),
                        "grupo_muscular": exercise.muscle_group,
                        "exercicio": exercise.canonical_name,
                        "tipo": exercise.equipment,
                        "peso_kg": float(item.weight_kg),
                        "serie": set_number,
                        "repeticoes": item.reps,
                    }
                )

    output_dir.mkdir(parents=True, exist_ok=True)
    generated_csv = output_dir / "academia_treinos_reprocessado.csv"
    with generated_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    reviews_file = output_dir / "pendencias_reprocessamento.json"
    reviews_file.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")

    old_rows = list(csv.DictReader(legacy_csv.read_text(encoding="utf-8-sig").splitlines()))
    old_counter = Counter(_row_key(row) for row in old_rows)
    new_counter = Counter(_row_key(row) for row in rows)
    report = QualityReport(
        messages_total=len(messages),
        messages_accepted=accepted_messages,
        messages_skipped=skipped_messages,
        messages_pending_review=len(pending),
        sets_accepted=len(rows),
        normalized_duplicates=duplicate_count,
        reasons=count_reasons(reasons_all),
    )
    comparison = {
        "quality": report.model_dump(),
        "legacy_rows": len(old_rows),
        "legacy_exact_duplicates": sum(count - 1 for count in old_counter.values() if count > 1),
        "reprocessed_rows": len(rows),
        "removed_or_pending": sum((old_counter - new_counter).values()),
        "new_or_changed": sum((new_counter - old_counter).values()),
        "generated_csv": str(generated_csv),
        "pending_file": str(reviews_file),
    }
    (output_dir / "relatorio_qualidade.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return comparison


def _row_key(row: dict) -> tuple[str, ...]:
    values = []
    for column in CSV_COLUMNS:
        value = str(row.get(column, ""))
        if column == "peso_kg":
            value = f"{float(value):g}"
        values.append(value)
    return tuple(values)
