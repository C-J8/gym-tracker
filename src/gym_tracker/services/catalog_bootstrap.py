import csv
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.models import ExerciseAlias, ExerciseVariant, User
from gym_tracker.repositories.catalog import (
    find_alias_record,
    find_exercise_by_catalog_key,
    get_or_create_exercise,
    get_or_create_variant,
)
from gym_tracker.repositories.state import bump_data_revision
from gym_tracker.schemas import CatalogBootstrapReport, CatalogReviewItem
from gym_tracker.services.normalization import (
    canonical_equipment,
    default_load_basis,
    normalize_catalog_key,
    normalize_muscle_group,
    resolve_static_exercise,
)

REQUIRED_COLUMNS = {
    "data",
    "grupo_muscular",
    "exercicio",
    "tipo",
    "peso_kg",
    "serie",
    "repeticoes",
}
COUNTER_KEYS = ("exercises", "aliases", "variants")


@dataclass(frozen=True)
class CatalogAssociation:
    normalized_key: str
    canonical_name: str
    muscle_group: str
    equipment: str
    load_basis: str


def catalog_bootstrap_apply_hook(index: int, association: CatalogAssociation) -> None:
    del index, association


def _empty_counters() -> dict[str, int]:
    return dict.fromkeys(COUNTER_KEYS, 0)


def _row_signature(row: dict[str, str], columns: list[str]) -> tuple[str, ...]:
    return tuple((row.get(column) or "").strip() for column in columns)


def _normalized_load_basis(value: str | None, equipment: str) -> str:
    if not value or not value.strip():
        return default_load_basis(equipment)
    return normalize_catalog_key(value).replace(" ", "_")


def _normalized_unit(value: str | None) -> str | None:
    key = normalize_catalog_key(value or "kg")
    if key in {"kg", "quilo", "quilos", "quilograma", "quilogramas"}:
        return "kg"
    return None


def _review_item(key: str, status: str, *reasons: str) -> CatalogReviewItem:
    return CatalogReviewItem(normalized_key=key, status=status, reasons=list(reasons))


def _analyze_group(
    session: Session,
    key: str,
    rows: list[dict[str, str]],
    user_id: uuid.UUID,
    has_unit_column: bool,
    has_load_basis_column: bool,
) -> tuple[CatalogAssociation | None, CatalogReviewItem | None]:
    static_matches = [resolve_static_exercise(row["exercicio"]) for row in rows]
    static_matches = [match for match in static_matches if match is not None]
    static_keys = {normalize_catalog_key(match.canonical_name) for match in static_matches}
    direct_existing = find_exercise_by_catalog_key(session, key)

    if direct_existing is not None:
        canonical_name = direct_existing.canonical_name
        muscle_group = direct_existing.muscle_group
        if static_keys and normalize_catalog_key(canonical_name) not in static_keys:
            return None, _review_item(key, "ambiguous", "catalogo existente diverge da regra deterministica")
    elif len(static_keys) == 1:
        static_match = static_matches[0]
        existing_static = find_exercise_by_catalog_key(session, static_match.canonical_name)
        canonical_name = existing_static.canonical_name if existing_static else static_match.canonical_name
        muscle_group = existing_static.muscle_group if existing_static else static_match.muscle_group
    elif not static_keys:
        return None, _review_item(key, "not_found", "nenhuma correspondencia deterministica")
    else:
        return None, _review_item(key, "ambiguous", "multiplas correspondencias canonicas")

    muscle_groups = {
        normalize_muscle_group(row["grupo_muscular"]) for row in rows if normalize_muscle_group(row["grupo_muscular"])
    }
    if len(muscle_groups) != 1:
        return None, _review_item(key, "ambiguous", "grupos musculares conflitantes")
    if normalize_muscle_group(muscle_group) not in muscle_groups:
        return None, _review_item(key, "ambiguous", "grupo muscular diverge do catalogo")

    equipment_values = [canonical_equipment(row["tipo"]) for row in rows]
    if any(equipment is None for equipment in equipment_values):
        return None, _review_item(key, "not_found", "equipamento nao reconhecido")
    equipments = {equipment for equipment in equipment_values if equipment is not None}
    if len(equipments) != 1:
        return None, _review_item(key, "ambiguous", "equipamentos conflitantes")
    equipment = next(iter(equipments))

    units = {_normalized_unit(row.get("unidade") if has_unit_column else "kg") for row in rows}
    if None in units or len(units) != 1:
        return None, _review_item(key, "ambiguous", "unidades conflitantes ou desconhecidas")

    load_bases = {
        _normalized_load_basis(row.get("load_basis") if has_load_basis_column else None, equipment) for row in rows
    }
    if len(load_bases) != 1:
        return None, _review_item(key, "ambiguous", "load_basis conflitante")
    load_basis = next(iter(load_bases))

    existing_alias = find_alias_record(session, key, user_id)
    if existing_alias is not None:
        alias_exercise_key = normalize_catalog_key(existing_alias.exercise.canonical_name)
        if alias_exercise_key != normalize_catalog_key(canonical_name):
            return None, _review_item(key, "ambiguous", "alias existente aponta para outro exercicio")
        if existing_alias.suggested_equipment not in {None, equipment}:
            return None, _review_item(key, "ambiguous", "alias existente possui outro equipamento")

    return (
        CatalogAssociation(
            normalized_key=key,
            canonical_name=canonical_name,
            muscle_group=muscle_group,
            equipment=equipment,
            load_basis=load_basis,
        ),
        None,
    )


