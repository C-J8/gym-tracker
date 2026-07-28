import enum
import re
from collections import Counter

from gym_tracker.schemas import ParseOutcome, WorkoutExtraction
from gym_tracker.services.loads import (
    MAX_PLAUSIBLE_LOAD_KG,
    MIN_PLAUSIBLE_LOAD_KG,
    validate_materializable_load,
)


class ReviewReasonKind(enum.StrEnum):
    SEMANTIC_SOLVABLE = "semantic_solvable"
    LLM_UNCERTAINTY = "llm_uncertainty"
    MANDATORY_MISSING = "mandatory_missing"
    AMBIGUOUS_OR_SUSPICIOUS = "ambiguous_or_suspicious"
    SOURCE_INTEGRITY = "source_integrity"
    UNKNOWN = "unknown"


class ReviewReasonCode(enum.StrEnum):
    UNKNOWN_EXERCISE = "exercicio desconhecido"
    UNKNOWN_ALIAS = "exercicio ou alias desconhecido"
    EQUIPMENT_NOT_DETERMINED = "equipamento nao determinado"
    PAYLOAD_NEEDS_REVIEW = "payload marcado para revisao"
    EXERCISE_NEEDS_REVIEW = "exercicio marcado para revisao"
    UNCERTAIN_FIELDS = "campos incertos"
    MISSING_FIELDS = "campos ausentes"
    MISSING_LOAD_UNIT = "carga sem unidade"
    MISSING_REPETITIONS = "repeticoes ausentes"
    MISSING_OR_INVALID_REPETITIONS = "repeticoes ausentes ou invalidas"
    NO_EXERCISES = "proposta sem exercicios"
    EXERCISE_WITHOUT_SETS = "exercicio sem series"
    NO_MATERIALIZABLE_SETS = "proposta sem series materializaveis"
    UNMATERIALIZABLE_SET = "serie nao materializavel"
    PROPOSAL_SOURCE_MISMATCH = "proposta diverge dos valores da origem"
    UNCONSUMED_CONTENT = "parte relevante da mensagem nao consumida"
    UNCONSUMED_TOKENS = "tokens relevantes nao consumidos"
    INVALID_LOAD = "carga invalida"
    NON_POSITIVE_LOAD = "carga deve ser maior que zero"
    LOAD_ABOVE_LIMIT = "carga acima do limite plausivel"
    LOAD_BELOW_LIMIT = "carga abaixo do limite plausivel"
    LOAD_OUTSIDE_EXPECTED = "carga fora do limite esperado"
    POSSIBLE_UNIT_ERROR = "possivel erro de unidade"
    REPS_OUTSIDE_EXPECTED = "repeticoes fora do limite esperado"
    POSSIBLE_EDIT = "possivel edicao"
    NORMALIZED_DUPLICATE = "duplicacao normalizada ou conflito temporal"
    TEMPORAL_CONFLICT = "conflito temporal"
    CONTENT_CONFLICT = "conflito de conteudo"
    AMBIGUOUS_IDENTITY = "identidade ambigua"


REASON_KINDS = {
    ReviewReasonCode.UNKNOWN_EXERCISE: ReviewReasonKind.SEMANTIC_SOLVABLE,
    ReviewReasonCode.UNKNOWN_ALIAS: ReviewReasonKind.SEMANTIC_SOLVABLE,
    ReviewReasonCode.EQUIPMENT_NOT_DETERMINED: ReviewReasonKind.SEMANTIC_SOLVABLE,
    ReviewReasonCode.MISSING_FIELDS: ReviewReasonKind.SEMANTIC_SOLVABLE,
    ReviewReasonCode.PAYLOAD_NEEDS_REVIEW: ReviewReasonKind.LLM_UNCERTAINTY,
    ReviewReasonCode.EXERCISE_NEEDS_REVIEW: ReviewReasonKind.LLM_UNCERTAINTY,
    ReviewReasonCode.UNCERTAIN_FIELDS: ReviewReasonKind.LLM_UNCERTAINTY,
    ReviewReasonCode.MISSING_LOAD_UNIT: ReviewReasonKind.MANDATORY_MISSING,
    ReviewReasonCode.MISSING_REPETITIONS: ReviewReasonKind.MANDATORY_MISSING,
    ReviewReasonCode.MISSING_OR_INVALID_REPETITIONS: ReviewReasonKind.MANDATORY_MISSING,
    ReviewReasonCode.NO_EXERCISES: ReviewReasonKind.MANDATORY_MISSING,
    ReviewReasonCode.EXERCISE_WITHOUT_SETS: ReviewReasonKind.MANDATORY_MISSING,
    ReviewReasonCode.NO_MATERIALIZABLE_SETS: ReviewReasonKind.MANDATORY_MISSING,
    ReviewReasonCode.UNMATERIALIZABLE_SET: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.PROPOSAL_SOURCE_MISMATCH: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.UNCONSUMED_CONTENT: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.UNCONSUMED_TOKENS: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.INVALID_LOAD: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.NON_POSITIVE_LOAD: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.LOAD_ABOVE_LIMIT: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.LOAD_BELOW_LIMIT: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.LOAD_OUTSIDE_EXPECTED: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.POSSIBLE_UNIT_ERROR: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.REPS_OUTSIDE_EXPECTED: ReviewReasonKind.AMBIGUOUS_OR_SUSPICIOUS,
    ReviewReasonCode.POSSIBLE_EDIT: ReviewReasonKind.SOURCE_INTEGRITY,
    ReviewReasonCode.NORMALIZED_DUPLICATE: ReviewReasonKind.SOURCE_INTEGRITY,
    ReviewReasonCode.TEMPORAL_CONFLICT: ReviewReasonKind.SOURCE_INTEGRITY,
    ReviewReasonCode.CONTENT_CONFLICT: ReviewReasonKind.SOURCE_INTEGRITY,
    ReviewReasonCode.AMBIGUOUS_IDENTITY: ReviewReasonKind.SOURCE_INTEGRITY,
}

