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

-- Segundo indice (27/08/2026, mesmo motivo, achado pelo
-- titan_romaneio_links.py): esse script filtra por SITUACAO (o status real
-- do Titan, ex. 'EMBARCADO' - coluna diferente de "status" acima, que e o
-- estado da fila pendente/concluido/erro) + romaneio_link IS NULL. Sem
-- indice pra essa combinacao, a mesma varredura sequencial acontecia e
-- estourava o mesmo erro 57014, mesmo ja paginando por cursor em vez de
-- OFFSET. Indice PARCIAL (so cobre linhas ainda sem link) - fica pequeno e
-- rapido, e encolhe sozinho conforme o script vai preenchendo os links.
create index if not exists infos_titan_romaneio_link_pendente_idx
  on public.infos_titan (situacao, atualizado_em)
  where romaneio_link is null and romaneio is not null;

-- Terceiro indice (27/08/2026): ate um simples "romaneio=eq.X" (achar todos
-- os pedidos de um romaneio, pra copiar o link de um pra quem ainda nao
-- tem) estourou o mesmo erro 57014 - nao existia indice nenhum na coluna
-- romaneio sozinha. Sem "where" (nao e parcial) porque esse acesso e por
-- valor especifico de romaneio, nao por "ainda pendente".
create index if not exists infos_titan_romaneio_idx
  on public.infos_titan (romaneio);
