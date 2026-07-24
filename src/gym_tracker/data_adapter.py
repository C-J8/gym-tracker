import os
import uuid
from pathlib import Path

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from gym_tracker.config import Settings, get_settings
from gym_tracker.db import get_session_factory
from gym_tracker.repositories.dashboard import DashboardRepository
from gym_tracker.services.metrics import prepare_dashboard_dataframe


class DataBackendError(RuntimeError):
    pass


def data_signature(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.data_backend == "csv":
        path = settings.legacy_csv_path
        return f"csv:{path}:{path.stat().st_mtime if path.exists() else 'missing'}"
    return f"postgres:{os.getenv('GYM_TRACKER_DATA_VERSION', 'current')}"


def load_dashboard_data(
    settings: Settings | None = None,
    user_id: uuid.UUID | None = None,
) -> pd.DataFrame:
    settings = settings or get_settings()
    if settings.data_backend == "csv":
        path = Path(settings.legacy_csv_path)
        if not path.exists():
            raise DataBackendError(f"CSV legado nao encontrado: {path}")
        return prepare_dashboard_dataframe(pd.read_csv(path))
    try:
        factory = get_session_factory(settings.database_url)
        with factory() as session:
            repository = DashboardRepository(session)
            if user_id is None:
                configured_user = os.getenv("GYM_TRACKER_USER_ID")
                if configured_user:
                    user_id = uuid.UUID(configured_user)
                else:
                    users = repository.users()
                    if len(users) == 1:
                        user_id = users[0].id
                    elif len(users) > 1:
                        raise DataBackendError(
                            "Ha mais de um usuario no banco. Defina GYM_TRACKER_USER_ID "
                            "para escolher os dados exibidos."
                        )
            data = repository.workout_dataframe(user_id)
    except DataBackendError:
        raise
    except SQLAlchemyError as error:
        raise DataBackendError(
            "Nao foi possivel conectar ao PostgreSQL. Inicie o banco com "
            "`docker compose up -d postgres` e aplique `uv run alembic upgrade head`, "
            "ou use DATA_BACKEND=csv para o modo legado."
        ) from error
    if data.empty:
        raise DataBackendError("O PostgreSQL esta acessivel, mas ainda nao possui series importadas.")
    return prepare_dashboard_dataframe(data)
