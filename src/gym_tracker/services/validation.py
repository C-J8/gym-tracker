import enum
from collections import Counter

from gym_tracker.schemas import ParseOutcome
from gym_tracker.services.loads import MAX_PLAUSIBLE_LOAD_KG, MIN_PLAUSIBLE_LOAD_KG


class ReviewReasonKind(enum.StrEnum):
    SEMANTIC = "semantic"
    SOURCE_INTEGRITY = "source_integrity"
    HUMAN_JUDGMENT = "human_judgment"


SOURCE_INTEGRITY_PREFIXES = (
    "possivel edicao",
    "duplicacao normalizada",
    "conflito temporal",
    "conflito de conteudo",
    "identidade ambigua",
)
HUMAN_JUDGMENT_PREFIXES = (
    "carga invalida",
    "carga deve",
    "carga acima",
    "carga abaixo",
    "carga fora",
    "possivel erro de unidade",
    "repeticoes fora",
)


def classify_review_reason(reason: str) -> ReviewReasonKind:
    normalized = reason.casefold().strip()
    if normalized.startswith(SOURCE_INTEGRITY_PREFIXES):
        return ReviewReasonKind.SOURCE_INTEGRITY
    if normalized.startswith(HUMAN_JUDGMENT_PREFIXES):
        return ReviewReasonKind.HUMAN_JUDGMENT
    return ReviewReasonKind.SEMANTIC


def llm_auto_accept_blockers(reasons: list[str]) -> list[str]:
    return [
        reason
        for reason in reasons
        if classify_review_reason(reason) in {ReviewReasonKind.SOURCE_INTEGRITY, ReviewReasonKind.HUMAN_JUDGMENT}
    ]


def review_reasons(outcome: ParseOutcome, known_alias: bool = True) -> list[str]:
    reasons = list(outcome.reasons)
    extraction = outcome.extraction
    if extraction is None:
        return reasons
    if extraction.needs_review:
        reasons.append("payload marcado para revisao")
    if not known_alias:
        reasons.append("exercicio ou alias desconhecido")
    if extraction.unconsumed_text:
        reasons.append("tokens relevantes nao consumidos")
    for exercise in extraction.exercises:
        if exercise.needs_review:
            reasons.append(f"exercicio marcado para revisao: {exercise.raw_name}")
        if exercise.uncertain_fields:
            reasons.append(f"campos incertos para {exercise.raw_name}: {', '.join(exercise.uncertain_fields)}")
        if not exercise.canonical_name or not exercise.muscle_group or not exercise.equipment:
            reasons.append(f"campos ausentes para {exercise.raw_name}")
        for item in exercise.sets:
            if not MIN_PLAUSIBLE_LOAD_KG <= item.weight_kg <= MAX_PLAUSIBLE_LOAD_KG:
                reasons.append(f"carga fora do limite esperado: {item.weight_kg}")
            if item.reps > 100:
                reasons.append(f"repeticoes fora do limite esperado: {item.reps}")
    return list(dict.fromkeys(reasons))


def count_reasons(reasons: list[str]) -> dict[str, int]:
    categories = [reason.split(":", 1)[0] for reason in reasons]
    return dict(Counter(categories))
