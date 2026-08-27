-- ============================================================================
-- enderecos_para_ticket - fila de tickets de alteracao de endereco
--
-- Schema COMPLETO e final, pra ambiente novo. Se o banco ja rodou a primeira
-- versao deste arquivo, use enderecos_para_ticket_migracao.sql em vez deste.
--
-- O QUE ESTA TABELA FAZ: o bot registra aqui o endereco novo que o cliente
-- pediu. O ticket com a transportadora NAO pode ser aberto nesse instante - so
-- depois que o pedido saiu. O cron da Central varre a fila, confirma na origem
-- (Cosmos pras 7 marcas, Middleware V1 pra Apice) que o pedido saiu, chama o
-- cx-ticketcreator, e registra o desfecho pra permitir redisparo.
--
-- Projeto Supabase: ozwcyrkzsqzmavjtsmsp (o mesmo de tickets_gobeaute).
-- ============================================================================

create table if not exists public.enderecos_para_ticket (
  id              bigserial   primary key,

  -- ---- alimentado pelo bot ----
  -- pedido = o orderId da ORIGEM da marca (id do Cosmos pras marcas Cosmos,
  -- numero Shopify pra Apice). NAO e a NF e NAO e a `reference`: a reference e
  -- devolvida pelo creator na criacao e fica em ticket_reference.
  pedido          text        not null,
  marca           text        not null,
  endereco_atual  text,
  novo_endereco   text        not null,   -- vira o fullAddress
  status          text        not null default 'aguardando',

  -- ---- endereco ESTRUTURADO (obrigatorio na pratica) ----
  -- A Regra 1 do creator compara city/state/address1/address2/zipcode e NUNCA
  -- faz parse do fullAddress. Linha so com novo_endereco passa do gate de
  -- ausencia e morre como ADDRESS_UNVERIFIABLE. Minimo pra decidir:
  -- cidade + UF, mais logradouro ou CEP.
  end_logradouro  text,   -- address1
  end_numero      text,   -- address2  (E O NUMERO, nao "linha 2")
  end_complemento text,   -- address3
  end_bairro      text,   -- address4  (E O BAIRRO, nao "linha 4")
  end_cidade      text,   -- city
  end_uf          text,   -- state
  end_cep         text,   -- zipcode
  end_pais        text default 'BR',

  -- ---- controle de erro / redisparo ----
  tentativas      integer     not null default 0,
  ultimo_erro     text,
  ultimo_erro_em  timestamptz,
  ultimo_code     text,       -- code da ultima resposta do creator
  blocked_reason  text,       -- different_city | jet_out_of_scope
  -- backoff: cada chamada de criacao custa ao creator uma busca na origem mais
  -- uma na Intelipost, mesmo quando termina em ORDER NO SENT.
  proxima_tentativa_em timestamptz,

  -- ---- espelho do ticket no creator ----
  ticket_reference text,      -- devolvida por ele na criacao; chave do GET /tickets
  ticket_id       bigint,
  ticket_status   text,       -- pending | processing | completed | error | manual
  ticket_deadline date,

  -- ---- status de entrega, FOTO do momento da abertura ----
  -- Vem no corpo da criacao. Nao se atualiza sozinho depois.
  delivery_status text,
  delivery_date   date,
  tracking_code   text,
  carrier         text,
  cliente         text,
  status_visto_em timestamptz,

  disparado_em    timestamptz,
  criado_em       timestamptz not null default now(),
  atualizado_em   timestamptz not null default now(),

  constraint enderecos_para_ticket_status_check check (status in (
    'aguardando',           -- pedido ainda nao saiu (ORDER NO SENT), com backoff
    'sem_fonte_de_status',  -- legado, sem uso novo
    'disparando',           -- linha reivindicada por uma execucao
    'criado',               -- ticket existe no creator
    'bloqueado',            -- Regra 1 recusou: TERMINAL, nao adianta redisparar
    'precisa_humano',       -- sem automacao, endereco insuficiente, escalonamento
    'erro',                 -- falha tecnica; entra na fila de redisparo
    'esgotado',             -- estourou o teto de tentativas
    'nao_se_aplica'         -- encerrado sem ticket
  ))
);

-- A chave inclui `marca` de proposito. O identificador de pedido NAO e unico
-- entre marcas: cada origem tem sua propria sequencia. Conferido em 24/08/2026
-- contra gold.vw_intelipost_orders - a NF 968609 existe em apice, barbours E
-- kokeshi. Com unique so em `pedido`, a segunda marca a pedir troca no mesmo
-- numero seria rejeitada e a cliente ficaria sem ticket sem ninguem ver.
create unique index if not exists enderecos_para_ticket_pedido_marca_uk
  on public.enderecos_para_ticket (pedido, marca);

create index if not exists enderecos_para_ticket_fila_idx
  on public.enderecos_para_ticket (status, proxima_tentativa_em);
create index if not exists enderecos_para_ticket_marca_idx
  on public.enderecos_para_ticket (marca);

-- ---------------------------------------------------------------------------
-- RLS: esta tabela guarda ENDERECO DE CLIENTE. Diferente de tickets_gobeaute,
-- ela NAO recebe policy pra role anon - a chave anon esta no codigo-fonte do
-- app, entao policy pra anon aqui equivale a publicar endereco de cliente na
-- internet. O worker fala com ela pela service_role key (secret SB_SERVICE_KEY).
-- ---------------------------------------------------------------------------
alter table public.enderecos_para_ticket enable row level security;

create or replace function public.tg_enderecos_para_ticket_touch()
returns trigger language plpgsql as $$
begin
  new.atualizado_em := now();
  return new;
end $$;

drop trigger if exists enderecos_para_ticket_touch on public.enderecos_para_ticket;
create trigger enderecos_para_ticket_touch
  before update on public.enderecos_para_ticket
  for each row execute function public.tg_enderecos_para_ticket_touch();
