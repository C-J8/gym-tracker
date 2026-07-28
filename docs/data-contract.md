# Contrato de dados

| Tabela | Papel | Invariantes |
|---|---|---|
| `users` | proprietário | mensagens e imports nunca cruzam usuários |
| `imports` | arquivo ingerido | SHA-256 único por usuário |
| `raw_messages` | mensagem canônica | identidade única por usuário |
| `import_message_occurrences` | aparição no export | posição única dentro do arquivo |
| `parse_runs` | execução versionada | uma execução por import/versão |
| `parse_results` | resultado derivado | no máximo um ativo por mensagem |
| `parse_reviews` | decisão humana | estado único, payload e `reviewed_at` |
| `workouts` | sessão derivada | preservada para auditoria |
| `sets` | série derivada | pertence a um parse result |
| `exercises` | exercício canônico | nome e grupo muscular |
| `exercise_variants` | execução | equipamento e base da carga |
| `exercise_aliases` | decisão aprendida | global ou por usuário |
| `data_revisions` | versão visível | invalida o cache do dashboard |

## Estados

`parse_results.status`: `accepted`, `review`, `skipped`, `reused` ou `rejected`.

`parse_results.is_active=true` significa que suas séries alimentam o dashboard. Um resultado aceito antigo pode permanecer auditável com `is_active=false`.

Métodos de extração:

- `rule`: parser determinístico;
- `llm`: proposta validada e explicitamente autoaceita;
- `manual`: revisão humana aceita.

## Cargas

O valor persistido usa quilogramas. `500g` vira `0,5kg`; ponto e vírgula decimais são aceitos. Os limites centralizados são `0,1kg` a `500kg`. Valores fora desse intervalo nunca são materializados sem correção humana. `22.5g` é literalmente `0,0225kg` e gera uma sugestão de possível erro de unidade, sem correção automática.

## Dashboard

O adapter retorna apenas resultados ativos, com uma linha por série:

`data`, `grupo_muscular`, `exercicio`, `tipo`, `peso_kg`, `serie`, `repeticoes`.

As colunas `volume`, `estimativa_1rm`, `semana`, `mes` e `exercicio_tipo` são derivadas. A semana vai de segunda a domingo.
