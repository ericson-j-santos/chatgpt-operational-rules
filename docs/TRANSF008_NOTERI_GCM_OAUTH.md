# TRANSF-008 — autenticação GitHub no Noteri (ação Risk 3 limitada)

**Finalidade:** admitir o checkout privado de `ericson-j-santos/transferidor-documental` para o E2E local Windows/NTFS. A identidade do produto é fixa na capability versionada; este fluxo não habilita deploy, CI privada, produção nem alteração de permissões do GitHub.

## Precondições

- Autorização explícita e atual do proprietário para iniciar **apenas** o OAuth do Git Credential Manager (GCM). Consentimento final e revisão das permissões na janela oficial do GitHub são gestos humanos.
- Noteri Windows, perfil operacional `NORMAL` lido de arquivo existente, com `accepts_new_development=true`.
- Session Launcher `SESSION_LAUNCH_OK`, `state_validated=true`, Command Gateway e worktree exclusivo/limpo de `chatgpt-operational-rules` no SHA atual.
- Origem Git de `scripts/noteri_transferidor_gcm_oauth.py` versionada e SHA/árvore comparados por leitura independente; Validator isolado.
- `scripts/set_owner_risk3_action.py` registra um único `action_id`, com **validade curta** (ex.: 15 minutos), `git_head`, `cwd`, escopo e confirmação exatos. Não reativar o modo Risk 3 genérico expirado.
- Invocar `scripts/owner_risk3_gateway.py` pelo Command Gateway usando a mesma sessão. O Risk 3 cria a autorização individual, não permite shell/terminal direto.

## Escopo e evidências

O script verifica apenas a branch `import/transf008-v0.4.0` e exige SHA `4bbcb6575773ae3e7ff7d54a81d7b3bea802c556`. Primeiro consulta GitHub sem interação; se já está autorizado, não inicia outro login. Se precisa autenticar, solicita **somente OAuth por navegador** via GCM, sem imprimir tokens, cookies, senhas, código de dispositivo, redirecionamentos OAuth ou stderr do provedor. O usuário inspeciona a conta e permissões solicitadas no navegador e pode recusar; GCM gerencia a credencial no cofre Windows. Após conclusão, a releitura Git não interativa deve confirmar o mesmo SHA. Resposta 2xx, processo iniciado ou GCM exit 0 isolados não bastam.

O Git Credential Manager pode solicitar permissões GitHub que abrangem mais de um repositório. O usuário deve confirmar o **escopo real** apresentado antes de aceitar; a capability fixa o repositório que será lido pelo teste, mas não promete restringir o consentimento OAuth a ele.

### Falha fechada

O fluxo bloqueia outros hosts/SO, perfil `ESTUDO`, arquivo de perfil ausente ou inválido, Git ausente, confirmação inválida, navegador indisponível, timeout, login negado e SHA remoto divergente. Não há fallback para token, PAT, dispositivo, shell, SSH, GUI automatizada ou mudança de visibilidade do repositório.

Não colocar a credencial, uma URL de callback, códigos ou resultados brutos do GCM em Issues/PRs/logs. Persistir apenas `action_id`, `correlation_id`, hash de script, SHA esperado, estado sanitizado, resultado de testes e check de integridade.

**Aceite parcial:** código + testes locais + PR draft + readback remoto. **Aceite físico:** login autorizado e confirmado pelo proprietário, checkout SHA-bound admitido em novo worktree governado no Noteri, 65 testes e canário NTFS reais, testes negativos/replay, leitura independente, recibo vinculado ao SHA. Sem esse segundo aceite, TRANSF-008 permanece bloqueado/parcial.

## Diagnóstico adicional: estação de janelas (somente leitura)

A sonda `scripts/noteri_transferidor_winsta_probe.py` consulta somente o atributo Win32 `WSF_VISIBLE` da estação de janelas associada ao processo, além dos identificadores de sessão já verificados. Ela não consulta usuários, títulos de janelas, tokens ou códigos de autorização e não inicia navegador.

`WINDOW_STATION_NOT_VISIBLE` indica que o processo não dispõe de estação visível; `VISIBLE_WINDOW_STATION_CANDIDATE` é apenas um candidato, **não** comprova desktop utilizável, janela exibida, consentimento humano ou autorização GitHub. Falhas de API retornam estado bloqueado, sem fallback por GUI, shell, credencial ou mudança de política. Validar a sonda no Noteri pelo Session Launcher e Command Gateway (risco 1) no SHA vigente.
