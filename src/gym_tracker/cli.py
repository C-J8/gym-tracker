import argparse
import json
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from gym_tracker.db import session_scope
from gym_tracker.models import Import, ParseReview, ReviewStatus, User
from gym_tracker.schemas import WorkoutExtraction
from gym_tracker.services.legacy_import import import_legacy_csv
from gym_tracker.services.legacy_migration import compare_legacy_sources
from gym_tracker.services.parser import parse_workout_message, split_whatsapp_messages
from gym_tracker.services.reviews import accept_review, reject_review
from gym_tracker.services.whatsapp_import import import_whatsapp_file


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("UUID invalido") from error


def _print(payload) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gym-tracker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("db-upgrade", help="aplica migrations ate a revisao mais recente")
    create_user = subcommands.add_parser("create-user", help="cria um usuario")
    create_user.add_argument("--name", required=True)
    import_whatsapp = subcommands.add_parser("import-whatsapp", help="importa um export TXT do WhatsApp")
    import_whatsapp.add_argument("--file", required=True, type=Path)
    import_whatsapp.add_argument("--user", required=True, type=_uuid)
    import_csv = subcommands.add_parser("import-legacy-csv", help="importa o CSV legado")
    import_csv.add_argument("--file", required=True, type=Path)
    import_csv.add_argument("--user", required=True, type=_uuid)
    quality = subcommands.add_parser("quality-report", help="mostra o relatorio de uma importacao")
    quality.add_argument("--import-id", required=True, type=_uuid)
    evaluate = subcommands.add_parser("evaluate-parser", help="avalia o parser sem gravar no banco")
    evaluate.add_argument("--file", required=True, type=Path)
    compare = subcommands.add_parser("compare-legacy", help="reprocessa o TXT e compara com o CSV sem sobrescrever")
    compare.add_argument("--whatsapp-file", required=True, type=Path)
    compare.add_argument("--legacy-csv", required=True, type=Path)
    compare.add_argument("--output-dir", type=Path, default=Path("quality_reports"))
    reviews = subcommands.add_parser("list-reviews", help="lista pendencias de revisao")
    reviews.add_argument("--status", choices=[item.value for item in ReviewStatus], default="pending")
    accept = subcommands.add_parser("accept-review", help="aceita uma revisao com payload corrigido")
    accept.add_argument("--review-id", required=True, type=_uuid)
    accept.add_argument("--payload", required=True, type=Path)
    accept.add_argument("--save-alias")
    reject = subcommands.add_parser("reject-review", help="rejeita uma revisao")
    reject.add_argument("--review-id", required=True, type=_uuid)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "db-upgrade":
        command.upgrade(Config("alembic.ini"), "head")
        return
    if args.command == "evaluate-parser":
        messages = split_whatsapp_messages(args.file.read_text(encoding="utf-8-sig"))
        outcomes = [parse_workout_message(message) for message in messages]
        _print(
            {
                "messages": len(messages),
                "with_workouts": sum(outcome.extraction is not None for outcome in outcomes),
                "needs_review": sum(outcome.needs_review for outcome in outcomes),
                "sets_proposed": sum(
                    len(exercise.sets)
                    for outcome in outcomes
                    if outcome.extraction
                    for exercise in outcome.extraction.exercises
                ),
            }
        )
        return
    if args.command == "compare-legacy":
        _print(compare_legacy_sources(args.whatsapp_file, args.legacy_csv, args.output_dir))
        return

    with session_scope() as session:
        if args.command == "create-user":
            user = User(display_name=args.name)
            session.add(user)
            session.flush()
            _print({"id": user.id, "display_name": user.display_name})
        elif args.command == "import-whatsapp":
            _print(import_whatsapp_file(session, args.file, args.user))
        elif args.command == "import-legacy-csv":
            _print(import_legacy_csv(session, args.file, args.user))
        elif args.command == "quality-report":
            record = session.get(Import, args.import_id)
            if record is None:
                raise SystemExit("importacao nao encontrada")
            _print({"import_id": record.id, "status": record.status, **record.metadata_})
        elif args.command == "list-reviews":
            rows = session.scalars(
                select(ParseReview).where(ParseReview.status == args.status).order_by(ParseReview.created_at)
            )
            _print(
                [
                    {"id": row.id, "raw_message_id": row.raw_message_id, "reason": row.reason, "status": row.status}
                    for row in rows
                ]
            )
        elif args.command == "accept-review":
            payload = json.loads(args.payload.read_text(encoding="utf-8"))
            WorkoutExtraction.model_validate(payload)
            review = accept_review(session, args.review_id, payload, args.save_alias)
            _print({"id": review.id, "status": review.status})
        elif args.command == "reject-review":
            review = reject_review(session, args.review_id)
            _print({"id": review.id, "status": review.status})


if __name__ == "__main__":
    main()
