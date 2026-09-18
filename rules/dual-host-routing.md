# Roteamento governado entre hosts

## Objetivo
Usar múltiplos computadores como workers independentes sem compartilhar working tree e sem roteamento para host degradado.

## Preflight obrigatório
Antes de distribuir trabalho entre dois ou mais hosts, coletar evidência atual para cada candidato:
- controller online;
- autenticação válida;
- SHA de regras igual ao SHA canônico exigido;
- `SESSION_LAUNCH_OK`;
- `state_validated=true`;
- Command Gateway funcional;
- worktree limpo;
- worktree solicitado não reservado por outra sessão.

A decisão deve ser fail-closed: qualquer requisito obrigatório ausente torna o host inelegível para aquela tarefa.

## Seleção
Executar `scripts/dual_host_preflight.py` com a evidência coletada.
O roteador considera capacidade declarada e quantidade de tarefas ativas.
Uma preferência de host pode desempatar, mas nunca pode superar um bloqueio de segurança.

## Concorrência
Cada tarefa deve ter `session_id`, `correlation_id`, branch/SHA e worktree próprios.
Não permitir duas sessões gravarem no mesmo working tree do mesmo host.
Hosts distintos podem usar caminhos textualmente iguais porque os sistemas de arquivos são independentes, desde que cada host mantenha reserva exclusiva local.

## Evidência
Persistir ou registrar o JSON de entrada e o JSON de saída do preflight.
O resultado deve identificar `selected_host`, `secondary_host`, hosts bloqueados e respectivos motivos.

## Versão do controller
Diferença de versão do Remote Desktop Commander gera aviso quando os demais requisitos estão íntegros.
Ela só se torna bloqueadora se houver incompatibilidade funcional comprovada ou regra específica mais restritiva.