DERIVED_UNCERTAINTY_CODES = {
    ReviewReasonCode.PAYLOAD_NEEDS_REVIEW,
    ReviewReasonCode.EXERCISE_NEEDS_REVIEW,
    ReviewReasonCode.UNCERTAIN_FIELDS,
}

_DYNAMIC_REASON_PATTERNS = (
    (re.compile(r"^campos incertos para .+$"), ReviewReasonCode.UNCERTAIN_FIELDS),
    (re.compile(r"^campos ausentes para .+$"), ReviewReasonCode.MISSING_FIELDS),
    (re.compile(r"^repeticoes ausentes para \d+(?:[.,]\d+)? kg$"), ReviewReasonCode.MISSING_REPETITIONS),
    (re.compile(r"^exercicio sem series(?: para)? .+$"), ReviewReasonCode.EXERCISE_WITHOUT_SETS),
    (re.compile(r"^serie nao materializavel(?: para)? .+$"), ReviewReasonCode.UNMATERIALIZABLE_SET),
    (re.compile(r"^carga acima do limite plausivel de \d+(?:[.,]\d+)? kg$"), ReviewReasonCode.LOAD_ABOVE_LIMIT),
    (re.compile(r"^carga abaixo do limite plausivel de \d+(?:[.,]\d+)? kg$"), ReviewReasonCode.LOAD_BELOW_LIMIT),
    (re.compile(r"^carga fora do limite esperado$"), ReviewReasonCode.LOAD_OUTSIDE_EXPECTED),
    (re.compile(r"^repeticoes fora do limite esperado$"), ReviewReasonCode.REPS_OUTSIDE_EXPECTED),
)


def review_reason_code(reason: str) -> ReviewReasonCode | None:
    normalized = reason.casefold().strip()
    stem = normalized.split(":", 1)[0].strip()
    stem = stem.split(";", 1)[0].strip()
    try:
        return ReviewReasonCode(stem)
    except ValueError:
        for pattern, code in _DYNAMIC_REASON_PATTERNS:
            if pattern.fullmatch(stem):
                return code
    return None


def classify_review_reason(reason: str) -> ReviewReasonKind:
    code = review_reason_code(reason)
    return REASON_KINDS.get(code, ReviewReasonKind.UNKNOWN)


def llm_auto_accept_blockers(reasons: list[str]) -> list[str]:
    classified = [(reason, review_reason_code(reason), classify_review_reason(reason)) for reason in reasons]
    concrete = [item for item in classified if item[1] not in DERIVED_UNCERTAINTY_CODES]
    semantic_context_only = bool(concrete) and all(
        kind == ReviewReasonKind.SEMANTIC_SOLVABLE for _, _, kind in concrete
    )
    return [
        reason
        for reason, code, kind in classified
        if kind != ReviewReasonKind.SEMANTIC_SOLVABLE
        and not (code in DERIVED_UNCERTAINTY_CODES and semantic_context_only)
    ]


def extraction_materialization_reasons(extraction: WorkoutExtraction) -> list[str]:
    reasons: list[str] = []
    if not extraction.exercises:
        reasons.append(ReviewReasonCode.NO_EXERCISES.value)

    materializable_sets = 0
    for exercise in extraction.exercises:
        if not exercise.sets:
            reasons.append(f"{ReviewReasonCode.EXERCISE_WITHOUT_SETS.value}: {exercise.raw_name}")
            continue
        for item in exercise.sets:
            try:
                validate_materializable_load(item.weight_kg)
                valid_reps = isinstance(item.reps, int) and 1 <= item.reps <= 200
            except (AttributeError, TypeError, ValueError):
                valid_reps = False
            if not valid_reps:
                reasons.append(f"{ReviewReasonCode.UNMATERIALIZABLE_SET.value}: {exercise.raw_name}")
                continue
            materializable_sets += 1

    if materializable_sets == 0:
        reasons.append(ReviewReasonCode.NO_MATERIALIZABLE_SETS.value)
    return list(dict.fromkeys(reasons))


def llm_source_evidence_reasons(
    deterministic: WorkoutExtraction,
    proposal: WorkoutExtraction,
) -> list[str]:
    if proposal.workout_date != deterministic.workout_date:
        return [ReviewReasonCode.PROPOSAL_SOURCE_MISMATCH.value]

    try:
        deterministic_sets = Counter(
            (item.weight_kg, item.reps, item.rpe, item.rir)
            for exercise in deterministic.exercises
            for item in exercise.sets
        )
        proposed_sets = Counter(
            (item.weight_kg, item.reps, item.rpe, item.rir) for exercise in proposal.exercises for item in exercise.sets
        )
    except AttributeError:
        return [ReviewReasonCode.PROPOSAL_SOURCE_MISMATCH.value]
    if proposed_sets != deterministic_sets:
        return [ReviewReasonCode.PROPOSAL_SOURCE_MISMATCH.value]
    return []


def review_reasons(outcome: ParseOutcome, known_alias: bool = True) -> list[str]:
    reasons = list(outcome.reasons)
    extraction = outcome.extraction
    if extraction is None:
        return reasons
    reasons.extend(extraction_materialization_reasons(extraction))
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
