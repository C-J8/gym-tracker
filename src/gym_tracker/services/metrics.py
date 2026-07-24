import pandas as pd

GROUP_LABELS = {"Antebraco": "Antebraço", "Biceps": "Bíceps", "Triceps": "Tríceps", "Abdomen": "Abdômen"}
EXERCISE_LABELS = {
    "Antebraco dentro": "Antebraço dentro",
    "Antebraco puxada": "Antebraço puxada",
    "Biceps": "Bíceps",
    "Biceps antebraco": "Bíceps antebraço",
    "Biceps hack": "Bíceps hack",
    "Biceps zottman": "Bíceps zottman",
    "Triceps": "Tríceps",
    "Triceps unilateral": "Tríceps unilateral",
}
TYPE_LABELS = {"Maquina": "Máquina", "Maquina com anilha": "Máquina com anilha"}


def prepare_dashboard_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    df = data.copy()
    df["data"] = pd.to_datetime(df["data"])
    df["peso_kg"] = pd.to_numeric(df["peso_kg"], errors="coerce")
    df["serie"] = pd.to_numeric(df["serie"], errors="coerce").astype("Int64")
    df["repeticoes"] = pd.to_numeric(df["repeticoes"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["data", "peso_kg", "repeticoes"])
    df["grupo_muscular"] = df["grupo_muscular"].replace(GROUP_LABELS)
    df["exercicio"] = df["exercicio"].replace(EXERCISE_LABELS)
    df["tipo"] = df["tipo"].replace(TYPE_LABELS)
    df["volume"] = df["peso_kg"] * df["repeticoes"]
    df["estimativa_1rm"] = estimate_epley_1rm(df["peso_kg"], df["repeticoes"])
    df["semana"] = df["data"].dt.to_period("W-MON").dt.start_time
    df["mes"] = df["data"].dt.to_period("M").dt.to_timestamp()
    df["exercicio_tipo"] = df["exercicio"] + " - " + df["tipo"]
    return df


def estimate_epley_1rm(weight_kg, reps):
    return weight_kg * (1 + reps / 30)
