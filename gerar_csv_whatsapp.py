import csv
import re
import unicodedata
from pathlib import Path


INPUT = Path("whatsapp_acad_export") / "Conversa do WhatsApp com Acad.txt"
OUTPUT = Path("academia_treinos_whatsapp.csv")
REVIEW = Path("academia_linhas_revisar.csv")


def normalize(text):
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return text


def classify_exercise(name, workout_title=""):
    key = normalize(f"{workout_title} {name}")

    if "crucifixo costas" in key:
        group = "Costas"
    elif any(word in key for word in ["extensora", "flexora", "abdutora", "adutora"]):
        group = "Pernas"
    elif any(word in key for word in ["supino", "crucifixo", "fechar peito", "pega peito", "peito"]):
        group = "Peito"
    elif "triceps" in key:
        group = "Triceps"
    elif "antebraco" in key or "antbraco" in key or "antb" in key:
        group = "Antebraco"
    elif "biceps" in key or "zootman" in key:
        group = "Biceps"
    elif any(word in key for word in ["puxada", "serrote", "barra", "crucifixo costas"]):
        group = "Costas"
    elif any(word in key for word in ["lateral", "desenvolvimento"]):
        group = "Ombro"
    elif any(word in key for word in ["abd", "maquina"]) and "lower" not in key and "upper" not in key:
        group = "Abdomen"
    else:
        group = "Outro"

    if any(word in key for word in ["halter", "zootman", "zottman", "hack"]):
        kind = "Halter"
    elif any(word in key for word in ["corda", "triceps barra", "triceps uni", "triceps unilateral", "antebraco puxada", "antbraco puxada"]):
        kind = "Cabo"
    elif any(word in key for word in ["maquina", "extensora", "flexora", "abdutora", "adutora", "serrote", "lateral", "crucifixo", "puxada"]):
        kind = "Maquina"
    else:
        kind = "Maquina"

    clean = re.sub(r"\([^)]*\)", "", name)
    clean = re.sub(r"\s+", " ", clean).strip(" -:")
    clean_key = normalize(clean)

    if "supino reto" in clean_key or clean_key == "supino halter":
        clean = "Supino reto"
    elif "supino inclinado" in clean_key:
        clean = "Supino inclinado"
    elif "crucifixo costas" in clean_key:
        clean = "Crucifixo costas"
    elif "crucifixo" in clean_key or "fechar peito" in clean_key:
        clean = "Crucifixo"
    elif "parece triceps" in clean_key or "triceps2_peito" in clean_key:
        clean = "Press peito/triceps"
    elif "puxada alta" in clean_key:
        clean = "Puxada alta"
    elif "puxada baixa" in clean_key:
        clean = "Puxada baixa"
    elif "puxada lateral" in clean_key:
        clean = "Puxada lateral"
    elif "serrote" in clean_key:
        clean = "Serrote"
    elif "triceps unilateral" in clean_key or "triceps uni" in clean_key:
        clean = "Triceps unilateral"
    elif "triceps" in clean_key:
        clean = "Triceps"
    elif "biceps" in clean_key and "ant" in clean_key:
        clean = "Biceps antebraco"
    elif "biceps" in clean_key and ("zootman" in clean_key or "zottman" in clean_key):
        clean = "Biceps zottman"
    elif "biceps" in clean_key and "hack" in clean_key:
        clean = "Biceps hack"
    elif "biceps" in clean_key:
        clean = "Biceps"
    elif "extensora" in clean_key:
        clean = "Extensora"
    elif "flexora" in clean_key:
        clean = "Flexora"
    elif "abdutora" in clean_key:
        clean = "Abdutora"
    elif "adutora" in clean_key:
        clean = "Adutora"
    elif "antbdentro" in clean_key:
        clean = "Antebraco dentro"
    elif "antebraco puxada" in clean_key or "antbraco puxada" in clean_key:
        clean = "Antebraco puxada"
    elif "abd" in clean_key or clean_key == "maquina":
        clean = "Abdominal"
    elif "desenvolvimento" in clean_key:
        clean = "Desenvolvimento"
    elif "lateral" in clean_key:
        clean = "Lateral"

    return group, clean, kind


def split_messages(text):
    pattern = re.compile(r"(?m)^(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\s+-\s+(.*)")
    matches = list(pattern.finditer(text))
    messages = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        head = match.group(3)
        body = text[start:end].strip("\n")
        if ": " in head:
            sender, first_line = head.split(": ", 1)
        else:
            sender, first_line = "", head
        content = first_line
        if body.strip():
            content += "\n" + body
        messages.append({
            "date_br": match.group(1),
            "time": match.group(2),
            "sender": sender.strip("\u200e "),
            "content": content.strip(),
        })
    return messages


def iso_date(date_br):
    day, month, year = date_br.split("/")
    return f"{year}-{month}-{day}"


