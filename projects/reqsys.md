# ReqSys

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
