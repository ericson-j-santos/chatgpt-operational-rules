# Changelog

## 1.2.0 - 2026-09-10
- Tornada obrigatória a validação ponta a ponta para incrementos funcionais criados ou modificados.
- Adicionados controles explícitos contra falso positivo: pré-condição, marcador único, caso positivo, caso negativo/controle, leitura independente e vínculo com branch/SHA/ambiente.
- Adicionado teste do próprio teste quando seguro e viável para comprovar que o mecanismo detecta falhas.
- Proibido declarar conclusão funcional quando a validação E2E aplicável estiver bloqueada ou baseada em evidência residual/ambígua.
- Adicionado gate automático do repositório com validação de manifesto e autotestes negativos do próprio validador.

## 1.1.0 - 2026-09-10
- Adicionada política de execução governada de terminal.
- Definidos níveis 🟢/🟡/🔴 para observação, alteração reversível e operações críticas.
- Adicionadas proteções para segredos, privilégios, comandos destrutivos e execução remota.
- Definidas evidências mínimas e critérios de conclusão para comandos executados por chats e agentes.

## 1.0.0 - 2026-09-09
- Criação da fonte canônica de regras operacionais.
- Política de armazenamento de artefatos no Google Drive.
- Regras de evidência, GitHub/CI, segurança, relatórios e engenharia.
- Regra de bootstrap para novos chats/agentes.
- Regras específicas iniciais para ReqSys, ocr_evidencia e BACEN.
- Proteção contra publicação de identificadores privados em repositório público.
