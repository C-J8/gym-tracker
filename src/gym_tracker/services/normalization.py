import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ExerciseMatch:
    canonical_name: str
    muscle_group: str
    equipment: str | None = None
    load_basis: str = "total"


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(character for character in value if unicodedata.category(character) != "Mn")
    return re.sub(r"\s+", " ", value).strip(" -:")


def normalize_message_content(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(lines).strip()


def normalize_equipment(value: str | None) -> str:
    key = normalize_text(value or "")
    aliases = {
        "maquina": "maquina",
        "maquina com anilha": "maquina com anilha",
        "halter": "halter",
        "cabo": "cabo",
        "corda": "cabo",
        "barra livre": "barra livre",
        "peso corporal": "peso corporal",
    }
    return aliases.get(key, key)


CATALOG_RULES: tuple[tuple[tuple[str, ...], ExerciseMatch], ...] = (
    (("crucifixo costas",), ExerciseMatch("Crucifixo costas", "Costas")),
    (("supino inclinado",), ExerciseMatch("Supino inclinado", "Peito")),
    (("supino reto", "supino halter"), ExerciseMatch("Supino reto", "Peito")),
    (("parece triceps", "triceps2_peito"), ExerciseMatch("Press peito/tríceps", "Peito")),
    (("fechar peito",), ExerciseMatch("Crucifixo", "Peito")),
    (("crucifixo",), ExerciseMatch("Crucifixo", "Peito")),
    (("puxada alta",), ExerciseMatch("Puxada alta", "Costas")),
    (("puxada baixa",), ExerciseMatch("Puxada baixa", "Costas")),
    (("puxada lateral",), ExerciseMatch("Puxada lateral", "Costas")),
    (("maquina serrote", "serrote"), ExerciseMatch("Serrote", "Costas")),
    (("triceps unilateral", "triceps uni"), ExerciseMatch("Tríceps unilateral", "Tríceps")),
    (("triceps corda lado",), ExerciseMatch("Tríceps lateral", "Tríceps", "Cabo")),
    (("triceps corda", "triceps barra", "triceps"), ExerciseMatch("Tríceps", "Tríceps")),
    (
        ("biceps antb", "biceps antebraco", "biceps que pega antbraco"),
        ExerciseMatch("Bíceps antebraço", "Bíceps"),
    ),
    (("biceps zootman", "biceps zottman"), ExerciseMatch("Bíceps zottman", "Bíceps", "Halter", "por_halter")),
    (("biceps hack",), ExerciseMatch("Bíceps hack", "Bíceps", "Halter", "por_halter")),
    (("biceps corda",), ExerciseMatch("Bíceps", "Bíceps", "Cabo")),
    (("biceps maquina", "biceps barra maquina", "biceps"), ExerciseMatch("Bíceps", "Bíceps")),
    (("antbdentro", "antebraco dentro"), ExerciseMatch("Antebraço dentro", "Antebraço")),
    (("antebraco puxada", "antbraco puxada"), ExerciseMatch("Antebraço puxada", "Antebraço", "Cabo")),
    (("extensora",), ExerciseMatch("Extensora", "Pernas", "Máquina")),
    (("flexora",), ExerciseMatch("Flexora", "Pernas", "Máquina")),
    (("abdutora",), ExerciseMatch("Abdutora", "Pernas", "Máquina")),
    (("adutora",), ExerciseMatch("Adutora", "Pernas", "Máquina")),
    (("desenvolvimento",), ExerciseMatch("Desenvolvimento", "Ombro")),
    (("lateral",), ExerciseMatch("Lateral", "Ombro")),
    (("abdominal", "abd maquina", "abdmaq", "abd", "maquina"), ExerciseMatch("Abdominal", "Abdômen")),
    (("barra",), ExerciseMatch("Barra", "Costas")),
)


def explicit_equipment(raw_name: str, context: str | None = None) -> str | None:
    key = normalize_text(f"{context or ''} {raw_name}")
    if re.search(r"\bhalter(?:es)?\b", key):
        return "Halter"
    if re.search(r"\b(?:corda|cabo)\b", key):
        return "Cabo"
    if re.search(r"\b(?:maquina|maq)\b", key):
        return "Máquina"
    return None


def resolve_static_exercise(raw_name: str, workout_type: str | None = None) -> ExerciseMatch | None:
    key = normalize_text(raw_name)
    context = normalize_text(workout_type or "")
    if key == "maquina" and context != "abd":
        return None

    for aliases, catalog_match in CATALOG_RULES:
        if any(alias in key for alias in aliases):
            equipment = explicit_equipment(raw_name, workout_type) or catalog_match.equipment
            load_basis = "por_halter" if equipment == "Halter" else catalog_match.load_basis
            return ExerciseMatch(
                catalog_match.canonical_name,
                catalog_match.muscle_group,
                equipment,
                load_basis,
            )
    return None
