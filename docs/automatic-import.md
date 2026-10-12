# Referência técnica da importação automática

O guia principal do uso combinado está em `E:\trabalhos\ToDoList\docs\uso-local.md`.
No dia a dia, use **Abrir Planner**, atualize o TXT e use **Parar Planner**.
O início é manual; não há instalador de inicialização com o Windows.

## Preparação e configuração

Instale as dependências com `uv sync --locked --all-groups`. Confira `.env`,
migrations e usuário existente. O importador não aplica migrations, não faz seed
nem inicia Docker. O [servidor compartilhado](shared-postgres.md) usa a porta 5434.
Com zero ou vários usuários, configure `GYM_TRACKER_USER_ID`; ele não cria nem
escolhe usuários arbitrariamente. O dashboard usa o mesmo usuário configurado.

| Variável | Padrão/uso |
| --- | --- |
| `GYM_IMPORT_FILE` | TXT atual em `whatsapp_acad_export/` |
| `GYM_IMPORT_INTERVAL_SECONDS` | 60 segundos |
| `GYM_IMPORT_SETTLE_SECONDS` | 10 segundos |
| `GYM_TRACKER_USER_ID` | Único usuário existente, se não configurado |
| `PARSER_VERSION` | Versão atual do parser |
| `GYM_BACKUP_CONTAINER` | `projeto-planejador-db-1` nesta instalação |
| `GYM_BACKUP_DIRECTORY` | `local_backups/automatic` |

Após mudar `.env`, pare e inicie os serviços novamente. Não publique credenciais,
TXT, `.gym-tracker/` nem `local_backups/`.

## Pipeline e segurança

1. Lê o arquivo limitado a 20 MB e confirma tamanho, data e conteúdo estáveis.
2. Valida UTF-8 e cabeçalhos do WhatsApp. Entrada ausente/em mudança é aguardada;
   entrada vazia ou inválida é recusada.
3. Identifica o usuário e, no PostgreSQL, obtém trava transacional por usuário.
4. Verifica conteúdo e versão do parser. Se já processados, não cria backup extra
   nem duplica mensagens/séries.
5. Antes de importar/reprocessar, salva um backup consistente do banco. Se falhar,
   não modifica o banco. O próximo ciclo pode tentar novamente.
6. Importa o snapshot em memória em uma transação. Confirma tudo ou reverte tudo.
   Mensagens duvidosas ficam para revisão. Não chama IA mesmo com chave configurada.
7. O dashboard aberto verifica a revisão do banco a cada 60 segundos e atualiza
   os gráficos quando os dados mudam. Os filtros podem limitar a visualização.

O backup PostgreSQL é um `.dump` custom, sem proprietários/ACLs. O contêiner é
validado contra a porta local de `DATABASE_URL`, sem enviar senha na linha de
comando. Sem contêiner configurado, exige `pg_dump` instalado. Conexões remotas
ou parâmetros extras são recusados. SQLite, usado nos testes, utiliza a API de
backup online, incluindo páginas confirmadas em WAL.

Os arquivos recebem a extensão final apenas após sucesso. `.partial` não é
restaurável. Cópias anteriores não são removidas. Não há retenção automática:
acompanhe espaço livre e guarde cópias importantes em outro disco.

Uma mudança de parser reanalisa o TXT existente uma vez. Resultados anteriores
aceitos seguem as regras existentes de preservação/ativação. A automação não
elimina a necessidade de revisão humana nem protege imports/migrations manuais:
antes deles, execute `python -B -m gym_tracker.backup`.

## Comandos de manutenção

Os três CMDs em `scripts/maintenance/` permitem iniciar, conferir e parar somente
o importador, independentemente do Planner. Também é possível executar na raiz:

```powershell
.venv\Scripts\python.exe -B -m gym_tracker.automation start
.venv\Scripts\python.exe -B -m gym_tracker.automation status
.venv\Scripts\python.exe -B -m gym_tracker.automation stop
# Visível até Ctrl+C, ou um único ciclo:
.venv\Scripts\python.exe -B -m gym_tracker.automation watch
.venv\Scripts\python.exe -B -m gym_tracker.automation once
# Backup extra (não importa nada):
.venv\Scripts\python.exe -B -m gym_tracker.backup
```

## Estado e controle dos processos

`automation.py` usa trava do sistema operacional, liberada após queda. Não apague
o arquivo de trava nem encerre processos Python desconhecidos.
`status.json` guarda `running`, último resultado, contagens e horários.
`events.log` tem rotação limitada; não contém conversa, credenciais ou respostas de IA.
`last_cycle_phase`/`last_cycle_message` preservam o último resultado ao parar.

`desktop.py` supervisiona o Streamlit e apenas o importador que iniciou. O controle
é autenticado por token local e identificação da pasta. Não adota um Streamlit
manual ocupando a porta. Um importador manual é reutilizado e preservado ao parar.
O status consulta a trava real do importador: dashboard ativo não significa
importação ativa. Reabrir recupera um importador parado; não tenta esconder erros
de entrada/backup. A parada aguarda a transação do importador próprio terminar.

## Status no Planner

O menu Status do Planner consulta `/overview` no controle local autenticado do
supervisor. Esse endpoint exporta somente estado do importador, horários,
disponibilidade do banco, contagem de revisões pendentes do usuário configurado
e data do backup automático mais recente. Não exporta nomes, conversas,
exercícios, cargas ou indicadores de treino.

A consulta usa uma conexão própria somente leitura, com limites de conexão e
consulta; não interfere na transação do importador. A contagem do banco tem cache
de dez segundos. Os controles de abrir/parar continuam sem consultar o banco,
para que uma indisponibilidade não impeça o encerramento dos serviços.

O arquivo de descoberta `desktop.json` não é regravado ao ficar pronto:
o estado atual vem do controle autenticado, evitando disputa de leitura no
Windows. Falhas do supervisor registram somente horário, classe e código do erro
em `desktop-error.json`; o texto bruto da exceção não é registrado.

## Verificação reproduzível

```powershell
.venv\Scripts\python.exe -B -m pytest
.venv\Scripts\python.exe -B scripts/test-planner-integration.py
```

O teste integrado exige Planner parado, usa exclusivamente o banco descartável
`planner_test` na porta 5435 e um SQLite sintético. Verifica falhas de inicialização
e parada, reuso, recuperação, menu opcional, TXT e backup. Nunca reprocessa o TXT
pessoal. Testes PostgreSQL do Gym exigem um servidor descartável e banco `_test`.

Para revisão humana, consulte [Operação](operations.md). O acesso à Academia já
funciona pelo menu do Planner; sincronizar métricas nas tabelas do Planner por uma
API somente leitura permanece uma etapa futura. Conversas não serão copiadas.
