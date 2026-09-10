# Armazenamento de artefatos

## Regra
Todo arquivo gerado como entregável deve, quando a integração estiver operacional:
1. ser salvo no Google Drive;
2. ter o upload validado;
3. ser registrado no índice de artefatos;
4. retornar o link persistente do Drive junto ao link local/sandbox.

## Destino lógico
- Pasta canônica: `ChatGPT - Artefatos`.
- Identificadores, URLs privadas, tokens ou credenciais devem ficar fora deste repositório público.

## GitHub
Usar GitHub para:
- código-fonte;
- testes;
- workflows de CI/CD;
- infraestrutura como código;
- documentação que precise acompanhar a versão do software.

Evitar GitHub para:
- relatórios gerados;
- PDFs exportados;
- binários;
- arquivos temporários;
- saídas executáveis de relatórios.
