# Inventário local PostgreSQL/TODO Gateway

`scripts/todo_gateway_local_inventory.py` é uma sonda Windows somente leitura,
destinada a uma sessão já admitida pelo Session Launcher e executada pelo
Command Gateway em risco 1.

## Contrato

- consulta somente nomes fixos de serviços PostgreSQL `postgresql-x64-12` a
  `postgresql-x64-18` pela API SCM;
- consulta somente as portas 5432 e 8000 pela API IP Helper e não emite PID ou
  endereço;
- não ativa nesta versão as fontes de identidade baseadas em executáveis,
  registro, marcador de tarefa ou runtime; elas retornam
  `filesystem_identity_not_activated` antes de qualquer I/O;
- emite somente booleanos de presença para `DATABASE_URL`,
  `TODO_GATEWAY_TOKEN`, `NOTION_TOKEN` e `NOTION_DATA_SOURCE_ID`;
- nunca abre conexão PostgreSQL, executa SQL, lê arquivo de credencial ou inicia
  serviço, tarefa, processo ou WSL;
- não abre conexão HTTP ou socket; `/healthz` e `/readyz` ficam
  `not_observed` até haver identidade local verificável e capacidade separada;
- limita a tabela TCP pelo tamanho realmente alocado e reportado;
- preserva contagem e escopo de bind observados no TCP mesmo quando a associação
  de identidade permanece separadamente `not_observed`;
- diferencia `observed`, `not_observed` e ausência apenas nas fontes e nomes
  conhecidos; nenhuma ausência limitada é apresentada como ausência global;
- falha fechado e publica somente códigos de erro sanitizados.

Como as fontes de identidade estão deliberadamente desativadas, o backend nativo
produz `status: partial` e o CLI encerra com código 3. Isso é o resultado esperado
deste contrato reduzido, não indicação para retry ou ampliação de capacidade.

## Ativação física

A presença do script não autoriza coleta. A execução física exige autorização
específica para o host, `SESSION_LAUNCH_OK`, `state_validated=true`, SHA completo
e chamada pelo Command Gateway. O resultado é evidência de inventário, não
autorização para instalar PostgreSQL, criar banco, iniciar o gateway ou aplicar
DDL.

Os testes em `tests/test_todo_gateway_local_inventory.py` usam somente fakes e
não consultam SCM, registro, processos, portas, caminhos operacionais do host ou
HTTP reais; leem apenas os próprios arquivos do repositório para carga e inspeção
estática. Eles cobrem falhas parciais, limites TCP, fontes de identidade
desativadas e comprovação de que a importação não dispara I/O operacional. O E2E
físico deve ser registrado separadamente quando for autorizado.
