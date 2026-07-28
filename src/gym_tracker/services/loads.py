from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MIN_PLAUSIBLE_LOAD_KG = Decimal("0.1")
MAX_PLAUSIBLE_LOAD_KG = Decimal("500")


@dataclass(frozen=True)
class ParsedLoad:
    weight_kg: Decimal | None
    reasons: tuple[str, ...] = ()
    suggested_weight_kg: Decimal | None = None


def parse_load(number: str, unit: str) -> ParsedLoad:
    try:
        value = Decimal(number.replace(",", "."))
    except InvalidOperation:
        return ParsedLoad(None, ("carga invalida",))

    normalized_unit = unit.casefold()
    weight_kg = value / Decimal("1000") if normalized_unit == "g" else value
    reasons: list[str] = []
    suggestion = None

    if weight_kg <= 0:
        reasons.append("carga deve ser maior que zero")
    elif weight_kg > MAX_PLAUSIBLE_LOAD_KG:
        reasons.append(f"carga acima do limite plausivel de {MAX_PLAUSIBLE_LOAD_KG:g} kg")
    elif weight_kg < MIN_PLAUSIBLE_LOAD_KG:
        reasons.append(f"carga abaixo do limite plausivel de {MIN_PLAUSIBLE_LOAD_KG:g} kg")
        if normalized_unit == "g" and MIN_PLAUSIBLE_LOAD_KG <= value <= MAX_PLAUSIBLE_LOAD_KG:
            suggestion = value
            reasons.append(f"possivel erro de unidade; confirmar se era {value:g} kg")

    return ParsedLoad(weight_kg, tuple(reasons), suggestion)


def validate_materializable_load(weight_kg: Decimal) -> None:
    if not MIN_PLAUSIBLE_LOAD_KG <= weight_kg <= MAX_PLAUSIBLE_LOAD_KG:
        raise ValueError(
            f"carga fora do intervalo materializavel "
            f"({MIN_PLAUSIBLE_LOAD_KG:g}-{MAX_PLAUSIBLE_LOAD_KG:g} kg): {weight_kg:g}"
        )
