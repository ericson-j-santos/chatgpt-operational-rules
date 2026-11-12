# Certificação física do par ReqSys DEV — dois desktops

**Estado desta versão:** contrato de segurança preparado; **sem E2E físico comprovado**, sem ativação de distribuição e sem garantia de perda zero. [Issue operacional](https://github.com/ericson-j-santos/chatgpt-operational-rules/issues/212).

## 1. Identidade e papéis — lista fechada

- **DESKTOP-PDQK954**: \`builder\`, desenvolvimento geral, escreve somente em branch e worktree exclusivos.
- **DESKTOP-RP23OGS**: \`validator\`, executa testes e leitura independente do commit SHA exato, **não escreve no worktree do Builder**.
- **Noteri**: contingência desativada para este par; exige qualificação e autorização específica antes de ser escolhido.
- **Dell OptiPlex 7080**: não faz parte do inventário autorizado e não deve ser oferecido como candidato.

**Fonte do código:** GitHub/main do ReqSys. **Fonte do estado da tarefa:** fila canônica existente. Nenhuma réplica independente de fila, banco ou repositório.

## 2. Bloqueio preventivo antes de dar trabalho

O produtor confiável deve coletar, para **cada** host, evidência autenticada da API GitHub e um recibo íntegro do Command Gateway. Usar \`scripts/reqsys_dual_desktop_guard.py\` para rejeitar inconsistências de papéis, identidade física, runner, SHA, perfil, sessão, integridade, custo, branch, worktree e posse exclusiva. Não usar logs livres ou JSON enviado por tarefa como fonte de autorização.

O verificador é **somente de consistência**: \`pair_candidate_consistent=true\` não comprova autenticidade da evidência nem libera job. Ele retorna **sempre** \`ready_for_work=false\`, \`dispatch_authorized=false\` e \`physical_simultaneity_verified=false\`. A implantação exige integrar o produtor confiável e os consumidores; usar o verificador isolado não impõe controle em máquinas remotas.

Qualquer falta de recibo, job concluído há mais de cinco minutos, regras divergentes, perfil diferente de NORMAL, custo desconhecido ou identidade física não vinculada **bloqueia** o par. Host online/Idle, check verde ou texto de sucesso não substituem isso.

## 3. Proteções obrigatórias contra conflitos e perdas

- **Fila durável e central**: cada tarefa tem identidade, chave de repetição e estado persistido; nenhuma tarefa depende só de RAM ou de arquivos locais.
- **Reserva exclusiva cercada**: apenas um executor pode deter o direito de alterar o estado canônico de uma tarefa. A geração da reserva aumenta na retomada; um executor antigo é impedido de publicar depois de perder a posse.
- **Trabalho isolado**: cada tarefa possui branch e worktree próprios, HEAD/base SHA fixos. Validador recebe uma cópia independente, por conteúdo, e opera sem direito de gravar no código do Builder.
- **Checkpoint imutável fora de ambos os computadores**: publicar por criação condicional, ler bytes de volta de fonte independente, recalcular SHA-256 e só então registrar o checkpoint sob a reserva atual.
- **Replay seguro**: queda antes da confirmação exige reconciliação do estado canônico. Operação incerta não é repetida às cegas. A mesma chave lógica não cria outro efeito, commit, branch ou PR.
- **Integração única**: patch persistido e relido; Builder, Validator e revisão do GitHub devem concordar no SHA da alteração. Sem os gates obrigatórios, sem merge ou publicação.
- **Segurança e custo**: DEV, sem Docker, custo incremental R$ 0,00 comprovado, sem segredos no Git/artefatos, sem GUI, sem alteração administrativa, reboot ou autorização financeira implícita.

Nenhum sistema pode prometer risco absolutamente nulo. A medida de aceite é ausência de duplicações/perdas no E2E com falhas controladas, além da capacidade de bloquear e recuperar corretamente quando houver incerteza.

## 4. Teste físico de aceitação — obrigatório

1. **Qualificar separadamente os dois desktops**: job físico atual no runner nomeado, ID real do runner e máquina, perfil NORMAL, \`SESSION_LAUNCH_OK\`, Gateway, regras SHA, worktree livre, recibo autenticado e leitura independente dos bytes.
2. **Paralelismo verdadeiro**: duas tarefas reais distintas em execução simultânea em duas máquinas diferentes, com intervalos de job comprovadamente sobrepostos, reserva exclusiva e nenhuma alteração cruzada. Os trabalhos não podem usar a mesma tarefa ou mesma branch.
3. **Separação Builder/Validator**: PDQK954 produz commit real; RP23OGS valida exatamente aquele SHA numa cópia isolada e sem permissões de escrita no código original. Confirmar resultado no destino.
4. **Continuidade entre hosts**: o primeiro executor publica um checkpoint durável e libera sua reserva; o outro adquire uma geração nova e retoma **a mesma** tarefa, com releitura independente. Não permitir dois donos simultâneos. A inversão de papéis só é permitida para o teste de recuperação por capacidade/escopo explicitamente autorizado, sem transformar o validador em Builder normal.
5. **Negativos obrigatórios**: mesmo ID de máquina, troca de papel, SHA antigo, lease duplicado, checkpoint adulterado, recibo ausente/stale, custo desconhecido, falha antes e depois da escrita, repetição da mesma chave e Validator tentando gravar. Verificar rejeição **e ausência de efeito** no destino.
6. **Evidência**: registrar para cada execução run/job/runner IDs, host, commit SHA, versão de regras, digests, timestamps, estado anterior e posterior, causa de falha, readback externo, rollback e responsável. Só marcar operacional quando todos os requisitos aplicáveis passarem no mesmo SHA/ambiente.

**Canônico para a prova física:** [E2E Platform #10](https://github.com/ericson-j-santos/e2e-platform/issues/10).

## 5. Próxima intervenção Pareto e responsáveis

1. **Runtime Platform**: restaurar a prontidão e comprovante físico de \`DESKTOP-RP23OGS-runtime\`, atendendo ao bloqueio S4U/LocalSystem de [Desktop Runtime #147](https://github.com/ericson-j-santos/desktop-pc24x7-runtime/issues/147) por canal governado, com backup e sem reconfiguração improvisada de serviço.
2. **Runtime Platform**: atualizar os recibos físicos do \`DESKTOP-PDQK954\` no transporte GitHub Actions; Remote Desktop Commander offline não comprova máquina desligada.
3. **Engineering Control Plane**: integrar o gate de papéis no consumidor, o controle de posse exclusiva na fila e testes obrigatórios, sem alterar o roteamento genérico do Noteri.
4. **E2E Platform**: executar certificação independente em dois hosts físicos e publicar readback de simultaneidade, checkpoint, retomada e casos negativos.
5. **Proprietário**: somente autorizações específicas para ações administrativas, instalação, credenciais, deploy ou despesas quando forem indispensáveis.

**Proibido tratar esta documentação, os testes locais ou a abertura de uma PR como certificação física.**
