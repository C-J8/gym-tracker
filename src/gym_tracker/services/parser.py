import hashlib
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from gym_tracker.schemas import ExercisePayload, ParseOutcome, SetPayload, WhatsAppMessage, WorkoutExtraction
from gym_tracker.services.loads import parse_load
from gym_tracker.services.normalization import (
    normalize_equipment,
    normalize_text,
    resolve_static_exercise,
)

WHATSAPP_HEADER = re.compile(
    r"(?mx)^"
    r"(?:"
    r"\[(?P<bracket_date>\d{1,2}/\d{1,2}/\d{2,4})[ ,]+"
    r"(?P<bracket_time>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:-\s*)?"
    r"|"
    r"(?P<plain_date>\d{1,2}/\d{1,2}/\d{2,4})[ ,]+"
    r"(?P<plain_time>\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*"
    r")"
    r"(?P<head>.*)$"
)
LOAD_PATTERN = re.compile(r"(?<!\w)(?P<load>-?\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g)\b", re.IGNORECASE)
NAMELESS_LOAD_PATTERN = re.compile(
    r"^(?P<name>[^\d]{2,}?)\s+(?P<load>\d+(?:[.,]\d+)?)\s*[/|-]\s*(?P<rest>.*(?:rep|\d\s*x).*)$",
    re.IGNORECASE,
)
WORKOUT_TITLES = {"acad nova", "upper", "lower a", "lower b", "abd", "halter", "maquina", "cabo"}
EDIT_MARKERS = ("<mensagem editada>", "(editada)")
MEDIA_MARKERS = ("<midia oculta>", "<media omitted>")


def content_hash(sent_at: datetime, sender: str, content: str) -> str:
    material = f"{sent_at.isoformat()}\n{normalize_text(sender)}\n{content.strip()}".encode()
    return hashlib.sha256(material).hexdigest()


def split_whatsapp_messages(text: str, timezone: str = "America/Sao_Paulo") -> list[WhatsAppMessage]:
    clean_text = text.replace("\ufeff", "")
    matches = list(WHATSAPP_HEADER.finditer(clean_text))
    messages: list[WhatsAppMessage] = []
    zone = ZoneInfo(timezone)

    for source_index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[source_index + 1].start() if source_index + 1 < len(matches) else len(clean_text)
        head = match.group("head")
        sender, separator, first_line = head.partition(": ")
        if not separator:
            sender, first_line = "", head
        continuation = clean_text[body_start:body_end].strip("\r\n")
        raw_content = first_line + (f"\n{continuation}" if continuation else "")
        raw_content = raw_content.strip()
        normalized = normalize_text(raw_content)
        date_text = match.group("bracket_date") or match.group("plain_date")
        time_text = match.group("bracket_time") or match.group("plain_time")
        year_format = "%Y" if len(date_text.rsplit("/", 1)[-1]) == 4 else "%y"
        time_format = "%H:%M:%S" if time_text.count(":") == 2 else "%H:%M"
        sent_at = datetime.strptime(f"{date_text} {time_text}", f"%d/%m/{year_format} {time_format}").replace(
            tzinfo=zone
        )
        messages.append(
            WhatsAppMessage(
                source_index=source_index,
                source_offset=match.start(),
                sent_at=sent_at,
                timestamp_precision="second" if time_text.count(":") == 2 else "minute",
                sender_raw=sender.strip("\u200e "),
                raw_content=raw_content,
                content_sha256=content_hash(sent_at, sender, raw_content),
                is_edited=any(marker in normalized for marker in EDIT_MARKERS),
                is_deleted="mensagem apagada" in normalized or "message deleted" in normalized,
                is_media=any(marker in normalized for marker in MEDIA_MARKERS),
            )
        )
    return messages


def _strip_markers(content: str) -> str:
    return re.sub(r"<(?:Mensagem editada|Mídia oculta|Media omitted)>", "", content, flags=re.IGNORECASE)


def prepare_training_lines(content: str) -> tuple[str | None, list[str]]:
    workout_type: str | None = None
    lines: list[str] = []

    for raw_line in _strip_markers(content).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = normalize_text(line)
        if normalized in WORKOUT_TITLES:
            workout_type = line
            continue
        if "mensagem apagada" in normalized:
            continue
        is_punctuated_continuation = line.startswith(("...", "..", "."))
        is_reps_only_continuation = bool(re.fullmatch(r"(?:\d+\s*(?:rep|ep)?\s*[/.-]*\s*)+", normalized))
        if lines and _is_clear_continuation(line) and (is_punctuated_continuation or is_reps_only_continuation):
            separator = " " if is_punctuated_continuation else " ... "
            lines[-1] = f"{lines[-1]}{separator}{line}"
            continue
        lines.append(line)
    return workout_type, lines


def _is_clear_continuation(line: str) -> bool:
    normalized = normalize_text(line)
    return bool(LOAD_PATTERN.search(line) or re.fullmatch(r"[. /-]*(?:\d+\s*(?:rep)?[ /.-]*)+", normalized))


def _expand_repetitions(segment: str) -> list[int]:
    normalized = normalize_text(segment).replace(",", ".")
    normalized = re.sub(r"(\d+)\s*ep\b", r"\1rep", normalized)
    multiplier_match = re.search(r"(?:/|\s)(\d+)\s*x\b", normalized)
    shorthand_match = re.search(r"(\d+)\s*rep?\s*/\s*(\d+)\s*$", normalized)
    if shorthand_match and int(shorthand_match.group(2)) <= 5:
        return [int(shorthand_match.group(1))] * int(shorthand_match.group(2))

    cleaned = re.sub(r"(?:/|\s)\d+\s*x\b", "", normalized)
    reps = [int(value) for value in re.findall(r"(\d+)\s*(?:rep)?(?=\s*(?:[/.-]|$))", cleaned)]
    reps = [value for value in reps if 1 <= value <= 200]
    if multiplier_match and len(reps) == 1:
        reps *= int(multiplier_match.group(1))
    return reps


