import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ImportStatus(enum.StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_REVIEWS = "completed_with_reviews"
    FAILED = "failed"


class ParseStatus(enum.StrEnum):
    ACCEPTED = "accepted"
    REVIEW = "review"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"


class ParseMethod(enum.StrEnum):
    RULE = "rule"
    LLM = "llm"
    MANUAL = "manual"


class ReviewStatus(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    imports: Mapped[list["Import"]] = relationship(back_populates="user")
    workouts: Mapped[list["Workout"]] = relationship(back_populates="user")


class Import(Base):
    __tablename__ = "imports"
    __table_args__ = (
        UniqueConstraint("user_id", "source_sha256", name="uq_import_user_source_hash"),
        CheckConstraint(
            "status IN ('processing','completed','completed_with_reviews','failed')",
            name="ck_import_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=ImportStatus.PROCESSING.value)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)

    user: Mapped[User] = relationship(back_populates="imports")
    raw_messages: Mapped[list["RawMessage"]] = relationship(back_populates="import_record")


class RawMessage(TimestampMixin, Base):
    __tablename__ = "raw_messages"
    __table_args__ = (
        UniqueConstraint("import_id", "source_index", name="uq_raw_message_source_index"),
        UniqueConstraint("import_id", "content_sha256", name="uq_raw_message_content_hash"),
        CheckConstraint(
            "parse_status IN ('accepted','review','skipped','duplicate')",
            name="ck_raw_message_parse_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), nullable=False)
    source_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sender_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_edited: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), nullable=False, default=ParseStatus.SKIPPED.value)

    import_record: Mapped[Import] = relationship(back_populates="raw_messages")
    sets: Mapped[list["WorkoutSet"]] = relationship(back_populates="raw_message")
    reviews: Mapped[list["ParseReview"]] = relationship(back_populates="raw_message")


class Workout(TimestampMixin, Base):
    __tablename__ = "workouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workout_date: Mapped[date] = mapped_column(Date, nullable=False)
    workout_type: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="whatsapp")

    user: Mapped[User] = relationship(back_populates="workouts")
    sets: Mapped[list["WorkoutSet"]] = relationship(back_populates="workout", cascade="all, delete-orphan")


class Exercise(TimestampMixin, Base):
    __tablename__ = "exercises"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    muscle_group: Mapped[str] = mapped_column(String(80), nullable=False)

    variants: Mapped[list["ExerciseVariant"]] = relationship(back_populates="exercise")
    aliases: Mapped[list["ExerciseAlias"]] = relationship(back_populates="exercise")


class ExerciseVariant(TimestampMixin, Base):
    __tablename__ = "exercise_variants"
    __table_args__ = (
        UniqueConstraint(
            "exercise_id",
            "equipment",
            "gym_or_location",
            "load_basis",
            name="uq_exercise_variant_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False)
    equipment: Mapped[str] = mapped_column(String(80), nullable=False)
    gym_or_location: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    load_basis: Mapped[str] = mapped_column(String(40), nullable=False, default="total")

    exercise: Mapped[Exercise] = relationship(back_populates="variants")
    sets: Mapped[list["WorkoutSet"]] = relationship(back_populates="exercise_variant")


class ExerciseAlias(TimestampMixin, Base):
    __tablename__ = "exercise_aliases"
    __table_args__ = (
        UniqueConstraint("user_id", "raw_alias", name="uq_exercise_alias_user_raw"),
        Index(
            "uq_exercise_alias_global_raw",
            "raw_alias",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
            sqlite_where=text("user_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    raw_alias: Mapped[str] = mapped_column(String(180), nullable=False)
    exercise_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False)
    suggested_equipment: Mapped[str | None] = mapped_column(String(80))

    exercise: Mapped[Exercise] = relationship(back_populates="aliases")


class WorkoutSet(TimestampMixin, Base):
    __tablename__ = "sets"
    __table_args__ = (
        UniqueConstraint(
            "raw_message_id",
            "exercise_variant_id",
            "set_number",
            name="uq_set_message_variant_number",
        ),
        CheckConstraint("set_number > 0", name="ck_set_number_positive"),
        CheckConstraint("weight_kg >= 0 AND weight_kg <= 1000", name="ck_set_weight_range"),
        CheckConstraint("reps > 0 AND reps <= 200", name="ck_set_reps_range"),
        CheckConstraint("rpe IS NULL OR (rpe >= 0 AND rpe <= 10)", name="ck_set_rpe_range"),
        CheckConstraint("rir IS NULL OR (rir >= 0 AND rir <= 20)", name="ck_set_rir_range"),
        CheckConstraint("parse_method IN ('rule','llm','manual')", name="ck_set_parse_method"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workout_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workouts.id", ondelete="CASCADE"), nullable=False)
    exercise_variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exercise_variants.id", ondelete="RESTRICT"), nullable=False
    )
    raw_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_messages.id", ondelete="SET NULL"))
    set_number: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    reps: Mapped[int] = mapped_column(Integer, nullable=False)
    rpe: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    rir: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    parse_method: Mapped[str] = mapped_column(String(20), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    llm_model: Mapped[str | None] = mapped_column(String(100))

    workout: Mapped[Workout] = relationship(back_populates="sets")
    exercise_variant: Mapped[ExerciseVariant] = relationship(back_populates="sets")
    raw_message: Mapped[RawMessage | None] = relationship(back_populates="sets")


class ParseReview(TimestampMixin, Base):
    __tablename__ = "parse_reviews"
    __table_args__ = (CheckConstraint("status IN ('pending','accepted','rejected')", name="ck_parse_review_status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    raw_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_messages.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    corrected_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ReviewStatus.PENDING.value)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw_message: Mapped[RawMessage] = relationship(back_populates="reviews")
