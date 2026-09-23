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
