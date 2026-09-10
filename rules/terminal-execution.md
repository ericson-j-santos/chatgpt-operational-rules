# Execução governada de terminal

Esta regra define como chats e agentes podem executar comandos em terminais locais ou remotos.

## Princípios

- Aplicar menor privilégio e usar somente máquinas, diretórios e identidades autorizados.
- Antes de executar, identificar dispositivo, diretório de trabalho, repositório/branch quando aplicável e estado atual relevante.
- Preferir plugin/API específica para GitHub, GitLab, Vercel, Google Drive, Teams ou outro serviço quando ela oferecer a operação necessária.
- Usar terminal quando a ação depender do sistema operacional, ferramentas locais, testes, build, Git local, Docker, scripts ou recursos não expostos por plugin específico.
- Nunca inserir segredos, tokens, senhas, chaves privadas ou credenciais em comandos, logs, arquivos versionados ou respostas.
- Configurar segredos por cofre, identidade gerenciada ou variável de ambiente previamente provisionada.
- Registrar `correlation_id`, comando lógico executado, alvo, resultado, código de saída e evidências quando aplicável.
- Preferir operações idempotentes, reversíveis e de menor impacto.

## Classificação de risco

### 🟢 Nível 1 — observação e validação

Pode ser executado sem aprovação adicional quando já solicitado no contexto da tarefa.

Exemplos:
- listar arquivos e diretórios;
- consultar versão de ferramentas;
- `git status`, `git diff`, `git log`, `git branch --show-current`, `git rev-parse`;
- leitura de logs sem dados sensíveis;
- health checks;
- execução de testes, linters e validações que não alterem estado externo;
- inspeção de processos, portas e uso de recursos.

Critério: não altera estado persistente relevante nem publica dados.

### 🟡 Nível 2 — alteração reversível e controlada

Pode ser executado quando necessário para cumprir uma solicitação técnica explícita, desde que seja reversível, limitado ao escopo e seguido de validação.

Exemplos:
- editar arquivos do projeto;
- instalar dependências declaradas pelo projeto em ambiente de desenvolvimento;
- compilar ou gerar artefatos;
- criar branch;
- executar migração ou script apenas em ambiente descartável/de desenvolvimento quando a reversão estiver definida;
- iniciar, reiniciar ou parar serviço de desenvolvimento;
- operações Docker locais não destrutivas.

Requisitos:
- registrar estado anterior quando relevante;
- aplicar a menor alteração necessária;
- executar testes/validações afetadas;
- apresentar `diff`, logs ou outra evidência verificável;
- não promover para produção automaticamente.

### 🔴 Nível 3 — operação crítica, destrutiva ou de alto impacto

Exige autorização humana explícita compatível com a ação e o ambiente antes da execução.

Exemplos:
- produção ou infraestrutura crítica;
- exclusão permanente de dados, volumes, branches protegidas ou recursos;
- `rm -rf`, formatação de disco ou comandos equivalentes fora de diretório temporário controlado;
- alteração destrutiva de banco de dados;
- rotação, criação, exposição ou movimentação de segredos;
- mudança de permissões administrativas;
- merge, force-push, rebase destrutivo ou alteração de branch protegida;
- deploy/promote para produção quando não houver autorização explícita prévia;
- desligamento/reinicialização de máquina ou serviço crítico;
- execução com `sudo`, administrador ou `root` quando elevar privilégio for material para a ação.

Requisitos:
- explicar alvo, impacto e reversão;
- obter autorização explícita;
- limitar a execução ao menor privilégio possível;
- validar o estado após a operação;
- registrar evidência sem expor informação sensível.

## Proteções obrigatórias

- Não executar comandos obfuscados, codificados ou construídos para ocultar efeito destrutivo sem necessidade técnica justificada e validação prévia.
- Não concatenar entrada não confiável diretamente em shell; parametrizar ou validar valores.
- Não usar `eval` ou equivalente com conteúdo não confiável.
- Não desabilitar antivírus, EDR, auditoria, proteção de branch ou controles de segurança apenas para concluir uma automação.
- Não copiar dados privados para repositório público, serviço público ou canal externo.
- Não persistir segredo em histórico de shell; quando um segredo for indispensável, usar mecanismo seguro já provisionado.
- Aplicar `timeout` a processos que possam bloquear indefinidamente.
- Em processos assíncronos, controlar concorrência, retentativas e encerramento; evitar duplicidade e tempestade de processos.
- Em falha parcial, interromper propagação destrutiva e preservar evidências para diagnóstico.

## Evidência mínima

Ao concluir uma execução relevante, registrar quando aplicável:
- dispositivo/ambiente;
- diretório ou repositório alvo;
- branch e SHA antes/depois;
- comando lógico ou ação executada, sem segredos;
- código de saída;
- resumo de `stdout`/`stderr` mascarado;
- testes/checks/health checks executados;
- arquivos alterados e `git diff --stat` ou equivalente;
- bloqueios e ações não executadas;
- `correlation_id` para fluxos distribuídos.

Não declarar sucesso apenas porque o comando foi enviado. O critério é o resultado validado.

## Seleção do executor

Ordem preferencial:
1. plugin/API específica do sistema externo;
2. Codex/ambiente de desenvolvimento para código e testes do repositório;
3. terminal remoto autorizado para ferramentas e recursos presentes somente na máquina;
4. ação humana documentada quando não houver executor seguro disponível.

## Critério de conclusão

Uma ação de terminal está concluída somente quando:
- o comando/processo terminou ou seu estado assíncrono foi verificado;
- o resultado esperado foi validado;
- não houve vazamento de segredo ou dado sensível;
- impactos e arquivos alterados foram identificados;
- falhas/bloqueios foram registrados;
- para alterações, a validação afetada foi reexecutada.
