-- ============================================================================
-- Acrescenta `origem` e `responsavel` a enderecos_para_ticket.
--
-- POR QUE: a fila passa a ter DOIS escritores - o bot (pedido do cliente
-- capturado no atendimento) e a Torre de Controle (agente registrando quando o
-- creator devolve ORDER NO SENT). Sem marcar a origem, linha da Torre fica
-- indistinguivel de linha do bot, e ninguem consegue responder "por que essa
-- linha existe?" nem medir os dois caminhos separadamente.
--
-- `responsavel` so e preenchido pela Torre (o agente que registrou). O bot nao
-- tem agente, entao fica nulo - e isso por si so ja diferencia os dois.
--
-- Idempotente. Rodar no SQL Editor do projeto ozwcyrkzsqzmavjtsmsp.
-- ============================================================================

alter table public.enderecos_para_ticket
  add column if not exists origem      text default 'bot',
  add column if not exists responsavel text;

comment on column public.enderecos_para_ticket.origem is
  'Quem criou a linha: bot (pedido do cliente no atendimento) ou torre (agente na Torre de Controle).';
comment on column public.enderecos_para_ticket.responsavel is
  'Agente que registrou, quando a origem e a Torre. Nulo para linha do bot.';

create index if not exists enderecos_para_ticket_origem_idx
  on public.enderecos_para_ticket (origem);

select column_name, data_type, column_default
from information_schema.columns
where table_schema = 'public' and table_name = 'enderecos_para_ticket'
  and column_name in ('origem', 'responsavel');
