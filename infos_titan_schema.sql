-- Rode isso UMA VEZ no SQL Editor do Supabase (projeto ozwcyrkzsqzmavjtsmsp,
-- o mesmo que a Torre ja usa pra tickets_gobeaute/tickets_gocase).
-- Cria uma tabela unica pra guardar o resultado da consulta do Titan BI -
-- serve tanto de "fila" (status=pendente = a Torre pediu, ainda nao chegou)
-- quanto de cache do resultado (status=concluido = pronto pra mostrar).

create table if not exists public.infos_titan (
  numero_pedido text primary key,
  numero_nf text,
  marca text,
  -- pendente = a Torre pediu, aguardando o script rodar no seu PC
  -- concluido = resultado pronto (ver campos abaixo)
  -- erro = o script tentou e nao achou/deu problema (ver "erro")
  status text not null default 'pendente',
  erro text,
  -- Depositante e Cliente NAO tem coluna aqui de proposito: confirmado que o
  -- Titan renderiza essas duas colunas sem texto acessivel no DOM (mesmo
  -- aparecendo na tela) - precisaria de OCR pra capturar, nao implementado.
  situacao text,
  romaneio text,
  valor_pedido text,
  volume text,
  observacao text,
  nome_projeto text,
  nome_projeto_antigo text,
  data_importado text,
  data_expedido text,
  data_conferido text,
  -- lista de {"Horário da Situação": ..., "Situação": ...} - tabela "Eventos" do Titan
  eventos jsonb,
  -- lista de {"Código": ..., "Descrição": ..., "Ean": ..., "Quantidade": ...,
  -- "Tipo": ..., "Checkout Realizado": ..., "Valor Total": ..., "Valor Unitário": ...}
  -- - tabela "Itens do pedido" do Titan
  itens jsonb,
  solicitado_em timestamptz not null default now(),
  atualizado_em timestamptz
);

-- RLS habilitado (24/08/2026, a pedido do proprio aviso do Supabase): sem
-- isso, qualquer pessoa que descobrisse a chave publicavel usada aqui
-- (TITAN_SB_KEY em src/server.ts / SUPABASE_KEY em titan_watcher.py) poderia
-- ler/escrever livremente nesta tabela. A chave em si so fica no Worker e no
-- seu PC (nunca no navegador do agente), mas nao custa nada fechar isso
-- direito. A politica abaixo libera tudo (select/insert/update) so pro papel
-- "anon" (e o papel que o PostgREST usa quando autentica com uma chave
-- publicavel/anon) - equivalente ao que a chave ja faz hoje, so que explicito
-- e sem o aviso.
alter table public.infos_titan enable row level security;

create policy "infos_titan_leitura_anon" on public.infos_titan
  for select to anon using (true);

create policy "infos_titan_insercao_anon" on public.infos_titan
  for insert to anon with check (true);

create policy "infos_titan_atualizacao_anon" on public.infos_titan
  for update to anon using (true) with check (true);
