import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from gym_tracker.config import get_settings
from gym_tracker.models import ParseMethod, ParseReview, ParseStatus, ReviewStatus
from gym_tracker.repositories.catalog import get_or_create_exercise, save_alias
from gym_tracker.schemas import WorkoutExtraction
from gym_tracker.services.whatsapp_import import _materialize_extraction


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
    if raw_message.sets:
        raise ValueError("a mensagem ja possui series materializadas")
    user_id = raw_message.import_record.user_id
    _materialize_extraction(
        session,
        user_id,
        raw_message,
        extraction,
        get_settings().parser_version,
        parse_method=ParseMethod.MANUAL.value,
    )
    if alias_raw and extraction.exercises:
        payload = extraction.exercises[0]
        if not payload.canonical_name or not payload.muscle_group:
            raise ValueError("nao e possivel salvar alias sem exercicio canonico e grupo")
        exercise = get_or_create_exercise(session, payload.canonical_name, payload.muscle_group)
        save_alias(session, alias_raw, exercise, payload.equipment, user_id)
    review.corrected_payload = extraction.model_dump(mode="json")
    review.status = ReviewStatus.ACCEPTED.value
    review.reviewed_at = datetime.now(UTC)
    raw_message.parse_status = ParseStatus.ACCEPTED.value
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
    review.raw_message.parse_status = ParseStatus.SKIPPED.value
    session.flush()
    return review
