# Inteligência contínua de falhas e Pareto operacional

## Objetivo

Transformar logs, falhas de CI, erros de runtime e bloqueios operacionais em um fluxo contínuo de melhoria entre projetos, evitando tratar cascatas, controles governados ou cancelamentos esperados como bugs independentes.

Fluxo canônico:

```text
coletar -> sanitizar -> normalizar -> fingerprint -> classificar
        -> Pareto -> corrigir causa-raiz -> regressão -> E2E
        -> medir reincidência -> próximo item
```

## Aplicação obrigatória

Aplicar esta regra:

- ao continuar a evolução de um projeto com falhas recentes conhecidas;
- antes de remediar CI vermelho recorrente;
- após incidentes de runtime, integração, disponibilidade ou publicação;
- ao revisar estabilidade operacional, padrão ouro ou prontidão para merge;
- em reconciliação periódica de baixa frequência para detectar reincidência.

Quando o analisador canônico do `observability-platform` estiver integrado e disponível para o projeto, preferir seu contrato versionado e fixar o consumidor por SHA imutável. Na ausência temporária do analisador, aplicar a mesma classificação manualmente com evidências reproduzíveis.

## Classificação obrigatória

Cada sinal deve cair em exatamente uma classe operacional:

1. `root_cause`
   - defeito ou condição técnica que precisa de correção;
   - entra no Pareto e pode originar backlog técnico.

2. `control`
   - bloqueio governado esperado;
   - exemplos: custo desconhecido bloqueado, autorização humana ausente, PR draft fora de fila elegível;
   - não entra no Pareto de bugs e não autoriza contorno do gate.

3. `cascade`
   - efeito downstream causado por falha upstream;
   - não abre bug independente;
   - deve apontar para a causa original.

4. `noise`
   - cancelamento/deduplicação/supersessão comprovada ou outro sinal operacional não funcional;
   - só pode ser descartado quando houver evidência de sucessor ou contrato equivalente;
   - cancelamento sem sucessor comprovado permanece não verificado.

## Pareto

Priorizar somente `root_cause`.

O ranking deve considerar, no mínimo:

- reincidência lógica;
- quantidade de workflows/serviços afetados;
- severidade;
- impacto observável;
- se a causa continua ativa no estado atual.

Não multiplicar a mesma regressão por:

- push + PR do mesmo SHA;
- gates derivados que apenas propagaram `ADMISSION_BLOCKED`;
- reruns sem mudança material;
- workflows downstream que falharam apenas porque o produtor upstream falhou.

## Correção

Para o primeiro item ativo do Pareto:

1. reproduzir ou obter evidência suficiente da causa;
2. corrigir a fonte responsável, não o sintoma;
3. adicionar teste de regressão ou controle preventivo equivalente;
4. revalidar o maior E2E autorizado;
5. vincular evidências ao SHA atual;
6. observar a janela seguinte;
7. considerar a causa reduzida somente quando o fingerprint deixar de reincidir ou cair de forma objetiva e explicável.

Não executar rerun repetitivo como estratégia de correção sem mudança de precondição.

## Estado atual e prioridade

Antes de abrir nova correção, revalidar se a causa ainda está ativa.

Se outra execução já corrigiu o defeito e os gates atuais estão verdes, registrar a recuperação e retirar a causa do backlog ativo em vez de duplicar trabalho.

Quando existir uma causa sistêmica recorrente que invalide vários projetos ou gates canônicos, ela pode preemptar trabalho funcional conforme a regra de prioridade do projeto. Ruído e controles governados não preemptam por si só.

## Qualidade dos produtores

Logs e workflows devem expor, sem segredos:

- código de erro estável quando viável;
- `correlation_id`;
- SHA/versão/ambiente;
- estado terminal;
- causa sanitizada ou classe de bloqueio;
- relação upstream/downstream quando houver cascata.

`unclassified_failure` é dívida de observabilidade: melhorar o produtor antes de adivinhar causa.

## Segurança e privacidade

- redaction antes de persistir amostras;
- não publicar logs brutos quando contiverem dados sensíveis;
- fingerprint deve ser derivado de assinatura sanitizada;
- segredo, token, credencial, connection string, PII ou conteúdo de negócio desnecessário não pode entrar no relatório;
- relatórios de análise não autorizam mutação administrativa, deploy, merge, produção ou gasto.

## Custo

Aplicar `rules/cost-governance.md`.

`COST_UNKNOWN_BLOCKED` e equivalentes são `control`, não bug. Nunca aumentar gasto, liberar overage ou enfraquecer hard stop para melhorar o indicador de falhas.

## Evidência mínima

Registrar:

- janela analisada;
- fonte;
- total de runs/eventos;
- quantidade por classe;
- fingerprints principais;
- run IDs/SHAs representativos;
- causa ativa ou resolvida;
- correção aplicada;
- teste de regressão;
- E2E;
- reincidência após a correção;
- risco residual e próximo passo.

## Critério de conclusão

A evolução é considerada aplicada quando:

- o Pareto separa causa-raiz de controles/cascatas/ruído;
- a maior causa ativa recebeu correção ou bloqueio explicitamente justificado;
- existe prevenção verificável;
- a validação atual pertence ao SHA final;
- a janela posterior confirma ausência ou redução objetiva da reincidência.

Sem medição posterior, classificar a correção como tecnicamente validada, mas a prevenção como ainda não comprovada longitudinalmente.
