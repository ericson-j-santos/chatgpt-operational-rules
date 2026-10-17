# Engineering Control Plane

## Estado atual

- prioridade: **melhoria contínua**;
- P0 temporário: **encerrado em 2026-09-23**;
- escopo: transversal e multi-repositório;
- regra Pareto: corrigir causas sistêmicas antes de automatizar sintomas.

## Responsabilidade

Manter a plataforma compartilhada de engenharia:

- CI Platform e workflows reutilizáveis;
- Pre-PR Readiness / Preflight;
- Repository Administration / RepoAdmin;
- Worker Pool / Codex e Engineering Orchestrator;
- locks por repositório/worktree e watchdog de progresso;
- Builder / Validator;
- Governed Merge Queue;
- evidências de engenharia;
- prevenção de erros recorrentes;
- observabilidade do processo de desenvolvimento.

Não absorver evolução funcional do ReqSys Produto nem dependências físicas da
Runtime Platform / PC24x7 salvo quando forem necessárias para validar a própria
plataforma de engenharia.

## Critério de saída do P0 cumprido

Evidência canônica no repositório
`ericson-j-santos/reqsys-v2-enterprise-real`:

1. baseline real: CI Lead Time Analytics run `35857466142`,
   `baseline_sample_valid=true`, 3 PRs, 158,37 minutos observados,
   rerun 12,7% e Governed Merge Queue com 46,98 minutos / 29,67%;
2. mudança Pareto: PR #1997 removeu setup Node/npm/lint/typecheck frontend
   duplicados do Governed Merge Queue, preservando os gates fail-closed;
3. nova medição: no mesmo workflow, 324 s -> 251 s (-22,53%); no job
   diretamente alterado `Validação isolada do PR`, 27 s -> 13 s (-51,85%);
4. PR #1997 integrada no merge SHA
   `e0ffa3154cdf12ed8c75c308e7b53e7ac47ef0b2`.

A remedição agregada com 3 PRs integralmente pós-mudança continua como evidência
de acompanhamento e **não bloqueia** a saída do P0, porque misturar execuções
anteriores contaminaria a comparação.

## Política de prioridade

ReqSys Produto permanece P0 principal.

O Engineering Control Plane só reassume P0 temporário quando houver evidência
objetiva de regressão sistêmica, por exemplo:

- CI ou gates canônicos impedindo desenvolvimento em múltiplos repositórios;
- regressão material de tempo/custo que volte a dominar o ciclo;
- falha de lock/watchdog causando colisão ou estagnação recorrente;
- Governed Merge Queue ou Pre-PR Readiness deixando de falhar fechado;
- perda de rastreabilidade/evidência que impeça validar incrementos.

Incidentes isolados de um único produto permanecem no projeto responsável,
salvo prova de causa transversal.

## Admissão profissional de Pull Request e merge

Para repositórios governados pelo Engineering Control Plane, separar três etapas:

1. **preflight antes da PR**: executar no HEAD exato todos os checks determinísticos
   que não dependem do contexto de Pull Request. Falha conhecida, branch atrás da
   base ou evidência de SHA antigo bloqueiam a abertura;
2. **validação exclusiva de PR**: quando algum check depender do evento
   `pull_request`, segredo/ambiente protegido, integração temporária ou recurso
   externo, abrir somente como **draft** após o preflight determinístico verde.
   Falha de runner/serviço deve ser classificada como infraestrutura, não como
   defeito de código sem evidência;
3. **admissão de merge**: tornar a PR elegível somente no HEAD atual, com base
   atualizada, mergeabilidade válida e gates obrigatórios verdes. O GitHub deve
   impor `required_status_checks` no branch/ruleset; workflow verde sem proteção
   server-side não é controle suficiente.

Regras adicionais:

- qualquer novo commit ou avanço da base invalida evidência anterior e exige
  revalidação;
- automações não devem criar PR não-draft quando houver falha determinística
  conhecida;
- `protected=true` sozinho não comprova que CI bloqueia merge: a auditoria deve
  ler rulesets e comprovar ao menos um `required_status_checks` ativo quando a
  política exigir;
- ausência ou impossibilidade de ler essa configuração é falha fechada de
  governança, sem alteração administrativa automática;
- mudança de branch protection/ruleset continua sendo ação administrativa e
  exige autorização explícita para o repositório e branch alvo.

## Próximas medições

Continuar coletando:

- minutos de CI por PR;
- p50/p90 até verde;
- taxa de rerun;
- percentual de PRs com commit corretivo de CI;
- workflows responsáveis por 80% dos minutos consumidos.

