# Recuperação control-plane-first

## Princípio

Quando um host já possui qualquer plano de controle, agente, worker, endpoint de
administração ou canal governado funcional, esse canal deve ser tratado como a
rota primária de recuperação e evolução.

Não pedir ao usuário para copiar comandos, baixar launchers, abrir TeamViewer,
usar GUI remota ou executar bootstrap manual enquanto existir uma rota de
controle programática capaz de ser estendida com segurança.

## Ordem obrigatória

1. Revalidar o plano de controle existente e suas capabilities reais.
2. Usar capability já instalada para recovery/refresh quando disponível.
3. Se a capability necessária não existir, implementar primeiro a menor
   extensão permanente do plano de controle que a disponibilize.
4. Validar a extensão por E2E, leitura independente, replay/idempotência e
   controle negativo.
5. Executar recovery/refresh pelo plano de controle e validar o efeito físico.
6. Somente quando o runtime legado for tecnicamente incapaz de receber a
   extensão por qualquer canal governado existente, admitir bootstrap manual
   como migração única.
7. O bootstrap manual de migração deve instalar uma capacidade permanente de
   auto-recovery/auto-refresh; repetir bootstrap manual em ciclos futuros é
   regressão arquitetural.

## Contrato mínimo da capacidade permanente

O host deve expor uma capability versionada de manutenção que:

- aceite somente alvo/host esperado;
- aceite somente DEV/local quando não houver autorização específica para outro
  ambiente;
- fixe ou valide SHA/digest da versão de destino;
- não aceite shell, comando arbitrário, URL arbitrária ou caminho arbitrário;
- seja idempotente;
- preserve backup/rollback;
- registre `correlation_id`, versão anterior, versão alvo e resultado;
- faça readback independente de health/readiness e capabilities após atualização;
- falhe fechado em divergência de host, SHA, digest, política ou pós-condição.

## Guardrail de instalação

Todo instalador de runtime persistente deve instalar desde a primeira versão uma
superfície de recovery/refresh separada do ciclo normal do worker. Atualizar o
worker não pode depender de o próprio worker já possuir a versão que se pretende
instalar.

CI deve falhar quando o pacote/runtime destinado a host persistente perder a
capacidade permanente de recovery/refresh ou o teste de upgrade legado ->
corrente.

## Critério de conclusão

Recuperação estrutural só está concluída quando:

- a capability permanente está instalada;
- o host pode ser atualizado sem GUI, clipboard ou intervenção manual;
- um replay da mesma versão não causa nova mutação;
- uma versão/alvo inválido falha fechado;
- o host retorna ao Command Gateway/session bootstrap normal após a recuperação;
- a evidência física pertence ao mesmo SHA/ambiente da atualização.
