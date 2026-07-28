import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from gym_tracker.models import ParseMethod, ParseResultStatus, ParseReview, ParseStatus, ReviewStatus
from gym_tracker.repositories.catalog import get_or_create_exercise, save_alias
from gym_tracker.repositories.state import bump_data_revision
from gym_tracker.schemas import WorkoutExtraction
from gym_tracker.services.normalization import normalize_text
from gym_tracker.services.whatsapp_import import _activate_result, _materialize_extraction


def accept_review(
    session: Session,
    review_id: uuid.UUID,
    corrected_payload: dict,
    alias_raw: str | None = None,
) -> ParseReview:
    review = session.get(ParseReview, review_id)
    if review is None:
        raise ValueError(f"review nao encontrado: {review_id}")
    if review.status != ReviewStatus.PENDING.value:
        raise ValueError("review ja finalizado")
    extraction = WorkoutExtraction.model_validate(corrected_payload)
    raw_message = review.raw_message
    result = review.parse_result
    if result.status != ParseResultStatus.REVIEW.value or result.sets:
        raise ValueError("o resultado da revisao nao esta pendente ou ja possui series")
    user_id = raw_message.user_id
    _materialize_extraction(
        session,
        user_id,
        raw_message,
        result,
        extraction,
        result.parse_run.parser_version,
        parse_method=ParseMethod.MANUAL.value,
    )
    result.parse_method = ParseMethod.MANUAL.value
    result.payload = extraction.model_dump(mode="json")
    result.reasons = []
    _activate_result(session, result)
    if alias_raw and extraction.exercises:
        matching = [
            payload for payload in extraction.exercises if normalize_text(payload.raw_name) == normalize_text(alias_raw)
        ]
        if not matching and len(extraction.exercises) == 1:
            matching = extraction.exercises
        if len(matching) != 1:
            raise ValueError("informe um alias que corresponda a exatamente um exercicio do payload")
        payload = matching[0]
        if not payload.canonical_name or not payload.muscle_group:
            raise ValueError("nao e possivel salvar alias sem exercicio canonico e grupo")
        exercise = get_or_create_exercise(session, payload.canonical_name, payload.muscle_group)
        save_alias(session, alias_raw, exercise, payload.equipment, user_id)
    review.corrected_payload = extraction.model_dump(mode="json")
    review.status = ReviewStatus.ACCEPTED.value
    review.reviewed_at = datetime.now(UTC)
    raw_message.parse_status = ParseStatus.ACCEPTED.value
    bump_data_revision(session)
    session.flush()
    return review


def reject_review(session: Session, review_id: uuid.UUID) -> ParseReview:
    review = session.get(ParseReview, review_id)
    if review is None:
        raise ValueError(f"review nao encontrado: {review_id}")
    if review.status != ReviewStatus.PENDING.value:
        raise ValueError("review ja finalizado")
    review.status = ReviewStatus.REJECTED.value
    review.reviewed_at = datetime.now(UTC)
    review.parse_result.status = ParseResultStatus.REJECTED.value
    if not any(result.is_active for result in review.raw_message.parse_results):
        review.raw_message.parse_status = ParseStatus.SKIPPED.value
    bump_data_revision(session)
    session.flush()
    return review
