"""Compatibilidade com o comando antigo, sem sobrescrever o CSV legado."""

from pathlib import Path

from gym_tracker.services.legacy_migration import compare_legacy_sources


def main() -> None:
    report = compare_legacy_sources(
        Path("whatsapp_acad_export") / "Conversa do WhatsApp com Acad.txt",
        Path("academia_treinos_whatsapp.csv"),
        Path("quality_reports"),
    )
    print("O CSV atual foi preservado. Resultado do reprocessamento:")
    print(report)


if __name__ == "__main__":
    main()
