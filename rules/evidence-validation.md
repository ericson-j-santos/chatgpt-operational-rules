# Evidência e validação

- Não declarar sucesso sem evidência verificável.
- Separar estado evidenciado de estado alvo.
- Não inventar status, dados, links, commits, checks ou resultados.
- Em CI, PR e deploy, consultar o estado real antes da conclusão.
- Registrar bloqueios externos explicitamente.
- Preferir a menor correção que trate a causa raiz.
- Quando uma ação puder ser executada com as ferramentas disponíveis, executar e validar antes de responder.
- Para todo incremento funcional criado ou modificado, aplicar `rules/e2e-validation.md`.
- Não aceitar evidência ambígua, residual, de outro SHA/ambiente ou produzida somente pela implementação sob teste quando houver fonte independente disponível.
- Quando o E2E aplicável não puder ser executado, registrar o bloqueio e não declarar conclusão funcional.
