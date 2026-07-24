# Contrato de dados

## Entidades

| Tabela | Papel | Regras principais |
|---|---|---|
| `users` | proprietário dos dados | UUID e nome de exibição |
| `imports` | execução de ingestão | hash único por usuário, versão do parser e relatório JSON |
| `raw_messages` | fonte imutável rastreável | índice e hash únicos dentro da importação |
| `workouts` | sessão de treino | usuário, data, tipo, notas e origem |
| `exercises` | movimento canônico | nome canônico único e grupo muscular |
| `exercise_variants` | forma de execução | equipamento, local e base da carga |
| `exercise_aliases` | expressão aprendida | alias global ou específico do usuário |
| `sets` | série aceita | carga, reps, RPE/RIR, método e origem |
| `parse_reviews` | decisão humana | proposta, correção, status e horário da revisão |

## Métodos de extração

- `rule`: aceito pelo parser determinístico.
- `llm`: reservado para ativação futura após avaliação do shadow mode.
- `manual`: materializado por uma revisão aceita.

## Base da carga

`load_basis` distingue `total`, `por_halter` e futuras bases como `por_lado`. Equipamento não faz parte do nome do exercício: `Supino reto / Halter` e `Supino reto / Máquina` compartilham o mesmo exercício e usam variantes distintas.

## DataFrame do dashboard

O adapter retorna uma linha por série com:

`data`, `grupo_muscular`, `exercicio`, `tipo`, `peso_kg`, `serie`, `repeticoes`.

As colunas derivadas `volume`, `estimativa_1rm`, `semana`, `mes` e `exercicio_tipo` são calculadas em `services/metrics.py`. O 1RM usa Epley: `peso * (1 + reps / 30)`.
