-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp).
-- "Depositante"/"Cliente" antes eram considerados impossiveis de capturar
-- (o Titan renderizava essas colunas sem texto acessivel no DOM/accessibility
-- tree - ver comentario no infos_titan_schema.sql original). Descoberto em
-- 25/08/2026 que a exportacao nativa do Power BI ("..." -> "Exportar dados")
-- traz o texto de verdade dessas duas colunas - o titan_backfill.py agora
-- passou a usar essa exportacao em vez de ler a tabela por scroll, e grava
-- os dois campos junto com o resto.

alter table public.infos_titan add column if not exists depositante text;
alter table public.infos_titan add column if not exists cliente text;
