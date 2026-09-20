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
- tarefas locais: Remote pode ser usado se controlador/host necessário estiver elegível;
- quando `remote_calls_left_pct <= 20`, ativar `reserve_mode`: usar Remote apenas para tarefas intrinsecamente locais, recuperação do controlador/host e transporte para bootstrap/Command Gateway;
- agrupar diagnóstico e execução local em scripts governados para reduzir round-trips;
- host/controller offline deve seguir `rules/dual-host-routing.md`; ausência de controller não prova que o PC está desligado.
- sucesso técnico da ferramenta (`exit code 0`, chamada MCP concluída ou `result=ok`) **não** é evidência funcional suficiente; toda operação RDC que afete roteamento/recuperação deve ser normalizada por `scripts/rdc_semantic_result.py`.
- `NO_CALLBACK`, `SESSION_LAUNCH_BLOCKED`, `matched=[]`, `controls=[]`, flag funcional falsa ou ausência de testemunho semântico devem produzir falha fechada e impedir retry imediato.
- retries de recuperação devem respeitar o circuit breaker persistente do transporte; não criar loops paralelos de retry em UI, scheduler e runner.

## Fail-closed

Se uma tarefa de escrita externa não possuir plugin/API autorizado, não converter automaticamente para web scraping/browser automation.

Se uma tarefa local exigir Remote e nenhum host/controlador elegível estiver disponível, retornar bloqueio com `dual_host_preflight`/recuperação governada como próximo passo.

## Evidência

Registrar quando aplicável:

- `task_type`;
- executor selecionado;
- capacidades observadas;
- saldo TinyFish;
- percentual restante do Remote;
- motivo do fallback/bloqueio;
- `correlation_id`.

A decisão deve ser reproduzível por `scripts/tool_router.py`.
