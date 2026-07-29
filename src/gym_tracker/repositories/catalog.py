import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from gym_tracker.models import Exercise, ExerciseAlias, ExerciseVariant, ParseResult, RawMessage, WorkoutSet
from gym_tracker.repositories.state import bump_data_revision
from gym_tracker.services.normalization import (
    ExerciseMatch,
    default_load_basis,
    normalize_catalog_key,
    normalize_muscle_group,
)


def find_exercise_by_catalog_key(session: Session, name: str) -> Exercise | None:
    key = normalize_catalog_key(name)
    matches = [
        exercise
        for exercise in session.scalars(select(Exercise).order_by(Exercise.created_at, Exercise.id))
        if normalize_catalog_key(exercise.canonical_name) == key
    ]
    if len(matches) > 1:
        raise ValueError(f"identidade normalizada duplicada no catalogo: {key}")
    return matches[0] if matches else None


def find_alias_record(session: Session, raw_name: str, user_id: uuid.UUID | None) -> ExerciseAlias | None:
    alias_key = normalize_catalog_key(raw_name)
    candidates = list(
        session.scalars(
            select(ExerciseAlias)
            .where(ExerciseAlias.user_id == user_id)
            .order_by(ExerciseAlias.created_at, ExerciseAlias.id)
        )
    )
    matches = [alias for alias in candidates if normalize_catalog_key(alias.raw_alias) == alias_key]
    if len(matches) > 1:
        raise ValueError(f"alias normalizado duplicado para o usuario: {alias_key}")
    return matches[0] if matches else None


def find_alias(session: Session, raw_name: str, user_id: uuid.UUID) -> ExerciseMatch | None:
    alias_key = normalize_catalog_key(raw_name)
    statement: Select[tuple[ExerciseAlias]] = (
        select(ExerciseAlias)
        .where((ExerciseAlias.user_id == user_id) | (ExerciseAlias.user_id.is_(None)))
        .order_by(ExerciseAlias.user_id.desc().nulls_last())
    )
    alias = next(
        (
            candidate
            for candidate in session.scalars(statement)
            if normalize_catalog_key(candidate.raw_alias) == alias_key
        ),
        None,
    )
    if alias is None:
        return None
    exercise = alias.exercise
    load_bases = list(
        session.scalars(
            select(ExerciseVariant.load_basis)
            .where(
                ExerciseVariant.exercise_id == exercise.id,
                ExerciseVariant.equipment == alias.suggested_equipment,
            )
            .distinct()
        )
    )
    load_basis = load_bases[0] if len(load_bases) == 1 else default_load_basis(alias.suggested_equipment or "")
    return ExerciseMatch(
        canonical_name=exercise.canonical_name,
        muscle_group=exercise.muscle_group,
        equipment=alias.suggested_equipment,
        load_basis=load_basis,
    )


def find_confirmed_variant(
    session: Session,
    canonical_name: str,
    user_id: uuid.UUID,
) -> tuple[str, str] | None:
    rows = session.execute(
        select(ExerciseVariant.equipment, ExerciseVariant.load_basis)
        .join(WorkoutSet, WorkoutSet.exercise_variant_id == ExerciseVariant.id)
        .join(ParseResult, ParseResult.id == WorkoutSet.parse_result_id)
        .join(RawMessage, RawMessage.id == ParseResult.raw_message_id)
        .join(Exercise, Exercise.id == ExerciseVariant.exercise_id)
        .where(
            Exercise.canonical_name == canonical_name,
            RawMessage.user_id == user_id,
            ParseResult.is_active.is_(True),
        )
        .distinct()
    ).all()
    return rows[0] if len(rows) == 1 else None


def get_or_create_exercise(session: Session, canonical_name: str, muscle_group: str) -> Exercise:
    exercise = find_exercise_by_catalog_key(session, canonical_name)
    if exercise is None:
        exercise = Exercise(canonical_name=canonical_name, muscle_group=muscle_group)
        session.add(exercise)
        session.flush()
    elif normalize_muscle_group(exercise.muscle_group) != normalize_muscle_group(muscle_group):
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
    alias_key = normalize_catalog_key(raw_alias)
    alias = find_alias_record(session, alias_key, user_id)
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
    bump_data_revision(session)
    return alias
