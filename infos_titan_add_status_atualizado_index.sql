-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp).
--
-- Achado em producao em 26/08/2026: depois de ligar o recheck de 'erro'
-- (buscarErrosParaRecheck em src/index.ts), toda rodada do Cron passou a
-- falhar com "canceling statement due to statement timeout" (Postgres
-- 57014). infos_titan nunca teve indice nenhum alem da PK unica
-- (numero_nf, marca) - tanto esse recheck novo quanto o recheck de
-- 'concluido' que ja existia filtram por status+atualizado_em, forcando
-- Postgres a varrer a tabela inteira sequencialmente pra achar as linhas
-- elegiveis. Um indice composto resolve os dois de uma vez.

create index if not exists infos_titan_status_atualizado_idx
  on public.infos_titan (status, atualizado_em);
