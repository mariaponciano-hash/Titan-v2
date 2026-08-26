-- Rode isso UMA VEZ no SQL Editor do Supabase - migra a chave da tabela
-- infos_titan de numero_pedido pra numero_nf.
--
-- Por que: pra funcionar tambem com o BACKFILL em massa (consulta por um
-- periodo de datas no Titan, sem passar por um pedido especifico da Torre),
-- a unica informacao que o Titan devolve em qualquer consulta (avulsa ou em
-- massa) e a Nota Fiscal - o "Numero do Pedido" que aparece la e um numero
-- interno do armazem, diferente do numero de pedido da Torre. So a NF e
-- compartilhada entre os dois mundos.
--
-- Risco aceito (conversa de 24/08/2026 com a Ivna): NF pode repetir entre
-- marcas diferentes, mas isso e raro/nunca visto na pratica - decidimos
-- arriscar em favor da simplicidade em vez de exigir NF+marca.

-- Remove a PK antiga (numero_pedido) e a linha de teste que ficou pra tras
-- (nao tinha DELETE liberado - ver conversa) antes de trocar a chave.
delete from public.infos_titan where numero_pedido = 'teste-verificacao-claude';
alter table public.infos_titan drop constraint if exists infos_titan_pkey;

-- numero_nf pode estar vazio quando o pedido ainda nao tem NF emitida - so
-- vira PK de verdade quando tiver valor. Enquanto isso, garante que nao haja
-- linha duplicada de NF (o "on conflict" do upsert depende disso).
alter table public.infos_titan add constraint infos_titan_nf_unica unique (numero_nf);

-- numero_pedido deixa de ser chave, mas continua guardado (referencia -
-- "qual pedido da Torre foi a origem/ultima consulta desta NF").
--
-- CORRECAO (rodado depois, mesmo dia): DROP CONSTRAINT da PK acima NAO tira
-- o NOT NULL que "numero_pedido text primary key" tinha deixado na coluna -
-- isso e comportamento do Postgres (PK = unique + not null, cada parte se
-- remove separado). Sem isso, toda insercao nova (Torre, titan_backfill.py)
-- que nao mandasse numero_pedido preenchido falhava com "null value in
-- column numero_pedido violates not-null constraint" - confirmado com um
-- INSERT de teste real que deu esse erro exato.
alter table public.infos_titan alter column numero_pedido drop not null;
