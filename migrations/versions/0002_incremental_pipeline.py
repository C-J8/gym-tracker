"""Incremental imports, versioned parse runs and active results.

Revision ID: 0002
Revises: 0001
"""

import hashlib
import json
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _normalized_sender(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(character for character in value if unicodedata.category(character) != "Mn")
    return re.sub(r"\s+", " ", value).strip(" -:")


def _normalized_content(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(lines).strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_key(value) -> str:
    parsed = _as_datetime(value)
    if hasattr(parsed, "tzinfo") and parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return f"minute:{parsed.replace(second=0, microsecond=0).isoformat()}"


def _as_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _bound_uuid(value, dialect_name: str):
    parsed = _as_uuid(value)
    return parsed.hex if dialect_name == "sqlite" else parsed


def _as_datetime(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _as_json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _legacy_message_rows(connection) -> list[dict]:
    rows = connection.execute(
        sa.text(
            """
            SELECT rm.id, rm.import_id, rm.sent_at, rm.sender_raw, rm.raw_content, rm.is_edited,
                   rm.source_index, rm.parse_status, rm.created_at, i.user_id, i.imported_at,
                   (SELECT s.parse_method FROM sets s WHERE s.raw_message_id = rm.id LIMIT 1) AS parse_method,
                   (SELECT pr.reason FROM parse_reviews pr WHERE pr.raw_message_id = rm.id LIMIT 1) AS reason,
                   (SELECT pr.status FROM parse_reviews pr WHERE pr.raw_message_id = rm.id LIMIT 1) AS review_status
            FROM raw_messages rm
            JOIN imports i ON i.id = rm.import_id
            ORDER BY i.imported_at, rm.import_id, rm.source_index, rm.id
            """
        )
    ).mappings()
    return [dict(row) for row in rows]


def _backfill_and_group_messages(
    connection,
    legacy_rows: list[dict],
    dialect_name: str,
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID | None]]:
    groups: dict[tuple[uuid.UUID, str], list[dict]] = defaultdict(list)
    occurrence_counts: dict[tuple[object, ...], int] = defaultdict(int)
    for row in legacy_rows:
        sender = _normalized_sender(row["sender_raw"])
        normalized_content_hash = _sha256(_normalized_content(row["raw_content"]))
        base_identity = (
            str(row["user_id"]),
            sender,
            _timestamp_key(row["sent_at"]),
            normalized_content_hash,
            str(bool(row["is_edited"])),
        )
        occurrence_key = (_as_uuid(row["import_id"]), *base_identity)
        occurrence_ordinal = occurrence_counts[occurrence_key]
        occurrence_counts[occurrence_key] += 1
        identity = _sha256("\n".join((*base_identity, str(occurrence_ordinal))))
        row.update(
            {
                "sender_normalized": sender,
                "normalized_content_sha256": normalized_content_hash,
                "identity_sha256": identity,
                "timestamp_precision": "minute",
                "occurrence_ordinal": occurrence_ordinal,
            }
        )
        connection.execute(
            sa.text(
                """
                UPDATE raw_messages
                SET user_id = :user_id,
                    source_offset = 0,
                    timestamp_precision = 'minute',
                    sender_normalized = :sender_normalized,
                    normalized_content_sha256 = :normalized_content_sha256,
                    identity_sha256 = :identity_sha256,
                    occurrence_ordinal = :occurrence_ordinal
                WHERE id = :id
                """
            ),
            {
                "id": _bound_uuid(row["id"], dialect_name),
                "user_id": _bound_uuid(row["user_id"], dialect_name),
                "sender_normalized": sender,
                "normalized_content_sha256": normalized_content_hash,
                "identity_sha256": identity,
                "occurrence_ordinal": occurrence_ordinal,
            },
        )
        groups[(_as_uuid(row["user_id"]), identity)].append(row)

    canonical_by_original: dict[uuid.UUID, uuid.UUID] = {}
    active_original_by_canonical: dict[uuid.UUID, uuid.UUID | None] = {}
    for rows in groups.values():
        canonical_id = _as_uuid(rows[0]["id"])
        accepted = next((row for row in rows if row["parse_status"] == "accepted"), None)
        active_original = _as_uuid(accepted["id"]) if accepted is not None else None
        active_original_by_canonical[canonical_id] = active_original
        canonical_status = (
            "accepted"
            if active_original
            else ("review" if any(row["parse_status"] == "review" for row in rows) else "skipped")
        )
        connection.execute(
            sa.text("UPDATE raw_messages SET parse_status = :status WHERE id = :id"),
            {"status": canonical_status, "id": _bound_uuid(canonical_id, dialect_name)},
        )
        for row in rows:
            canonical_by_original[_as_uuid(row["id"])] = canonical_id
    return canonical_by_original, active_original_by_canonical


def _consolidate_legacy_dependencies(
    connection,
    canonical_by_original: dict[uuid.UUID, uuid.UUID],
    active_original_by_canonical: dict[uuid.UUID, uuid.UUID | None],
    dialect_name: str,
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID]]:
    set_rows = [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT id, raw_message_id, exercise_variant_id, set_number, weight_kg, reps,
                       rpe, rir, parse_method, parser_version, llm_model
                FROM sets
                WHERE raw_message_id IS NOT NULL
                ORDER BY created_at, id
                """
            )
        ).mappings()
    ]
    active_original_ids = {item for item in active_original_by_canonical.values() if item is not None}
    set_rows.sort(
        key=lambda row: (
            _as_uuid(row["raw_message_id"]) not in active_original_ids,
            str(row["id"]),
        )
    )
    review_origins = {
        _as_uuid(row["id"]): _as_uuid(row["raw_message_id"])
        for row in connection.execute(sa.text("SELECT id, raw_message_id FROM parse_reviews")).mappings()
    }
    kept_set_origins: dict[uuid.UUID, uuid.UUID] = {}
    seen_sets: set[tuple] = set()
    for row in set_rows:
        set_id = _as_uuid(row["id"])
        original_id = _as_uuid(row["raw_message_id"])
        canonical_id = canonical_by_original[original_id]
        signature = (
            canonical_id,
            _as_uuid(row["exercise_variant_id"]),
            row["set_number"],
            row["weight_kg"],
            row["reps"],
            row["rpe"],
            row["rir"],
            row["parse_method"],
            row["parser_version"],
            row["llm_model"],
        )
        if signature in seen_sets:
            connection.execute(
                sa.text("DELETE FROM sets WHERE id = :id"),
                {"id": _bound_uuid(set_id, dialect_name)},
            )
            continue
        seen_sets.add(signature)
        kept_set_origins[set_id] = original_id
        connection.execute(
            sa.text("UPDATE sets SET raw_message_id = :canonical_id WHERE id = :id"),
            {
                "canonical_id": _bound_uuid(canonical_id, dialect_name),
                "id": _bound_uuid(set_id, dialect_name),
            },
        )

    for review_id, original_id in review_origins.items():
        connection.execute(
            sa.text("UPDATE parse_reviews SET raw_message_id = :canonical_id WHERE id = :id"),
            {
                "canonical_id": _bound_uuid(canonical_by_original[original_id], dialect_name),
                "id": _bound_uuid(review_id, dialect_name),
            },
        )

    duplicate_ids = [
        original_id for original_id, canonical_id in canonical_by_original.items() if original_id != canonical_id
    ]
    for duplicate_id in duplicate_ids:
        connection.execute(
            sa.text("DELETE FROM raw_messages WHERE id = :id"),
            {"id": _bound_uuid(duplicate_id, dialect_name)},
        )
    return kept_set_origins, review_origins


def upgrade() -> None:
    op.add_column("raw_messages", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.add_column("raw_messages", sa.Column("source_offset", sa.Integer(), nullable=True))
    op.add_column("raw_messages", sa.Column("timestamp_precision", sa.String(length=10), nullable=True))
    op.add_column("raw_messages", sa.Column("sender_normalized", sa.String(length=255), nullable=True))
    op.add_column("raw_messages", sa.Column("normalized_content_sha256", sa.String(length=64), nullable=True))
    op.add_column("raw_messages", sa.Column("identity_sha256", sa.String(length=64), nullable=True))
    op.add_column("raw_messages", sa.Column("occurrence_ordinal", sa.Integer(), nullable=True))

    connection = op.get_bind()
    dialect_name = connection.dialect.name
    legacy_rows = _legacy_message_rows(connection)
    canonical_by_original, active_original_by_canonical = _backfill_and_group_messages(
        connection,
        legacy_rows,
        dialect_name,
    )

    with op.batch_alter_table("sets") as batch:
        batch.drop_constraint("uq_set_message_variant_number", type_="unique")
    kept_set_origins, review_origins = _consolidate_legacy_dependencies(
        connection,
        canonical_by_original,
        active_original_by_canonical,
        dialect_name,
    )

    with op.batch_alter_table("raw_messages") as batch:
        batch.drop_constraint("uq_raw_message_content_hash", type_="unique")
        batch.alter_column("user_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("source_offset", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("timestamp_precision", existing_type=sa.String(length=10), nullable=False)
        batch.alter_column("sender_normalized", existing_type=sa.String(length=255), nullable=False)
        batch.alter_column("normalized_content_sha256", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("identity_sha256", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("occurrence_ordinal", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key("fk_raw_messages_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE")
        batch.create_unique_constraint("uq_raw_message_user_identity", ["user_id", "identity_sha256"])

    op.create_table(
        "import_message_occurrences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("raw_message_id", sa.Uuid(), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.Column("source_offset", sa.Integer(), nullable=False),
        sa.Column("occurrence_ordinal", sa.Integer(), nullable=False),
        sa.Column("timestamp_precision", sa.String(length=10), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raw_message_id"], ["raw_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "source_index", name="uq_occurrence_import_source_index"),
        sa.UniqueConstraint(
            "import_id",
            "raw_message_id",
            "occurrence_ordinal",
            name="uq_occurrence_import_message_ordinal",
        ),
    )
    occurrence_table = sa.table(
        "import_message_occurrences",
        sa.column("id", sa.Uuid()),
        sa.column("import_id", sa.Uuid()),
        sa.column("raw_message_id", sa.Uuid()),
        sa.column("source_index", sa.Integer()),
        sa.column("source_offset", sa.Integer()),
        sa.column("occurrence_ordinal", sa.Integer()),
        sa.column("timestamp_precision", sa.String()),
    )
    occurrence_rows = [
        {
            "id": uuid.uuid4(),
            "import_id": _as_uuid(row["import_id"]),
            "raw_message_id": canonical_by_original[_as_uuid(row["id"])],
            "source_index": row["source_index"],
            "source_offset": 0,
            "occurrence_ordinal": row["occurrence_ordinal"],
            "timestamp_precision": "minute",
        }
        for row in legacy_rows
    ]
    if occurrence_rows:
        op.bulk_insert(occurrence_table, occurrence_rows)

    op.create_table(
        "parse_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("parser_version", sa.String(length=40), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=True),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column("llm_shadow_mode", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
        sa.CheckConstraint(
            "status IN ('processing','completed','completed_with_reviews','failed')",
            name="ck_parse_run_status",
        ),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "parser_version", name="uq_parse_run_import_parser_version"),
    )
    parse_run_table = sa.table(
        "parse_runs",
        sa.column("id", sa.Uuid()),
        sa.column("import_id", sa.Uuid()),
        sa.column("parser_version", sa.String()),
        sa.column("prompt_version", sa.String()),
        sa.column("llm_model", sa.String()),
        sa.column("llm_shadow_mode", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("completed_at", sa.DateTime(timezone=True)),
        sa.column("metadata", json_type),
    )
    import_rows = list(
        connection.execute(
            sa.text("SELECT id, parser_version, status, imported_at, metadata FROM imports ORDER BY imported_at")
        ).mappings()
    )
    run_by_import: dict[uuid.UUID, uuid.UUID] = {}
    run_rows = []
    for row in import_rows:
        run_id = uuid.uuid4()
        import_id = _as_uuid(row["id"])
        run_by_import[import_id] = run_id
        run_rows.append(
            {
                "id": run_id,
                "import_id": import_id,
                "parser_version": row["parser_version"],
                "prompt_version": None,
                "llm_model": None,
                "llm_shadow_mode": True,
                "status": row["status"],
                "completed_at": _as_datetime(row["imported_at"]),
                "metadata": _as_json(row["metadata"]),
            }
        )
    if run_rows:
        op.bulk_insert(parse_run_table, run_rows)

    op.create_table(
        "parse_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("raw_message_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("parse_method", sa.String(length=20), nullable=False),
        sa.Column("payload", json_type, nullable=True),
        sa.Column("reasons", json_type, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('accepted','review','skipped','reused','rejected')",
            name="ck_parse_result_status",
        ),
        sa.CheckConstraint("parse_method IN ('rule','llm','manual')", name="ck_parse_result_method"),
        sa.ForeignKeyConstraint(["parse_run_id"], ["parse_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raw_message_id"], ["raw_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parse_run_id", "raw_message_id", name="uq_parse_result_run_message"),
    )
    op.create_index(
        "uq_parse_result_active_message",
        "parse_results",
        ["raw_message_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )
    parse_result_table = sa.table(
        "parse_results",
        sa.column("id", sa.Uuid()),
        sa.column("parse_run_id", sa.Uuid()),
        sa.column("raw_message_id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("parse_method", sa.String()),
        sa.column("payload", json_type),
        sa.column("reasons", json_type),
        sa.column("is_active", sa.Boolean()),
        sa.column("activated_at", sa.DateTime(timezone=True)),
    )
    result_by_original: dict[uuid.UUID, uuid.UUID] = {}
    result_rows = []
    for row in legacy_rows:
        original_id = _as_uuid(row["id"])
        canonical_id = canonical_by_original[original_id]
        result_id = uuid.uuid4()
        result_by_original[original_id] = result_id
        active = active_original_by_canonical[canonical_id] == original_id
        status = row["parse_status"]
        if active:
            status = "accepted"
        elif status in {"accepted", "duplicate"}:
            status = "reused"
        elif row["review_status"] == "rejected":
            status = "rejected"
        result_rows.append(
            {
                "id": result_id,
                "parse_run_id": run_by_import[_as_uuid(row["import_id"])],
                "raw_message_id": canonical_id,
                "status": status,
                "parse_method": row["parse_method"] or "rule",
                "payload": None,
                "reasons": [row["reason"]] if row["reason"] else [],
                "is_active": active,
                "activated_at": _as_datetime(row["created_at"]) if active else None,
            }
        )
    if result_rows:
        op.bulk_insert(parse_result_table, result_rows)

    op.add_column("sets", sa.Column("parse_result_id", sa.Uuid(), nullable=True))
    op.add_column("parse_reviews", sa.Column("parse_result_id", sa.Uuid(), nullable=True))
    for set_id, original_id in kept_set_origins.items():
        connection.execute(
            sa.text("UPDATE sets SET parse_result_id = :result_id WHERE id = :id"),
            {
                "result_id": _bound_uuid(result_by_original[original_id], dialect_name),
                "id": _bound_uuid(set_id, dialect_name),
            },
        )
    for review_id, original_id in review_origins.items():
        connection.execute(
            sa.text("UPDATE parse_reviews SET parse_result_id = :result_id WHERE id = :id"),
            {
                "result_id": _bound_uuid(result_by_original[original_id], dialect_name),
                "id": _bound_uuid(review_id, dialect_name),
            },
        )

    with op.batch_alter_table("sets") as batch:
        batch.drop_constraint("ck_set_weight_range", type_="check")
        batch.alter_column("parse_result_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key(
            "fk_sets_parse_result_id", "parse_results", ["parse_result_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_unique_constraint(
            "uq_set_result_variant_number", ["parse_result_id", "exercise_variant_id", "set_number"]
        )
        batch.create_check_constraint("ck_set_weight_range", "weight_kg > 0 AND weight_kg <= 500")

    with op.batch_alter_table("parse_reviews") as batch:
        batch.alter_column("parse_result_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key(
            "fk_parse_reviews_parse_result_id",
            "parse_results",
            ["parse_result_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.create_table(
        "data_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table("data_revisions", sa.column("id", sa.Integer()), sa.column("version", sa.BigInteger())),
        [{"id": 1, "version": 1}],
    )


def _make_legacy_content_hashes_unique(connection, dialect_name: str) -> None:
    rows = connection.execute(
        sa.text("SELECT id, import_id, content_sha256 FROM raw_messages ORDER BY import_id, source_index, id")
    ).mappings()
    seen: set[tuple[uuid.UUID, str]] = set()
    for row in rows:
        key = (_as_uuid(row["import_id"]), row["content_sha256"])
        if key not in seen:
            seen.add(key)
            continue
        replacement = _sha256(f"{row['content_sha256']}:{row['id']}")
        connection.execute(
            sa.text("UPDATE raw_messages SET content_sha256 = :hash WHERE id = :id"),
            {"hash": replacement, "id": _bound_uuid(row["id"], dialect_name)},
        )


def downgrade() -> None:
    connection = op.get_bind()
    dialect_name = connection.dialect.name
    connection.execute(
        sa.text("DELETE FROM sets WHERE parse_result_id IN (SELECT id FROM parse_results WHERE NOT is_active)")
    )
    with op.batch_alter_table("parse_reviews") as batch:
        batch.drop_constraint("fk_parse_reviews_parse_result_id", type_="foreignkey")
        batch.drop_column("parse_result_id")
    with op.batch_alter_table("sets") as batch:
        batch.drop_constraint("uq_set_result_variant_number", type_="unique")
        batch.drop_constraint("fk_sets_parse_result_id", type_="foreignkey")
        batch.drop_constraint("ck_set_weight_range", type_="check")
        batch.drop_column("parse_result_id")
        batch.create_unique_constraint(
            "uq_set_message_variant_number", ["raw_message_id", "exercise_variant_id", "set_number"]
        )
        batch.create_check_constraint("ck_set_weight_range", "weight_kg >= 0 AND weight_kg <= 1000")
    op.drop_table("data_revisions")
    op.drop_index("uq_parse_result_active_message", table_name="parse_results")
    op.drop_table("parse_results")
    op.drop_table("parse_runs")
    op.drop_table("import_message_occurrences")
    _make_legacy_content_hashes_unique(connection, dialect_name)
    with op.batch_alter_table("raw_messages") as batch:
        batch.drop_constraint("uq_raw_message_user_identity", type_="unique")
        batch.drop_constraint("fk_raw_messages_user_id", type_="foreignkey")
        batch.drop_column("occurrence_ordinal")
        batch.drop_column("identity_sha256")
        batch.drop_column("normalized_content_sha256")
        batch.drop_column("sender_normalized")
        batch.drop_column("timestamp_precision")
        batch.drop_column("source_offset")
        batch.drop_column("user_id")
        batch.create_unique_constraint("uq_raw_message_content_hash", ["import_id", "content_sha256"])
