from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight_kg: Decimal = Field(ge=0, le=10000)
    reps: int = Field(ge=1, le=200)
    rpe: Decimal | None = Field(default=None, ge=0, le=10)
    rir: Decimal | None = Field(default=None, ge=0, le=20)


class ExercisePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_name: str = Field(min_length=1, max_length=180)
    canonical_name: str | None = Field(default=None, max_length=160)
    muscle_group: str | None = Field(default=None, max_length=80)
    equipment: str | None = Field(default=None, max_length=80)
    load_basis: str = Field(default="total", max_length=40)
    sets: list[SetPayload] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    needs_review: bool = False


class WorkoutExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workout_date: date
    workout_type: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    exercises: list[ExercisePayload] = Field(default_factory=list)
    unconsumed_text: list[str] = Field(default_factory=list)
    needs_review: bool = False

    @field_validator("exercises")
    @classmethod
    def require_sets_for_exercises(cls, value: list[ExercisePayload]) -> list[ExercisePayload]:
        if any(not exercise.sets for exercise in value):
            raise ValueError("todo exercicio extraido precisa ter ao menos uma serie")
        return value


class WhatsAppMessage(BaseModel):
    source_index: int = Field(ge=0)
    source_offset: int = Field(default=0, ge=0)
    sent_at: datetime
    timestamp_precision: str = Field(default="minute", pattern="^(minute|second)$")
    sender_raw: str
    raw_content: str
    content_sha256: str = Field(min_length=64, max_length=64)
    is_edited: bool = False
    is_deleted: bool = False
    is_media: bool = False


class ParseOutcome(BaseModel):
    extraction: WorkoutExtraction | None = None
    reasons: list[str] = Field(default_factory=list)
    consumed_lines: list[str] = Field(default_factory=list)
    resolution_evidence: list[dict[str, str | int | None]] = Field(default_factory=list)
    parse_method: str = "rule"

    @property
    def needs_review(self) -> bool:
        return bool(self.reasons or (self.extraction and self.extraction.needs_review))


class QualityReport(BaseModel):
    import_id: str | None = None
    parse_run_id: str | None = None
    duplicate_import: bool = False
    reprocessed: bool = False
    messages_total: int = 0
    messages_new: int = 0
    messages_reused: int = 0
    conflicting_messages: int = 0
    messages_accepted: int = 0
    messages_skipped: int = 0
    messages_pending_review: int = 0
    duplicate_messages: int = 0
    sets_accepted: int = 0
    normalized_duplicates: int = 0
    llm_proposals: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)


class CatalogReviewItem(BaseModel):
    normalized_key: str
    status: str
    reasons: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CatalogBootstrapReport(BaseModel):
    dry_run: bool
    applied: bool
    rows_read: int = 0
    exact_duplicates: int = 0
    unique_rows: int = 0
    normalized_exercises: int = 0
    applicable_associations: int = 0
    ambiguities: int = 0
    not_found: int = 0
    records_created: dict[str, int] = Field(default_factory=dict)
    records_updated: dict[str, int] = Field(default_factory=dict)
    records_reused: dict[str, int] = Field(default_factory=dict)
    records_planned: dict[str, int] = Field(default_factory=dict)
    records_ignored: int = 0
    review_items: list[CatalogReviewItem] = Field(default_factory=list)