def parse_set_line(line: str) -> tuple[str, list[SetPayload], list[str]]:
    normalized = normalize_text(line)
    if "n fiz" in normalized or "nao fiz" in normalized:
        return "", [], []

    cleaned = re.sub(r"\([^)]*\)", "", line)
    load_matches = list(LOAD_PATTERN.finditer(cleaned))
    reasons: list[str] = []

    if not load_matches:
        chunks = [chunk.strip() for chunk in re.split(r"\.{2,}", cleaned) if chunk.strip()]
        without_unit = NAMELESS_LOAD_PATTERN.match(chunks[0]) if chunks else None
        if without_unit is None:
            return "", [], ["linha de treino nao reconhecida"]
        raw_name = without_unit.group("name").strip(" -:")
        parsed_without_unit: list[SetPayload] = []
        continuation_pattern = re.compile(
            r"^(?P<load>\d+(?:[.,]\d+)?)\s*[/|-]\s*(?P<rest>.*)$",
            re.IGNORECASE,
        )
        for index, chunk in enumerate(chunks):
            match = without_unit if index == 0 else continuation_pattern.match(chunk)
            if match is None:
                return raw_name, [], ["linha sem unidade nao reconhecida"]
            load = Decimal(match.group("load").replace(",", "."))
            reps = _expand_repetitions(match.group("rest"))
            if not reps:
                return raw_name, [], ["repeticoes ausentes ou invalidas"]
            parsed_without_unit.extend(SetPayload(weight_kg=load, reps=rep) for rep in reps)
        reasons.append("carga sem unidade")
        return raw_name, parsed_without_unit, reasons

    raw_name = cleaned[: load_matches[0].start()].replace("-", " ").strip(" -:")
    if not raw_name:
        reasons.append("exercicio sem nome")
        raw_name = "Exercicio sem nome"

    parsed_sets: list[SetPayload] = []
    for index, load_match in enumerate(load_matches):
        segment_end = load_matches[index + 1].start() if index + 1 < len(load_matches) else len(cleaned)
        segment = cleaned[load_match.end() : segment_end]
        parsed_load = parse_load(load_match.group("load"), load_match.group("unit"))
        reasons.extend(parsed_load.reasons)
        if parsed_load.weight_kg is None or parsed_load.weight_kg < 0:
            continue
        load = parsed_load.weight_kg
        reps = _expand_repetitions(segment)
        if not reps:
            reasons.append(f"repeticoes ausentes para {load:g} kg")
            continue
        parsed_sets.extend(SetPayload(weight_kg=load, reps=rep) for rep in reps)
    return raw_name, parsed_sets, reasons


def parse_workout_message(message: WhatsAppMessage) -> ParseOutcome:
    if message.is_deleted or message.is_media or not message.sender_raw:
        return ParseOutcome()

    workout_type, lines = prepare_training_lines(message.raw_content)
    exercises: list[ExercisePayload] = []
    reasons: list[str] = []
    consumed: list[str] = []
    notes: list[str] = []

    for line in lines:
        notes.extend(re.findall(r"\(([^)]*)\)", line))
        raw_name, sets, line_reasons = parse_set_line(line)
        if not raw_name and not sets and not line_reasons:
            consumed.append(line)
            continue
        if not sets:
            reasons.extend(f"{reason}: {line}" for reason in line_reasons)
            continue

        match = resolve_static_exercise(raw_name, workout_type)
        if match is None:
            reasons.append(f"exercicio desconhecido: {raw_name}")
            exercises.append(
                ExercisePayload(
                    raw_name=raw_name,
                    sets=sets,
                    uncertain_fields=["canonical_name", "muscle_group", "equipment"],
                    needs_review=True,
                )
            )
            consumed.append(line)
            continue

        uncertain_fields = ["weight_kg"] if line_reasons else []
        if match.equipment is None:
            uncertain_fields.append("equipment")
            line_reasons.append(f"equipamento nao determinado: {raw_name}")
        exercises.append(
            ExercisePayload(
                raw_name=raw_name,
                canonical_name=match.canonical_name,
                muscle_group=match.muscle_group,
                equipment=match.equipment,
                load_basis=match.load_basis,
                sets=sets,
                uncertain_fields=uncertain_fields,
                needs_review=bool(line_reasons),
            )
        )
        reasons.extend(f"{reason}: {line}" for reason in line_reasons)
        consumed.append(line)

    if not exercises and not reasons:
        return ParseOutcome()

    extraction = WorkoutExtraction(
        workout_date=message.sent_at.date(),
        workout_type=workout_type,
        notes="; ".join(notes) or None,
        exercises=exercises,
        unconsumed_text=[line for line in lines if line not in consumed],
        needs_review=bool(reasons),
    )
    if extraction.unconsumed_text:
        reasons.append("parte relevante da mensagem nao consumida")
    return ParseOutcome(extraction=extraction, reasons=reasons, consumed_lines=consumed)


def normalized_set_signatures(outcome: ParseOutcome) -> set[tuple[str, str, str, str]]:
    if not outcome.extraction:
        return set()
    signatures = set()
    for exercise in outcome.extraction.exercises:
        for item in exercise.sets:
            signatures.add(
                (
                    exercise.canonical_name or normalize_text(exercise.raw_name),
                    normalize_equipment(exercise.equipment),
                    str(item.weight_kg.normalize()),
                    str(item.reps),
                )
            )
    return signatures
