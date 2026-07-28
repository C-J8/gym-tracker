import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gym_tracker.schemas import WhatsAppMessage
from gym_tracker.services.normalization import explicit_equipment
from gym_tracker.services.parser import (
    content_hash,
    parse_set_line,
    parse_workout_message,
    split_whatsapp_messages,
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Extensora 50kg - 8rep/2x", [(50, 8), (50, 8)]),
        ("Puxada 20kg/10rep ... 30kg/8rep", [(20, 10), (30, 8)]),
        ("Rosca 12,5kg/10rep", [(12.5, 10)]),
        ("Flexora 30kg - 12rep/2", [(30, 12), (30, 12)]),
    ],
)
def test_parse_supported_set_formats(line: str, expected: list[tuple[float, int]]) -> None:
    _, sets, reasons = parse_set_line(line)
    assert [(float(item.weight_kg), item.reps) for item in sets] == expected
    assert reasons == []


@pytest.mark.parametrize(
    ("line", "expected_weight", "reason_fragment"),
    [
        ("Rosca 500g/10rep", 0.5, None),
        ("Rosca 500 g/10rep", 0.5, None),
        ("Rosca 0,5kg/10rep", 0.5, None),
        ("Rosca 22.5g/10rep", 0.0225, "abaixo do limite"),
        ("Rosca 22,5 kg/10rep", 22.5, None),
        ("Rosca 0kg/10rep", 0, "maior que zero"),
        ("Rosca 600kg/10rep", 600, "acima do limite"),
    ],
)
def test_load_units_and_suspicious_values(
    line: str,
    expected_weight: float,
    reason_fragment: str | None,
) -> None:
    _, sets, reasons = parse_set_line(line)
    assert float(sets[0].weight_kg) == pytest.approx(expected_weight)
    if reason_fragment:
        assert any(reason_fragment in reason for reason in reasons)
    else:
        assert reasons == []


def test_negative_load_is_preserved_as_review_reason_without_a_set() -> None:
    _, sets, reasons = parse_set_line("Rosca -5kg/10rep")
    assert sets == []
    assert "carga deve ser maior que zero" in reasons


def test_line_without_kg_is_not_silently_accepted() -> None:
    text = "01/01/2026 10:00 - Pessoa: Puxada alta 40kg/10rep\nBarra 14/10rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert len(outcome.extraction.exercises) == 2
    assert any("carga sem unidade" in reason for reason in outcome.reasons)


def test_multiple_unitless_loads_keep_their_own_repetitions() -> None:
    _, sets, reasons = parse_set_line("Barra 14/10rep ... 7/5ep")
    assert [(float(item.weight_kg), item.reps) for item in sets] == [(14.0, 10), (7.0, 5)]
    assert reasons == ["carga sem unidade"]


def test_clear_continuation_and_observation() -> None:
    text = "01/01/2026 10:00 - Pessoa: Puxada alta 40kg/10rep\n... 45kg/8rep (subi carga)"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert len(outcome.extraction.exercises) == 1
    assert [item.reps for item in outcome.extraction.exercises[0].sets] == [10, 8]
    assert outcome.extraction.notes == "subi carga"


def test_reps_only_line_is_a_clear_continuation() -> None:
    text = "01/01/2026 10:00 - Pessoa: Abdutora 40kg/10\n10rep/10"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    sets = outcome.extraction.exercises[0].sets
    assert [item.reps for item in sets] == [10, 10, 10]


def test_unstructured_line_is_not_attached_to_previous_exercise() -> None:
    text = "01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep\nhoje foi pesado"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert any("hoje foi pesado" in reason for reason in outcome.reasons)
    assert len(outcome.extraction.exercises[0].sets) == 1


def test_explicit_equipment_is_used_and_load_never_changes_it() -> None:
    text = "01/01/2026 10:00 - Pessoa: Supino reto halter 20kg/10rep ... 45kg/5rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert len(outcome.extraction.exercises) == 1
    exercise = outcome.extraction.exercises[0]
    assert exercise.equipment == "Halter"
    assert [float(item.weight_kg) for item in exercise.sets] == [20.0, 45.0]


def test_ambiguous_equipment_stays_unknown_for_all_loads() -> None:
    text = "01/01/2026 10:00 - Pessoa: Supino reto 20kg/10rep ... 45kg/5rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert len(outcome.extraction.exercises) == 1
    assert outcome.extraction.exercises[0].equipment is None
    assert any("equipamento nao determinado" in reason for reason in outcome.reasons)


def test_explicit_equipment_block_context_is_applied() -> None:
    text = "01/01/2026 10:00 - Pessoa: Máquina\nSupino reto 40kg/10rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert outcome.extraction.exercises[0].equipment == "Máquina"


@pytest.mark.parametrize("name", ["Bíceps hack", "Bíceps zottman", "Bíceps zootman"])
def test_exercise_technique_names_are_not_explicit_equipment(name: str) -> None:
    assert explicit_equipment(name) is None


def test_unknown_exercise_goes_to_review() -> None:
    now = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))
    content = "Movimento misterioso 10kg/10rep"
    message = WhatsAppMessage(
        source_index=0,
        sent_at=now,
        sender_raw="Pessoa",
        raw_content=content,
        content_sha256=content_hash(now, "Pessoa", content),
    )
    outcome = parse_workout_message(message)
    assert outcome.needs_review
    assert outcome.extraction is not None
    assert outcome.extraction.exercises[0].canonical_name is None


def test_whatsapp_format_golden_fixture() -> None:
    text = Path("tests/fixtures/whatsapp_formats.txt").read_text(encoding="utf-8")
    expected = json.loads(Path("tests/fixtures/whatsapp_formats_expected.json").read_text(encoding="utf-8"))
    messages = split_whatsapp_messages(text)
    actual = [
        {
            "source_index": item.source_index,
            "timestamp": item.sent_at.isoformat(),
            "precision": item.timestamp_precision,
            "sender": item.sender_raw,
            "edited": item.is_edited,
            "deleted": item.is_deleted,
            "media": item.is_media,
            "content": item.raw_content,
        }
        for item in messages
    ]
    assert actual == expected
    assert [item.source_offset for item in messages] == sorted(item.source_offset for item in messages)


def test_multiple_exercises_in_same_message_are_preserved() -> None:
    text = "[24/07/2026, 13:45:12] Pessoa: Extensora 40kg/10rep\nFlexora 30kg/8rep\nAbdutora 50kg/10rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert [exercise.canonical_name for exercise in outcome.extraction.exercises] == [
        "Extensora",
        "Flexora",
        "Abdutora",
    ]