def _variant_exists(session: Session, association: CatalogAssociation) -> bool:
    exercise = find_exercise_by_catalog_key(session, association.canonical_name)
    if exercise is None:
        return False
    return (
        session.scalar(
            select(ExerciseVariant.id).where(
                ExerciseVariant.exercise_id == exercise.id,
                ExerciseVariant.equipment == association.equipment,
                ExerciseVariant.load_basis == association.load_basis,
                ExerciseVariant.gym_or_location == "",
            )
        )
        is not None
    )


def _planned_counts(
    session: Session,
    associations: list[CatalogAssociation],
    user_id: uuid.UUID,
) -> tuple[dict[str, int], dict[str, int]]:
    planned = _empty_counters()
    reused = _empty_counters()
    for association in associations:
        if find_exercise_by_catalog_key(session, association.canonical_name) is None:
            planned["exercises"] += 1
        else:
            reused["exercises"] += 1
        if find_alias_record(session, association.normalized_key, user_id) is None:
            planned["aliases"] += 1
        else:
            reused["aliases"] += 1
        if _variant_exists(session, association):
            reused["variants"] += 1
        else:
            planned["variants"] += 1
    return planned, reused


def _apply_associations(
    session: Session,
    associations: list[CatalogAssociation],
    user_id: uuid.UUID,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    created = _empty_counters()
    updated = _empty_counters()
    reused = _empty_counters()

    with session.begin_nested():
        for index, association in enumerate(associations):
            exercise = find_exercise_by_catalog_key(session, association.canonical_name)
            if exercise is None:
                exercise = get_or_create_exercise(session, association.canonical_name, association.muscle_group)
                created["exercises"] += 1
            else:
                reused["exercises"] += 1

            variant_exists = _variant_exists(session, association)
            get_or_create_variant(
                session,
                exercise,
                association.equipment,
                association.load_basis,
            )
            if variant_exists:
                reused["variants"] += 1
            else:
                created["variants"] += 1

            alias = find_alias_record(session, association.normalized_key, user_id)
            if alias is None:
                session.add(
                    ExerciseAlias(
                        user_id=user_id,
                        raw_alias=association.normalized_key,
                        exercise=exercise,
                        suggested_equipment=association.equipment,
                    )
                )
                session.flush()
                created["aliases"] += 1
            elif alias.suggested_equipment is None:
                alias.suggested_equipment = association.equipment
                updated["aliases"] += 1
            else:
                reused["aliases"] += 1

            catalog_bootstrap_apply_hook(index, association)

        if any(created.values()) or any(updated.values()):
            bump_data_revision(session)

    return created, updated, reused


def bootstrap_catalog_from_csv(
    session: Session,
    file_path: Path,
    user_id: uuid.UUID,
    *,
    apply: bool = False,
) -> CatalogBootstrapReport:
    if session.get(User, user_id) is None:
        raise ValueError(f"usuario nao encontrado: {user_id}")

    content = file_path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(content.splitlines())
    fieldnames = [field.strip() for field in (reader.fieldnames or []) if field]
    missing = REQUIRED_COLUMNS - set(fieldnames)
    if missing:
        raise ValueError(f"CSV deve conter as colunas: {', '.join(sorted(REQUIRED_COLUMNS))}")

    rows = [{key: value or "" for key, value in row.items() if key is not None} for row in reader]
    signatures = [_row_signature(row, fieldnames) for row in rows]
    signature_counts = Counter(signatures)
    unique_by_signature: dict[tuple[str, ...], dict[str, str]] = {}
    for signature, row in zip(signatures, rows, strict=True):
        unique_by_signature.setdefault(signature, row)
    unique_rows = list(unique_by_signature.values())

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in unique_rows:
        key = normalize_catalog_key(row["exercicio"])
        grouped[key].append(row)

    associations: list[CatalogAssociation] = []
    review_items: list[CatalogReviewItem] = []
    for key, group_rows in grouped.items():
        association, review_item = _analyze_group(
            session,
            key,
            group_rows,
            user_id,
            has_unit_column="unidade" in fieldnames,
            has_load_basis_column="load_basis" in fieldnames,
        )
        if association is not None:
            associations.append(association)
        if review_item is not None:
            review_items.append(review_item)

    planned, currently_reused = _planned_counts(session, associations, user_id)
    created = _empty_counters()
    updated = _empty_counters()
    reused = currently_reused
    if apply:
        created, updated, reused = _apply_associations(session, associations, user_id)

    ambiguities = sum(item.status == "ambiguous" for item in review_items)
    not_found = sum(item.status == "not_found" for item in review_items)
    return CatalogBootstrapReport(
        dry_run=not apply,
        applied=apply,
        rows_read=len(rows),
        exact_duplicates=sum(count - 1 for count in signature_counts.values() if count > 1),
        unique_rows=len(unique_rows),
        normalized_exercises=len(grouped),
        applicable_associations=len(associations),
        ambiguities=ambiguities,
        not_found=not_found,
        records_created=created,
        records_updated=updated,
        records_reused=reused,
        records_planned=planned,
        records_ignored=ambiguities + not_found,
        review_items=review_items,
    )
