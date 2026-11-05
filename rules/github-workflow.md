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

## Mantenedor único — revisão técnica sem simular aprovação humana

Aplicável somente quando o proprietário declara e registra que não possui outro
colaborador humano elegível no repositório. O autor não pode solicitar aprovação
a si mesmo nem criar identidade alternativa para fingir revisão. Essa condição
não elimina a validação independente nem dispensa autorizações de integração.

1. Admissão: ler via API a PR, head_sha, base_sha, diff, checks e ruleset/proteção
   da branch alvo. Confirmar required_approving_review_count=0 e os checks
   obrigatórios. Se o GitHub exigir aprovação humana externa ou se a
   configuração não puder ser lida, BLOQUEAR sem contornar proteções.
2. Revisão técnica: exigir relatório novo verificável de processo/identidade
   diferente do Builder/autor, com checkout SHA-bound, diff, riscos, fronteiras
   de confiança, regressões, controles negativos, CAS/readback e permissões.
   CI verde, comentário do próprio autor ou autoavaliação do mesmo agente
   não constituem revisão técnica independente. Registrar identidade do
   Validator, head_sha, ambiente, run/artefato e conclusão reproduzível.
3. Decisão: após o relatório, o proprietário registra no GitHub head_sha,
   base_sha, checks obrigatórios, riscos aceitos e autorização específica
   de merge. O ateste é decisão do proprietário, NÃO é aprovação de
   outro revisor humano nem substitui o Validator. Sem decisão ou após
   avanço de HEAD/base, READY_FOR_MERGE=false e revalidar novamente.
4. Gates: custo R$ 0,00, integridade, CAS, segurança, CI exigida, testes
   positivos/negativos, regressões, idempotência e E2E aplicável continuam
   obrigatórios. Gates skipped/neutral não aprovam. Não libera deploy,
   produção, execução no host, privilégios ou segredos por inferência.
5. Estados: sem relatório usar INDEPENDENT_TECHNICAL_REVIEW_PENDING;
   com relatório válido, mas sem decisão, usar OWNER_DECISION_PENDING.
   Nunca registrar INDEPENDENT_HUMAN_APPROVAL sem revisor humano real.
   Só após gates/decisão avaliar READY_FOR_MERGE. Integrar protocolo à
   CI/consumidores antes de alegar enforcement automatizado.

Copilot Code Review é opcional; somente usar com licença, saldo/franquia
e bloqueio de overage comprovados. Não presumir aprovação ou custo zero.
