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

## Artefatos de continuidade de execução

Artefato operacional usado para retomar uma tarefa em outro executor não é o
mesmo que um entregável final. Ele pode usar qualquer armazenamento autorizado
que satisfaça o contrato abaixo; Google Drive não é requisito desse handoff.

1. Tudo que for necessário para continuar deve existir fora do executor que
   produziu o resultado. Cache, commit ou recibo presente somente no disco local
   não comprovam durabilidade.
2. O conteúdo deve ser tipado, imutável e endereçado por hash. Em lease cercada,
   o envelope deve obrigatoriamente vincular tarefa, efeito, geração e tentativa;
   para payload Git, também repositório, base, SHA produzido, contrato do adapter,
   caminhos e evidências.
3. A referência persistida no checkpoint deve ser opaca e limitada à gramática
   do transporte. Endpoint, bucket privado, região e credenciais vêm
   exclusivamente de configuração local confiável; tarefa, evento e conversa
   não podem escolhê-los. Exigir TLS com verificação, acesso não público e
   credencial de menor privilégio limitada ao prefixo e às operações necessárias.
   Referência, log e erro não podem conter segredo ou material de autenticação.
4. Antes de adquirir uma lease que exija handoff, o executor deve executar
   preflight do transporte e comprovar configuração, escrita condicional no
   prefixo autorizado e leitura integral do objeto de prova. Falha nessa etapa
   bloqueia o claim. A publicação continua sujeita a readback próprio.
5. A publicação deve ser `create-only`/condicional em uma chave derivada do
   digest. Objeto preexistente só representa replay idempotente depois de baixar
   e comparar os bytes completos; ETag, HEAD ou metadado autodeclarado não bastam.
6. Depois da escrita, baixar novamente o objeto por uma leitura independente,
   limitar o tamanho e recalcular o hash. Timeout ou resposta perdida exige esse
   readback: bytes idênticos confirmam o efeito; ausência só permite
   `not_applied` quando a consistência dessa conclusão estiver comprovada; caso
   contrário registrar `unknown`, bloquear e não repetir automaticamente.
7. O checkpoint canônico deve permanecer protegido por lease e geração. Um
   executor antigo pode, no máximo, deixar um objeto CAS órfão e imutável; não
   pode atualizar alias mutável, substituir conteúdo nem tornar seu efeito
   canônico depois de perder a posse.
8. A materialização ocorre em workspace isolado e valida referência, envelope,
   digest, formato interno, tarefa, base e resultado antes de alterar o checkout.
   Para bundle Git, verificar o bundle e o commit anunciado antes do fetch e
   revalidar limpeza, HEAD e diff depois da materialização.
9. O aceite exige E2E entre dois workspaces sem filesystem compartilhado, com
   controles negativos para adulteração, objeto divergente, limite de tamanho,
   repetição idempotente e timeout antes/depois da escrita.

O provedor de armazenamento é substituível. A homologação vale somente para o
backend, região, credencial, executor e versão efetivamente testados.
