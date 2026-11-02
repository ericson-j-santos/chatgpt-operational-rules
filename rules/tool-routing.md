# Roteamento Pareto de ferramentas e cotas

## Objetivo

Retirar ferramentas com cota/custo do caminho crítico quando existe executor nativo mais direto, mantendo TinyFish e Remote Desktop Commander como contingências especializadas.

## Ordem canônica

1. plugin/API específica do sistema externo;
2. busca web nativa para pesquisa pública;
3. browser/cloud automation nativa quando disponível;
4. TinyFish somente quando a tarefa realmente exige sua capacidade e houver saldo utilizável;
5. Remote Desktop Commander somente quando a ação depender da máquina física, sistema operacional, arquivos/processos locais ou do transporte até o Command Gateway;
6. ação humana somente quando nenhum executor seguro estiver disponível.

Não usar TinyFish ou Remote Desktop Commander para GitHub, Drive, Gmail, Calendar, pesquisa pública ou outra operação que possua executor nativo adequado.

## Continuidade gratuita de monitoramentos sem agendamento no ChatGPT

**Aplicação:** rotinas de pesquisa, acompanhamento e notificação quando o ChatGPT não permite criar ou manter uma tarefa (limite do plano, indisponibilidade, permissões ou custo). Esta regra escolhe uma rota; **não habilita agendamentos nem altera sistemas por si só**. A decisão do proprietário está registrada na [issue #191](https://github.com/ericson-j-santos/chatgpt-operational-rules/issues/191).

Ordem de decisão, preservando `rules/cost-governance.md`:

1. **Inventariar antes de criar:** verificar automações e workflows existentes, escopo, periodicidade, fonte, último resultado real e notificações anteriores. Reaproveitar um agendamento capaz de atender ao requisito, sem duplicar produtores, trabalhos ou avisos.
2. **GitHub Actions gratuito (primeira contingência):** preferir um workflow existente no repositório público, com runner padrão hospedado pelo GitHub e documentação oficial vigente comprovando custo incremental R$ 0,00. Se precisar de novo workflow, usar somente o fluxo governado de issue/branch/PR/gates; agendamento real só ocorre depois da integração autorizada à branch padrão. Não presumir que armazenamento de artefatos, cache, executores especiais, repositório privado ou integrações externas sejam gratuitos. Se houver limite de armazenamento ou possibilidade de overage sem bloqueio, não usar esses recursos.
3. **Computadores próprios (segunda contingência):** somente se GitHub Actions não atender ou estiver bloqueado, selecionar PC24x7, Noteri ou host autorizado, com evidência atual de energia/rede, runner e etiquetas, dependências operantes, cota e custo zero. Exigir `SESSION_LAUNCH_OK`, `state_validated=true`, sessão/worktree exclusivos e Command Gateway; observar `rules/runtime-routing.md`, `rules/session-bootstrap.md` e `rules/dual-host-routing.md` quando aplicáveis. Nunca contornar indisponibilidade com shell irrestrito, RDC bloqueado, Docker, Fly.io ou provedor pago.
4. **Falhar fechado:** se nenhuma rota puder ser comprovada segura e gratuita, registrar `BLOQUEADO`, evidência atual, dono, impedimento e próxima ação no TODO Global; não simular execução nem solicitar gasto.

**Exclusividade e troca de rota.** Um monitor lógico deve ter um único publicador ativo ou coordenação por lease/CAS e `idempotency_key` estável compartilhados. Antes de ligar o substituto, validar estado e checkpoint atuais; comprovar cenário positivo, negativo, replay sem duplicação e leitura independente no destino. Desativar apenas o escopo antigo sobreposto, nunca outros acompanhamentos agrupados; manter reversão documentada. `scheduled`, workflow verde ou HTTP 2xx isolados não comprovam entrega nem recebimento de alerta.

**Qualidade da agenda.** GitHub Actions `schedule` depende da branch padrão, pode sofrer atraso/perda em horários congestionados e, em repositórios públicos sem atividade por 60 dias, pode ser desabilitado. Usar horários desencontrados do início da hora, vigilância de última execução, limite de atraso e reconciliação idempotente. Não declarar SLA contínuo sem medição.

**Critério de alerta.** Para monitores de produto (por exemplo, Power BI/SQL Server/Power Automate no ReqSys), citar somente mudança material confirmada em fonte oficial com data/versão, impacto, risco e adaptação mínima segura. Distinguir hipótese e uso real do tenant. Na ausência de novidade comprovada, permanecer silencioso. Custo adicional máximo R$ 0,00 e nenhuma alteração de produção implícita.

## TinyFish

- saldo `<= 0`: indisponível para novas execuções pagas;
- saldo `> 0`: contingência, nunca rota padrão para pesquisa pública quando web nativa estiver disponível;
- browser automation deve preferir navegador/cloud automation nativo antes de TinyFish;
- não tentar compensar falta de saldo com Remote Desktop em tarefa que não dependa da máquina física.

## Remote Desktop Commander

Remote Desktop é transporte especializado, não executor genérico.

- tarefas externas com API/plugin: Remote não entra na rota;
- tarefas locais: Remote pode ser usado somente com cota positiva, sem bloqueio pendente e controlador/host elegível;
- quando `0 < remote_calls_left_pct <= 20`, ativar `reserve_mode`: usar Remote apenas para tarefas intrinsecamente locais, recuperação do controlador/host e transporte para bootstrap/Command Gateway;
- agrupar diagnóstico e execução local em scripts governados para reduzir round-trips;
- host/controller offline deve seguir `rules/dual-host-routing.md`; ausência de controller não prova que o PC está desligado.
- sucesso técnico da ferramenta (`exit code 0`, chamada MCP concluída ou `result=ok`) **não** é evidência funcional suficiente; toda operação RDC que afete roteamento/recuperação deve ser normalizada por `scripts/rdc_semantic_result.py`.
- `NO_CALLBACK`, `SESSION_LAUNCH_BLOCKED`, `matched=[]`, `controls=[]`, flag funcional falsa ou ausência de testemunho semântico devem produzir falha fechada e impedir retry imediato.
- retries de recuperação devem respeitar o circuit breaker persistente do transporte; não criar loops paralelos de retry em UI, scheduler e runner.

### Bloqueio prioritário por cota — RDC_QUOTA_MEMORY_POLICY

Decisão explícita do proprietário registrada na issue #117. Esta proibição
prevalece sobre modo de reserva, recuperação de host e qualquer fallback:

1. Antes de selecionar RDC, conferir a evidência vigente da conta e o último
   bloqueio persistido. A cota exposta em `remote_calls_left_pct` é de chamadas
   Remote MCP; não confundir com tokens do modelo. Evidência antiga não é saldo atual.
2. Se `remote_calls_left_pct <= 0`, **NÃO UTILIZAR RDC**: não executar comandos,
   sondas, recuperação, leitura de arquivos ou transporte. Cota desconhecida
   também não autoriza execução. Não repetir chamadas para tentar desbloquear.
3. Usar integração/API oficial, ambiente de desenvolvimento/Codex ou, quando
   aplicáveis, GitHub Actions e Command Gateway por transporte independente do RDC.
   Sem alternativa autorizada, registrar bloqueio; não encaminhar automaticamente
   para sonda ou recuperação pelo próprio RDC.
4. Consultar renovação no painel/API oficial da conta, por rota independente de
   operações remotas. A consulta não é exceção implícita para consumir RDC bloqueado.
   Não comprar créditos, alterar assinatura ou assumir gasto adicional.
5. Registrar data/hora e fuso oficiais em `reset_at`. Quando a fonte não expuser
   a data, usar `reset_at=unknown`. Não deduzir renovação da virada do mês,
   da data de cadastro, de saldo histórico ou do plano anunciado.
6. Após esgotamento, só retomar com renovação efetiva confirmada em fonte oficial
   e nova leitura de saldo positivo, respeitando a data oficial quando conhecida.
   A passagem de uma data, saldo positivo isolado ou memória de outro chat
   **não** apagam um bloqueio anterior.
7. Sem data oficial, exigir evidência independente da renovação efetiva e da
   leitura positiva posterior ao bloqueio. Sem essa prova, manter o bloqueio.
8. Após liberação, RDC apenas volta a ser elegível para dependência física.
   Permanecem os controles de host, sucesso funcional, sessão e Command Gateway.

### Contrato do roteador e conservação do bloqueio

`scripts/tool_router.py` avalia entradas e grava a decisão com `--output`; não
consulta a conta, não executa ferramentas e não mantém estado global sozinho.
O chat/worker chamador deve ler o último checkpoint e conservar
`remote_quota_blocked` e `quota_blocked_at` entre execuções. Omitir ou apagar
um bloqueio conhecido para obter rota positiva é proibido.

- Zero ou valor negativo produz `ready=false`, `selected_executor=null` e
  `remote_quota_blocked=true`, inclusive em recuperação de host.
- Cota ausente produz `remote_quota_unknown`, sem inventar uma renovação.
- Reserva só se aplica a `0 < remote_calls_left_pct <= 20`.
- Saldo positivo somente torna RDC elegível quando `quota_observed_at` for uma
  data/hora com fuso, não futura e observada nos últimos 15 minutos, e
  `quota_observation_source` identificar a fonte da leitura. Essa janela é uma
  validade operacional conservadora da leitura, não uma data de reset/renovação
  da conta; o roteador nunca inventa `reset_at`. O chamador deve obter a evidência
  imediatamente antes da decisão e não pode reaproveitar uma leitura histórica.
- Um bloqueio anterior requer `remote_quota_renewal_confirmed=true`,
  `quota_renewal_source` identificando a evidência oficial independente e
  `quota_blocked_at < quota_renewed_at <= quota_observed_at`, sem datas futuras.
  Todas as datas devem conter fuso explícito.
- Quando `reset_at` for conhecido, ele deve pertencer ao ciclo atual:
  `quota_blocked_at < reset_at <= quota_renewed_at`. `unknown` nunca é
  convertido em data estimada.
- Cada nova observação `<= 0` atualiza o marco do bloqueio somente para diante;
  o CLI conserva o timestamp mais recente entre payload e checkpoint. Portanto,
  replay antigo e novo esgotamento após liberação não podem reutilizar evidência
  temporal de ciclo anterior. Sem timestamp válido, o bloqueio conserva um marco
  válido já conhecido; sem marco anterior, o checkpoint fica incompleto e nenhuma
  data é inventada.
- Conectores nativos continuam prioritários e disponíveis mesmo com RDC bloqueado.
  `preferred_native_executor` aceita somente IDs nativos conhecidos pelo roteador;
  casing, espaços, pontuação, sufixos ou aliases não podem disfarçar RDC como
  executor não local.
- `ready=true` significa elegibilidade de rota, não prova de execução remota.
  O CLI deve conservar monotonicamente um bloqueio já presente em `--output`:
  erro de entrada, checkpoint ilegível ou tentativa de apagar a flag não libera
  RDC. Checkpoint ilegível persiste `remote_quota_checkpoint_invalid=true`; payload
  comum não limpa esse marcador nem pode reconstruir datas perdidas. A liberação
  exige recuperação explícita de um checkpoint confiável por fluxo governado.
  Bloqueio sem `quota_blocked_at` persiste
  `remote_quota_checkpoint_incomplete=true`; somente uma nova observação recente,
  com fonte identificada e cota `<= 0` pode preencher o marco, nunca um payload
  positivo ou evento histórico.
  Erro de entrada também substitui eventual decisão positiva residual; erro de
  gravação retorna falha, nunca sucesso.

O percentual e as datas usados nos testes são fixtures sintéticas. Nenhum saldo
de conta deve ser fixado neste documento ou tratado como autorização permanente.

### Persistência quando a memória nativa estiver indisponível

Para decisões explícitas já autorizadas, conservar o texto em issue/PR neste
repositório e incorporá-lo à regra aplicável por mudança validada. Confirmar a
gravação por leitura independente. Uma issue registra a decisão; a presença
na `main` confirma sua incorporação à fonte canônica.

Não afirmar que um registro no GitHub alterou a memória nativa, instruções
personalizadas ou configurações do ChatGPT. Não prometer que abrir outro chat
resolverá indisponibilidade de gravação. Outros chats/agentes devem consultar
esta regra no bootstrap; armazenamento externo, isoladamente, não comprova
carregamento ou aplicação automática.

Não registrar segredos, credenciais, identificadores privados de conta ou dados
pessoais desnecessários. Manter o estado evidenciado separado da política.

## Fail-closed

Se uma tarefa de escrita externa não possuir plugin/API autorizado, não converter automaticamente para web scraping/browser automation.

Após comprovar elegibilidade de cota, se uma tarefa local exigir Remote e nenhum host/controlador elegível estiver disponível, retornar bloqueio com `dual_host_preflight`/recuperação governada como próximo passo.

## Evidência

Registrar quando aplicável:

- `task_type`;
- executor selecionado;
- capacidades observadas;
- saldo TinyFish;
- percentual restante do Remote, `quota_observed_at` e fonte oficial;
- `remote_quota_blocked`, `quota_blocked_at`, `reset_at` e evidência de renovação;
- motivo do fallback/bloqueio;
- `correlation_id`.

A decisão deve ser reproduzível por `scripts/tool_router.py`.
