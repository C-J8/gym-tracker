# Operação, parsing e revisão

## Variáveis

- `DATABASE_URL`: conexão SQLAlchemy, por padrão o PostgreSQL do Compose.
- `DATA_BACKEND`: `postgres` ou `csv`.
- `LEGACY_CSV_PATH`: caminho usado somente no modo CSV.
- `GYM_TRACKER_USER_ID`: usuário exibido pelo app quando há múltiplos usuários.
- `OPENAI_API_KEY`: opcional; sem ela, nenhuma chamada é tentada.
- `OPENAI_MODEL`: modelo usado pelo extrator.
- `LLM_SHADOW_MODE`: ligado por padrão.
- `LLM_AUTO_ACCEPT`: desligado por padrão e ainda não materializa propostas.
- `PARSER_VERSION`: versão registrada em imports e séries.

## Regras do parser

O parser aceita números com ponto/vírgula, `kg`, o erro contextual `22.5g`, multiplicadores como `8rep/2x`, múltiplas cargas por linha e continuação iniciada por reticências. Observações entre parênteses viram notas.

Uma linha nomeada sem `kg`, como `Barra 14/10rep`, é interpretada isoladamente e enviada para revisão por unidade ausente. Ela nunca é anexada ao exercício anterior. Mensagens apagadas e mídia são preservadas como fonte e ignoradas como treino.

## Revisão e aliases

`list-reviews` mostra as pendências. Exporte ou escreva um JSON no contrato `WorkoutExtraction`, valide-o com `accept-review` e, quando útil, passe `--save-alias`. Em importações futuras o alias do usuário é consultado antes da LLM.

Rejeitar uma revisão não apaga a mensagem; apenas marca a decisão e mantém o histórico.

## Migração legada

`compare-legacy` gera três arquivos ignorados pelo Git:

- `academia_treinos_reprocessado.csv`;
- `pendencias_reprocessamento.json`;
- `relatorio_qualidade.json`.

O CSV original não é sobrescrito. Somente mensagens aceitas são gravadas no arquivo reprocessado ou no PostgreSQL.

## Limitações

- A UI de revisão ainda é CLI/JSON.
- A associação de usuário é informada no comando; não há autenticação web.
- Detecção de conflito temporal é conservadora e pode exigir revisão de séries legítimas repetidas no mesmo dia.
- A LLM não decide automaticamente nesta versão.
