# Exceção local para Risk 3 do proprietário

## Objetivo

Manter o `command_gateway.py` com **Risk 3 negado por padrão** e oferecer duas rotas restritas a `local/DEV`:

1. allowlist privada por `action_id` para operação crítica individual;
2. **modo de desenvolvimento temporário** para acelerar a evolução até o estado padrão ouro sem cadastrar cada `action_id` separadamente.

A decisão operacional vigente é usar o modo de desenvolvimento enquanto a solução estiver sendo construída e reativar a exigência normal da allowlist antes de qualquer transição para HML/STG/PROD.

## Garantias permanentes

Mesmo com o modo de desenvolvimento ativo:

- HML, STG e PROD permanecem bloqueados;
- reboot, shutdown e poweroff permanecem bloqueados;
- operações destrutivas permanecem bloqueadas;
- billing permanece bloqueado;
- `Owner`, `Contributor`, `User Access Administrator` e administração ampla de RBAC permanecem bloqueados;
- valores/opções explícitas de token, senha, client secret ou API key permanecem bloqueados;
- shells intermediários e metacaracteres permanecem bloqueados;
- o executor aceita apenas script Python versionado em repositório Git limpo;
- o script precisa estar dentro da raiz Git corrente;
- a execução continua auditada por `correlation_id`, hash do comando, HEAD Git e SHA-256 do script;
- stdout/stderr são registrados somente por hash na auditoria do Risk 3.

## Modo de desenvolvimento até padrão ouro

O modo DEV remove **somente** a obrigação de pré-cadastrar cada `action_id` na allowlist privada.

Ele não transforma Risk 3 em terminal irrestrito.

Condições obrigatórias:

- ambiente `local` ou `dev`;
- `reason=standard_gold_development`;
- validade máxima de 30 dias por ativação;
- diretório dentro dos roots permitidos;
- worktree Git limpo;
- script Python rastreado pelo Git;
- nenhuma promoção para ambiente não DEV;
- auditoria preservada.

Se o desenvolvimento ultrapassar 30 dias, a janela pode ser renovada explicitamente enquanto o estado continuar sendo desenvolvimento. Expiração reativa automaticamente o comportamento fail-closed.

### Ativar

```text
python scripts/set_owner_risk3_development_mode.py \
  --enable \
  --days 14 \
  --confirm ENABLE-RISK3-DEV-MODE-STANDARD-GOLD
```

### Consultar

```text
python scripts/set_owner_risk3_development_mode.py --status
```

### Reativar a proteção normal

```text
python scripts/set_owner_risk3_development_mode.py \
  --disable \
  --confirm DISABLE-RISK3-DEV-MODE
```

Após a desativação, ações não cadastradas voltam a falhar com `action_id não está allowlisted localmente`.

## Gate de saída do desenvolvimento

Antes de considerar a solução pronta para HML/STG/PROD, executar e evidenciar:

- testes unitários/integrados aplicáveis verdes;
- E2E real aplicável verde no SHA vigente;
- controles negativos contra falso positivo;
- segurança/segredos sem regressão;
- idempotência e cleanup quando aplicáveis;
- observabilidade/evidência suficiente;
- `development_mode.status=disabled`;
- reexecução negativa comprovando que ação Risk 3 não allowlisted voltou a ser bloqueada.

A reativação do Risk 3 é parte do critério de conclusão do padrão ouro, não uma melhoria opcional posterior.

## Configuração local

O arquivo real **não deve ser commitado**. Caminho padrão:

- Windows: `%LOCALAPPDATA%\ReqSys\CommandGateway\owner-risk3-exceptions.local.json`
- Linux/macOS: `~/.reqsys-command-gateway/owner-risk3-exceptions.local.json`

O arquivo continua vinculado ao fingerprint usuário + máquina.

## Allowlist individual

Quando o modo DEV estiver desativado, a execução segue o modelo tradicional:

```text
python scripts/owner_risk3_gateway.py \
  --action-id azure.kv.role.assignment.add.minimal_dev \
  --scope /subscriptions/.../vaults/... \
  --cwd C:\dev\reqsys-v2-enterprise-real
```

A execução só ocorre se `action_id`, escopo, expiração e fingerprint coincidirem.

## Execução durante modo DEV

Para uma ação não cadastrada individualmente, o comando precisa vir após `--` e ser um script Python versionado:

```text
python scripts/owner_risk3_gateway.py \
  --action-id reqsys.exemplo.dev \
  --scope repo://reqsys/environment/dev \
  --cwd C:\dev\reqsys-v2-enterprise-real \
  -- python scripts/acao_governada.py --confirm ACAO-DEV
```

## Critério de segurança

O modo DEV existe para reduzir atrito de desenvolvimento, não para reduzir o perímetro de segurança de ambientes superiores. Qualquer expansão para HML/STG/PROD, operação destrutiva, reboot/shutdown, billing ou administração ampla exige mudança governada própria e autorização correspondente.
