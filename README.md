# Gym Tracker

Dashboard Streamlit e pipeline rastreável para importar treinos registrados no WhatsApp. O PostgreSQL é a fonte oficial; o CSV existente continua disponível como modo legado/demonstração.

## Início rápido

Pré-requisitos: Python 3.12, [uv](https://docs.astral.sh/uv/) e Docker com Compose.

```bash
git clone https://github.com/C-J8/gym-tracker.git
cd gym-tracker
uv sync --locked --all-groups
docker compose up -d postgres
uv run alembic upgrade head
```

Crie o arquivo `.env` a partir de `.env.example`. Depois crie um usuário e importe uma fonte:

```bash
uv run python -m gym_tracker.cli create-user --name "Meu nome"
uv run python -m gym_tracker.cli import-whatsapp --file caminho/export.txt --user UUID_DO_USUARIO
uv run streamlit run app.py
```

O dashboard abre em `http://localhost:8501`. Com mais de um usuário no banco, defina `GYM_TRACKER_USER_ID`.

## Comandos

```bash
uv run alembic upgrade head
uv run python -m gym_tracker.cli db-upgrade
uv run python -m gym_tracker.cli import-whatsapp --file export.txt --user UUID
uv run python -m gym_tracker.cli import-whatsapp --file export.txt --user UUID --parser-version 3.1.0
uv run python -m gym_tracker.cli import-legacy-csv --file academia_treinos_whatsapp.csv --user UUID
uv run python -m gym_tracker.cli quality-report --import-id UUID
uv run python -m gym_tracker.cli evaluate-parser --file export.txt
uv run python -m gym_tracker.cli list-reviews --status pending
uv run python -m gym_tracker.cli show-review --review-id UUID --json
uv run python -m gym_tracker.cli accept-review --review-id UUID --payload correcao.json --save-alias "sup reto halter"
uv run python -m gym_tracker.cli reject-review --review-id UUID
```

Para reprocessar o TXT atual, comparar com o CSV e preservar ambos:

```bash
uv run python -m gym_tracker.cli compare-legacy \
  --whatsapp-file "whatsapp_acad_export/Conversa do WhatsApp com Acad.txt" \
  --legacy-csv academia_treinos_whatsapp.csv \
  --output-dir quality_reports
```

## Modo CSV

O backend padrão é PostgreSQL. Para abrir o dashboard sem banco:

```powershell
$env:DATA_BACKEND="csv"
uv run streamlit run app.py
```

Esse modo é explícito e não participa do pipeline oficial.

## Qualidade

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run alembic check
```

O CI executa os mesmos passos e também os testes de integração com PostgreSQL. Testes usam fixtures anonimizadas e nunca chamam serviços externos.

## Documentação

- [Arquitetura](docs/architecture.md)
- [Contrato de dados](docs/data-contract.md)
- [Operação, parsing e revisão](docs/operations.md)
- [Privacidade](docs/privacy.md)

Exports sobrepostos reutilizam mensagens canônicas e registram suas ocorrências em cada arquivo. Reprocessamentos são separados por `PARSER_VERSION`, e somente o resultado aceito ativo alimenta o dashboard. Veja a documentação para identidade, equipamento, unidades, cache e shadow mode.

Limitações atuais: não há autenticação no Streamlit e a revisão humana ocorre por CLI/JSON. O redesign visual está deliberadamente fora desta etapa.
