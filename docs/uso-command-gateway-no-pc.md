# Uso do Command Gateway no PC

## Regra principal

Para qualquer trabalho técnico local/remoto, o ponto de entrada é `scripts/session_preflight.py`.
Não executar comandos técnicos diretamente em PowerShell, CMD, Bash, WSL ou SSH como fallback.

Fluxo obrigatório:

`session_preflight.py -> BOOTSTRAP_OK -> session_id -> worktree reservado -> command_gateway.py`

Em host novo sem Gateway, a única exceção é `install_command_gateway_host.py`: ele exige SHA completo aprovado e SHA-256 do próprio instalador, valida o manifesto, instala o runtime, cria um repositório de validação no commit exato e então o fluxo volta imediatamente ao preflight.

Se o preflight não retornar `BOOTSTRAP_OK` com `state_validated=true`, interromper a execução.

## 1. Iniciar uma sessão governada

Exemplo para ReqSys:

```bat
python scripts\\session_preflight.py ^
  --policy config\\command-gateway.policy.json ^
  --repo C:\\dev\\reqsys-v2-enterprise-real ^
  --session-id chat-reqsys-001 ^
  --correlation-id chat-reqsys-001-bootstrap ^
  --materialize
```

A saída deve conter:

- `result = BOOTSTRAP_OK`;
- `state_validated = true`;
- `session_id`;
- `target_path`;
- `snapshot_sha256`;
- `reservation_status = materialized` para tarefas que possam alterar arquivos.

Use o `target_path` retornado como diretório das operações da sessão.

## 2. Inspecionar pelo gateway

```bat
python scripts\\command_gateway.py ^
  --policy config\\command-gateway.policy.json ^
  --correlation-id chat-reqsys-001-inspect ^
  inspect ^
  --cwd <TARGET_PATH> ^
  --session-id chat-reqsys-001
```

Sem `session_id`/snapshot válidos, o gateway deve bloquear com código 25 (`BOOTSTRAP_REQUIRED`).

## 3. Executar comando de risco 1

```bat
python scripts\\command_gateway.py ^
  --policy config\\command-gateway.policy.json ^
  --correlation-id chat-reqsys-001-read ^
  run ^
  --cwd <TARGET_PATH> ^
  --session-id chat-reqsys-001 ^
  --risk 1 ^
  -- git status --short
```

Risco 1 não pode modificar o estado do repositório. Se modificar, o gateway deve bloquear como falso positivo.

## 4. Executar alteração de risco 2

Risco 2 deve ocorrer somente no worktree materializado retornado pelo preflight:

```bat
python scripts\\command_gateway.py ^
  --policy config\\command-gateway.policy.json ^
  --correlation-id chat-reqsys-001-change ^
  run ^
  --cwd <TARGET_PATH> ^
  --session-id chat-reqsys-001 ^
  --risk 2 ^
  -- <COMANDO_AUTORIZADO>
```

Executar risco 2 na árvore base deve ser recusado.

## Como não esquecer em novos chats

Todo novo chat/agente técnico deve executar, antes do primeiro comando:

1. carregar `README.md`, `AGENTS.md` e as regras aplicáveis;
2. gerar um `session_id` exclusivo;
3. executar `session_preflight.py`;
4. exigir `BOOTSTRAP_OK` e `state_validated=true`;
5. usar somente `command_gateway.py` para comandos seguintes;
6. usar o worktree da sessão para qualquer risco 2;
7. não fazer fallback para terminal direto se o gateway bloquear.

O launcher não é a fronteira de segurança. A fronteira é o próprio gateway com `require_session_bootstrap=true` e `require_preflight_snapshot=true`.

## Critério operacional

Uma sessão está pronta somente quando:

```text
BOOTSTRAP_OK
state_validated=true
session_id=<id único>
reservation_status=reserved|materialized
snapshot_sha256=<hash válido>
```

Para alterações, exigir adicionalmente:

```text
reservation_status=materialized
cwd=<worktree reservado>
```
