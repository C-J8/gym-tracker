# Arquitetura

## Pipeline incremental

```text
arquivo exportado
  -> imports
  -> mensagem canônica (raw_messages)
  -> ocorrência no arquivo (import_message_occurrences)
  -> parse_run versionado
  -> parse_result
       -> aceito e ativo -> séries visíveis no dashboard
       -> revisão        -> decisão humana
       -> reutilizado    -> aponta que a mensagem já foi processada
       -> ignorado       -> sem treino materializável
```

`imports` representa o arquivo. O SHA-256 do arquivo continua detectando a repetição exata. `raw_messages` representa uma mensagem canônica do usuário e pode aparecer em muitos exports. `import_message_occurrences` preserva arquivo, posição, offset e ordinal de cada aparição.

## Identidade da mensagem

A identidade combina usuário, remetente normalizado, instante, precisão disponível (`minute` ou `second`), hash do conteúdo com Unicode/espaços normalizados, marcador de edição e ordinal entre ocorrências idênticas. Consequências:

- exports sobrepostos reutilizam a mesma mensagem;
- o mesmo texto em datas diferentes é legítimo;
- usuários diferentes nunca colidem;
- repetições idênticas no mesmo minuto são preservadas por ordinal;
- mesmo remetente/horário/ordinal com conteúdo diferente gera conflito, sem sobrescrita.

## Parse runs e ativação

`parse_runs` separa ingestão de processamento e registra versão do parser, prompt, modelo, shadow mode e status. A combinação import/versão é única. Repetir o mesmo arquivo na mesma versão é no-op; mudar a versão cria uma nova execução sem duplicar a origem.

Cada `parse_result` pertence a uma mensagem e a um run. Resultados antigos e suas séries permanecem auditáveis. Um índice parcial permite no máximo um resultado ativo por mensagem. A nova extração é materializada antes da troca; desativação e ativação ocorrem na mesma transação. Se a nova versão falhar ou exigir revisão, o resultado aceito anterior continua ativo.

O dashboard consulta somente séries cujo resultado está ativo. Assim, versões antigas e novas nunca são somadas.

## Equipamento

Carga não participa da resolução. A prioridade é:

1. equipamento explícito no texto;
2. alias confirmado pelo usuário;
3. contexto explícito do bloco;
4. única variante ativa anteriormente confirmada para o usuário;
5. revisão humana.

Quando há zero ou mais de uma variante possível, `equipment` permanece vazio e nenhuma série final é criada.

A proposta da LLM não é evidência. Para autoaceitação, `equipment` e
`load_basis` precisam ter sido resolvidos pelo texto, alias do usuário, contexto
do bloco, catálogo determinístico ou variante confirmada. A origem usada fica em
`resolution_evidence` no payload do resultado. Divergência ou ausência de
evidência envia a mensagem inteira para revisão.

## Catálogo auxiliar

O bootstrap do CSV acontece fora da pipeline de treinos:

```text
CSV legado -> agregação e normalização -> exercícios / aliases / variantes
```

Ele não cria origem, parse run, resultado, treino ou série. A normalização de
catálogo usa uma chave Unicode sem acentos, diferenças de caixa, pontuação
irrelevante ou espaços extras, preservando o nome canônico legível já existente.
Somente associações unívocas são aplicadas; ambiguidades permanecem em relatório.
O dashboard PostgreSQL continua consultando exclusivamente séries aceitas e
ativas originadas pela pipeline do TXT.

## Cache e semana

`data_revisions` mantém uma versão monotônica. Importações, ativações, decisões de revisão e aliases incrementam essa versão. `data_signature()` consulta o valor fora do cache do Streamlit, invalidando o DataFrame automaticamente.

A semana analítica usa `W-SUN`: começa na segunda-feira, termina no domingo e exibe a segunda-feira como início.

## LLM

O parser determinístico sempre roda primeiro. Sem `OPENAI_API_KEY`, o pipeline funciona sem chamadas externas. Com shadow mode ligado, a proposta estruturada pode ser armazenada, mas nunca ativa séries.

Com shadow mode desligado, a saída ainda passa por Pydantic e validação determinística. A ativação automática só existe quando `LLM_AUTO_ACCEPT=true` também é informado explicitamente; ambiguidades continuam em revisão. Testes e CI usam mocks.

## Migrations

- `0001`: schema multiusuário inicial.
- `0002`: mensagens canônicas, ocorrências, parse runs, resultados ativos e revisão de dados.

A `0002` retropreenche ocorrências e resultados para dados existentes, mantendo as séries anteriores ativas.