def prepare_lines(content):
    content = content.replace("<Mensagem editada>", "")
    content = content.replace("<Mídia oculta>", "")
    raw_lines = [line.strip() for line in content.splitlines()]
    lines = []
    workout_title = ""

    for raw in raw_lines:
        if not raw:
            continue
        if raw in {"ACAD NOVA", "Upper", "Lower A", "Lower B", "Abd"}:
            workout_title = raw
            continue
        if "mensagem apagada" in normalize(raw):
            continue

        if lines and (raw.startswith(".") or not re.search(r"\d+(?:[.,]\d+)?\s*k?g", normalize(raw))):
            lines[-1] = f"{lines[-1]} {raw}"
        else:
            lines.append(raw)

    return workout_title, lines


def expand_rep_token(token):
    original = normalize(token).replace(",", ".")
    token = original
    token = token.replace("rep", "")
    token = token.strip(" .-/")
    if not token:
        return []

    multiplier = re.search(r"(\d+)\s*x", token)
    shorthand_multiplier = None
    shorthand = re.search(r"(\d+)\s*/\s*(\d+)\s*$", token)
    if shorthand and int(shorthand.group(2)) <= 5:
        if original.count("rep") <= 1:
            shorthand_multiplier = int(shorthand.group(2))
            token = shorthand.group(1)

    token = re.sub(r"\d+\s*x", "", token)
    reps = [int(value) for value in re.findall(r"\d+", token)]
    if multiplier and len(reps) == 1:
        reps = reps * int(multiplier.group(1))
    if shorthand_multiplier and len(reps) == 1:
        reps = reps * shorthand_multiplier
    return reps


def parse_line(line):
    if "n fiz" in normalize(line):
        return []

    cleaned = line.replace(",", ".")
    cleaned = re.sub(r"\b(\d+(?:\.\d+)?)\s*g\b", r"\1kg", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned)

    load_matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*kg", cleaned, flags=re.I))
    if not load_matches:
        return []

    sets = []
    for index, match in enumerate(load_matches):
        load = float(match.group(1))
        start = match.end()
        end = load_matches[index + 1].start() if index + 1 < len(load_matches) else len(cleaned)
        token = cleaned[start:end]
        reps = expand_rep_token(token)

        if not reps and index + 1 == len(load_matches):
            before = cleaned[:match.start()]
            reps = expand_rep_token(before.split("...")[-1])

        for rep in reps:
            if 1 <= rep <= 30:
                sets.append((load, rep))

    return sets


def exercise_name(line):
    before = re.split(r"\d+(?:[.,]\d+)?\s*k?g", line, maxsplit=1, flags=re.I)[0]
    before = before.replace("-", " ")
    before = re.sub(r"\s+", " ", before).strip()
    return before or "Exercicio sem nome"


def adjust_kind_by_load(name, exercise, kind, sets):
    key = normalize(name)
    explicit_type = any(word in key for word in ["halter", "maquina", "maq", "máquina"])
    is_supino = exercise in {"Supino reto", "Supino inclinado"}
    if not is_supino or explicit_type or not sets:
        return kind

    max_load = max(load for load, _ in sets)
    return "Maquina" if max_load > 24 else "Halter"


def adjust_kind_for_set(group, kind, load):
    if group == "Peito" and kind == "Maquina" and load <= 40:
        return "Maquina com anilha"
    return kind


def main():
    text = INPUT.read_text(encoding="utf-8")
    rows = []
    review = []

    for message in split_messages(text):
        workout_title, lines = prepare_lines(message["content"])
        for line in lines:
            if "kg" not in normalize(line):
                continue
            if "n fiz" in normalize(line):
                continue
            sets = parse_line(line)
            name = exercise_name(line)
            group, exercise, kind = classify_exercise(name, workout_title)
            if not sets:
                review.append({
                    "data": iso_date(message["date_br"]),
                    "remetente": message["sender"],
                    "linha": line,
                    "motivo": "sem series interpretadas",
                })
                continue

            kind = adjust_kind_by_load(name, exercise, kind, sets)

            for series_number, (load, reps) in enumerate(sets, start=1):
                row_kind = adjust_kind_for_set(group, kind, load)
                rows.append({
                    "data": iso_date(message["date_br"]),
                    "grupo_muscular": group,
                    "exercicio": exercise,
                    "tipo": row_kind,
                    "peso_kg": int(load) if load.is_integer() else load,
                    "serie": series_number,
                    "repeticoes": reps,
                })

    with OUTPUT.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["data", "grupo_muscular", "exercicio", "tipo", "peso_kg", "serie", "repeticoes"])
        writer.writeheader()
        writer.writerows(rows)

    with REVIEW.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["data", "remetente", "linha", "motivo"])
        writer.writeheader()
        writer.writerows(review)

    print(f"{OUTPUT}: {len(rows)} linhas")
    print(f"{REVIEW}: {len(review)} linhas para revisar")


if __name__ == "__main__":
    main()
