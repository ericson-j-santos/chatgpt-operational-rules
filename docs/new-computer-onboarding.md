# Arquitetura e continuidade: integração de um novo computador

> Guia de consulta para outro chat, agente ou computador. **Nunca** interpretar
> este guia como autorização para instalar, elevar privilégios, alterar produção
> ou registrar um host desconhecido automaticamente.

## Ponto de entrada

Fonte canônica: https://github.com/ericson-j-santos/chatgpt-operational-rules
(branch `main`).

O agente deve receber o repositório e o identificador do computador. Depois:

1. Ler `README.md`, `AGENTS.md`, a regra específica em `projects/` e as
   regras em `rules/` pertinentes, sempre na versão aprovada de `main`.
2. Identificar o host exato, o papel pretendido e o ambiente. Não assumir que
   um nome cadastrado significa máquina conectada ou homologada.
3. Identificar o controlador/API governado já existente antes de qualquer comando.
4. Levantar **somente** as capacidades relevantes ao papel escolhido.
5. Gerar o relatório de compatibilidade; executar apenas as correções ausentes
   e autorizadas, mantendo rollback e evidências.
6. Validar `HOST_BOOTSTRAP_OK` quando o Gateway for instalado, seguido de
   `SESSION_LAUNCH_OK` / `BOOTSTRAP_OK` e `state_validated=true`, conforme
   o caminho de sessão vigente.
7. Executar E2E físico no próprio host e registrar o resultado por SHA, ambiente,
   sessão, `correlation_id` e leitura independente.

Este documento explica a arquitetura; as decisões operacionais atuais devem
ser consultadas nas regras canônicas e não inferidas de um texto histórico.

## Arquitetura compartilhada

```text
Usuário / ChatGPT / agentes / ferramentas oficiais
                  |
          Regras operacionais (main)
                  |
       TODO Global (estado canônico)
        Event Gateway / fila durável
                  |
       Engineering Control Plane
  orchestrator / Worker Pool / locks / watchdog
           GitHub / PR / CI / E2E
                  |
       Runtime Platform / PC24x7
  Desktop principal | Desktop secundário | Noteri
         runners / serviços / Gateway
                  |
       Produtos e integrações
  ReqSys / Power Platform / OCR / APIs / dados
```

Limites de responsabilidade:

- `chatgpt-operational-rules`: política, ferramentas de execução e contratos.
- `reqsys-v2-enterprise-real`: produto ReqSys; não absorver controlador físico.
- `reqsys-engineering-orchestrator`: coordenação, fila e roteamento de agentes.
- `engineering-worker-pool`: leases, concorrência, watchdog e contratos de worker.
- `desktop-pc24x7-runtime`: manutenção, supervisão e recovery dos desktops.
- `noteri-runtime`: modos NORMAL/ESTUDO e particularidades do Noteri.

Os repositórios específicos de host não devem duplicar o Worker Pool.
Migrações do ReqSys para componentes independentes devem preservar a origem
até equivalência testada no mesmo SHA.

## Hosts conhecidos e novos

A política de hosts atualmente publicada em
`desktop-pc24x7-runtime/scripts/pc24x7_host_policy.py` reconhece
`DESKTOP-PDQK954`, `DESKTOP-RP23OGS` e `NOTERI`, com papéis distintos.
**Essa declaração não comprova conexão, instalação, capacidade de processamento
ou teste E2E atual.**

Para novo host, selecionar o papel, inventariar suas capacidades e registrar a
identidade por mudança versionada e revisada na política aplicável. Não copiar
a identidade de outra máquina para obter elegibilidade. A publicação de uma
capability no registry também não dispensa prova de execução no host.

O `config/host-readiness.json` define requisitos por perfil:

- `noteri`: Gateway, preflight, runner, runtime health; modo de ESTUDO opcional.
- `desktop-pc24x7`: os anteriores e recuperação/restart.

O perfil é a **função**, não uma declaração de que todos os computadores usam
o mesmo Windows, disco, instalador, serviço ou conjunto de aplicações.

## Inventário Pareto + relatório de compatibilidade

Instrumentos:

- `scripts/host_onboarding_report.py` — inventário mínimo e avaliação.
- `scripts/host_readiness.py` — verifica contrato e evidências booleanas.
- `config/host-readiness.json` — fonte versionada dos requisitos.

Inventário limitado: hostname, sistema operacional, arquitetura, CPUs lógicas,
memória total, capacidade do volume do SO e presença **não executada** dos CLIs
Python/Git/Docker. Não captura usuário, IP, MAC, serial, processos, arquivos
pessoais, credenciais, variáveis de ambiente, tokens nem conteúdo dos serviços.

A presença de Git/Docker **não prova** que o runner, Docker daemon, API,
sessão, controle ou recovery estejam operacionais. A ausência do CLI Docker
também não prova incompatibilidade com todos os perfis.

**Caminho A — já existe controlador/API governado acessível (pré-instalação)**

1. Obter inventário por uma capability de leitura já autorizada; nunca executar
   shell ou script arbitrário para contornar o Gateway.
2. Normalizar a evidência no esquema JSON abaixo. Relatórios de inventário são
   locais/protegidos, não devem ser commitados.
3. Avaliar o JSON com `--inventory` num executor que já possua uma sessão
   governada. Isso produz lacunas e requisitos antes de instalar o novo host.

**Caminho B — host realmente novo, sem controlador/Gateway**

