from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gym_tracker.schemas import WhatsAppMessage
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
        ("Triceps 22.5g/6rep", [(22.5, 6)]),
        ("Flexora 30kg - 12rep/2", [(30, 12), (30, 12)]),
    ],
)
def test_parse_supported_set_formats(line: str, expected: list[tuple[float, int]]) -> None:
    _, sets, reasons = parse_set_line(line)
    assert [(float(item.weight_kg), item.reps) for item in sets] == expected
    assert reasons == []


def test_line_without_kg_is_not_attached_and_requires_review() -> None:
    text = "01/01/2026 10:00 - Pessoa: Puxada alta 40kg/10rep\nBarra 14/10rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    assert len(outcome.extraction.exercises) == 2
    assert any("carga sem unidade" in reason for reason in outcome.reasons)
    barra_sets = outcome.extraction.exercises[1].sets
    assert [(float(item.weight_kg), item.reps) for item in barra_sets] == [(14.0, 10)]


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


def test_abd_alias_resolves_to_abdominal_machine() -> None:
    text = "01/01/2026 10:00 - Pessoa: Abd 20kg/10rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    exercise = outcome.extraction.exercises[0]
    assert (exercise.canonical_name, exercise.muscle_group, exercise.equipment) == (
        "Abdominal",
        "Abdômen",
        "Máquina",
    )


def test_message_metadata_and_multiple_senders() -> None:
    text = (
        "01/01/2026 10:00 - Pessoa A: Supino reto 20kg/10rep <Mensagem editada>\n"
        "01/01/2026 11:00 - Pessoa B: Mensagem apagada"
    )
    messages = split_whatsapp_messages(text)
    assert messages[0].is_edited is True
    assert messages[1].is_deleted is True
    assert messages[0].sender_raw != messages[1].sender_raw


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


def test_chest_equipment_is_classified_per_set_load() -> None:
    text = "01/01/2026 10:00 - Pessoa: Supino reto 20kg/10rep ... 30kg/8rep ... 45kg/5rep"
    outcome = parse_workout_message(split_whatsapp_messages(text)[0])
    assert outcome.extraction is not None
    variants = {
        exercise.equipment: [float(item.weight_kg) for item in exercise.sets]
        for exercise in outcome.extraction.exercises
    }
    assert variants == {"Halter": [20.0], "Máquina com anilha": [30.0], "Máquina": [45.0]}
