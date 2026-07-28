from collections import Counter

from gym_tracker.schemas import ParseOutcome
from gym_tracker.services.loads import MAX_PLAUSIBLE_LOAD_KG, MIN_PLAUSIBLE_LOAD_KG


def review_reasons(outcome: ParseOutcome, known_alias: bool = True) -> list[str]:
    reasons = list(outcome.reasons)
    extraction = outcome.extraction
    if extraction is None:
        return reasons
    if not known_alias:
        reasons.append("exercicio ou alias desconhecido")
    if extraction.unconsumed_text:
        reasons.append("tokens relevantes nao consumidos")
    for exercise in extraction.exercises:
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
