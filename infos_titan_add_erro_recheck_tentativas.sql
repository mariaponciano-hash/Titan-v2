-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp).
-- Contador de quantas vezes um pedido com status='erro' ja foi devolvido pra
-- fila automaticamente pra nova tentativa (recheck de erro, ver
-- buscarErrosParaRecheck/devolverParaFila em titan_cf_worker/src/index.ts).
-- Sem um teto, uma NF permanentemente quebrada (nunca vai existir no Titan,
-- marca errada pra sempre etc.) ficaria sendo re-tentada pra sempre.
-- marcarConcluido zera esse contador assim que o pedido finalmente da certo.

alter table public.infos_titan
  add column if not exists erro_recheck_tentativas integer not null default 0;

-- OPCIONAL mas recomendado: pedidos que ja estao com status='erro' de ANTES
-- deste deploy podem ter atualizado_em NULL (marcarErro nao gravava essa
-- coluna ate 26/08/2026, ja corrigido no codigo) - sem isso, eles nunca batem
-- no filtro "atualizado_em < cutoff" do recheck de erro e ficam presos pra
-- sempre. Roda a linha abaixo uma vez, junto com o ALTER acima, pra destravar
-- esses casos antigos.
update public.infos_titan
  set atualizado_em = coalesce(atualizado_em, solicitado_em)
  where status = 'erro' and atualizado_em is null;
