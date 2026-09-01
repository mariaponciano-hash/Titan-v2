-- ============================================================================
-- Migracao de enderecos_para_ticket, pra banco que JA rodou a primeira versao
-- do schema. Idempotente: pode rodar mais de uma vez.
--
-- Se o banco ainda nao tem a tabela, rode enderecos_para_ticket_schema.sql em
-- vez deste arquivo.
--
-- Traz tres coisas que faltavam na primeira versao:
--   1. a chave unica correta, (pedido, marca) em vez de (pedido)
--   2. o endereco estruturado e o controle de desfecho do creator
--   3. o status de entrega que o creator devolve na criacao
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. CHAVE UNICA
-- A primeira versao criou unique em (pedido) sozinho, e isso esta errado: o
-- identificador de pedido nao e unico entre marcas. Conferido em 24/08/2026
-- contra gold.vw_intelipost_orders - a NF 968609 existe em apice, barbours E
-- kokeshi; a 957026 em apice, kokeshi E rituaria. Com a chave antiga, a segunda
-- marca a pedir troca no mesmo numero era rejeitada como duplicada e a cliente
-- ficava sem ticket sem sintoma nenhum.
-- A troca nao pode falhar por dado existente: o indice novo e mais permissivo.
-- ---------------------------------------------------------------------------
drop index if exists public.enderecos_para_ticket_pedido_uk;

create unique index if not exists enderecos_para_ticket_pedido_marca_uk
  on public.enderecos_para_ticket (pedido, marca);

-- ---------------------------------------------------------------------------
-- 2. ENDERECO ESTRUTURADO + DESFECHO DO CREATOR
-- A Regra 1 do creator compara city/state/address1/address2/zipcode e nunca faz
-- parse do fullAddress: sem as partes, toda criacao morre em
-- ADDRESS_UNVERIFIABLE. Quem alimenta a tabela precisa gravar as partes.
-- ---------------------------------------------------------------------------
alter table public.enderecos_para_ticket
  add column if not exists ticket_reference text,
  add column if not exists ultimo_code      text,
  add column if not exists blocked_reason   text,
  add column if not exists proxima_tentativa_em timestamptz,
  add column if not exists end_logradouro  text,  -- address1
  add column if not exists end_numero      text,  -- address2 = NUMERO
  add column if not exists end_complemento text,  -- address3
  add column if not exists end_bairro      text,  -- address4 = BAIRRO
  add column if not exists end_cidade      text,  -- city
  add column if not exists end_uf          text,  -- state
  add column if not exists end_cep         text,  -- zipcode
  add column if not exists end_pais        text default 'BR';

-- ---------------------------------------------------------------------------
-- 3. STATUS DE ENTREGA (foto do momento da abertura, nao valor vivo)
-- ---------------------------------------------------------------------------
alter table public.enderecos_para_ticket
  add column if not exists delivery_status text,
  add column if not exists delivery_date   date,
  add column if not exists tracking_code   text,
  add column if not exists carrier         text,
  add column if not exists cliente         text,
  add column if not exists status_visto_em timestamptz;

-- ---------------------------------------------------------------------------
-- 4. ESTADOS NOVOS no CHECK
-- 'bloqueado'      = Regra 1 recusou (terminal)
-- 'precisa_humano' = sem automacao / endereco insuficiente / escalonamento
-- 'sem_fonte_de_status' fica no CHECK so pra nao quebrar linha que ja tenha
-- esse valor gravado de uma versao intermediaria; nao ha uso novo dele.
-- ---------------------------------------------------------------------------
alter table public.enderecos_para_ticket
  drop constraint if exists enderecos_para_ticket_status_check;

alter table public.enderecos_para_ticket
  add constraint enderecos_para_ticket_status_check check (status in (
    'aguardando', 'sem_fonte_de_status', 'disparando', 'criado',
    'bloqueado', 'precisa_humano', 'erro', 'esgotado', 'nao_se_aplica'
  ));

create index if not exists enderecos_para_ticket_fila_idx
  on public.enderecos_para_ticket (status, proxima_tentativa_em);

-- ---------------------------------------------------------------------------
-- Conferencia
-- ---------------------------------------------------------------------------
select indexname, indexdef
from pg_indexes
where schemaname = 'public' and tablename = 'enderecos_para_ticket'
order by indexname;

select conname, pg_get_constraintdef(oid) as definicao
from pg_constraint
where conrelid = 'public.enderecos_para_ticket'::regclass
  and conname = 'enderecos_para_ticket_status_check';
