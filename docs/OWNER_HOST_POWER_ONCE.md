# Exceção governada de reboot único do proprietário

## Objetivo

Permitir um único reboot local/DEV quando houver autorização explícita do proprietário no chat atual, sem remover o bloqueio permanente de reboot/shutdown/poweroff do `owner_risk3_gateway.py`.

A rota é implementada por `scripts/owner_host_power_once.py` e existe somente para validações operacionais que dependem de reinicialização física.

## Restrições obrigatórias

- somente Windows local/DEV;
- somente operação `reboot`;
- host solicitado deve ser exatamente o host local;
- autorização vinculada a usuário + host por fingerprint;
- janela máxima de 15 minutos;
- `action_id` explícito;
- referência da autorização registrada somente por SHA-256;
- confirmação distinta para autorizar, executar e revogar;
- autorização consumida atomicamente **antes** de submeter o reboot;
- replay da mesma autorização é recusado;
- `shutdown` e `poweroff` continuam indisponíveis;
- o gateway Risk 3 geral continua bloqueando qualquer host-power;
- auditoria local registra autorização, consumo, submissão e revogação.

## Fluxo

```text
autorização explícita no chat
        ↓
authorize (curta duração)
        ↓
config local vinculada a host/usuário
        ↓
execute (consome antes)
        ↓
shutdown.exe /r /t 5
        ↓
reboot
        ↓
validação pós-reboot
        ↓
revoke
```

## Critério de conclusão

A exceção só é considerada encerrada quando:

1. o host retorna;
2. o controlador volta a responder;
3. o runtime esperado volta por autostart;
4. heartbeat/worker ficam elegíveis;
5. o E2E pós-reboot aplicável é executado;
6. a autorização local é revogada;
7. uma tentativa de replay permanece bloqueada.

Esta rota não deve ser usada como substituto para operação normal de host nem ser promovida para HML/STG/PROD.