Usar essas medições para selecionar o próximo incremento Pareto em melhoria
contínua, sem reabrir automaticamente Auto-Remediator, dashboards ou expansões
sofisticadas antes de um novo gargalo sistêmico comprovado.

<!-- gitlab-secondary-architecture-contract-20261017 -->
## GitLab secundário e CI próprio PC24x7 — contrato operacional

### Fonte e fluxo de integração
- GitHub `ericson-j-santos/reqsys-v2-enterprise-real`, branch `main`, permanece a fonte canônica para código, Pull Requests, testes, gates, revisões, releases e homologações.
- GitLab `ericson-j-santos/reqsys-v2-enterprise-real` opera somente como espelho unidirecional **GitHub → GitLab** e CI secundário. Nunca tratar mudanças independentes no GitLab como fonte de verdade nem criar sincronização bidirecional.
- Reutilizar `scripts/sync_gitlab_mirror.py`, `.github/workflows/gitlab-main-mirror.yml` e a credencial técnica gerenciada no Azure Key Vault. Não criar outro sincronizador, token nem canal de push.
- O mirror é permitido somente com atestação da identidade técnica exata, ancestralidade comprovada, fast-forward sem force-push e releitura independente do SHA GitLab igual ao HEAD corrente do GitHub. Divergência e acesso insuficiente bloqueiam a operação.

### Segurança e admissão de merge
- Exigir branch `main` GitLab protegida, `allow_force_push=false`, permissão de push estritamente limitada à identidade técnica de mirror e **nenhum acesso genérico de Developers**.
- Configurar `only_allow_merge_if_pipeline_succeeds=true` e `allow_merge_on_skipped_pipeline=false`; exigir jobs obrigatórios de scanner e evidência realmente executados no SHA correto. Jobs skipped, no jobs run, allow_failure, 2xx ou pipeline green sem gate real não são comprovação de merge elegível.
- GitLab Free não oferece aprovação obrigatória equivalente aos controles de revisão do GitHub. Não declarar equivalência de governança nem autorizar merge canônico pelo GitLab; conservar merge governado no GitHub até prova formal de alternativa.
- Achados de scanner, incluindo Gitleaks, exigem triagem confidencial, correção da causa, teste de regressão e verificação limpa. Não divulgar achados sensíveis, criar allowlist ampla, desativar scanner ou ignorar falha para atingir CI verde.
- Alterações de branch protegida, membros, permissions e credenciais requerem autorização explícita por alvo e ambiente e confirmação do efeito por API independente; ausência de ferramenta com escrita autorizada é bloqueio, não permissão para bypass.

### Runner de projeto PC24x7
- Preferir **GitLab project runner próprio** em piloto DEV no PC24x7 para reduzir uso de compute dos runners GitLab.com, respeitando custo adicional zero; nunca inferir que infraestrutura física é gratuita.
- Antes de instalar/registrar, inventariar o host real, disponibilidade de CPU/RAM/disco, processos e jobs existentes, sistema operacional, runtime supervisionado, secrets e rollback. Cumprir `session_launcher.py`, `SESSION_LAUNCH_OK`, Command Gateway e worktree/sessão exclusivos; sem terminal irrestrito, SSH, GUI ou RDC como contorno.
- Criar runner com escopo, tags e menor privilégio, sem conceder variáveis protegidas a MRs não confiáveis; separar jobs confiáveis de código externo e limitar concorrência. Alinhar com a migração gradual sem Docker documentada em `chatgpt-operational-rules#128`; não interromper runners GitHub existentes.
- Aceite físico exige job real executado no project runner esperado, SHA e correlação exatos, controle negativo de elegibilidade, repetição sem duplicidade, readback independente do GitLab e host, e capacidade de recuperação/reversão. Sem esse E2E: `PARCIAL`/`BLOQUEADO`, nunca homologado.

### Custos, artefatos e continuidade
- Antes de excluir artefatos ou alterar retenção no GitHub Actions, reconciliar Billing autenticado com artefatos, Packages, histórico e consumo acumulado do ciclo; volume ativo em bytes não prova custo faturável.
- Preservar a última evidência necessária para gates, reconstituição de histórico e recuperação; não apagar artefatos em massa nem sacrificar auditabilidade para liberar cota.
- Registrar checkpoint em issue/TODO Global existente com `correlation_id`, HEADs de ambos os remotos, pipeline/job IDs, evidência de aceitação e próximo bloqueio. Não duplicar filas/PRs ou despachar pipelines repetidos sem mudança material de precondição.

Referências operacionais: ReqSys issues #846 e #855; regras `projects/reqsys.md`, `projects/runtime-platform.md`, `rules/e2e-validation.md`, `rules/pre-delivery-validation.md`, `rules/security-governance.md` e `rules/artifact-storage.md`.
