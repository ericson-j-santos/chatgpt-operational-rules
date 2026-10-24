# Melhoria contínua orientada por logs e falhas

Esta regra torna a análise de falhas uma etapa recorrente dos projetos, sem transformar ruído operacional em backlog falso.

## Fluxo obrigatório

Quando houver logs, falhas de CI, incidentes, retries, cancelamentos recorrentes ou telemetria equivalente disponível:

1. coletar evidência atual vinculada ao repositório, ambiente, SHA, run/job e janela temporal;
2. sanitizar segredos e dados pessoais antes de persistir, publicar ou agregar;
3. normalizar mensagens voláteis e gerar fingerprint estável por causa observável;
4. separar:
   - causa-raiz candidata;
   - efeito cascata de gate/dependência upstream;
   - ruído de orquestração, cancelamento ou concorrência;
5. deduplicar incidentes lógicos sem apagar reincidência real:
   - em eventos ligados à mesma mudança (`push`, PR ou merge group), o mesmo repositório + SHA + fingerprint conta como um incidente;
   - execuções agendadas/manuais distintas continuam separadas, mesmo no mesmo SHA;
6. construir Pareto por incidentes lógicos, frequência e blast radius, com score simples e explicável;
7. atacar primeiro a maior causa-raiz comprovada que admita correção segura;
8. reproduzir a falha anterior quando seguro e viável;
9. corrigir a causa e a lacuna de prevenção;
10. adicionar teste de regressão ou controle equivalente que detecte reincidência;
11. revalidar no maior E2E autorizado;
12. verificar na janela seguinte se o fingerprint deixou de reincidir;
13. somente então avançar para a próxima causa do Pareto.

## Regras contra falso positivo

- Falhas derivadas como `ADMISSION_BLOCKED` ou gates downstream não contam como causas independentes quando houver erro upstream comprovado.
- Cancelamento só pode ser classificado como ruído de concorrência quando houver evidência de sucessor/supersessão ou causa equivalente. Cancelamento sem sucessor comprovado permanece `unverified_cancellation` até diagnóstico.
- `unclassified_failure` indica deficiência de observabilidade. Melhorar o diagnóstico sanitizado antes de inferir causa.
- CI verde após correção não basta: exigir teste de regressão e efeito funcional aplicável.
- O score nunca autoriza merge, deploy, produção, permissão administrativa, gasto ou ação destrutiva.

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
- top fingerprints;
- incidentes lógicos e critério de deduplicação;
- workflows/repositórios afetados;
- evidência da causa-raiz;
- cascatas descartadas como backlog independente;
- cancelamentos sem sucessor ainda não classificados como ruído;
- correção aplicada;
- teste preventivo;
- reincidência observada ou ausente na janela seguinte;
- bloqueios e próximo passo.

Aplicar também `rules/e2e-validation.md`, `rules/pre-delivery-validation.md` e `rules/cost-governance.md`.