A política `rules/session-bootstrap.md` **não permite executar o coletor
localmente como primeiro comando**. O único bootstrap inicial permitido no host
Windows é `scripts/install_command_gateway_host.py` com SHA completo aprovado
e SHA-256 esperado do instalador. Depois de `HOST_BOOTSTRAP_OK`, realizar o
preflight e usar Command Gateway para coletar os dados. Para outros sistemas,
a instalação depende de adaptação específica e autorização; não executar o
instalador Windows em Linux. Este é um limite real da segurança, não um passo
que se possa pular.

**Invocação após bootstrap/Gateway governado:**

```text
python scripts/host_onboarding_report.py --profile desktop-pc24x7 --collect-local --source-sha <SHA_COMPLETO_APROVADO> --output <ARQUIVO_LOCAL_PROTEGIDO>
```

**Invocação pré-instalação com inventário já obtido por API autorizada:**

```text
python scripts/host_onboarding_report.py --profile desktop-pc24x7 --inventory <INVENTARIO_JSON_LOCAL> --source-sha <SHA_COMPLETO_APROVADO> --output <RELATORIO_JSON_LOCAL>
```

O segundo caso processa um arquivo de entrada **fora do computador novo**:
não executa código nele. O arquivo JSON esperado é:

```json
{
  "schema_version": 1,
  "hostname": "HOST-TESTE",
  "os": "Windows",
  "architecture": "AMD64",
  "logical_cpus": 6,
  "memory_gib": 16.0,
  "disk_total_gib": 256.0,
  "disk_free_gib": 128.0,
  "tools_detected": {"python": true, "git": true, "docker": false}
}
```

Valores desconhecidos de capacidade podem ser `null`; sinalizar ausência
é preferível a inventar. Campos adicionais são rejeitados para impedir
retransmissão involuntária de dados privados. O arquivo `--output` deve
permanecer fora do Git e em diretório protegido. Replay idêntico é permitido;
diferença de conteúdo no mesmo caminho bloqueia a sobrescrita.

O relatório distingue:

- inventário observável;
- ferramenta detectada (não equivale a serviço saudável);
- capabilities requeridas sem evidência válida;
- estado de compatibilidade para triagem;
- decisão de trabalho `ready_for_work=false`, até homologação independente.

Mesmo com evidência declarativa de todas as capabilities, o resultado é
`awaiting_physical_e2e` e **não** autoriza instalar nem executar trabalho.
O `host_readiness.py` continua sendo o validador do contrato declarativo.
A decisão definitiva depende de `dual_host_preflight.py`, sessão válida,
execução real e E2E do repositório específico do host.

## Sequência para homologar um novo host

1. **Estado:** identificar host e SO, controladores existentes, contexto de
   projeto, regras/HEAD/PR atuais e recursos necessários. Não pedir segredo bruto.
2. **Perfil:** selecionar NOTERI ou desktop somente pela função; ajustar política
   versionada se a identidade ainda não estiver aprovada.
3. **Preflight:** executar inventário por canal governado disponível e avaliar
   lacunas. Sem canal, aplicar a exceção única de bootstrap inicial.
4. **Preparação:** priorizar reaproveitar serviços e instalações; nunca clonar
   indiscriminadamente o Noteri ou reconfigurar sem necessidade.
5. **Sessão:** exigir `SESSION_LAUNCH_OK`, `state_validated=true`, regras no SHA
   aprovado e worktree exclusivo. Após bootstrap todo comando passa pelo Gateway.
6. **Runtime:** registrar runner e capabilities conforme contratos existentes;
   aplicar startup, health/readiness, recovery e proteção de segredos.
7. **E2E:** provar a capacidade no host real e no SHA atual, casos positivo e
   negativo, replay sem duplicidade, e leitura independente do efeito esperado.
8. **Roteamento:** somente depois tornar o host elegível ao plano de controle.
   O roteamento multi-host deve falhar fechado para controlador ou sessão inválida.
9. **Checkpoint:** publicar estado, SHA, run/job, `correlation_id`, testes,
   bloqueios e próximo passo em superfície persistente governada.

## Segurança, continuidade e custos

- Prioridade: plugin/API oficial → ferramenta nativa → Codex/ambiente de
  desenvolvimento → transporte remoto especializado → ação humana indispensável.
- Não usar RDC como terminal genérico nem contornar cota, bootstrap ou Gateway.
- Risco 1: leitura/diagnóstico; risco 2: mudança reversível validada; risco 3:
  autorização explícita para alvo/ambiente e política aplicável.
- Não ativar Fly.io: está proibido em `rules/runtime-routing.md` desde
  02/10/2026. PC24x7-first para cargas compatíveis; não inferir HML/PROD.
- Falha de heartbeat isolada não comprova que o computador está desligado.
- Após 300 segundos sem **progresso material**, aplicar watchdog: mudar rota
  segura ou bloquear, não manter tarefas vivas apenas renovando lease.
- Segredos ficam em cofre/ambiente protegido, fora de Git, chat e artifacts.
- Só declarar concluído com E2E no ambiente/versão alvo e efeito independente.

## Entrega de continuidade a outro chat/agente

Fornecer:

1. URL canônica acima e o SHA de `main` recém-confirmado.
2. Nome do host e papel pretendido, sem dados privados de acesso.
3. Identificador do checkpoint/PR/issue mais recente (se houver).
4. Link do relatório de inventário **protegido** e suas evidências governadas.

A ordem é **descobrir o que existe → avaliar o que falta → corrigir o mínimo
seguro → homologar → disponibilizar para execução**. Não reconstruir do zero.
