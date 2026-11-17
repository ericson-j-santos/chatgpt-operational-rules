# Bootstrap para novos chats

## Pré-verificação de demandas em todos os assuntos

Aplicar a admissão universal de `rules/todo-global.md` antes de criar, continuar ou executar solicitações, mesmo fora de engenharia: pessoas, documentos, pesquisa, mensagens, agenda, automações e integrações. Consultar fontes autorizadas disponíveis, reutilizar a referência canônica e registrar limitações de visibilidade. Suspeita não resolvida bloqueia novos efeitos externos, não leitura ou diagnóstico. Acompanhamento legítimo não é uma duplicação.

## Fluxo obrigatório

Antes de trabalho técnico ou geração de artefatos, consulte o repositório `ericson-j-santos/chatgpt-operational-rules` como fonte canônica das regras operacionais. A instrução explícita do chat atual prevalece.

Para qualquer tarefa que possa exigir terminal local/remoto, o chat deve seguir:

```text
RULES -> PROJECT -> SESSION_PREFLIGHT -> GATEWAY -> INSPECT -> EXECUTE -> VALIDATE -> E2E -> EVIDENCE
```

Regras:
- ler `AGENTS.md`, `rules/terminal-execution.md`, `rules/command-gateway.md` e `rules/session-bootstrap.md`;
- executar `scripts/session_preflight.py` antes do primeiro comando local/remoto; o script chama a reserva/materialização necessária e captura o estado automaticamente;
- exigir `BOOTSTRAP_OK`, `state_validated=true` e snapshot íntegro; registrar `session_id`, `correlation_id`, repositório, SHA, digest de estado e worktree reservado;
- Remote Desktop Commander é somente transporte para o preflight/gateway;
- após o bootstrap, todo comando deve passar pelo Command Gateway;
- não usar PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito como fallback;
- falhar fechado quando bootstrap, política, gateway, reserva ou validação não estiver disponível;
- para risco 2, materializar e usar o worktree exclusivo da sessão antes de editar arquivos.

## Arquivos gerados

- usar Google Drive como armazenamento persistente quando a integração estiver disponível;
- validar o upload;
- devolver o link persistente;
- usar GitHub para código, testes, CI/CD, infraestrutura como código e documentação versionada.

## Limite operacional

O repositório e o bootstrap tornam a regra verificável dentro do ecossistema de agentes que os consultam, mas não modificam por si só a configuração global do produto ChatGPT. Se um executor não carregar estas regras, não existe garantia técnica de que ele as aplique. Por isso, qualquer agente que participe do fluxo deve tratar `BOOTSTRAP_OK` como pré-condição obrigatória de execução.
