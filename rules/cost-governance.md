# Governança de custo — Regra nº 1

## Regra principal

O custo adicional máximo autorizado é **R$ 0,00**.

Esta regra é global e deve ser aplicada antes de selecionar runtime, executor,
serviço, plano, armazenamento, runner, API, crédito ou qualquer outro recurso
que possa gerar cobrança incremental.

## Fail-closed

- custo desconhecido = **BLOQUEADO**;
- possibilidade de overage sem hard stop comprovado = **BLOQUEADO**;
- custo adicional maior que R$ 0,00 = **BLOQUEADO**;
- cartão, método de pagamento ou assinatura existente não constituem autorização
  para gasto adicional;
- autorização técnica genérica, como "continue", "execute" ou "autorizo", não
  constitui autorização financeira.

Só considerar um recurso elegível quando houver evidência atual suficiente de
que a ação corrente não produzirá custo incremental. Franquia incluída,
assinatura já existente ou recurso gratuito podem ser usados somente quando a
operação não criar cobrança adicional.

## Proibições

Sem uma nova autorização financeira específica, é proibido:

- comprar créditos, minutos, armazenamento, runners, planos, licenças, add-ons
  ou capacidade;
- aumentar budget, spending limit, overage, quota paga ou teto de cobrança;
- habilitar faturamento para contornar limite técnico;
- trocar para recurso pago porque a alternativa gratuita está indisponível;
- reduzir testes, evidências, backup, segurança ou gates para economizar cota;
- apagar artefatos de forma especulativa para tentar liberar uma cota sem
  diagnóstico e preservação prévios.

## Exceção explícita

Qualquer exceção acima de R$ 0,00 exige instrução nova e inequívoca do
proprietário que substitua explicitamente esta regra e informe, no mínimo:

- produto/serviço;
- ação;
- valor máximo autorizado;
- moeda;
- escopo ou período aplicável.

Na ausência de qualquer desses elementos, manter custo adicional em R$ 0,00.

## GitHub Actions e produtos medidos

- runners padrão hospedados pelo GitHub em repositórios públicos podem ser
  usados apenas enquanto a documentação oficial vigente os classificar sem
  cobrança incremental;
- larger runners nunca são presumidos gratuitos;
- em repositórios privados, minutos, artefatos, Packages e outras medições só
  podem ser usados quando a operação estiver comprovadamente dentro da franquia
  sem overage pago ou protegida por hard stop suficiente;
- quando Billing/Usage/Budgets da conta não estiver acessível, não presumir
  saldo, franquia restante nem proteção de gasto;
- self-hosted runner elimina a cobrança de minutos do GitHub apenas no escopo
  documentado pelo GitHub; custos do host continuam fora dessa afirmação e não
  podem ser inventados como zero.

## Ordem de decisão

1. confirmar se a tarefa realmente precisa de recurso medido;
2. preferir integração/API nativa gratuita e capacidade já autorizada;
3. obter evidência atual de preço, franquia, limite e proteção contra overage;
4. classificar a ação como:
   - `COST_ZERO_CONFIRMED`;
   - `COST_UNKNOWN_BLOCKED`;
   - `COST_NONZERO_BLOCKED`;
5. executar somente em `COST_ZERO_CONFIRMED`;
6. depois da execução, registrar a evidência de que nenhuma ampliação financeira
   foi feita.

## Evidência mínima

Quando custo for material à decisão, registrar:

- produto e executor;
- público/privado quando afetar preço;
- fonte e data da regra de cobrança;
- condição que comprova custo incremental zero;
- estado de budget/hard stop quando aplicável;
- bloqueio quando a informação de conta não estiver disponível;
- `correlation_id` quando houver fluxo técnico associado.

Nunca registrar número de cartão, credencial, segredo ou dado financeiro
sensível.

## Relação com outras regras

Esta regra não autoriza execução por si só. Permanecem obrigatórios segurança,
governança, E2E, Command Gateway, runtime routing e demais controles aplicáveis.

Em conflito entre otimização de custo e segurança/validação, preservar os
controles e **bloquear a execução**, em vez de pagar ou enfraquecer a proteção.
