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

- Zero produz `ready=false`, `selected_executor=null` e
  `remote_quota_blocked=true`, inclusive em recuperação de host.
- Cota ausente produz `remote_quota_unknown`, sem inventar uma renovação.
- Reserva só se aplica a `0 < remote_calls_left_pct <= 20`.
- Um bloqueio anterior requer `remote_quota_renewal_confirmed=true`,
  `quota_renewal_source` identificando a evidência oficial independente e
  `quota_blocked_at < quota_renewed_at <= quota_observed_at`, sem datas futuras.
  Todas as datas devem conter fuso explícito.
- Quando `reset_at` for conhecido, ele deve ser válido e não posterior à
  renovação comprovada. `unknown` nunca é convertido em data estimada.
- Conectores nativos continuam prioritários e disponíveis mesmo com RDC bloqueado.
  `preferred_native_executor` não pode disfarçar RDC como executor não local.
- `ready=true` significa elegibilidade de rota, não prova de execução remota.
  Erro de entrada deve substituir eventual decisão positiva residual no arquivo
  de saída; erro de gravação deve retornar falha, nunca sucesso.

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
