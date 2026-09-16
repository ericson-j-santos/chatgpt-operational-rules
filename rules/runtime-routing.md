# Roteamento canônico de runtime

## Escopo
Esta regra se aplica ao TODO Global / AI Control Plane e aos seus jobs, gateway, fila, worker, persistência e validações operacionais.

## Runtime canônico
- `PC24x7 Desktop` é o runtime primário e canônico do TODO Global.
- A implementação canônica usa `docker-compose.pc24x7.yml`, PostgreSQL persistente e o TODO Gateway local supervisionado.
- Diagnóstico, recuperação, deploy, smoke test e E2E devem começar pelo PC24x7.
- Um provedor legado não pode ser usado como evidência de disponibilidade do runtime canônico.

## Render
- Render é legado/contingência para o TODO Global.
- Workflows de Render devem ser somente manuais (`workflow_dispatch`), sem `push`, `schedule` ou outro gatilho automático.
- Não iniciar bootstrap, deploy, reparo de secret ou troubleshooting no Render enquanto o PC24x7 estiver disponível.
- O uso de Render exige indisponibilidade comprovada do PC24x7, causa/evidência registrada e decisão explícita de contingência.
- Após a recuperação do PC24x7, a execução deve retornar ao runtime canônico e a contingência deve ser encerrada.

## Fly.io
- Fly.io não é runtime do TODO Global.
- Não usar Fly.io para deploy, recuperação ou troubleshooting do TODO Global.
- Workloads de outros projetos hospedados no Fly.io não alteram esta regra.

## Anti-regressão
Antes de qualquer ação específica de provedor, identificar o projeto e o `runtime_target`.
Para TODO Global, `runtime_target=pc24x7` por padrão.
Uma alteração que reative execução automática do Render ou introduza Fly.io no caminho normal deve falhar no CI.

## Evidência mínima
A promoção é considerada operacional somente com evidência do PC24x7 no host canônico: containers esperados em execução, `/healthz` e `/readyz` HTTP 200 e E2E aplicável com leitura independente. Falhas devem ser tratadas no PC24x7 antes de acionar contingência.
