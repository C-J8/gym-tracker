"""Initial multiuser workout schema.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "exercises",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_name", sa.String(length=160), nullable=False),
        sa.Column("muscle_group", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )
    op.create_table(
        "imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("metadata", json_type, nullable=False),
        sa.CheckConstraint(
            "status IN ('processing','completed','completed_with_reviews','failed')", name="ck_import_status"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source_sha256", name="uq_import_user_source_hash"),
    )
    op.create_table(
        "workouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workout_date", sa.Date(), nullable=False),
        sa.Column("workout_type", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "exercise_variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("equipment", sa.String(length=80), nullable=False),
        sa.Column("gym_or_location", sa.String(length=120), nullable=False),
        sa.Column("load_basis", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exercise_id", "equipment", "gym_or_location", "load_basis", name="uq_exercise_variant_identity"
        ),
    )
    op.create_table(
        "exercise_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("raw_alias", sa.String(length=180), nullable=False),
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("suggested_equipment", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "raw_alias", name="uq_exercise_alias_user_raw"),
    )
    op.create_index(
        "uq_exercise_alias_global_raw",
        "exercise_aliases",
        ["raw_alias"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
        sqlite_where=sa.text("user_id IS NULL"),
    )
    op.create_table(
        "raw_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_raw", sa.String(length=255), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_edited", sa.Boolean(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("parse_status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "parse_status IN ('accepted','review','skipped','duplicate')", name="ck_raw_message_parse_status"
        ),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "content_sha256", name="uq_raw_message_content_hash"),
        sa.UniqueConstraint("import_id", "source_index", name="uq_raw_message_source_index"),
    )
    op.create_table(
        "parse_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("raw_message_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_payload", json_type, nullable=True),
        sa.Column("corrected_payload", json_type, nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("status IN ('pending','accepted','rejected')", name="ck_parse_review_status"),
        sa.ForeignKeyConstraint(["raw_message_id"], ["raw_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workout_id", sa.Uuid(), nullable=False),
        sa.Column("exercise_variant_id", sa.Uuid(), nullable=False),
        sa.Column("raw_message_id", sa.Uuid(), nullable=True),
        sa.Column("set_number", sa.Integer(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=7, scale=2), nullable=False),
        sa.Column("reps", sa.Integer(), nullable=False),
        sa.Column("rpe", sa.Numeric(precision=3, scale=1), nullable=True),
        sa.Column("rir", sa.Numeric(precision=3, scale=1), nullable=True),
        sa.Column("parse_method", sa.String(length=20), nullable=False),
        sa.Column("parser_version", sa.String(length=40), nullable=False),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("parse_method IN ('rule','llm','manual')", name="ck_set_parse_method"),
        sa.CheckConstraint("reps > 0 AND reps <= 200", name="ck_set_reps_range"),
        sa.CheckConstraint("rir IS NULL OR (rir >= 0 AND rir <= 20)", name="ck_set_rir_range"),
        sa.CheckConstraint("rpe IS NULL OR (rpe >= 0 AND rpe <= 10)", name="ck_set_rpe_range"),
        sa.CheckConstraint("set_number > 0", name="ck_set_number_positive"),
        sa.CheckConstraint("weight_kg >= 0 AND weight_kg <= 1000", name="ck_set_weight_range"),
        sa.ForeignKeyConstraint(["exercise_variant_id"], ["exercise_variants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["raw_message_id"], ["raw_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workout_id"], ["workouts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "raw_message_id", "exercise_variant_id", "set_number", name="uq_set_message_variant_number"
        ),
    )


def downgrade() -> None:
    op.drop_table("sets")
    op.drop_table("parse_reviews")
    op.drop_table("raw_messages")
    op.drop_index("uq_exercise_alias_global_raw", table_name="exercise_aliases")
    op.drop_table("exercise_aliases")
    op.drop_table("exercise_variants")
    op.drop_table("workouts")
    op.drop_table("imports")
    op.drop_table("exercises")
    op.drop_table("users")
