# GitHub e CI/CD

- Verificar branch, PR, mergeabilidade, checks e logs antes de modificar.
- Preservar o trabalho existente.
- Corrigir a menor causa raiz possível.
- Revalidar checks após alteração.
- Não fazer merge sem solicitação ou autorização compatível com o contexto.
- Retornar links observados nas ferramentas; não sintetizar links de evidência.
- Para alterações sequenciais no mesmo arquivo, respeitar o SHA corrente.
- Probes/E2E descartáveis que induzem falha deliberada, autocorreção ou mutação automática nunca devem usar a branch padrão/protegida como base do PR. Criar uma base temporária isolada a partir do SHA corrente, executar o probe contra essa base e encerrar o PR após a evidência.
- Se a branch padrão estiver sem proteção/ruleset efetivo, tratar isso como bloqueio de governança para enforcement: `draft`, texto "NUNCA MERGEAR" e checks de CI são sinalização, não barreira suficiente contra merge manual/externo.

## Dispatch manual do diagnóstico SQL EventLog no Noteri

O publicador governado de `workflow_dispatch` admite somente a tupla exata:

- repositório `ericson-j-santos/noteri-runtime`;
- workflow `noteri-sql-application-eventlog.yml`;
- ref `main`.

A chamada exige o SHA remoto completo de `main` e encaminha somente os metadados
controlados `expected_sha` e `correlation_id`, derivados dos argumentos já validados.
O workflow bloqueia o job quando `github.sha` diverge de `expected_sha`, e o readback
exige o mesmo SHA e `run-name` correlacionado. Inputs livres ou de comando são
proibidos. Qualquer outro repositório, workflow, ref ou conjunto de inputs exige
mudança versionada e revisada.
A presença da tupla na allowlist não autoriza, por si só, merge ou dispatch.
