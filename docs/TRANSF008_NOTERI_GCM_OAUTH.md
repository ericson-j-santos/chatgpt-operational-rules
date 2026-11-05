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

## Classificador sanitizado de falhas GCM

A execução governada no Noteri confirmou que o GCM 2.9.1 aceita `github login --browser --url` e que o Git utiliza `credential.helper=manager`. Isso não comprova autenticação, mas descarta incompatibilidade desses parâmetros e ausência de helper como causas ativas.

Em uma futura tentativa OAuth **explicitamente autorizada**, o stdout do GCM permanece descartado. O stderr é lido apenas em memória transitória para reconhecer categorias fixas (parâmetro incompatível, navegador indisponível, consentimento cancelado, rede/TLS, cofre indisponível ou não classificado). O texto bruto, códigos e URLs de callback jamais entram em recibos, arquivos, logs ou Issues. GCM_TRACE e GCM_TRACE_SECRETS permanecem desabilitados. A categoria não comprova janela exibida: consentimento humano e Git SHA-bound são gates independentes.

Não é possível classificar retroativamente o erro anterior porque a saída foi descartada. Não repetir o login automaticamente sem precondição material alterada e proprietário presente no console.

## Contrato de códigos de saída do Owner Risk 3

O gateway de risco 3 não transmite stdout de scripts que falham, preservando a confidencialidade. Para um diagnóstico governado, os erros classificados são representados por códigos **numéricos fixos**, sem alterar essa política: argumento GCM incompatível (31), navegador indisponível (32), consentimento cancelado (33), rede/TLS (34), cofre local indisponível (35), erro GCM não classificado (36), autenticação concluída mas leitura do repositório privado bloqueada (37). Outros bloqueios permanecem no código 28. Somente `SOURCE_AUTH_READY` (com SHA remoto conferido) é aceite; o preflight sem interação também pode retornar 0 com `OAUTH_HUMAN_CONSENT_REQUIRED`, sem conceder login.

Mesmo que o processo retorne uma categoria numerada, nenhuma URL de callback, stdout/stderr bruto, token ou código de dispositivo poderá ser persistido. Cada nova execução real exige autorização Risk 3 individual vinculada ao SHA e consentimento do proprietário.

## Diagnóstico do acesso Git privado sem OAuth

O modo `--diagnose-readback` executa somente `git -c credential.interactive=never ls-remote --heads` para URL, referência e SHA fixos da v0.4.0. Não permite URL, branch ou comando arbitrários. Ele não inicia login nem navegador e retorna somente estados sanitizados: credencial ausente, autenticação/permissão negada, repositório inacessível, TLS/proxy, rede, referência ausente, SHA divergente, resposta inválida ou transporte não classificado.

Um exit 0 dessa **consulta diagnóstica** significa apenas que a classificação foi executada; nunca significa que o acesso privado foi autorizado. Somente `status=SOURCE_AUTH_READY` e `source_head_verified=true` comprovam o ref SHA esperado. Proibido publicar stderr/stdout brutos, identidades, URL de callback ou credenciais. Executar somente em Noteri DEV com perfil NORMAL, Session Launcher e Command Gateway Risk1, worktree e SHA exclusivos.
