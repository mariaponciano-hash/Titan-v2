-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp),
-- DEPOIS de infos_titan_add_erro_recheck_tentativas.sql.
-- Tabela de "sinal de vida" do titan-watcher-worker (Cloudflare Worker, Cron
-- a cada 5 min) - PROPOSITALMENTE separada de infos_titan: infos_titan so
-- tem linha quando ha pedido pra processar, entao "0 pedidos pendentes" e "o
-- worker parou de rodar" ficavam indistinguiveis so olhando infos_titan.
-- O worker grava aqui (UPSERT, ver gravarHeartbeat em src/index.ts) ao FINAL
-- DE TODA rodada do scheduled(), sucesso ou falha - se last_run_at parar de
-- andar, o worker parou (crash, Cloudflare fora do ar, chave revogada etc.),
-- com ou sem trabalho pendente.
-- Ver o watchdog em worker_heartbeats_watchdog.sql, que le esta tabela.

create table if not exists public.worker_heartbeats (
  worker text primary key,          -- 'titan-watcher-worker' (= name em wrangler.toml)
  last_run_at timestamptz not null,
  status text not null,             -- 'ok' | 'error'
  detail text,
  itens_processados integer not null default 0,
  itens_com_erro integer not null default 0,
  -- Dono exclusivo: worker_heartbeats_watchdog.sql. O Worker NUNCA escreve
  -- nesta coluna (o UPSERT dele em src/index.ts lista so as colunas acima).
  last_alert_at timestamptz
);

-- RLS habilitado, mesmo padrao ja usado em infos_titan_schema.sql: libera
-- select/insert/update pro papel "anon" (o que o PostgREST usa quando
-- autentica com a chave publicavel/anon, a mesma TITAN_SB_KEY que o Worker
-- ja usa pra infos_titan).
alter table public.worker_heartbeats enable row level security;

create policy "worker_heartbeats_leitura_anon" on public.worker_heartbeats
  for select to anon using (true);

create policy "worker_heartbeats_insercao_anon" on public.worker_heartbeats
  for insert to anon with check (true);

create policy "worker_heartbeats_atualizacao_anon" on public.worker_heartbeats
  for update to anon using (true) with check (true);
