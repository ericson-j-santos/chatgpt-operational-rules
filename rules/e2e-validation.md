# Validação ponta a ponta sem falsos positivos

Esta regra define a validação mínima obrigatória para incrementos técnicos criados ou modificados por chats e agentes.

## Regra principal

- Todo incremento funcional deve receber validação ponta a ponta no maior escopo executável disponível.
- A validação deve exercitar o fluxo real desde a entrada relevante até o efeito observável final.
- Não considerar sucesso apenas por compilação, teste unitário, código de saída zero, HTTP 2xx, ausência de exceção ou mensagem de log.
- Se uma dependência externa impedir a validação ponta a ponta, registrar o bloqueio e manter o estado como não validado; não declarar conclusão funcional.
- Mudanças exclusivamente documentais podem ser validadas por consistência estrutural, links, esquema, lint e evidência equivalente, sem simular um fluxo funcional inexistente.

## Controles obrigatórios contra falso positivo

1. Confirmar pré-condições e estado anterior antes do teste.
2. Usar identificador único de execução, `correlation_id`, marcador de teste ou equivalente quando aplicável.
3. Vincular todas as evidências ao mesmo incremento, branch/SHA, ambiente e janela temporal.
4. Executar ao menos um caso positivo que prove o efeito esperado.
5. Executar caso negativo ou de controle que prove que comportamento indevido não ocorreu, quando houver comportamento condicional, filtro, bloqueio, autorização ou supressão.
6. Validar o efeito de negócio ou estado persistido por uma leitura independente após a ação.
7. Não reutilizar evidência antiga, cache, execução anterior ou saída sem vínculo verificável com a execução atual.
8. Em fluxos assíncronos, aguardar com limite definido e consultar o estado até conclusão; não interpretar ausência imediata de erro como sucesso.
9. Em operações idempotentes, repetir a mesma entrada quando aplicável e verificar ausência de duplicidade ou efeito adicional indevido.
10. Quando tecnicamente seguro e viável, executar um teste do próprio teste: alterar uma expectativa de forma controlada, injetar falha conhecida ou usar entrada deliberadamente inválida para comprovar que o mecanismo de validação detecta falhas.
11. Restaurar qualquer alteração temporária usada no teste do teste e reexecutar a validação real.
12. Nenhuma evidência pode depender exclusivamente de texto produzido pela própria implementação sob teste quando existir uma fonte independente de verdade.

## Exemplos de falso positivo proibido

- endpoint retorna `200`, mas o registro esperado não foi persistido;
- workflow termina verde, mas a etapa relevante foi ignorada, pulada ou executada contra outro SHA;
- mensagem de sucesso aparece em log, mas o efeito externo não ocorreu;
- teste procura qualquer registro existente em vez do registro criado pela execução atual;
- teste de notificação valida somente que o fluxo disparou, sem provar entrega ou supressão conforme a regra;
- teste E2E passa porque encontrou evidência residual de uma execução anterior;
- mock, stub ou ambiente simulado é apresentado como evidência de integração real sem identificação explícita.

## Critérios por tipo de fluxo

### APIs e persistência

- validar requisição, resposta e efeito persistido;
- realizar leitura independente do recurso alterado;
- validar também ausência de alteração quando a regra determina bloqueio ou rejeição.

### CI/CD e repositórios

- vincular execução ao SHA exato;
- conferir que a etapa relevante realmente executou e não foi `skipped`;
- revalidar checks após qualquer alteração;
- não usar check verde de commit anterior como evidência.

### Integrações e notificações

- validar origem, processamento e destino final;
- usar marcador único para distinguir a execução;
- quando houver filtro, executar par positivo/negativo;
- exemplo: tarefa normal notifica e tarefa de teste bloqueada não notifica.

### Processos assíncronos, filas e automações

- usar `correlation_id` ou identificador equivalente;
- verificar transição de estados até estado terminal;
- validar retentativas, duplicidade e quarentena/DLQ quando afetadas;
- aplicar espera limitada e nunca aguardar indefinidamente.

### Interface de usuário

- executar o caminho real do usuário quando a alteração afetar comportamento observável;
- confirmar estado antes/depois e efeito persistido;
- não aceitar apenas renderização sem erro como prova de funcionamento.

## Evidência mínima

Registrar, quando aplicável:
- objetivo e critério de aceite;
- ambiente;
- branch e SHA;
- identificador único da execução;
- pré-condição;
- entrada utilizada;
- caso positivo;
- caso negativo/controle;
- resultado observado na fonte independente;
- teste do próprio teste, quando executado;
- código de saída, run/job/check ou evidência equivalente;
- bloqueios;
- riscos residuais de falso positivo.

## Critério de conclusão

Um incremento funcional só pode ser declarado concluído quando:
- a validação ponta a ponta aplicável foi executada;
- a evidência pertence à versão atual;
- o efeito esperado foi observado;
- os controles negativos aplicáveis foram aprovados;
- nenhuma evidência residual ou ambígua sustenta o resultado;
- riscos conhecidos de falso positivo foram eliminados ou explicitamente registrados como bloqueio;
- qualquer alteração temporária de validação foi revertida;
- o estado final foi novamente verificado.

Na ausência dessas condições, usar estado `parcialmente validado`, `bloqueado` ou `não validado`, conforme a evidência.
