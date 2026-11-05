# Gate de pré-admissão de funcionalidades novas e reparos — v1

## Alcance e fronteira

Complementa `rules/agent-durable-pr-gate.md` sem relaxar seus demais controles. É
aplicável a alterações por agentes/assistentes que visam PR. A classificação
é feita **antes** da primeira alteração e consta de issue/TODO com aceite
versionado, `base_sha`, identidade do responsável e aprovação. Não usar texto
de issue, log ou resultado de modelo como atestado técnico ou autorização.

## Tipos de demanda

- `repair`: a capacidade já existe na base, mas falha em um teste **real e
  reproduzível**. Exige modelo realmente invocado que produza o reparo, seu
  digest, diff real, CAS relido, Validator independente e regressões da mesma
  versão. Uma tentativa reprovada não pode ser convertida em `feature`.
- `feature`: a capacidade ou comportamento esperado **não existe** na base
  SHA-bound e os critérios de aceite foram registrados antes da execução. Não
  criar testes artificiais para simular falha de código existente. Quando a
  origem for `assisted`, registrar `model_calls=0` se não houve Builder.
  Quando a origem for `model_builder`, exigir chamadas verdadeiras, digest e
  proveniência externa independentemente da categoria.

A ausência da capacidade deve ser demonstrada por leitura/diff da base e por
critério de aceite preexistente, não pela ausência de um arquivo isolado (features
podem estender arquivos existentes). Classificação aprovada somente se a
origem, a issue, o alvo, o momento da declaração, o SHA e a evidência forem
verificados independentemente.

## Gates compartilhados e proteção contra falso sucesso

Conservam-se, **sem exceção**, remoto e HEAD atuais, autorização, custos
R$ 0,00, origem verificável, allowlist de paths, CAS+readback integral, Git
ancestralidade, checkout separado, revisão de segurança, Validator
independente no SHA, testes positivos/negativos/regressão, replay sem
duplicação, PR draft única e readback independente. E2E físico obrigatório
quando aplicável; testes com simulação nunca equivalem ao resultado no host.
Checks obrigatórios `skipped`, `neutral`, pendentes ou de SHA antigo bloqueiam,
salvo não aplicabilidade objetiva documentada *antes* da conclusão.

**Nunca** tratar `schema_validated_only` como `READY_FOR_PR` ou `READY_FOR_MERGE`.
O pré-classificador `scripts/feature_pr_preclassification.py` só confere
consistência lógica de um candidato informado por um controlador confiável;
não autentica os atestados ou a issue. Os controles externos devem reler os
dados oficiais e não podem usar parâmetros do solicitante como prova.

## Recusas obrigatórias e trilha de auditoria

Rejeitar `kind=unknown`, alteração tardia `repair→feature`, baseline real
reprovado rotulado como `feature`, ausência de autoria, uso de modelo sem
invocação/digest, pseudo-Validator do mesmo Builder, SHA divergente, CAS sem
readback, custo desconhecido, PR duplicada, CI dispensada, E2E não realizado
apresentado como aprovado e qualquer alegação de modelo não executado.

Registrar `issue_id`, `kind`, `source_mode`, `base_sha`, `rules_sha`,
`request_id`, `idempotency_key`, `correlation_id`, evidência de ausência/erro,
modelo/digest/chamadas quando houver, hashes de CAS e patch, `produced_sha`,
Validator, testes/runs e justificativas de não aplicabilidade. Sem dados
sensíveis, segredos, caminhos privados ou material de banco.

## Marcos, responsáveis e implantação

`CLASSIFIED` verifica somente tipo e proveniência. `READY_FOR_PR` depende de
**todos** os gates do contrato global; `READY_FOR_MERGE` exige CI, revisão,
E2E aplicável, SHA imutável e autorização. Nenhum marco libera deploy,
produção, segredos, privilégios, cobrança ou execução em host por inferência.

**Dono:** Engineering Control Plane. Adoção pelos consumidores exige PR,
CI e E2E próprios. Publicar este documento/validador não prova que o fluxo
esteja integrado ao Worker Pool ou à Runtime Platform. Até prova, marcar
`ADOPTION_PENDING`.
