from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.cli import _review_view, build_parser
from gym_tracker.config import Settings
from gym_tracker.models import ParseReview, User
from gym_tracker.services.legacy_migration import _row_key
from gym_tracker.services.whatsapp_import import import_whatsapp_file


def test_comparison_ignores_visual_text_differences() -> None:
    first = {
        "data": "2026-01-01",
        "grupo_muscular": "  BÍCEPS ",
        "exercicio": "Bíceps máquina",
        "tipo": "Maquina",
        "peso_kg": "22,5".replace(",", "."),
        "serie": "1",
        "repeticoes": "10",
    }
    second = {
        "data": "2026-01-01",
        "grupo_muscular": "biceps",
        "exercicio": "biceps   maquina",
        "tipo": "MÁQUINA",
        "peso_kg": "22.50",
        "serie": "1.0",
        "repeticoes": "10.0",
    }
    assert _row_key(first) == _row_key(second)
    second["tipo"] = "Halter"
    assert _row_key(first) != _row_key(second)


def test_review_cli_exposes_list_and_detail_context(session: Session, user: User, tmp_path: Path) -> None:
    source = tmp_path / "review.txt"
    source.write_text("01/01/2026 10:00 - Pessoa: Movimento X 10kg/10rep", encoding="utf-8")
    import_whatsapp_file(
        session,
        source,
        user.id,
        settings=Settings(parser_version="cli-test", openai_api_key=None),
    )
    review = session.scalar(select(ParseReview))
    assert review is not None

    listing = _review_view(review, include_content=False)
    detail = _review_view(review, include_content=True)
    assert {
        "id",
        "message_date",
        "reason",
        "parse_method",
        "content_preview",
        "has_proposal",
        "status",
        "origin",
    } <= listing.keys()
    assert detail["original_content"] == "Movimento X 10kg/10rep"
    assert "proposed_payload" in detail
    assert build_parser().parse_args(["show-review", "--review-id", str(review.id), "--json"]).json is True


def test_catalog_bootstrap_cli_defaults_to_dry_run_and_requires_apply_flag() -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    dry_run = build_parser().parse_args(["bootstrap-catalog", "--file", "legacy.csv", "--user", user_id])
    applied = build_parser().parse_args(["bootstrap-catalog", "--file", "legacy.csv", "--user", user_id, "--apply"])

    assert dry_run.apply is False
    assert applied.apply is True
