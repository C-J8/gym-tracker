"""Read-only operational counts. No workout metrics or conversations are exported."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from gym_tracker.config import Settings
from gym_tracker.models import ParseReview, RawMessage, User


def operational_database_status(settings: Settings) -> dict:
    engine = None
    try:
        url = make_url(settings.database_url)
        connect_args = {}
        if url.get_backend_name() == "postgresql":
            connect_args = {
                "connect_timeout": 2,
                "options": "-c default_transaction_read_only=on -c statement_timeout=2000 -c lock_timeout=1000",
            }
        elif url.get_backend_name() == "sqlite":
            if not url.database or url.database == ":memory:":
                return {"database": "unavailable", "pending_reviews": None}
            url = url.set(
                database="file:" + quote(Path(url.database).resolve().as_posix(), safe="/"),
                query={"mode": "ro", "uri": "true"},
            )
        engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
            user_id = settings.gym_tracker_user_id
            if user_id is None:
                users = list(session.scalars(select(User.id).limit(2)))
                if len(users) != 1:
                    return {"database": "user_required", "pending_reviews": None}
                user_id = users[0]
            elif session.get(User, user_id) is None:
                return {"database": "user_required", "pending_reviews": None}
            count = session.scalar(
                select(func.count())
                .select_from(ParseReview)
                .join(RawMessage)
                .where(ParseReview.status == "pending", RawMessage.user_id == user_id)
            )
            return {"database": "connected", "pending_reviews": count}
    except Exception:
        # No exception text: database URLs, names and raw messages stay private.
        return {"database": "unavailable", "pending_reviews": None}
    finally:
        if engine is not None:
            engine.dispose()


def last_automatic_backup(settings: Settings) -> str | None:
    try:
        files = (
            file
            for file in settings.gym_backup_directory.glob("gym-*.*")
            if file.suffix in {".dump", ".sqlite"} and file.is_file() and file.stat().st_size > 0
        )
        latest = max((file.stat().st_mtime for file in files), default=None)
        return datetime.fromtimestamp(latest, UTC).isoformat() if latest is not None else None
    except OSError:
        return None
