# Runtime Platform / PC24x7

## Objetivo

Fornecer a camada de execução física e de runtime necessária aos produtos e ao
Engineering Control Plane, preferencialmente com custo adicional zero.

## Responsabilidade

A Runtime Platform é infraestrutura de sustentação, não produto funcional.

Escopo obrigatório:

- Desktop PC24x7 como runtime preferencial quando tecnicamente compatível;
- Noteri e modo NORMAL/ESTUDO;
- runners self-hosted;
- Command Gateway e sessões governadas;
- Remote Desktop Commander exclusivamente como transporte;
- endpoints/ingress DEV e Cloudflare quando aplicável;
- startup, recovery, health/readiness e circuit breaker;
- infraestrutura física necessária aos workers.

## Fronteiras

Não colocar nesta frente:

- regras de negócio ou funcionalidades do ReqSys Produto;
- regras genéricas de CI, Governed Merge Queue, Builder/Validator, Worker Pool,
  locks/watchdog transversal ou outras capacidades compartilhadas do Engineering
  Control Plane.

Componentes compartilhados devem ser consumidos por contrato, não duplicados nos
repositórios específicos de host.

## Repositórios-alvo

A separação da infraestrutura física deve convergir para:

- `ericson-j-santos/noteri-runtime`;
- `ericson-j-santos/desktop-pc24x7-runtime`.

Enquanto a migração não estiver validada, o código operacional existente no
ReqSys não deve ser removido apenas para completar a separação estrutural.

## Regras operacionais

1. Aplicar `rules/runtime-routing.md`: PC24x7-first para workloads compatíveis.
2. Antes do primeiro comando local/remoto, aplicar
   `rules/session-bootstrap.md` e exigir estado validado.
3. Todo comando após bootstrap deve passar pelo Command Gateway.
4. Remote Desktop Commander nunca pode ser usado como fallback de digitação,
   clipboard, mouse, GUI ou terminal irrestrito.
5. Falhar fechado quando alvo, sessão, gateway, callback ou pós-condição não
   forem comprovados.
6. Exit code 0 ou heartbeat não comprovam sucesso funcional.
7. Runtime legado, inclusive Fly.io, não pode reaparecer silenciosamente como
   fallback normal quando a rota canônica for PC24x7.
8. Segredos devem permanecer fora de Git, chat, logs e artifacts.

## Critério de evidência

Um incremento só conta como operacional quando houver, conforme aplicável:

- processo/container esperado em execução;
- health/readiness verificáveis;
- endpoint DEV estável quando houver consumo externo;
- restart/recovery validado;
- E2E no mesmo SHA da implementação;
- leitura independente do efeito;
- controle negativo/replay/idempotência quando aplicável;
- evidência vinculando ambiente, host, SHA, correlation_id e execução.

## Migração por host

### Noteri

Migrar primeiro os componentes exclusivos do Noteri.

Critério mínimo: E2E `NORMAL -> ESTUDO -> NORMAL` no host Noteri, no mesmo SHA
do novo repositório, com leitura independente do estado e sem GUI/RDC como
fallback de comando.

### Desktop PC24x7

Migrar depois os componentes exclusivos do Desktop.

Critério mínimo: health/readiness, restart/recovery, endpoint DEV aplicável e E2E
no mesmo SHA do novo repositório.

## Prioridade

Atuar sob demanda por bloqueio real de produto ou plano de engenharia. Evitar
expansão de infraestrutura sem requisito objetivo; preferir reaproveitamento da
capacidade existente e custo adicional zero.
