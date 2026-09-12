# Bootstrap obrigatório de sessão

Esta regra transforma o uso do Command Gateway em fluxo operacional obrigatório para chats/agentes que executem comandos locais ou remotos.

## Princípio fail-closed

- Antes do primeiro comando técnico local, executar o bootstrap da sessão.
- O bootstrap é a única etapa local permitida antes do Command Gateway.
- Depois do bootstrap, todo comando deve passar pelo Command Gateway.
- Remote Desktop Commander é somente transporte; não pode ser usado para contornar o gateway.
- Se bootstrap, política, gateway, reserva ou validação falhar, interromper a execução.
- Não usar PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito como fallback.
- Plugins/APIs específicas continuam preferíveis para sistemas externos, conforme `rules/terminal-execution.md`.

## Handshake obrigatório

O bootstrap deve produzir evidência equivalente a:

```text
BOOTSTRAP_OK
session_id=<identificador único>
repo=<repositório>
base_head=<sha>
reserved_worktree=<caminho>
state=reserved|materialized
correlation_id=<id>
```

Sem `BOOTSTRAP_OK`, nenhuma execução local subsequente é considerada autorizada pela política.

## Identidade da sessão

- Cada chat/agente recebe `session_id` próprio, com 3 a 64 caracteres alfanuméricos, ponto, hífen ou sublinhado.
- O mesmo `session_id` pode ser reutilizado idempotentemente somente para o mesmo repositório.
- Reutilização do mesmo `session_id` para outro repositório deve falhar.
- A reserva é persistida fora do working tree, no diretório operacional do gateway.

## Reserva de worktree

- O bootstrap reserva um caminho exclusivo derivado do `session_id`.
- O caminho precisa continuar dentro da allowlist do gateway.
- A materialização usa `git worktree add --detach` no SHA capturado no bootstrap.
- A base pode conter arquivos não rastreados, mas não pode ter alterações rastreadas quando o worktree for materializado.
- O worktree criado deve terminar limpo e no mesmo SHA reservado.
- Nunca sobrescrever diretório de worktree já existente sem vínculo válido com a mesma reserva.

## Uso por risco

- Risco 1 pode inspecionar a base ou o worktree da sessão depois do bootstrap.
- Alterações de risco 2 devem ocorrer no worktree reservado/materializado da sessão.
- Risco 3 continua exigindo autorização humana explícita e não é liberado pelo bootstrap.

## Critério de conclusão

O bootstrap está válido somente quando:
- política e gateway são carregados;
- repositório e SHA foram observados;
- reserva exclusiva foi persistida;
- caminho reservado está dentro da allowlist;
- quando materializado, o worktree foi verificado por leitura independente;
- repetição idempotente não cria segunda reserva;
- conflitos e identificadores inválidos falham em testes negativos;
- E2E comprova que o fluxo não depende de fallback de shell.

## Preflight automático

Antes do primeiro `inspect/run`, executar `scripts/session_preflight.py`. O preflight captura host, versão das regras, branch, SHA, digest/contagem do estado Git, reserva e worktree, grava snapshot com SHA-256 e retorna `BOOTSTRAP_OK` somente após revalidar estado estável. Quando `require_preflight_snapshot=true`, o gateway deve bloquear sessão sem snapshot íntegro.

## Session Launcher

- `scripts/session_launcher.py` é a entrada preferencial para iniciar sessões em hosts que já possuem Gateway.
- O launcher exige `rules_version >= 1.5.2`, valida opcionalmente o SHA esperado e gera `session_id` quando ele não for fornecido.
- O launcher sempre chama o preflight com materialização obrigatória; não executa `git fetch`, comandos arbitrários ou mutações de projeto antes da sessão.
- O aceite do launcher deve retornar `SESSION_LAUNCH_OK`, `session_id`, `target_path`, `head`, `snapshot_sha256` e `state_validated=true`.
- Após `SESSION_LAUNCH_OK`, toda execução técnica continua passando pelo Command Gateway e usando o worktree retornado.
- `SESSION_LAUNCH_BLOCKED` é fail-closed e não autoriza fallback para terminal direto.
