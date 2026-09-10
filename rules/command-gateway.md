# Command Gateway local

Esta regra define o mecanismo operacional para executar comandos governados em máquinas autorizadas.

## Estado alvo

O gateway deve mediar comandos locais sempre que possível, aplicando:
- allowlist explícita de diretórios;
- bloqueio de caminhos e nomes sensíveis;
- execução sem shell intermediário;
- classificação de risco;
- lock exclusivo por repositório;
- `correlation_id`;
- validação do estado Git antes e depois;
- log estruturado com mascaramento;
- `timeout`;
- controles negativos contra falso positivo.

## Escopo operacional

O perfil padrão autoriza somente:
- `C:\dev\reqsys-v2-enterprise-real`;
- worktrees legados que correspondam a `C:\dev\wt-*`;
- repositórios/worktrees isolados sob `C:\dev\chatgpt-workers\*`.

O namespace `C:\dev\chatgpt-workers\*` é o local preferencial para múltiplos chats/agentes. Cada frente deve usar diretório e branch próprios; não compartilhar o mesmo working tree entre workers.

Para OCR, quando o código estiver no ReqSys, usar um worker isolado do repositório ReqSys. Para Portal Portabilidade, usar clone/worktree isolado do repositório canônico `ericson-j-santos/portal-portabilidade`.

O perfil padrão não autoriza clones existentes no diretório de usuário, o volume `D:` nem outros diretórios fora da allowlist. Um novo projeto só entra no gateway ao ser colocado no namespace de workers ou por alteração explícita desta política.

Clones com alterações locais preexistentes não devem ser adotados como workspace automático. Preservar o trabalho existente e criar um worker limpo a partir da referência canônica.

## Bloqueios

O gateway deve bloquear:
- risco 3; operações críticas continuam exigindo ação governada e autorização humana fora do gateway automático;
- diretórios fora da allowlist;
- execução dentro de `.ssh`, `.azure`, `.aws`, `.gnupg`, `.kube` ou equivalentes configurados;
- argumentos que referenciem `.env`, chaves privadas, credenciais ou nomes sensíveis configurados;
- shells e ferramentas administrativas bloqueadas pela política;
- composição por `&&`, `||`, `;`, pipes ou redirecionamentos;
- código inline (`python -c`, `node -e`) no perfil operacional padrão;
- comandos destrutivos conhecidos, inclusive `git push`, `git reset --hard`, `git clean -f`, `docker system prune` e equivalentes.

## Risco 1

Para uma operação declarada como risco 1:
1. capturar branch, HEAD e digest do `git status --porcelain`;
2. adquirir lock exclusivo;
3. confirmar novamente o estado após o lock;
4. executar o comando com `shell=False`;
5. capturar novamente o estado;
6. falhar se HEAD ou estado Git tiver mudado.

Um comando que retorna código zero, mas altera o repositório, é falso positivo e deve ser rejeitado.

## Risco 2

Para risco 2:
- a árvore deve estar limpa por padrão;
- o HEAD esperado pode ser informado e deve coincidir antes da execução;
- mudanças no working tree são permitidas e registradas;
- mudança de HEAD é bloqueada por padrão;
- `--allow-head-change` só pode ser usado quando a solicitação atual autorizar explicitamente a criação de commit correspondente;
- após a ação, testes e E2E aplicáveis continuam obrigatórios.

## Concorrência

O lock fica fora do repositório, no diretório operacional local configurado. Todos os chats/agentes que operem pelo ecossistema devem respeitar esse lock.

Além do lock, o gateway compara o estado imediatamente antes e depois da aquisição. Mudança concorrente nesse intervalo aborta a execução.

Mudanças feitas por processos que ignorem o gateway não podem ser atribuídas com certeza ao executor. Se isso ocorrer, registrar bloqueio e não sobrescrever trabalho existente.

## Evidência

Cada execução deve produzir evento JSONL contendo, sem segredos:
- `correlation_id`;
- instante UTC;
- diretório;
- risco;
- executável e argumentos mascarados;
- branch/HEAD antes e depois;
- digest do estado Git;
- código de saída;
- classificação final.

## Critério de conclusão

O gateway está operacional somente quando:
- política é carregada;
- diretórios permitidos e bloqueados são testados;
- lock concorrente é testado;
- risco 1 detecta mutação indevida;
- divergência de HEAD é testada;
- referência sensível é bloqueada;
- o fluxo positivo executa com código zero;
- os testes negativos comprovam que o gate falha quando deve;
- o estado final do repositório real é revalidado.
