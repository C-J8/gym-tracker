import csv
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gym_tracker.models import (
    DataRevision,
    Exercise,
    ExerciseAlias,
    ExerciseVariant,
    Import,
    ImportMessageOccurrence,
    ParseResult,
    ParseRun,
    RawMessage,
    User,
    Workout,
    WorkoutSet,
)
from gym_tracker.repositories.catalog import get_or_create_exercise
from gym_tracker.services.catalog_bootstrap import REQUIRED_COLUMNS, bootstrap_catalog_from_csv

BASE_ROW = {
    "data": "2026-01-01",
    "grupo_muscular": "Pernas",
    "exercicio": "Extensora",
    "tipo": "Máquina",
    "peso_kg": "40",
    "serie": "1",
    "repeticoes": "10",
}


def write_catalog_csv(path: Path, rows: list[dict[str, str]], extra_columns: tuple[str, ...] = ()) -> Path:
    columns = [*sorted(REQUIRED_COLUMNS), *extra_columns]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def table_count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_exact_duplicate_rows_do_not_duplicate_catalog(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_catalog_csv(tmp_path / "duplicates.csv", [BASE_ROW, BASE_ROW])

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert report.rows_read == 2
    assert report.exact_duplicates == 1
    assert report.unique_rows == 1
    assert table_count(session, Exercise) == 1
    assert table_count(session, ExerciseAlias) == 1
    assert table_count(session, ExerciseVariant) == 1


def test_dry_run_performs_zero_writes(session: Session, user: User, tmp_path: Path) -> None:
    source = write_catalog_csv(tmp_path / "dry-run.csv", [BASE_ROW])

    report = bootstrap_catalog_from_csv(session, source, user.id)

    assert report.dry_run is True
    assert report.records_planned == {"exercises": 1, "aliases": 1, "variants": 1}
    assert table_count(session, Exercise) == 0
    assert table_count(session, ExerciseAlias) == 0
    assert table_count(session, ExerciseVariant) == 0
    assert table_count(session, DataRevision) == 0


def test_two_applied_runs_produce_the_same_catalog_state(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_catalog_csv(tmp_path / "idempotent.csv", [BASE_ROW])
    first = bootstrap_catalog_from_csv(session, source, user.id, apply=True)
    counts_after_first = (
        table_count(session, Exercise),
        table_count(session, ExerciseAlias),
        table_count(session, ExerciseVariant),
    )

    second = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert first.records_created == {"exercises": 1, "aliases": 1, "variants": 1}
    assert second.records_created == {"exercises": 0, "aliases": 0, "variants": 0}
    assert second.records_reused == {"exercises": 1, "aliases": 1, "variants": 1}
    assert counts_after_first == (
        table_count(session, Exercise),
        table_count(session, ExerciseAlias),
        table_count(session, ExerciseVariant),
    )


def test_bootstrap_never_creates_pipeline_or_dashboard_data(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_catalog_csv(tmp_path / "catalog-only.csv", [BASE_ROW])

    bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    for model in (
        Workout,
        WorkoutSet,
        RawMessage,
        ImportMessageOccurrence,
        Import,
        ParseRun,
        ParseResult,
    ):
        assert table_count(session, model) == 0


def test_unambiguous_exercise_and_equipment_are_applied(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_catalog_csv(tmp_path / "unique.csv", [BASE_ROW])

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)
    alias = session.scalar(select(ExerciseAlias))
    variant = session.scalar(select(ExerciseVariant))

    assert report.applicable_associations == 1
    assert report.ambiguities == 0
    assert alias is not None and alias.suggested_equipment == "Máquina"
    assert variant is not None and variant.load_basis == "total"


def test_conflicting_equipment_is_reported_without_automatic_choice(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    other = {**BASE_ROW, "tipo": "Halter", "serie": "2"}
    source = write_catalog_csv(tmp_path / "equipment-conflict.csv", [BASE_ROW, other])

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert report.ambiguities == 1
    assert report.applicable_associations == 0
    assert "equipamentos conflitantes" in report.review_items[0].reasons
    assert table_count(session, ExerciseAlias) == 0
    assert table_count(session, ExerciseVariant) == 0


def test_unknown_exercise_is_reported_without_catalog_write(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = write_catalog_csv(
        tmp_path / "unknown.csv",
        [{**BASE_ROW, "exercicio": "Movimento Lunar"}],
    )

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert report.not_found == 1
    assert report.review_items[0].status == "not_found"
    assert table_count(session, Exercise) == 0


def test_abdomen_accent_variants_resolve_to_one_identity(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    rows = [
        {
            **BASE_ROW,
            "grupo_muscular": "Abdomen",
            "exercicio": "Abdomen",
            "tipo": "Máquina",
        },
        {
            **BASE_ROW,
            "grupo_muscular": "Abdômen",
            "exercicio": "Abdômen",
            "tipo": "maquina",
            "serie": "2",
        },
    ]
    source = write_catalog_csv(tmp_path / "abdomen.csv", rows)

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert report.normalized_exercises == 1
    assert report.applicable_associations == 1
    assert table_count(session, Exercise) == 1
    assert table_count(session, ExerciseAlias) == 1


def test_existing_canonical_display_wins_for_accent_equivalent_name(
    session: Session,
) -> None:
    first = get_or_create_exercise(session, "Abdomen", "Abdomen")
    second = get_or_create_exercise(session, "Abdômen", "Abdômen")

    assert second.id == first.id
    assert second.canonical_name == "Abdomen"
    assert second.muscle_group == "Abdomen"
    assert table_count(session, Exercise) == 1


def test_spacing_case_accents_and_punctuation_do_not_duplicate_identity(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    rows = [
        BASE_ROW,
        {
            **BASE_ROW,
            "exercicio": "  EXTÉNSORA!!! ",
            "tipo": " MÁQUINA ",
            "serie": "2",
        },
    ]
    source = write_catalog_csv(tmp_path / "visual.csv", rows)

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert report.normalized_exercises == 1
    assert table_count(session, Exercise) == 1
    assert table_count(session, ExerciseAlias) == 1
    assert table_count(session, ExerciseVariant) == 1


def test_user_aliases_remain_isolated(session: Session, user: User, tmp_path: Path) -> None:
    other_user = User(display_name="Outra pessoa")
    session.add(other_user)
    session.commit()
    source = write_catalog_csv(tmp_path / "users.csv", [BASE_ROW])

    bootstrap_catalog_from_csv(session, source, user.id, apply=True)
    bootstrap_catalog_from_csv(session, source, other_user.id, apply=True)

    aliases = list(session.scalars(select(ExerciseAlias).order_by(ExerciseAlias.user_id)))
    assert len(aliases) == 2
    assert {alias.user_id for alias in aliases} == {user.id, other_user.id}
    assert table_count(session, Exercise) == 1
    assert table_count(session, ExerciseVariant) == 1


def test_failure_mid_apply_rolls_back_all_catalog_writes(
    session: Session,
    user: User,
    tmp_path: Path,
    monkeypatch,
) -> None:
    rows = [
        BASE_ROW,
        {
            **BASE_ROW,
            "exercicio": "Flexora",
            "tipo": "Máquina",
            "serie": "2",
        },
    ]
    source = write_catalog_csv(tmp_path / "rollback.csv", rows)

    def fail_after_first(index, association) -> None:
        del association
        if index == 0:
            raise RuntimeError("falha sintetica")

    monkeypatch.setattr(
        "gym_tracker.services.catalog_bootstrap.catalog_bootstrap_apply_hook",
        fail_after_first,
    )
    with pytest.raises(RuntimeError, match="sintetica"):
        bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert table_count(session, Exercise) == 0
    assert table_count(session, ExerciseAlias) == 0
    assert table_count(session, ExerciseVariant) == 0
    assert table_count(session, DataRevision) == 0


def test_empty_csv_returns_empty_report(session: Session, user: User, tmp_path: Path) -> None:
    source = write_catalog_csv(tmp_path / "empty.csv", [])

    report = bootstrap_catalog_from_csv(session, source, user.id)

    assert report.rows_read == 0
    assert report.unique_rows == 0
    assert report.normalized_exercises == 0
    assert report.applicable_associations == 0


def test_missing_required_columns_are_rejected(
    session: Session,
    user: User,
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing.csv"
    source.write_text("exercicio,tipo\nExtensora,Máquina\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CSV deve conter"):
        bootstrap_catalog_from_csv(session, source, user.id)


@pytest.mark.parametrize(
    ("column", "first", "second", "expected_reason"),
    [
        ("unidade", "kg", "g", "unidades conflitantes ou desconhecidas"),
        ("load_basis", "total", "por_halter", "load_basis conflitante"),
    ],
)
def test_conflicting_unit_or_load_basis_is_not_confirmed(
    session: Session,
    user: User,
    tmp_path: Path,
    column: str,
    first: str,
    second: str,
    expected_reason: str,
) -> None:
    rows = [
        {**BASE_ROW, column: first},
        {**BASE_ROW, column: second, "serie": "2"},
    ]
    source = write_catalog_csv(tmp_path / f"{column}.csv", rows, (column,))

    report = bootstrap_catalog_from_csv(session, source, user.id, apply=True)

    assert report.ambiguities == 1
    assert expected_reason in report.review_items[0].reasons
    assert table_count(session, ExerciseAlias) == 0
    assert table_count(session, ExerciseVariant) == 0
