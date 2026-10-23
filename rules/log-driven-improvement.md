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
5. construir Pareto por frequência e blast radius, com score simples e explicável;
6. atacar primeiro a maior causa-raiz comprovada que admita correção segura;
7. reproduzir a falha anterior quando seguro e viável;
8. corrigir a causa e a lacuna de prevenção;
9. adicionar teste de regressão ou controle equivalente que detecte reincidência;
10. revalidar no maior E2E autorizado;
11. verificar na janela seguinte se o fingerprint deixou de reincidir;
12. somente então avançar para a próxima causa do Pareto.

## Regras contra falso positivo

- Falhas derivadas como `ADMISSION_BLOCKED` ou gates downstream não contam como causas independentes quando houver erro upstream comprovado.
- Cancelamento não é falha funcional por padrão; concentração de cancelamentos deve ser tratada como sinal de concorrência/event storm.
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
- workflows/repositórios afetados;
- evidência da causa-raiz;
- cascatas descartadas como backlog independente;
- correção aplicada;
- teste preventivo;
- reincidência observada ou ausente na janela seguinte;
- bloqueios e próximo passo.

Aplicar também `rules/e2e-validation.md`, `rules/pre-delivery-validation.md` e `rules/cost-governance.md`.
