# ReqSys

## Prioridade atual — ReqSys Produto

A partir de 2026-09-23, **ReqSys Produto é o P0 principal**.

O Engineering Control Plane deixou o P0 temporário após cumprir o critério
`baseline -> mudança -> nova medição com redução objetiva`. Correções genéricas
de CI, administração transversal de repositórios, workers/orquestrador,
locks/watchdog, Governed Merge Queue e evidências de engenharia continuam
pertencendo ao Engineering Control Plane, mas em regime de melhoria contínua.

O Engineering Control Plane só pode voltar a preemptar o ReqSys Produto como P0
quando existir regressão sistêmica comprovada que afete múltiplos repositórios,
impeça o ciclo de desenvolvimento ou invalide seus gates/evidências canônicos.

No ReqSys Produto, priorizar evolução funcional, regras de negócio, UX e
integrações do produto. Problemas exclusivamente físicos de runner/host,
Command Gateway ou PC24x7 pertencem à Runtime Platform.

Aplicar as regras globais com ênfase em:
- CI/CD e mergeabilidade;
- gates de governança e segurança;
- evidência de runtime;
- correção mínima;
- rastreabilidade por `correlation_id`;
- idempotência;
- próximo incremento executável;
- preservação de trabalho existente.

## Seleção de runtime

Aplicar `rules/runtime-routing.md`.

Para novos workloads auxiliares compatíveis do ReqSys, especialmente em DEV — brokers, workers, gateways, filas, executores e serviços de apoio — verificar e preferir o `PC24x7 Desktop` existente antes de criar runtime pago adicional.

A decisão deve considerar persistência, backup/restauração, HTTPS estável quando necessário, secret store/arquivo protegido, restart automático, isolamento, disponibilidade e E2E. Se o PC24x7 atender esses requisitos, usar `runtime_target=pc24x7` por padrão.

Render, Fly.io ou outro provedor não devem ser introduzidos como novo caminho normal sem requisito objetivo não atendido pelo PC24x7 ou decisão explícita. Render pode permanecer como contingência quando justificado.

Esta preferência não altera automaticamente HML/PROD. Promoção de ambiente continua condicionada aos gates específicos de governança, segurança, continuidade e autorização.

## Prontidão antes de abrir Pull Request

Para o repositório `ericson-j-santos/reqsys-v2-enterprise-real`, agentes, automações e assistentes devem exigir `READY_FOR_PR=passed` antes de criar uma nova Pull Request.

A evidência deve:
- pertencer ao `HEAD` exato que será publicado;
- usar a `main` corrente como base e comprovar que a branch não está atrás dela;
- vir do `Pre-PR Readiness Gate` ou do executor canônico equivalente versionado no próprio ReqSys;
- ter conclusão verde e não reutilizar run, artifact ou SHA anterior;
- ser revalidada se houver novo commit ou avanço da base antes da abertura da PR.

Ausência, falha, pendência, SHA divergente ou base obsoleta deve bloquear a criação da PR. Não contornar o gate criando a PR diretamente por API, CLI, interface web ou outro executor quando a automação canônica estiver disponível.

`READY_FOR_PR=passed` autoriza somente a abertura da PR. Não substitui `READY_FOR_MERGE`, checks completos, E2E aplicável, revisão, mergeabilidade, autorização de merge, deploy ou promoção de ambiente.

## Merge automático CI-driven

Para o repositório `ericson-j-santos/reqsys-v2-enterprise-real`, o owner mantém autorização operacional para executar merge automaticamente quando o PR estiver integralmente pronto no HEAD atual.

O mecanismo preferencial é o `auto-merge` / `Governed Merge Queue` do próprio GitHub, orientado por eventos de CI e nunca por agendamento periódico.

Fluxo obrigatório:
1. CI falhou: identificar a menor causa raiz segura, corrigir no mesmo PR e revalidar no novo SHA.
2. CI verde: revalidar estado real do PR, HEAD SHA, mergeabilidade, conflitos, gates obrigatórios e concorrência.
3. Somente com PR aberto, não-draft, mergeável, sem conflitos e todos os gates obrigatórios aprovados no HEAD atual, executar o merge automaticamente.
4. A mutação de merge deve ser protegida pelo SHA esperado (`expected_head_sha` ou campo `sha` equivalente da API GitHub).
5. Se o SHA mudar ou qualquer gate voltar a falhar/pender, invalidar a decisão anterior, não mergear e reiniciar a validação no novo SHA.
6. Esta autorização de merge não autoriza deploy, promoção de ambiente, alteração destrutiva, segredo, permissão administrativa, force-push ou outra ação crítica distinta; essas ações continuam exigindo autorização específica quando aplicável.

A autorização não permite bypass de branch protection, revisão, segurança, E2E, evidência ou qualquer gate obrigatório.

