# PostgreSQL compartilhado com o Planner

## Uso diário nesta instalação

Planner e Academia usam **um servidor PostgreSQL 17**, no contêiner
`projeto-planejador-db-1`, porta `localhost:5434`. A definição oficial continua
em `E:\trabalhos\ToDoList\compose.yaml`, serviço `db`.

Abra Docker Desktop e use **Abrir Planner**, ou execute:

```powershell
docker compose -f "E:\trabalhos\ToDoList\compose.yaml" up -d db
```

O `.env` da Academia aponta para `gym_tracker` na porta 5434. O Planner continua
usando `planejador` na mesma porta. Cada aplicação mantém suas próprias tabelas
e migrations. O usuário da Academia é proprietário dos bancos da Academia,
mas não é superusuário do servidor compartilhado.

| Banco | Uso |
| --- | --- |
| `planejador` | Planner, atividades e acompanhamento |
| `gym_tracker` | Academia: dados oficiais e dashboard |
| `gym_tracker_stage2_csv` | Comparação histórica, preservada |
| `gym_tracker_stage2_txt` | Comparação histórica, preservada |

`postgres` também existe: é o banco administrativo padrão, não um projeto.
Os bancos de comparação não são fontes do dashboard.

O atalho **Abrir Planner**, com `GYM_TRACKER_PATH=../Acad` no `.env` do Planner,
inicia também o Streamlit e o importador. O menu **Academia** abre o dashboard
em nova aba. **Parar Planner** encerra os processos que o atalho iniciou e
aguarda importações em andamento terminarem. Um importador já iniciado
manualmente permanece ativo. As métricas ainda não são copiadas para o Planner.

## Testes e servidor antigo

- O PostgreSQL descartável do Planner continua isolado na porta 5435.
- Não configure `GYM_TRACKER_TEST_DATABASE_URL` para o servidor pessoal 5434:
  os testes da Academia apagam o schema do banco de testes.
- O antigo contêiner `acad-postgres-1` está parado. Seu volume
  `acad_gym_tracker_postgres` permanece intacto, incluindo `gym_tracker_test`.
- O serviço antigo só é iniciado explicitamente com o perfil `legacy`.
  Não o use no dia a dia: ele contém a cópia anterior à consolidação.
- Não execute `docker compose down -v` em nenhum projeto: remove volumes.
- Parar o servidor compartilhado interrompe temporariamente os dois aplicativos.

## Backups e conferência da consolidação

Os backups anteriores à mudança estão em:

`E:\trabalhos\Acad\local_backups\postgres-consolidation-20260929`

Incluem todos os bancos não-template dos dois servidores, os papéis de acesso,
checksums dos arquivos e contagens/hashes por tabela e sequência. A pasta contém
dados privados e hashes de credenciais; não publique nem envie ao GitHub.

`manifest.json` descreve os backups; `migration-verified.json` confirma a
comparação dos três bancos restaurados e a preservação dos dados do Planner.
Nenhum parser nem migration da aplicação foi executado nessa transferência.

O preparo automático do Planner e `npm.cmd run backup` agora salvam `planejador`
e `gym_tracker` em dumps separados em `ToDoList/backups/`, associados por manifesto.
O importador também salva `gym_tracker` em `Acad/local_backups/automatic` antes
de cada importação nova/reprocessamento. Se o backup falhar, não importa.
Os bancos históricos e papéis de acesso não entram nessas cópias rotineiras;
os dumps da consolidação permanecem preservados para essa recuperação completa.
Antes de migrations ou imports manuais, faça um backup extra. O guia principal
de operação está em `E:\trabalhos\ToDoList\docs\uso-local.md`.

## Recuperação

Se for necessário voltar à cópia anterior da Academia, primeiro pare importador
e Streamlit, faça backup do estado atual na 5434 e avalie alterações feitas após
a consolidação. A cópia antiga não recebe os novos treinos automaticamente.

Inicie o contêiner antigo com `docker start acad-postgres-1`, altere apenas a
porta da `DATABASE_URL` da Academia para 5432 e reinicie a aplicação. Isso volta
ao estado anterior, sem apagar o banco novo. Não use os dois como fontes ativas
ao mesmo tempo; reconciliar mudanças posteriores exige uma migração própria.

Em um computador novo, restaure os dumps e os proprietários antes de configurar
os aplicativos. Apenas criar o contêiner do Planner não cria os bancos da
Academia automaticamente.
