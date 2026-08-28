-- Rode uma vez no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp).
--
-- Reparo pontual (27/08/2026, a pedido da Ivna): o titan_romaneio_links.py
-- quebrou varias vezes no meio da execucao (statement timeout, ver
-- infos_titan_add_status_atualizado_index.sql) antes dos indices existirem -
-- cada execucao parcial deixou alguns pedidos de um romaneio com o link
-- gravado e outros do MESMO romaneio sem (confirmado manualmente: romaneio
-- 166723 tinha 414 pedidos, so 82 com link ate a Ivna pedir esse reparo).
-- Como todo pedido do mesmo romaneio aponta pro MESMO PDF, isso replica o
-- link ja conhecido de cada romaneio pros pedidos irmaos que ainda estao
-- com romaneio_link nulo - sem precisar buscar de novo no Drive.
--
-- Nao e um fix definitivo pro processo (o titan_romaneio_links.py, agora
-- com os indices no lugar, deve manter isso em dia sozinho daqui pra
-- frente) - so uma faxina pontual no que ja acumulou.
update public.infos_titan t
set romaneio_link = s.romaneio_link
from (
  select distinct on (romaneio) romaneio, romaneio_link
  from public.infos_titan
  where romaneio_link is not null
  order by romaneio, atualizado_em desc
) s
where t.romaneio = s.romaneio
  and t.romaneio_link is null;
