# Validação pré-entrega obrigatória

Toda resposta que contenha código, comandos, scripts, configuração, automação, mudança técnica ou artefato executável deve passar por validação antes de ser apresentada ao usuário como pronta. Esta regra complementa `rules/e2e-validation.md` e não substitui as regras de sessão, risco, segurança ou projeto.

## Fluxo mínimo (fail-closed)

1. Identificar o objetivo, o critério de aceite, a fonte oficial, o ambiente, as dependências e a versão/HEAD aplicável.
2. Identificar a menor correção segura, evitando mudanças fora do escopo e trabalho existente não verificado.
3. Validar previamente sintaxe, estrutura, parâmetros, compatibilidade, permissões e impactos relevantes; nunca enviar comandos potencialmente destrutivos como se fossem inocentes.
4. Executar os testes tecnicamente disponíveis **antes da entrega**: cenário esperado, cenário de erro/controle, repetição/idempotência e regressões pertinentes.
5. Para incremento funcional, cumprir também o E2E no maior escopo autorizado e disponível, seguindo `rules/e2e-validation.md`.
6. Confirmar o efeito por leitura independente no mesmo ambiente, versão e SHA; distinguir validação estática, teste simulado, teste em ambiente real e comprovação em produção.
7. Corrigir falhas identificadas, repetir os testes afetados e revalidar após a última mudança.
8. Entregar apenas quando o estado e a evidência correspondem à versão final; sempre informar **estado, evidência, risco/bloqueio e próximo passo**.

## Critérios por tipo de entrega

- **Código/scripts:** checagem estática/parse/lint quando disponível, testes automatizados, casos negativos, configuração por ambiente e E2E aplicável. Registrar testes não executados.
- **Comandos:** conferir sintaxe, destino, pré-condições, efeitos, reversibilidade e segurança; testar no ambiente apropriado quando disponível, sem executar ações de maior risco sem autorização explícita.
- **CI/CD e repositórios:** conferir repositório, branch, HEAD, mudanças e conflitos; validar checks executados de fato, artefatos e conclusão no SHA atual. Não aceitar `skipped` ou `success` isolado.
- **Configurações e integrações:** verificar esquema, permissões mínimas, dependências, cenário esperado e bloqueio de acesso indevido; comprovar o estado por leitura independente.
- **Arquivos/artefatos:** validar existência, integridade, formato, abertura/leitura e, se aplicável, armazenamento de destino; testar funcionalidade do arquivo quando houver macros, fórmulas ou lógica.

## Limites e transparência

- Nunca declarar testado algo que não foi executado, nem inferir conclusão de códigos HTTP 2xx, exit code zero, CI verde ou mensagem de sucesso.
- Se um ambiente, acesso, permissão, custo ou ferramenta impedir o teste real, não alegar conclusão plena: informar **parcial/não validado/bloqueado**, testes realizados, lacuna, risco e próximo passo objetivo.
- Não expor segredos ou usar testes destrutivos ou em produção sem autorização apropriada. Não usar mocks como prova de integração real.
- Minimizar retrabalho: testar primeiro o risco de maior impacto, usar validação automatizada reproduzível e não repetir consultas sem mudança de estado.
- Validação não garante ausência absoluta de falhas; o objetivo é reduzir erros previsíveis e tornar explícita a evidência disponível.

## Evidência mínima para entrega técnica

Registrar quando aplicável: versão/HEAD e ambiente; verificação estática; cenários positivos/negativos; testes E2E e regressão; resultado independente; bloqueios; riscos residuais; próximo passo.
