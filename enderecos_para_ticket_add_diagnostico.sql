-- ============================================================================
-- Colunas de diagnostico e de visibilidade em enderecos_para_ticket.
--
-- POR QUE: apareceu um caso (01/09) de linha marcada como `criado` num pedido
-- que o Cosmos reporta como `waiting`, e a Maria disse que aconteceu no dia
-- anterior tambem. Tentei explicar tres vezes pelos logs e nao consegui, por um
-- motivo simples: o dado que explicaria nao era gravado.
--
--   - no desfecho `criado` o codigo LIMPAVA o ultimo_erro e nunca guardava o
--     corpo da resposta do creator;
--   - o veredito do gate so era persistido quando dava "nao" (e mesmo assim
--     enfiado no delivery_status, que e outra coisa).
--
-- Entao a proxima ocorrencia fica explicavel pela propria tabela, sem depender
-- de log com atraso. E de quebra atende o pedido de ver o status do Cosmos e a
-- transportadora na tela.
--
-- Idempotente. Rodar no SQL Editor do projeto ozwcyrkzsqzmavjtsmsp.
-- ============================================================================

alter table public.enderecos_para_ticket
  -- veredito do gate: sim | nao | morto | desconhecido
  add column if not exists gate_veredito     text,
  -- o valor CRU lido na origem: 'waiting', 'sent', 'in_transit', 'delivered'...
  add column if not exists gate_status_origem text,
  -- de onde veio: cosmos | middleware_v1 | nenhuma
  add column if not exists gate_fonte        text,
  add column if not exists gate_visto_em     timestamptz,
  -- corpo da resposta do creator, truncado. Gravado em TODOS os desfechos,
  -- inclusive sucesso - era justamente no sucesso que a evidencia se perdia.
  add column if not exists resposta_creator  text;

comment on column public.enderecos_para_ticket.gate_status_origem is
  'Status cru lido na origem (Cosmos logistic_status, ou status do Middleware V1). E o que o gate usou pra decidir.';
comment on column public.enderecos_para_ticket.resposta_creator is
  'Resposta do cx-ticketcreator, truncada. Gravada tambem no sucesso, para o caso de ticket criado indevidamente ser auditavel.';

create index if not exists enderecos_para_ticket_gate_idx
  on public.enderecos_para_ticket (gate_veredito, gate_status_origem);

select column_name, data_type
from information_schema.columns
where table_schema = 'public' and table_name = 'enderecos_para_ticket'
  and column_name in ('gate_veredito','gate_status_origem','gate_fonte','gate_visto_em','resposta_creator','carrier')
order by column_name;
