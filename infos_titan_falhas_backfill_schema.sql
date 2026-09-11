-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp
-- que infos_titan ja usa). JA APLICADO direto no projeto em 11/09/2026 -
-- este arquivo fica so como registro/documentacao do schema, no mesmo
-- padrao dos outros infos_titan_*.sql deste repo.
--
-- Por que: titan_backfill.py tinha dois jeitos de PERDER pedido sem deixar
-- rastro nenhum (achado real, 11/09/2026 - a Maria reportou a NF 30280 que
-- estava no Titan mas nunca apareceu no Supabase):
--   1. Linha sem "Nome Projeto" (marca) identificavel - so incrementava um
--      contador "ignorados" no log da Action, que ninguem olha no dia a dia.
--   2. Lote que falhasse no upsert (schema/rede/Supabase fora do ar) mesmo
--      depois de isolado por chave uniforme - so um print no stderr da Action.
-- Em ambos os casos a linha nunca chegava a existir em lugar nenhum
-- consultavel: sumia de verdade. Esta tabela vira o destino dessas linhas -
-- nada mais e descartado silenciosamente, tudo fica visivel aqui pra
-- revisao manual (ou reprocessamento automatico futuro).

create table if not exists public.infos_titan_falhas_backfill (
  id bigint generated always as identity primary key,
  -- '' (nao null) de proposito, nos dois campos abaixo - o unique
  -- constraint e (numero_nf, marca, motivo) e o Postgres trata NULL como
  -- sempre distinto de outro NULL, entao duas falhas da MESMA nf+motivo sem
  -- marca nunca dariam upsert uma na outra (virariam linhas duplicadas a
  -- cada rodada do backfill, ja que ele roda 2x/dia sobre janela deslizante).
  numero_nf text not null default '',
  marca text not null default '',
  -- 'sem_nf_ou_marca' = registro_para_supabase nao conseguiu identificar a
  --                     marca (ou a propria NF) na linha exportada do Titan
  -- 'upsert_falhou'   = o POST em lote pro Supabase falhou mesmo depois das
  --                     tentativas com backoff (ver _supabase_upsert_grupo_uniforme)
  motivo text not null,
  detalhe text,
  -- linha bruta da exportacao do Titan (sem_nf_ou_marca) ou o registro que
  -- se tentou gravar em infos_titan (upsert_falhou) - contexto completo pra
  -- quem for revisar nao precisar adivinhar.
  payload jsonb,
  resolvido boolean not null default false,
  criado_em timestamptz not null default now(),
  atualizado_em timestamptz
);

alter table public.infos_titan_falhas_backfill
  add constraint infos_titan_falhas_backfill_unica unique (numero_nf, marca, motivo);

-- Indice parcial pra consulta do dia a dia ("o que ainda precisa de
-- atencao?") nao precisar varrer linhas ja resolvidas.
create index if not exists infos_titan_falhas_backfill_nao_resolvido
  on public.infos_titan_falhas_backfill (criado_em)
  where not resolvido;

-- RLS igual a infos_titan (mesma chave publicavel/anon usada pelo backfill
-- e pelo watcher) - ver infos_titan_schema.sql pro raciocinio completo.
alter table public.infos_titan_falhas_backfill enable row level security;

create policy "infos_titan_falhas_backfill_leitura_anon" on public.infos_titan_falhas_backfill
  for select to anon using (true);

create policy "infos_titan_falhas_backfill_insercao_anon" on public.infos_titan_falhas_backfill
  for insert to anon with check (true);

create policy "infos_titan_falhas_backfill_atualizacao_anon" on public.infos_titan_falhas_backfill
  for update to anon using (true) with check (true);
