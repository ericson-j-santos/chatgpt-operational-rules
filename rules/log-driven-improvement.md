# Melhoria contínua orientada por logs e falhas

Esta regra torna a análise de falhas uma etapa recorrente dos projetos, sem transformar ruído operacional em backlog falso.

## Fluxo obrigatório

Quando houver logs, falhas de CI, incidentes, retries, cancelamentos recorrentes ou telemetria equivalente disponível:

1. coletar evidência atual vinculada ao repositório, ambiente, SHA, run/job e janela temporal;
2. sanitizar segredos e dados pessoais antes de persistir, publicar ou agregar;
3. normalizar mensagens voláteis e gerar fingerprint estável por causa observável;
4. classificar cada sinal em exatamente uma classe:
   - `root_cause`: defeito ou condição técnica corrigível; entra no Pareto;
   - `control`: bloqueio governado esperado, como `COST_UNKNOWN_BLOCKED`, autorização humana ausente ou PR draft fora da fila; não entra no Pareto e não autoriza contorno;
   - `cascade`: efeito downstream ligado à causa upstream; não abre incidente independente;
   - `noise`: cancelamento, deduplicação ou supersessão comprovada; cancelamento sem sucessor permanece `unverified_cancellation`;
5. deduplicar incidentes lógicos sem apagar reincidência real:
   - em eventos ligados à mesma mudança (`push`, PR ou merge group), o mesmo repositório + SHA + fingerprint conta como um incidente;
   - execuções agendadas/manuais distintas continuam separadas, mesmo no mesmo SHA;
6. revalidar se a causa ainda está ativa no estado atual; se outra execução já corrigiu o defeito e os gates atuais estão verdes, registrar a recuperação e retirá-la do backlog ativo;
7. construir Pareto somente com `root_cause` ativa, considerando frequência, severidade, impacto observável e blast radius;
8. atacar primeiro a maior causa-raiz comprovada que admita correção segura;
9. reproduzir a falha anterior quando seguro e viável;
10. corrigir a causa e a lacuna de prevenção;
11. adicionar teste de regressão ou controle equivalente que detecte reincidência;
12. revalidar no maior E2E autorizado;
13. verificar na janela seguinte se o fingerprint deixou de reincidir;
14. somente então avançar para a próxima causa do Pareto.

## Regras contra falso positivo

- Falhas derivadas como `ADMISSION_BLOCKED` ou gates downstream não contam como causas independentes quando houver erro upstream comprovado.
- Cancelamento só pode ser classificado como ruído de concorrência quando houver evidência de sucessor/supersessão ou causa equivalente. Cancelamento sem sucessor comprovado permanece `unverified_cancellation` até diagnóstico.
- `unclassified_failure` indica deficiência de observabilidade. Melhorar o diagnóstico sanitizado antes de inferir causa.
- CI verde após correção não basta: exigir teste de regressão e efeito funcional aplicável.
- Rerun sem mudança material de precondição não é correção e não multiplica o incidente.
- `control`, `cascade` e `noise` não preemptam trabalho funcional nem entram no Pareto de bugs.
- O score nunca autoriza merge, deploy, produção, permissão administrativa, gasto ou ação destrutiva.

## Qualidade dos produtores

Logs e workflows devem expor, sem segredos:

- código de erro estável quando viável;
- `correlation_id`;
- SHA, versão e ambiente;
- estado terminal;
- causa ou classe sanitizada;
- relação upstream/downstream quando houver cascata.

`unclassified_failure` é dívida de observabilidade: melhorar o produtor antes de inferir causa.

## Automação preferencial

Quando existir SHA integrado e validado do `ericson-j-santos/observability-platform`, preferir a ação reutilizável de failure intelligence fixada por SHA imutável.

Enquanto essa capacidade ainda não estiver integrada, executar análise equivalente por API/plugin, preservando o mesmo contrato conceitual.

A automação deve ser orientada a eventos após falha relevante. Reconciliação periódica pode existir como rede de segurança em baixa frequência; não criar polling ruidoso.

## Custo e segurança

- custo adicional máximo: R$ 0,00;
- não habilitar recurso pago para obter telemetria;
- não publicar logs brutos quando contiverem potencialmente segredo, PII ou payload sensível;
- token usado para coleta não pode ser encaminhado a hosts de storage por redirect;
- relatórios devem conter apenas evidência sanitizada suficiente para diagnóstico.

## Estado e evidência

Toda análise deve registrar, quando aplicável:

- janela analisada;
- quantidade de execuções/falhas/cancelamentos;
- quantidade por classe operacional;
- top fingerprints;
- incidentes lógicos e critério de deduplicação;
- workflows/repositórios afetados;
- evidência da causa-raiz e estado ativo ou recuperado;
- cascatas descartadas como backlog independente;
- cancelamentos sem sucessor ainda não classificados como ruído;
- correção aplicada;
- teste preventivo;
- reincidência observada ou ausente na janela seguinte;
- bloqueios e próximo passo.

Aplicar também `rules/e2e-validation.md`, `rules/pre-delivery-validation.md` e `rules/cost-governance.md`.

## Critério de conclusão

Sem janela posterior observada, classificar a correção como tecnicamente validada, mas a prevenção como ainda não comprovada longitudinalmente.
