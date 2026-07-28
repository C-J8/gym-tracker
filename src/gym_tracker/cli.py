import argparse
import json
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from gym_tracker.config import get_settings
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


def _extraction_summary(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    if "exercises" not in payload:
        return []
    return [
        {
            "exercise": item.get("canonical_name") or item.get("raw_name"),
            "equipment": item.get("equipment"),
            "sets": [{"weight_kg": row.get("weight_kg"), "reps": row.get("reps")} for row in item.get("sets", [])],
        }
        for item in payload.get("exercises", [])
    ]


def _review_view(review: ParseReview, include_content: bool) -> dict:
    proposed = review.proposed_payload or {}
    rule = proposed.get("rule") if isinstance(proposed, dict) else None
    llm = proposed.get("llm") if isinstance(proposed, dict) else None
    raw = review.raw_message
    parse_run = review.parse_result.parse_run
    occurrence = next(
        (item for item in raw.occurrences if item.import_id == parse_run.import_id),
        None,
    )
    payload = {
        "id": review.id,
        "message_date": raw.sent_at,
        "reason": review.reason,
        "parse_method": review.parse_result.parse_method,
        "content_preview": raw.raw_content.replace("\n", " ")[:160],
        "has_proposal": bool(proposed),
        "status": review.status,
        "origin": {
            "import_id": parse_run.import_id,
            "parse_run_id": parse_run.id,
            "source_filename": parse_run.import_record.source_filename,
            "source_index": occurrence.source_index if occurrence else raw.source_index,
        },
    }
    if include_content:
        payload.update(
            {
                "original_content": raw.raw_content,
                "proposed_payload": proposed,
                "corrected_payload": review.corrected_payload,
                "differences": {
                    "rule": _extraction_summary(rule),
                    "llm": _extraction_summary(llm),
                },
                "reviewed_at": review.reviewed_at,
            }
        )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gym-tracker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("db-upgrade", help="aplica migrations ate a revisao mais recente")
    create_user = subcommands.add_parser("create-user", help="cria um usuario")
    create_user.add_argument("--name", required=True)
    import_whatsapp = subcommands.add_parser("import-whatsapp", help="importa um export TXT do WhatsApp")
    import_whatsapp.add_argument("--file", required=True, type=Path)
    import_whatsapp.add_argument("--user", required=True, type=_uuid)
    import_whatsapp.add_argument("--parser-version", help="sobrescreve PARSER_VERSION para este parse run")
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
    reviews.add_argument("--json", action="store_true", help="mantido por compatibilidade; a saida ja e JSON")
    show_review = subcommands.add_parser("show-review", help="mostra contexto e propostas de uma revisao")
    show_review.add_argument("--review-id", required=True, type=_uuid)
    show_review.add_argument("--json", action="store_true", help="mantido por compatibilidade; a saida ja e JSON")
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
            configured = get_settings()
            run_settings = (
                configured.model_copy(update={"parser_version": args.parser_version})
                if args.parser_version
                else configured
            )
            _print(import_whatsapp_file(session, args.file, args.user, settings=run_settings))
        elif args.command == "import-legacy-csv":
            _print(import_legacy_csv(session, args.file, args.user))
        elif args.command == "quality-report":
            record = session.get(Import, args.import_id)
            if record is None:
                raise SystemExit("importacao nao encontrada")
            _print(
                {
                    "import_id": record.id,
                    "status": record.status,
                    "parse_runs": [
                        {
                            "id": run.id,
                            "parser_version": run.parser_version,
                            "prompt_version": run.prompt_version,
                            "llm_model": run.llm_model,
                            "shadow_mode": run.llm_shadow_mode,
                            "status": run.status,
                            "started_at": run.started_at,
                            "completed_at": run.completed_at,
                            **(run.metadata_ or {}),
                        }
                        for run in record.parse_runs
                    ],
                }
            )
        elif args.command == "list-reviews":
            rows = session.scalars(
                select(ParseReview).where(ParseReview.status == args.status).order_by(ParseReview.created_at)
            )
            _print([_review_view(row, include_content=False) for row in rows])
        elif args.command == "show-review":
            review = session.get(ParseReview, args.review_id)
            if review is None:
                raise SystemExit("revisao nao encontrada")
            _print(_review_view(review, include_content=True))
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
