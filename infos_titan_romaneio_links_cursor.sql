-- Rode uma vez no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp).
--
-- Guarda o ponto (atualizado_em) ate onde titan_romaneio_links.py ja
-- processou, pra proxima execucao CONTINUAR dali em vez de sempre
-- recomecar do inicio da janela de 45 dias (27/08/2026, a pedido da
-- Ivna). Sem isso, um romaneio sem PDF na pasta (ainda nao subiu) fica na
-- FRENTE da fila pra sempre (ordenada por atualizado_em) e e retentado em
-- toda execucao, consumindo o tempo do timeout de 30min antes do script
-- conseguir alcancar romaneios mais novos que nunca foram nem tentados.
--
-- "id boolean primary key default true" + o check e um truque comum pra
-- forcar uma tabela SINGLETON (nunca mais de 1 linha) - so existe o valor
-- true possivel pra chave primaria.
create table if not exists public.titan_romaneio_links_cursor (
  id boolean primary key default true,
  ultimo_atualizado_em timestamptz,
  constraint titan_romaneio_links_cursor_singleton check (id)
);

-- RLS habilitado, mesmo padrao ja usado em infos_titan_schema.sql/
-- worker_heartbeats_schema.sql: libera select/insert/update pro papel
-- "anon" (a chave publicavel que titan_romaneio_links.py ja usa).
alter table public.titan_romaneio_links_cursor enable row level security;

create policy "titan_romaneio_links_cursor_leitura_anon" on public.titan_romaneio_links_cursor
  for select to anon using (true);

create policy "titan_romaneio_links_cursor_insercao_anon" on public.titan_romaneio_links_cursor
  for insert to anon with check (true);

create policy "titan_romaneio_links_cursor_atualizacao_anon" on public.titan_romaneio_links_cursor
  for update to anon using (true) with check (true);
