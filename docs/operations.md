# Operação, parsing e revisão

## Variáveis

- `DATABASE_URL`: conexão SQLAlchemy.
- `DATA_BACKEND`: `postgres` ou `csv`.
- `GYM_TRACKER_USER_ID`: usuário mostrado no dashboard.
- `OPENAI_API_KEY`: opcional.
- `OPENAI_MODEL`: modelo auditado no parse run.
- `LLM_SHADOW_MODE`: `true` por padrão.
- `LLM_AUTO_ACCEPT`: `false` por padrão.
- `PARSER_VERSION`: versão lógica do parser, atualmente `3.0.0`.

## Importação e reprocessamento

```bash
uv run python -m gym_tracker.cli import-whatsapp --file export.txt --user UUID
uv run python -m gym_tracker.cli import-whatsapp --file export.txt --user UUID --parser-version 3.1.0
uv run python -m gym_tracker.cli quality-report --import-id UUID
```

O mesmo arquivo/versão retorna `duplicate_import=true`. Uma nova versão cria outro parse run. Em exports sobrepostos, a ocorrência é registrada no novo import e a mensagem anterior é reutilizada.

## Formatos do WhatsApp

São aceitos cabeçalhos com ano de dois ou quatro dígitos, segundos opcionais e estas formas:

```text
[24/07/2026, 13:45:12] Nome: mensagem
[24/07/2026 13:45] Nome: mensagem
24/07/2026 13:45 - Nome: mensagem
24/07/2026, 13:45:12 - Nome: mensagem
```

Mensagens multilinha e múltiplos exercícios são preservados. Só são juntadas linhas iniciadas por reticências ou continuações compostas claramente apenas por repetições. Texto livre separado vai para revisão. Marcadores de edição, exclusão e mídia ficam na origem.

## Revisões

```bash
uv run python -m gym_tracker.cli list-reviews --status pending --json
uv run python -m gym_tracker.cli show-review --review-id UUID --json
uv run python -m gym_tracker.cli accept-review --review-id UUID --payload correcao.json
uv run python -m gym_tracker.cli accept-review --review-id UUID --payload correcao.json --save-alias "mov x"
uv run python -m gym_tracker.cli reject-review --review-id UUID
```

A listagem mostra data, motivo, método, trecho limitado, proposta e status. O detalhe mostra conteúdo original, origem, payloads e resumo das diferenças. Aceitar ou rejeitar uma revisão finalizada novamente é erro. Aceitar materializa e ativa transacionalmente; rejeitar preserva qualquer resultado ativo anterior.

## Comparação legada

O comparador normaliza Unicode, acentos, caixa, espaços, equipamento e números. Diferenças apenas visuais não contam como alteração. Equipamentos ou séries realmente diferentes continuam aparecendo.

## Limitações

- revisão humana ainda usa CLI/JSON;
- não há autenticação web;
- correlação de exports com precisão temporal diferente é conservadora;
- duas variantes históricas confirmadas tornam o equipamento ambíguo;
- LLM autoaceita somente com duas configurações explícitas e continua sendo uma opção de alto controle.
