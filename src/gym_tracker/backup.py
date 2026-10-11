"""Private, atomic database backups shared by the importer and Planner launcher."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import make_url

from gym_tracker.config import Settings, get_settings


class BackupError(RuntimeError):
    """Fixed operational messages only: never expose credentials/subprocess output."""


def postgres_dump_command(settings: Settings) -> tuple[list[str], dict[str, str]]:
    url = make_url(settings.database_url)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", url.database or "")
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", url.username or "")
        or url.query
    ):
        raise BackupError("Backup bloqueado: configure um PostgreSQL local, sem parametros extras em DATABASE_URL.")
    args = ["pg_dump", "-U", url.username, "-d", url.database, "-Fc", "--no-owner", "--no-acl"]
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    if settings.gym_backup_container:
        container = settings.gym_backup_container
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            raise BackupError("GYM_BACKUP_CONTAINER invalido.")
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container],
            capture_output=True,
            timeout=15,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        bindings = json.loads(result.stdout).get("5432/tcp") or []
        if not any(
            binding.get("HostIp") in {"127.0.0.1", "0.0.0.0", "::1", "::"}
            and int(binding.get("HostPort", 0)) == (url.port or 5432)
            for binding in bindings
        ):
            raise BackupError("O conteiner de backup nao publica a porta local de DATABASE_URL.")
        return ["docker", "exec", container, *args], env
    executable = shutil.which("pg_dump")
    if executable is None:
        raise BackupError("Configure GYM_BACKUP_CONTAINER ou instale pg_dump para proteger o banco antes de importar.")
    env["PGPASSWORD"] = url.password or ""
    return [executable, "-h", url.host, "-p", str(url.port or 5432), *args[1:]], env


def backup_database(settings: Settings, directory: Path | None = None) -> Path:
    directory = (directory or settings.gym_backup_directory).resolve()
    url = make_url(settings.database_url)
    sqlite = url.get_backend_name() == "sqlite"
    if not sqlite and url.get_backend_name() != "postgresql":
        raise BackupError("Tipo de banco nao suportado pelo backup automatico.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"gym-{stamp}-{uuid.uuid4().hex[:8]}.{'sqlite' if sqlite else 'dump'}"
    partial = destination.with_suffix(destination.suffix + ".partial")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if sqlite:
            source = Path(url.database or "").resolve()
            if url.database == ":memory:" or not source.is_file() or url.query:
                raise BackupError("O backup exige um arquivo SQLite existente, sem parametros extras.")
            # SQLite's online backup API includes committed WAL pages. No file copy.
            with (
                closing(sqlite3.connect(f"file:{quote(source.as_posix(), safe='/')}?mode=ro", uri=True)) as original,
                closing(sqlite3.connect(partial)) as copy,
            ):
                original.backup(copy)
        else:
            command, env = postgres_dump_command(settings)
            with partial.open("xb") as output:
                subprocess.run(
                    command,
                    env=env,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        if not partial.stat().st_size:
            raise BackupError("Backup vazio. Nenhum dado sera importado.")
        partial.rename(destination)
        return destination
    except BackupError:
        raise
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        raise BackupError("Backup falhou. Confira Docker, .env e permissoes; nenhum dado sera importado.") from None


def main():
    parser = argparse.ArgumentParser(description="Backup privado da Academia")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    try:
        destination = backup_database(get_settings(), args.directory)
        print(json.dumps({"file": str(destination)}))
    except (BackupError, ValueError, OSError):
        print(json.dumps({"error": "Backup falhou. Confira GYM_BACKUP_CONTAINER, .env, Docker e permissoes."}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
