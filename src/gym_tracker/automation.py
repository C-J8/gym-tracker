"""Local background importer. No scheduler server or external packages required."""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from gym_tracker.config import Settings, get_settings
from gym_tracker.services.automatic_import import (
    ImportInputError,
    SourceNotReady,
    import_snapshot,
    stable_snapshot,
)


class AlreadyRunning(RuntimeError):
    pass


class ProcessLock:
    """OS-owned lock: automatically released even after a crash or reboot."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise AlreadyRunning("A importacao automatica ja esta em execucao.") from error
        self.handle = handle
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            # Close releases the lock; never delete the lock file while another
            # process may be opening it.
            self.handle.close()
            self.handle = None


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def is_running(runtime: Path) -> bool:
    if not (runtime / "worker.lock").exists():
        return False
    try:
        with ProcessLock(runtime / "worker.lock"):
            return False
    except AlreadyRunning:
        return True


def status(runtime: Path) -> dict:
    return {**read_json(runtime / "status.json"), "running": is_running(runtime)}


def request_stop(runtime: Path, *, expected_run_id: str | None = None) -> dict:
    current = status(runtime)
    if not current["running"]:
        return {"message": "Importacao automatica ja esta parada.", "running": False}
    run_id = current.get("run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        return {
            "message": "Outra instancia esta ativa; ela foi preservada.",
            "running": current["running"],
        }
    if not run_id:
        return {
            "message": "Processo iniciando. Tente parar novamente em alguns segundos.",
            "running": True,
        }
    write_json(runtime / "stop.json", {"run_id": run_id})
    return {
        "message": "Parada solicitada. A transacao em andamento sera finalizada antes de encerrar.",
        "running": True,
    }


class ImportWorker:
    def __init__(self, settings: Settings, runtime: Path, run_id: str):
        self.settings = settings
        self.runtime = runtime
        self.run_id = run_id
        self.last_event = None
        self.logger = logging.getLogger(f"gym-auto.{run_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        runtime.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(runtime / "events.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        self.logger.addHandler(self.handler)

    def close(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)

    def stopping(self) -> bool:
        return read_json(self.runtime / "stop.json").get("run_id") == self.run_id

    def wait(self, seconds: float):
        deadline = time.monotonic() + seconds
        while not self.stopping() and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def publish(self, phase: str, message: str, **fields):
        previous = read_json(self.runtime / "status.json")
        now = datetime.now(UTC).isoformat()
        current = {
            **previous,
            "run_id": self.run_id,
            "pid": os.getpid(),
            "phase": phase,
            "message": message,
            "checked_at": now,
            "source": str(self.settings.gym_import_file.resolve()),
            **fields,
        }
        if phase not in {"starting", "stopped"}:
            current["last_cycle_phase"] = phase
            current["last_cycle_message"] = message
        write_json(self.runtime / "status.json", current)
        event = json.dumps(
            {"phase": phase, "message": message, **fields},
            sort_keys=True,
            ensure_ascii=False,
        )
        if event != self.last_event:
            # Only operational summaries, never message contents or exception text.
            self.logger.info("%s %s", now, event)
            self.last_event = event

    def cycle(self):
        try:
            content = stable_snapshot(
                self.settings.gym_import_file,
                self.settings.gym_import_settle_seconds,
                wait=self.wait,
            )
            if self.stopping():
                return
            report = import_snapshot(self.settings, self.settings.gym_import_file, content)
            summary = {
                key: getattr(report, key)
                for key in (
                    "import_id",
                    "parse_run_id",
                    "messages_total",
                    "messages_new",
                    "messages_accepted",
                    "messages_pending_review",
                    "sets_accepted",
                )
            }
            if report.duplicate_import:
                self.publish(
                    "unchanged",
                    "TXT ja processado nesta versao. Aguardando mudancas.",
                    summary=summary,
                )
            else:
                self.publish(
                    "imported",
                    "Importacao concluida. Mensagens duvidosas continuam na fila de revisao.",
                    summary=summary,
                    last_import_at=datetime.now(UTC).isoformat(),
                )
        except FileNotFoundError:
            self.publish("waiting", "TXT nao encontrado. Aguardando o arquivo configurado.")
        except SourceNotReady as error:
            self.publish("waiting", str(error))
        except ImportInputError as error:
            self.publish("error", str(error))
        except SQLAlchemyError:
            self.publish(
                "error",
                "Banco indisponivel ou desatualizado. Confira Docker e migrations; nova tentativa automatica.",
            )
        except OSError:
            self.publish(
                "error",
                "Nao foi possivel ler o TXT. Confira permissoes; nova tentativa automatica.",
            )
        except Exception:
            self.publish(
                "error",
                "A importacao falhou e foi revertida. Confira a configuracao e execute os testes.",
            )


def watch(settings: Settings, runtime: Path, run_id: str, *, once: bool = False):
    with ProcessLock(runtime / "worker.lock"):
        worker = ImportWorker(settings, runtime, run_id)
        try:
            worker.publish("starting", "Importacao automatica iniciada.")
            while not worker.stopping():
                worker.cycle()
                if once:
                    break
                worker.wait(settings.gym_import_interval_seconds)
        finally:
            worker.publish("stopped", "Importacao automatica parada. Banco e arquivos preservados.")
            worker.close()


def start(runtime: Path, *, run_id: str | None = None) -> dict:
    if is_running(runtime):
        return {
            "message": "Importacao automatica ja esta em execucao.",
            **status(runtime),
        }
    run_id = run_id or uuid.uuid4().hex
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
            "gym_tracker.automation",
            "watch",
            "--runtime-dir",
            str(runtime.resolve()),
            "--run-id",
            run_id,
        ],
        cwd=Path.cwd(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        current = status(runtime)
        if current["running"] and current.get("run_id") == run_id:
            return {
                "message": "Importacao automatica iniciada em segundo plano.",
                **current,
            }
        if child.poll() is not None:
            if is_running(runtime):
                return status(runtime)
            raise RuntimeError("Nao foi possivel iniciar. Execute o comando watch no terminal para conferir.")
        time.sleep(0.1)
    raise RuntimeError("A inicializacao demorou. Consulte status antes de tentar novamente.")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Importacao automatica local do Gym Tracker")
    parser.add_argument("command", choices=["start", "stop", "status", "watch", "once"])
    parser.add_argument("--runtime-dir", type=Path, default=Path(".gym-tracker"))
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = status(args.runtime_dir)
        elif args.command == "stop":
            result = request_stop(args.runtime_dir)
        else:
            settings = get_settings()
            if args.command == "start":
                result = start(args.runtime_dir)
            else:
                watch(
                    settings,
                    args.runtime_dir,
                    args.run_id or uuid.uuid4().hex,
                    once=args.command == "once",
                )
                result = status(args.runtime_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "once" and result.get("last_cycle_phase") == "error":
            raise SystemExit(1)
    except (AlreadyRunning, RuntimeError):
        print("Nao foi possivel executar. Confira se outra instancia esta ativa e use o comando status.")
        raise SystemExit(1) from None
    except (ValueError, OSError):
        print("Configuracao invalida ou pasta sem permissao. Confira .env e o ambiente Python.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
