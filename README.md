# Gym Tracker

Dashboard Streamlit e pipeline rastreável para importar treinos registrados no WhatsApp. O PostgreSQL é a fonte oficial; o CSV existente é usado apenas para catálogo e comparação, nunca como fonte do dashboard.

## Atualizar apenas o TXT

Com a Academia configurada no Planner, use **Abrir Planner**: ele inicia o
dashboard e o monitor do TXT. Clique em **Academia** no menu para os gráficos.
**Parar Planner** encerra os processos iniciados pelo atalho. O início continua
manual; nada é instalado na Inicialização do Windows.

Nesta instalação, Academia e Planner usam o mesmo servidor PostgreSQL na porta
**5434**, com bancos separados. Abra **Abrir Planner** para iniciar o servidor,
ou siga o [guia do servidor compartilhado](docs/shared-postgres.md).

Depois de abrir o Planner, substitua
`whatsapp_acad_export/Conversa do WhatsApp com Acad.txt` quando exportar novos treinos.
O monitor importa em segundo plano, evita duplicatas e mantém dúvidas para revisão,
sem Airflow ou chamadas pagas de IA. Antes de cada importação nova ou mudança
do parser, salva um backup; se o backup falhar, não altera o banco.

O guia principal do uso combinado está em `E:\trabalhos\ToDoList\docs\uso-local.md`.
Para operar somente o importador, os três CMDs estão em `scripts/maintenance/`,
fora da rotina diária. Veja [detalhes técnicos da importação](docs/automatic-import.md).

## Início rápido

Pré-requisitos: Python 3.12, [uv](https://docs.astral.sh/uv/) e Docker com Compose.

Os comandos abaixo são para uma **instalação independente nova**. Na instalação
compartilhada existente, não crie outro banco/usuário: siga o guia acima.
Para a instalação independente, configure a porta **5432** em `DATABASE_URL`
no `.env`; o exemplo está preparado para a instalação compartilhada na 5434.

```bash
git clone https://github.com/C-J8/gym-tracker.git
cd gym-tracker
uv sync --locked --all-groups
```

Antes de subir o banco ou aplicar migrations, crie `.env` a partir de
`.env.example`, configure a porta 5432 e ajuste `GYM_BACKUP_CONTAINER` para o
nome real do contêiner do serviço `postgres` (confira `docker ps`). Não substitua
um `.env` existente. Depois crie um usuário e importe uma fonte:

```bash
docker compose --profile legacy up -d postgres
uv run alembic upgrade head
uv run python -m gym_tracker.cli create-user --name "Meu nome"
uv run python -m gym_tracker.cli import-whatsapp --file caminho/export.txt --user UUID_DO_USUARIO
uv run streamlit run app.py --server.address=127.0.0.1
```

O dashboard abre em `http://localhost:8501`. Com mais de um usuário no banco, defina `GYM_TRACKER_USER_ID`.

## Comandos

```bash
uv run alembic upgrade head
uv run python -m gym_tracker.cli db-upgrade
uv run python -m gym_tracker.cli import-whatsapp --file export.txt --user UUID
uv run python -m gym_tracker.cli import-whatsapp --file export.txt --user UUID --parser-version 3.1.0
uv run python -m gym_tracker.cli bootstrap-catalog --file academia_treinos_whatsapp.csv --user UUID
uv run python -m gym_tracker.cli bootstrap-catalog --file academia_treinos_whatsapp.csv --user UUID --apply
uv run python -m gym_tracker.cli quality-report --import-id UUID
uv run python -m gym_tracker.cli evaluate-parser --file export.txt
uv run python -m gym_tracker.cli list-reviews --status pending
uv run python -m gym_tracker.cli show-review --review-id UUID --json
uv run python -m gym_tracker.cli accept-review --review-id UUID --payload correcao.json --save-alias "sup reto halter"
uv run python -m gym_tracker.cli reject-review --review-id UUID
```

`bootstrap-catalog` executa dry-run por padrão. Somente `--apply` grava associações
unívocas de exercício, alias e variante. O comando nunca cria treinos, séries,
mensagens, imports ou parse runs, e o CSV não alimenta o dashboard PostgreSQL.
Conflitos de equipamento, grupo, unidade ou `load_basis` permanecem no relatório
para revisão.

Para reprocessar o TXT atual, comparar com o CSV e preservar ambos:

```bash
uv run python -m gym_tracker.cli compare-legacy \
  --whatsapp-file "whatsapp_acad_export/Conversa do WhatsApp com Acad.txt" \
  --legacy-csv academia_treinos_whatsapp.csv \
  --output-dir quality_reports
```

## Fonte do dashboard

O dashboard usa exclusivamente resultados aceitos e ativos no PostgreSQL. O
backend CSV foi desativado para evitar que o legado se torne uma segunda fonte
de séries. O arquivo permanece disponível somente para bootstrap do catálogo e
comparação de qualidade.

## Qualidade

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run alembic check
```

Com os dois projetos instalados e o Planner parado, o teste de acesso integrado
usa apenas `planner_test` na porta 5435 e um SQLite com treinos sintéticos:

```powershell
.venv\Scripts\python.exe -B scripts/test-planner-integration.py
```

Ele verifica falhas de abertura/parada, reuso, recuperação do importador,
menu opcional e backup antes da importação. Não importa o TXT pessoal.

O CI executa os mesmos passos e também os testes de integração com PostgreSQL. Testes usam fixtures anonimizadas e nunca chamam serviços externos.

## Documentação

- [Arquitetura](docs/architecture.md)
- [Contrato de dados](docs/data-contract.md)
- [Operação, parsing e revisão](docs/operations.md)
- [Importação automática local](docs/automatic-import.md)
- [Privacidade](docs/privacy.md)

Exports sobrepostos reutilizam mensagens canônicas e registram suas ocorrências em cada arquivo. Reprocessamentos são separados por `PARSER_VERSION`, e somente o resultado aceito ativo alimenta o dashboard. Veja a documentação para identidade, equipamento, unidades, cache e shadow mode.

Limitações atuais: não há autenticação no Streamlit e a revisão humana ocorre por CLI/JSON. O redesign visual está deliberadamente fora desta etapa.
