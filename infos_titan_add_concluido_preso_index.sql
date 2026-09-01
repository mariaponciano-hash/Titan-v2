-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp).
--
-- Achado em producao em 27/08/2026: infos_titan tem 1.236.209 linhas, das
-- quais 1.222.269 (98,9%) sao status='concluido' - o indice
-- infos_titan_status_atualizado_idx (status, atualizado_em), criado antes
-- hoje, ajuda MUITO o recheck de 'erro' (so 1.033 linhas, bem seletivo), mas
-- nao ajuda em nada o recheck de 'concluido' (buscarNaoFinalizadosParaRecheck
-- em src/index.ts): com quase a tabela inteira batendo status='concluido',
-- Postgres corretamente prefere Seq Scan a esse indice (EXPLAIN ANALYZE
-- confirmou: Seq Scan, 1.236.209 linhas varridas, ~4.3s so nessa consulta -
-- por isso o timeout do statement).
--
-- A condicao realmente seletiva e o "preso de verdade": status='concluido' E
-- (situacao nula OU nao-final). Um indice PARCIAL cobrindo exatamente essa
-- combinacao fica minusculo comparado a tabela inteira (assumindo que a
-- grande maioria dos pedidos concluidos ja tem situacao final, o que e o
-- esperado - um pedido "preso" e a excecao, nao a regra), e bate exatamente
-- com o WHERE que o PostgREST gera pra essa consulta.

create index if not exists infos_titan_concluido_preso_idx
  on public.infos_titan (atualizado_em)
  where status = 'concluido'
    and (situacao is null or situacao not in ('EMBARCADO', 'CANCELADO'));
