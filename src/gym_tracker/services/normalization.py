import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExerciseMatch:
    canonical_name: str
    muscle_group: str
    equipment: str
    load_basis: str = "total"


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(character for character in value if unicodedata.category(character) != "Mn")
    return re.sub(r"\s+", " ", value).strip(" -:")


CATALOG_RULES: tuple[tuple[tuple[str, ...], ExerciseMatch], ...] = (
    (("crucifixo costas",), ExerciseMatch("Crucifixo costas", "Costas", "Máquina")),
    (("supino inclinado",), ExerciseMatch("Supino inclinado", "Peito", "Máquina")),
    (("supino reto", "supino halter"), ExerciseMatch("Supino reto", "Peito", "Máquina")),
    (("parece triceps", "triceps2_peito"), ExerciseMatch("Press peito/tríceps", "Peito", "Máquina")),
    (("fechar peito",), ExerciseMatch("Crucifixo", "Peito", "Máquina")),
    (("crucifixo",), ExerciseMatch("Crucifixo", "Peito", "Máquina")),
    (("puxada alta",), ExerciseMatch("Puxada alta", "Costas", "Máquina")),
    (("puxada baixa",), ExerciseMatch("Puxada baixa", "Costas", "Máquina")),
    (("puxada lateral",), ExerciseMatch("Puxada lateral", "Costas", "Máquina")),
    (("maquina serrote", "serrote"), ExerciseMatch("Serrote", "Costas", "Máquina")),
    (("triceps unilateral", "triceps uni"), ExerciseMatch("Tríceps unilateral", "Tríceps", "Cabo")),
    (("triceps corda lado",), ExerciseMatch("Tríceps lateral", "Tríceps", "Cabo")),
    (("triceps corda", "triceps barra", "triceps"), ExerciseMatch("Tríceps", "Tríceps", "Cabo")),
    (
        ("biceps antb", "biceps antebraco", "biceps que pega antbraco"),
        ExerciseMatch("Bíceps antebraço", "Bíceps", "Halter", "por_halter"),
    ),
    (("biceps zootman", "biceps zottman"), ExerciseMatch("Bíceps zottman", "Bíceps", "Halter", "por_halter")),
    (("biceps hack",), ExerciseMatch("Bíceps hack", "Bíceps", "Halter", "por_halter")),
    (("biceps corda",), ExerciseMatch("Bíceps", "Bíceps", "Cabo")),
    (("biceps maquina", "biceps barra maquina", "biceps"), ExerciseMatch("Bíceps", "Bíceps", "Máquina")),
    (("antbdentro", "antebraco dentro"), ExerciseMatch("Antebraço dentro", "Antebraço", "Halter", "por_halter")),
    (("antebraco puxada", "antbraco puxada"), ExerciseMatch("Antebraço puxada", "Antebraço", "Cabo")),
    (("extensora",), ExerciseMatch("Extensora", "Pernas", "Máquina")),
    (("flexora",), ExerciseMatch("Flexora", "Pernas", "Máquina")),
    (("abdutora",), ExerciseMatch("Abdutora", "Pernas", "Máquina")),
    (("adutora",), ExerciseMatch("Adutora", "Pernas", "Máquina")),
    (("desenvolvimento",), ExerciseMatch("Desenvolvimento", "Ombro", "Máquina")),
    (("lateral",), ExerciseMatch("Lateral", "Ombro", "Máquina")),
    (("abdominal", "abd maquina", "abdmaq", "abd", "maquina"), ExerciseMatch("Abdominal", "Abdômen", "Máquina")),
    (("barra",), ExerciseMatch("Barra", "Costas", "Máquina")),
)


def resolve_static_exercise(raw_name: str, workout_type: str | None = None) -> ExerciseMatch | None:
    key = normalize_text(raw_name)
    context = normalize_text(workout_type or "")
    if key == "maquina" and context != "abd":
        return None

    for aliases, match in CATALOG_RULES:
        if any(alias in key for alias in aliases):
            equipment = infer_equipment(key, match.equipment)
            load_basis = "por_halter" if equipment == "Halter" else match.load_basis
            return ExerciseMatch(match.canonical_name, match.muscle_group, equipment, load_basis)
    return None


def infer_equipment(key: str, default: str) -> str:
    if any(token in key for token in ("halter", "zootman", "zottman", "hack")):
        return "Halter"
    if any(token in key for token in ("corda", "triceps barra", "triceps uni", "puxada cabo")):
        return "Cabo"
    if any(token in key for token in ("maquina", "maq")):
        return "Máquina"
    return default


def adjust_equipment_for_load(match: ExerciseMatch, raw_name: str, loads: list[Decimal]) -> ExerciseMatch:
    key = normalize_text(raw_name)
    explicit = any(token in key for token in ("halter", "maquina", "maq"))
    equipment = match.equipment

    if match.canonical_name in {"Supino reto", "Supino inclinado"} and not explicit and loads:
        equipment = "Máquina" if max(loads) > Decimal("24") else "Halter"

    if match.muscle_group == "Peito" and equipment == "Máquina" and loads and max(loads) <= Decimal("40"):
        equipment = "Máquina com anilha"

    if match.canonical_name == "Lateral" and not explicit and loads and max(loads) <= Decimal("10"):
        equipment = "Halter"

    load_basis = "por_halter" if equipment == "Halter" else "total"
    return ExerciseMatch(match.canonical_name, match.muscle_group, equipment, load_basis)
