-- Rode uma vez no SQL Editor do Supabase.
-- Link do PDF do romaneio (Google Drive, pasta "Romaneios" compartilhada)
-- pra pedidos ja EMBARCADO - preenchido por titan_romaneio_links.py (GitHub
-- Actions), nunca pelo titan_cf_worker/titan_backfill.py.
alter table public.infos_titan add column if not exists romaneio_link text;
