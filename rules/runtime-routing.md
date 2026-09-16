# Roteamento canônico de runtime

## Escopo
Esta regra se aplica à seleção de runtime para serviços, workers, filas, gateways, brokers, automações e demais workloads dos projetos do usuário, salvo quando uma regra específica do projeto exigir outra infraestrutura.

## Princípio PC24x7-first
Antes de contratar, criar ou recuperar um runtime pago adicional, verificar primeiro se o `PC24x7 Desktop` existente atende ao workload.

O PC24x7 deve ser a rota preferencial quando todos estes requisitos forem compatíveis:
- execução supervisionável por Docker/serviço equivalente;
- persistência local durável com backup/restauração definidos;
- endpoint HTTPS estável quando acesso externo for necessário;
- segredos mantidos fora do Git/chat e injetados por arquivo protegido, cofre ou mecanismo equivalente;
- restart automático após falha/reboot;
- isolamento e disponibilidade suficientes para o ambiente e criticidade do workload;
- health/readiness e E2E verificáveis.

Se esses requisitos forem atendidos, usar `runtime_target=pc24x7` por padrão e reaproveitar a infraestrutura existente antes de adicionar provedor pago.

## Quando não usar PC24x7
Um runtime gerenciado pode ser escolhido quando houver evidência objetiva de pelo menos um dos casos abaixo:
- PC24x7 indisponível ou incapaz de atender requisito técnico obrigatório;
- necessidade de SLA, redundância, isolamento, elasticidade ou localização incompatível com o host local;
- requisito regulatório, de segurança ou continuidade que proíba/torne inadequado o self-hosted;
- regra específica do projeto/ambiente determinando outro provedor;
- decisão explícita do usuário por contingência ou arquitetura diferente.

Registrar causa, evidência e impacto antes de provisionar novo runtime pago. Não tratar conveniência ou hábito de provedor como justificativa suficiente.

## TODO Global / AI Control Plane
- `PC24x7 Desktop` é o runtime primário e canônico.
- A implementação canônica usa `docker-compose.pc24x7.yml`, PostgreSQL persistente e o TODO Gateway local supervisionado.
- Diagnóstico, recuperação, deploy, smoke test e E2E devem começar pelo PC24x7.
- Um provedor legado não pode ser usado como evidência de disponibilidade do runtime canônico.

## ReqSys
Para workloads auxiliares compatíveis, especialmente em DEV — por exemplo brokers, workers, gateways, filas e executores — avaliar e preferir o PC24x7 antes de criar serviço pago adicional.

Essa preferência não promove automaticamente HML/PROD para o PC24x7. Gates específicos de ambiente, continuidade, segurança e governança permanecem obrigatórios.

## Render
- Para TODO Global, Render é legado/contingência.
- Para outros workloads, Render deve ser tratado como opção gerenciada posterior à avaliação PC24x7-first, salvo regra específica em contrário.
- Workflows de contingência do TODO Global devem ser somente manuais (`workflow_dispatch`), sem `push`, `schedule` ou outro gatilho automático.
- Não iniciar bootstrap, deploy, reparo de secret ou troubleshooting no Render para um workload elegível ao PC24x7 enquanto o PC24x7 estiver disponível.
- O uso de Render como contingência exige causa/evidência registrada e decisão explícita quando houver custo ou mudança arquitetural relevante.
- Após a recuperação do PC24x7, a execução deve retornar ao runtime canônico quando aplicável.

## Fly.io
- Fly.io não é runtime do TODO Global.
- Para outros projetos, somente usar Fly.io quando a regra específica do projeto/ambiente o mantiver como runtime vigente ou quando houver decisão explícita baseada em requisitos.
- Não introduzir Fly.io como novo caminho normal apenas por convenção histórica se o workload for elegível ao PC24x7.

## Anti-regressão
Antes de qualquer ação específica de provedor:
1. identificar projeto, ambiente e `runtime_target`;
2. verificar a regra específica do projeto;
3. avaliar elegibilidade do PC24x7;
4. registrar por que um provedor pago é necessário quando o PC24x7 seria tecnicamente possível.

Uma alteração que reative execução automática de contingência ou introduza provedor pago no caminho normal sem justificativa deve falhar nos controles aplicáveis.

## Evidência mínima
A promoção de um workload no PC24x7 só conta como operacional quando houver evidência compatível com sua criticidade, incluindo:
- processo/container esperado em execução;
- `/healthz` e `/readyz` HTTP 200 quando existirem;
- persistência comprovada após restart quando houver estado durável;
- endpoint público estável quando consumidores externos dependerem dele;
- segredo não exposto em Git, chat, logs ou artifacts;
- E2E aplicável com leitura independente e controle contra falso positivo/replay.

Falhas devem ser tratadas no PC24x7 antes de acionar contingência quando a regra do projeto permitir.
