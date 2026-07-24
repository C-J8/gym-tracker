import sqlite3
from pathlib import Path

import pandas as pd
from alembic import command
from alembic.config import Config

from gym_tracker.config import Settings, get_settings
from gym_tracker.data_adapter import load_dashboard_data


def test_migrations_apply_from_zero(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert {"users", "imports", "raw_messages", "workouts", "exercises", "sets", "parse_reviews"} <= tables
    assert revision == "0001"
    get_settings.cache_clear()


def test_csv_adapter_returns_streamlit_contract(tmp_path: Path) -> None:
    csv_path = tmp_path / "legacy.csv"
    pd.DataFrame(
        [
            {
                "data": "2026-01-01",
                "grupo_muscular": "Peito",
                "exercicio": "Supino reto",
                "tipo": "Halter",
                "peso_kg": 20,
                "serie": 1,
                "repeticoes": 10,
            }
        ]
    ).to_csv(csv_path, index=False)
    frame = load_dashboard_data(Settings(data_backend="csv", legacy_csv_path=csv_path))
    assert {"volume", "estimativa_1rm", "semana", "mes", "exercicio_tipo"} <= set(frame.columns)
    assert frame.iloc[0]["volume"] == 200


def test_streamlit_app_starts_in_legacy_mode(monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DATA_BACKEND", "csv")
    monkeypatch.setenv("LEGACY_CSV_PATH", "academia_treinos_whatsapp.csv")
    get_settings.cache_clear()
    app = AppTest.from_file("app.py").run(timeout=30)
    assert not app.exception
    get_settings.cache_clear()
