"""Real launch/stop regression checks: disposable Planner DB and synthetic Gym only."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from gym_tracker.automation import request_stop, status
from gym_tracker.models import Base, User, WorkoutSet


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise AssertionError("A condicao de integracao nao foi atingida.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def main():
    gym = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planner", type=Path, default=gym.parent / "ToDoList")
    args = parser.parse_args()
    planner = args.planner.resolve()
    node = shutil.which("node")
    if not node or not (planner / "scripts/planner.mjs").is_file():
        raise RuntimeError("Confira Node e --planner.")
    if (planner / ".planner/server.json").exists():
        raise RuntimeError("Use Parar Planner antes do teste. Uma instancia pessoal nao sera adotada.")
    # Validation is the same guard used by the Planner's database integration suite.
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "import {testDatabaseUrl} from './scripts/local/test-database.mjs'; console.log(testDatabaseUrl());",
        ],
        cwd=planner,
        capture_output=True,
        check=True,
    )
    test_url = result.stdout.decode().strip()
    environment = {**os.environ, "DATABASE_URL": test_url, "TEST_DATABASE_URL": test_url, "NODE_ENV": "development"}
    subprocess.run(["docker", "compose", "-f", "compose.test.yaml", "up", "-d", "--wait"], cwd=planner, check=True)
    subprocess.run(
        [node, "node_modules/prisma/build/index.js", "migrate", "deploy"], cwd=planner, env=environment, check=True
    )
    fixture = Path(tempfile.mkdtemp(prefix="planner-gym-test-"))
    database = f"sqlite:///{(fixture / 'gym.sqlite').as_posix()}"
    shutil.copy2(gym / "app.py", fixture / "app.py")
    first = b"01/01/2026 10:00 - Pessoa: Extensora 40kg/10rep"
    (fixture / "source.txt").write_bytes(first)
    (fixture / ".env").write_text(
        f"DATABASE_URL={database}\nGYM_IMPORT_FILE=source.txt\nGYM_IMPORT_INTERVAL_SECONDS=10\n"
        "GYM_IMPORT_SETTLE_SECONDS=1\nPARSER_VERSION=integration-test\nGYM_TRACKER_USER_ID=\nOPENAI_API_KEY=\n"
        "GYM_BACKUP_DIRECTORY=backups\nGYM_BACKUP_CONTAINER=\n",
        encoding="utf-8",
    )
    engine = create_engine(database)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(display_name="Synthetic integration test"))
        session.commit()
    engine.dispose()
    gym_port, planner_port = free_port(), free_port()
    while gym_port == planner_port:
        planner_port = free_port()
    environment.update(
        {
            "GYM_TRACKER_PATH": str(fixture),
            "GYM_TRACKER_PYTHON": sys.executable,
            "GYM_TRACKER_PORT": str(gym_port),
            "PLANNER_PORT": str(planner_port),
        }
    )

    def command(*command_args, env=None, expected=0):
        result = subprocess.run(
            [node, "scripts/planner.mjs", *command_args],
            cwd=planner,
            env=env or environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=240,
        )
        print(result.stdout.decode(errors="replace"), flush=True)
        assert result.returncode == expected, f"Unexpected CLI exit: {result.returncode}"

    def sets_count():
        engine = create_engine(database)
        try:
            with Session(engine) as session:
                return session.scalar(select(func.count()).select_from(WorkoutSet))
        finally:
            engine.dispose()

    def state(file):
        return json.loads(file.read_text(encoding="utf-8"))

    def planner_health():
        with urlopen(f"http://127.0.0.1:{planner_port}/api/health", timeout=10) as response:
            return response.status == 200

    runtime = fixture / ".gym-tracker"
    try:
        # A Gym startup failure returns nonzero but leaves the working Planner online.
        command(
            "dev", "--no-browser", env={**environment, "GYM_TRACKER_PYTHON": str(fixture / "missing.exe")}, expected=1
        )
        assert planner_health()
        # Invalid Gym shutdown config cannot prevent Planner from stopping.
        command("stop", env={**environment, "GYM_TRACKER_PORT": str(planner_port)}, expected=1)
        assert not (planner / ".planner/server.json").exists()
        command("dev", "--no-browser")
        planner_state = state(planner / ".planner/server.json")
        gym_state = state(runtime / "desktop.json")
        command("dev", "--no-browser")
        assert state(planner / ".planner/server.json")["token"] == planner_state["token"]
        assert state(runtime / "desktop.json")["token"] == gym_state["token"]
        with urlopen(f"http://127.0.0.1:{planner_port}/semana", timeout=60) as response:
            assert 'href="/academia"' in response.read().decode()
        try:
            build_opener(NoRedirect).open(f"http://127.0.0.1:{planner_port}/academia", timeout=30)
            raise AssertionError("Expected redirect")
        except HTTPError as response:
            assert response.code == 307
            assert response.headers["Location"].rstrip("/") == f"http://127.0.0.1:{gym_port}"
        wait_until(lambda: sets_count() == 1)
        request_stop(runtime, expected_run_id=status(runtime)["run_id"])
        wait_until(lambda: not status(runtime)["running"])
        command("status")
        command("dev", "--no-browser")
        assert status(runtime)["running"]
        assert state(runtime / "desktop.json")["token"] == gym_state["token"]
        (fixture / "source.txt").write_bytes(first + b"\n02/01/2026 10:00 - Pessoa: Extensora 45kg/10rep")
        wait_until(lambda: sets_count() == 2)
        assert len(list((fixture / "backups").glob("*.sqlite"))) == 2
        command("stop")
        assert not (runtime / "desktop.json").exists()
        assert not status(runtime)["running"]
        assert sets_count() == 2
        # Disable the optional integration: no inactive menu entry should remain.
        command("dev", "--no-browser", env={**environment, "GYM_TRACKER_PATH": ""})
        with urlopen(f"http://127.0.0.1:{planner_port}/semana", timeout=60) as response:
            assert 'href="/academia"' not in response.read().decode()
        print(
            "VERIFICADO: falhas, reuso, recuperacao, menu opcional, TXT e backup; somente dados de teste.", flush=True
        )
    finally:
        command("stop")
        print(f"Dados sinteticos preservados para inspecao: {fixture}", flush=True)


if __name__ == "__main__":
    main()
