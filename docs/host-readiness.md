# Contrato mínimo de prontidão de hosts

## Objetivo

Evitar inventário completo ou cópia cega do Noteri ao preparar outro computador.
O estado desejado é declarado em `config/host-readiness.json`; cada host deve
verificar apenas o que falta para cumprir esse contrato.

Para arquitetura, procedimento completo, inventário mínimo e relatório de
compatibilidade antes da homologação, consulte
[`new-computer-onboarding.md`](new-computer-onboarding.md). O coletor
`scripts/host_onboarding_report.py` pode processar inventário obtido por canal
já autorizado sem instalar nada no novo host. Sem controlador ou Gateway,
o único primeiro comando local permitido continua sendo o bootstrap canônico;
o inventário local deve vir **após** ele e o preflight governado.

## Fluxo Pareto

1. Escolher o perfil do host: `noteri` ou `desktop-pc24x7`.
2. Validar o contrato com `scripts/host_readiness.py --profile <perfil>`.
3. Para host novo sem Command Gateway, usar exclusivamente
   `scripts/install_command_gateway_host.py` no SHA canônico aprovado e exigir
   `HOST_BOOTSTRAP_OK`.
4. Executar `scripts/session_preflight.py` e exigir `BOOTSTRAP_OK` com estado validado.
5. Instalar/configurar somente capacidades marcadas como ausentes.
6. Executar o E2E específico do runtime no host real e registrar evidência no mesmo SHA.

## O que não fazer

- não clonar toda a configuração do Noteri;
- não inventariar software sem relação com uma capacidade requerida;
- não copiar segredos ou credenciais entre máquinas;
- não desativar firewall, antivírus ou controles para fazer o bootstrap passar;
- não usar GUI/RDC/terminal irrestrito como fallback do Gateway;
- não considerar exit code 0, heartbeat ou HTTP 2xx como prova final isolada.

## Repositórios

- Regras e contrato compartilhado: `ericson-j-santos/chatgpt-operational-rules`.
- Runtime exclusivo do Noteri: `ericson-j-santos/noteri-runtime`.
- Runtime exclusivo do Desktop: `ericson-j-santos/desktop-pc24x7-runtime`.
- Orquestração/worker compartilhado: `ericson-j-santos/reqsys-engineering-orchestrator`.

## Uso do validador

O validador não executa comandos no host nem lê segredos. Ele valida a declaração
de evidências fornecida por um coletor/automação governado:

```bash
python scripts/host_readiness.py --profile desktop-pc24x7 --evidence host-evidence.json
```

Sem `--evidence`, ele imprime as capacidades exigidas e retorna estado
`needs_evidence`. Com evidência, cada capacidade deve estar explicitamente
`true`; ausência ou valor diferente de `true` falha fechado.

Exemplo de evidência não sensível:

```json
{
  "profile": "desktop-pc24x7",
  "capabilities": {
    "command_gateway": true,
    "session_preflight": true,
    "github_runner": true,
    "runtime_health": true,
    "restart_recovery": true
  }
}
```

A evidência real do E2E continua pertencendo ao repositório específico do runtime.
