# Arquitetura

## Pipeline

```text
Export WhatsApp
  -> mensagens brutas + hashes
  -> parser determinístico
  -> validação e resolução de aliases
  -> aceito -----------------------> PostgreSQL -> adapter DataFrame -> Streamlit
  -> ambíguo -> LLM opcional ------> parse_reviews -> decisão humana -> PostgreSQL
```

O PostgreSQL é a fonte oficial. `DATABASE_URL` permite trocar o Compose local por um serviço gerenciado sem alterar a aplicação. O CSV é somente um adapter legado ativado por `DATA_BACKEND=csv`.

## Módulos

- `models.py`: entidades e constraints SQLAlchemy.
- `schemas.py`: contratos Pydantic de mensagens, extrações e relatórios.
- `services/parser.py`: divisão de mensagens e parser determinístico.
- `services/normalization.py`: catálogo, aliases estáticos e equipamento.
- `services/whatsapp_import.py`: idempotência, validação, shadow mode e persistência.
- `services/llm_extractor.py`: interface, implementação OpenAI e mock.
- `services/reviews.py`: aceitação, rejeição e aprendizado por alias.
- `repositories/`: consultas e catálogo persistido.
- `data_adapter.py`: contrato de DataFrame consumido pelo app atual.

## Idempotência e rastreabilidade

O hash SHA-256 do arquivo é único por usuário. Cada mensagem preserva índice, horário, remetente, conteúdo, hash e marcadores de edição/exclusão. Cada série aponta para `raw_messages`, `workouts` e uma `exercise_variant`. Reimportar o mesmo arquivo retorna o relatório original sem inserir novos registros.

Conflitos normalizados no mesmo dia/remetente são enviados para revisão. Nenhuma versão conflitante é escolhida automaticamente.

## LLM

O parser por regras sempre roda primeiro. A LLM só é considerada para formato não reconhecido, alias desconhecido, texto não consumido ou ambiguidade. A implementação usa Responses API com `responses.parse`, Structured Outputs e o modelo Pydantic `WorkoutExtraction`, seguindo a [documentação oficial de Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

Em shadow mode, a proposta e o identificador do modelo/prompt são armazenados no review, mas não criam séries. O sistema não armazena raciocínio interno. `LLM_AUTO_ACCEPT` existe como configuração futura, mas permanece desativado e não é usado para gravação automática nesta versão.
