# Privacidade

Exports reais do WhatsApp podem conter nomes, telefones, horários e conteúdo pessoal. Eles estão no `.gitignore`; somente fixtures anonimizadas devem entrar no repositório público. Nunca coloque `OPENAI_API_KEY`, `DATABASE_URL` de produção ou `.env` no Git.

## Arquivo já versionado

O export histórico já presente no Git não foi removido nem teve o histórico reescrito nesta etapa. A correção segura exige uma decisão explícita:

1. Remover o arquivo da versão atual com `git rm --cached`, mantendo a cópia local.
2. Se a exposição histórica for indesejada, usar `git filter-repo` para expurgá-lo de todos os commits.
3. Fazer force-push coordenado e pedir que colaboradores façam novo clone.
4. Considerar o conteúdo previamente público e avaliar troca/remoção de qualquer identificador sensível.

Reescrever histórico altera SHAs e afeta todos os clones, por isso deve ser feito separadamente e com autorização.
