"""One safe import cycle, independent of the process that schedules it."""

from collections.abc import Callable
from pathlib import Path
from time import sleep

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from gym_tracker.backup import BackupError, backup_database
from gym_tracker.config import Settings
from gym_tracker.models import Import, ParseRun, User
from gym_tracker.schemas import QualityReport
from gym_tracker.services.parser import split_whatsapp_messages
from gym_tracker.services.whatsapp_import import import_whatsapp_file, sha256_bytes

MAX_SOURCE_BYTES = 20 * 1024 * 1024


class SourceNotReady(ValueError):
    """The producer may still be writing the source; retry on the next cycle."""


class ImportInputError(ValueError):
    """An actionable message that is safe to show without exposing source text."""


def _read_version(path: Path) -> tuple[tuple[int, int, int], bytes]:
    before = path.stat()
    if not path.is_file():
        raise ImportInputError("A entrada precisa ser um arquivo TXT.")
    if before.st_size > MAX_SOURCE_BYTES:
        raise ImportInputError("O TXT excede o limite de 20 MB.")
    with path.open("rb") as source:
        content = source.read(MAX_SOURCE_BYTES + 1)
    after = path.stat()
    signature = (after.st_ino, after.st_size, after.st_mtime_ns)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != signature or len(content) != after.st_size:
        raise SourceNotReady("O TXT esta mudando. Aguardando a copia terminar.")
    if len(content) > MAX_SOURCE_BYTES:
        raise ImportInputError("O TXT excede o limite de 20 MB.")
    return signature, content


def stable_snapshot(
    path: Path,
    settle_seconds: float,
    wait: Callable[[float], object] = sleep,
) -> bytes:
    first_version, first = _read_version(path)
    wait(settle_seconds)
    second_version, second = _read_version(path)
    if first_version != second_version or first != second:
        raise SourceNotReady("O TXT esta mudando. Aguardando a copia terminar.")
    if not second.strip():
        raise ImportInputError("O TXT esta vazio. Nenhum dado foi alterado.")
    try:
        messages = split_whatsapp_messages(second.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise ImportInputError("O TXT nao e um export valido do WhatsApp em UTF-8.") from error
    if not messages:
        raise ImportInputError("Nenhuma mensagem do WhatsApp reconhecida. Confira o arquivo.")
    return second


def import_snapshot(settings: Settings, path: Path, content: bytes) -> QualityReport:
    # Unattended execution never sends conversations to a paid/external service.
    local_settings = settings.model_copy(
        update={"openai_api_key": None, "llm_shadow_mode": True, "llm_auto_accept": False}
    )
    connect_args = {}
    if settings.database_url.startswith("postgresql"):
        connect_args = {"connect_timeout": 5, "options": "-c statement_timeout=60000 -c lock_timeout=5000"}
    engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
    try:
        with Session(engine) as session, session.begin():
            user_id = settings.gym_tracker_user_id
            if user_id is None:
                users = list(session.scalars(select(User.id).limit(2)))
                if len(users) != 1:
                    raise ImportInputError(
                        "Configure GYM_TRACKER_USER_ID no .env; o banco precisa ter um usuario definido."
                    )
                user_id = users[0]
            elif session.get(User, user_id) is None:
                raise ImportInputError("GYM_TRACKER_USER_ID nao corresponde a um usuario existente.")
            if engine.dialect.name == "postgresql":
                # Also protects against another automatic worker using a different
                # runtime directory but the same account/database.
                locked = session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": int.from_bytes(user_id.bytes[:8], "big", signed=True)},
                )
                if not locked:
                    raise SourceNotReady("Outra importacao deste usuario esta em andamento.")
            existing = session.scalar(
                select(ParseRun.id)
                .join(Import)
                .where(
                    Import.user_id == user_id,
                    Import.source_sha256 == sha256_bytes(content),
                    ParseRun.parser_version == settings.parser_version,
                )
                .limit(1)
            )
            if existing is None:
                # The user transaction lock is already held. No application writes
                # occur until a consistent database snapshot has been saved.
                try:
                    backup_database(settings)
                except BackupError as error:
                    raise ImportInputError(str(error)) from error
            report = import_whatsapp_file(session, path, user_id, settings=local_settings, source_bytes=content)
        # Return only after the transaction committed successfully.
        return report
    finally:
        engine.dispose()
