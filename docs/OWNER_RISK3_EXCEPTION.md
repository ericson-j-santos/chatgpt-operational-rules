# Exceção local para Risk 3 do proprietário

## Objetivo

Manter o `command_gateway.py` com **Risk 3 negado por padrão** e oferecer uma rota separada somente para ações explicitamente allowlisted pelo proprietário, vinculadas ao usuário + máquina local e com auditoria sem conteúdo sensível.

## Garantias

- O Gateway canônico continua recusando Risk 3.
- A exceção aceita apenas `local` e `dev`; produção não é elegível.
- O comando não vem do chat/CLI: ele é lido de uma configuração local privada.
- `action_id`, `scope`, expiração e fingerprint usuário+máquina precisam coincidir.
- Shells intermediários, metacaracteres, operações destrutivas, billing, `Owner`, `Contributor`, administradores de RBAC e operações `az keyvault secret` permanecem bloqueados.
- Auditoria registra hashes de escopo, comando e saídas; não grava stdout/stderr nem segredos.

## Configuração local

O arquivo real **não deve ser commitado**. Caminho padrão:

- Windows: `%LOCALAPPDATA%\ReqSys\CommandGateway\owner-risk3-exceptions.local.json`
- Linux/macOS: `~/.reqsys-command-gateway/owner-risk3-exceptions.local.json`

Use `config/owner-risk3-exceptions.example.json` apenas como modelo. Gere o fingerprint na própria máquina:

```text
python scripts/owner_risk3_gateway.py --print-owner-fingerprint
```

Copie o hash somente para o arquivo local. Em sistemas POSIX, aplique permissão `600`.

## Execução

```text
python scripts/owner_risk3_gateway.py \
  --action-id azure.kv.role.assignment.add.minimal_dev \
  --scope /subscriptions/.../vaults/... \
  --cwd C:\dev\reqsys-v2-enterprise-real
```

A execução só ocorre se o `action_id` existir na configuração local, o escopo for idêntico e a exceção não tiver expirado.

## Critério de segurança

Qualquer expansão de ambiente, classe de ação ou privilégio exige nova mudança governada. Não usar essa rota para produção, exclusões destrutivas, billing, administração ampla de RBAC ou leitura/exposição direta de segredos.
