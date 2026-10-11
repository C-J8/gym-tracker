"""Own the dashboard and importer started from the Planner's desktop shortcut."""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from gym_tracker.automation import (
    AlreadyRunning,
    ProcessLock,
    read_json,
    request_stop,
    write_json,
)
from gym_tracker.automation import (
    start as start_importer,
)
from gym_tracker.automation import (
    status as importer_status,
)
from gym_tracker.config import get_settings


def control(runtime: Path, action: str = "status") -> dict | None:
    state = read_json(runtime / "desktop.json")
    try:
        port = int(state["control_port"])
        token = str(uuid.UUID(state["token"]))
        if not 1 <= port <= 65535:
            return None
        request = Request(
            f"http://127.0.0.1:{port}/{action}",
            headers={"Authorization": f"Bearer {token}"},
            method="POST" if action in {"stop", "ensure"} else "GET",
        )
        with urlopen(request, timeout=3) as response:
            result = json.load(response)
        if result.get("token") == token and result.get("root") == str(Path.cwd().resolve()):
            return result
    except (OSError, ValueError, KeyError, URLError):
        pass
    return None


def dashboard_healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
            return response.status == 200 and response.read() == b"ok"
    except (OSError, URLError):
        return False


def watch(runtime: Path, port: int, token: str) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    with ProcessLock(runtime / "desktop.lock"):
        get_settings()  # Validate configuration before starting either child.
        root = str(Path.cwd().resolve())
        stopping = threading.Event()
        ensuring = threading.Event()
        state = {"root": root, "token": token, "port": port, "phase": "starting"}
        dashboard = None
        owned_importer = None

        def service_status():
            importer = importer_status(runtime)
            phase = state["phase"]
            if phase == "ready" and (not importer["running"] or importer.get("phase") == "error"):
                phase = "degraded"
            return {
                **state,
                "phase": phase,
                "importer_owned": owned_importer is not None and importer.get("run_id") == owned_importer,
                "importer": {key: importer.get(key) for key in ("running", "phase", "message", "run_id")},
            }

        def ensure_importer():
            nonlocal owned_importer
            run_id = uuid.uuid4().hex
            imported = start_importer(runtime, run_id=run_id)
            owned_importer = run_id if imported.get("run_id") == run_id else None
            state.pop("error", None)
            state["phase"] = "ready"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_error(403)
                    return
                if not (
                    (self.path == "/status" and self.command == "GET")
                    or (self.path in {"/stop", "/ensure"} and self.command == "POST")
                ):
                    self.send_error(404)
                    return
                if self.path == "/stop":
                    stopping.set()
                    state["phase"] = "stopping"
                elif self.path == "/ensure" and not stopping.is_set():
                    if not importer_status(runtime)["running"]:
                        state["phase"] = "starting"
                        ensuring.set()
                result = service_status()
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state["control_port"] = server.server_port
        write_json(runtime / "desktop.json", state)
        try:
            # Refuse occupied ports: do not adopt or terminate a manual Streamlit.
            with socket.socket() as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", port))
            with (runtime / "dashboard.log").open("ab") as log:
                dashboard = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "streamlit",
                        "run",
                        "app.py",
                        "--server.address=127.0.0.1",
                        f"--server.port={port}",
                        "--server.headless=true",
                        "--server.fileWatcherType=none",
                        "--browser.gatherUsageStats=false",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            deadline = time.monotonic() + 45
            while not dashboard_healthy(port):
                if stopping.is_set():
                    return
                if dashboard.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("O dashboard nao ficou pronto. Confira .gym-tracker/dashboard.log.")
                stopping.wait(0.25)
            ensure_importer()
            write_json(runtime / "desktop.json", state)
            while not stopping.wait(0.5):
                if dashboard.poll() is not None:
                    break
                if ensuring.is_set():
                    ensuring.clear()
                    try:
                        ensure_importer()
                    except (RuntimeError, OSError, ValueError):
                        state["phase"] = "degraded"
                        state["error"] = "Importador nao reiniciou. Confira .env e .gym-tracker/events.log."
        finally:
            state["phase"] = "stopping"
            if owned_importer is not None:
                request_stop(runtime, expected_run_id=owned_importer)
            if dashboard is not None and dashboard.poll() is None:
                dashboard.terminate()
                try:
                    dashboard.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    dashboard.kill()
                    dashboard.wait(timeout=5)
            # Let an in-flight import commit/roll back before releasing ownership.
            while owned_importer is not None:
                current = importer_status(runtime)
                if not current["running"] or current.get("run_id") != owned_importer:
                    break
                time.sleep(0.25)
            if read_json(runtime / "desktop.json").get("token") == token:
                (runtime / "desktop.json").unlink(missing_ok=True)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def start(runtime: Path, port: int) -> dict:
    current = control(runtime)
    if current:
        if current["port"] != port or current["phase"] == "stopping":
            raise RuntimeError("A Academia esta encerrando ou usa outra porta. Aguarde e confira status.")
        if current.get("importer", {}).get("running"):
            return current
        control(runtime, "ensure")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = control(runtime)
            if current and current.get("importer", {}).get("running"):
                return current
            if not current or current.get("error") or current["phase"] == "stopping":
                break
            time.sleep(0.1)
        raise RuntimeError("O dashboard esta aberto, mas o importador nao reiniciou. Confira status e .env.")
    token = str(uuid.uuid4())
    kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "gym_tracker.desktop",
            "watch",
            "--runtime-dir",
            str(runtime.resolve()),
            "--port",
            str(port),
            "--token",
            token,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = control(runtime)
        if current and current["phase"] in {"ready", "degraded"} and current.get("importer", {}).get("running"):
            return current
        if child.poll() is not None:
            raise RuntimeError("Academia nao iniciou. Confira porta, .env e .gym-tracker/dashboard.log.")
        time.sleep(0.25)
    current = control(runtime)
    if current and current.get("token") == token:
        control(runtime, "stop")
    raise RuntimeError("Academia demorou para iniciar. Confira status antes de tentar novamente.")


def main():
    parser = argparse.ArgumentParser(description="Dashboard e importador da Academia")
    parser.add_argument("command", choices=["start", "stop", "status", "watch"])
    parser.add_argument("--runtime-dir", type=Path, default=Path(".gym-tracker"))
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("A porta precisa estar entre 1024 e 65535.")
    try:
        if args.command == "start":
            result = start(args.runtime_dir, args.port)
        elif args.command == "stop":
            result = control(args.runtime_dir, "stop") or {"phase": "stopped"}
        elif args.command == "status":
            result = control(args.runtime_dir) or {"phase": "stopped"}
        else:
            watch(args.runtime_dir, args.port, args.token or str(uuid.uuid4()))
            return
        print(json.dumps(result, ensure_ascii=False))
    except (AlreadyRunning, RuntimeError, OSError, ValueError) as error:
        # Only our fixed operational messages are printed; no settings/credentials.
        message = str(error) if isinstance(error, RuntimeError) else "Confira configuracao, porta e ambiente Python."
        print(json.dumps({"error": message}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
