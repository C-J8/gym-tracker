import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from gym_tracker.models import Exercise, ExerciseAlias, ExerciseVariant
from gym_tracker.services.normalization import ExerciseMatch, normalize_text


def find_alias(session: Session, raw_name: str, user_id: uuid.UUID) -> ExerciseMatch | None:
    alias_key = normalize_text(raw_name)
    statement: Select[tuple[ExerciseAlias]] = (
        select(ExerciseAlias)
        .where(ExerciseAlias.raw_alias == alias_key)
        .where((ExerciseAlias.user_id == user_id) | (ExerciseAlias.user_id.is_(None)))
        .order_by(ExerciseAlias.user_id.desc().nulls_last())
    )
    alias = session.scalar(statement)
    if alias is None:
        return None
    exercise = alias.exercise
    return ExerciseMatch(
        canonical_name=exercise.canonical_name,
        muscle_group=exercise.muscle_group,
        equipment=alias.suggested_equipment or "Máquina",
        load_basis="por_halter" if alias.suggested_equipment == "Halter" else "total",
    )


def get_or_create_exercise(session: Session, canonical_name: str, muscle_group: str) -> Exercise:
    exercise = session.scalar(select(Exercise).where(Exercise.canonical_name == canonical_name))
    if exercise is None:
        exercise = Exercise(canonical_name=canonical_name, muscle_group=muscle_group)
        session.add(exercise)
        session.flush()
    elif exercise.muscle_group != muscle_group:
        raise ValueError(f"conflito de grupo muscular para {canonical_name}: {exercise.muscle_group} != {muscle_group}")
    return exercise


def get_or_create_variant(
    session: Session,
    exercise: Exercise,
    equipment: str,
    load_basis: str,
    gym_or_location: str = "",
) -> ExerciseVariant:
    variant = session.scalar(
        select(ExerciseVariant).where(
            ExerciseVariant.exercise_id == exercise.id,
            ExerciseVariant.equipment == equipment,
            ExerciseVariant.load_basis == load_basis,
            ExerciseVariant.gym_or_location == gym_or_location,
        )
    )
    if variant is None:
        variant = ExerciseVariant(
            exercise=exercise,
            equipment=equipment,
            load_basis=load_basis,
            gym_or_location=gym_or_location,
        )
        session.add(variant)
        session.flush()
    return variant


def save_alias(
    session: Session,
    raw_alias: str,
    exercise: Exercise,
    equipment: str | None,
    user_id: uuid.UUID | None,
) -> ExerciseAlias:
    alias_key = normalize_text(raw_alias)
    alias = session.scalar(
        select(ExerciseAlias).where(ExerciseAlias.user_id == user_id, ExerciseAlias.raw_alias == alias_key)
    )
    if alias is None:
        alias = ExerciseAlias(
            user_id=user_id,
            raw_alias=alias_key,
            exercise=exercise,
            suggested_equipment=equipment,
        )
        session.add(alias)
    else:
        alias.exercise = exercise
        alias.suggested_equipment = equipment
    session.flush()
    return alias
