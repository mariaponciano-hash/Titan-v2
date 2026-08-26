-- Rode isso UMA VEZ no SQL Editor do Supabase - troca a chave unica de
-- infos_titan de (numero_nf) para (numero_nf, marca).
--
-- Por que: confirmado com print real da tela do Titan (24/08/2026) que a
-- MESMA Nota Fiscal pode aparecer em mais de uma linha, uma por marca
-- diferente, cada uma com Romaneio DIFERENTE (ex: NF 940380 apareceu pra
-- Kokeshi com romaneio 169114 E pra Rituaria com romaneio 173827). O risco
-- que tinhamos combinado como "raro, pode arriscar" (24/08/2026, conversa
-- anterior) aconteceu de verdade - por isso NF sozinha nao e mais suficiente
-- como identificador unico.

alter table public.infos_titan drop constraint if exists infos_titan_nf_unica;
alter table public.infos_titan add constraint infos_titan_nf_marca_unica unique (numero_nf, marca);
