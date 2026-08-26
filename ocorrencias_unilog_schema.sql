-- Rode isso UMA VEZ no SQL Editor do Supabase (mesmo projeto ozwcyrkzsqzmavjtsmsp
-- que infos_titan ja usa).
-- Guarda os rascunhos de ocorrencia da Unilog CD ANTES de irem pro formulario
-- real da Unilog - fica aqui (nao no D1/env.DB do app) de proposito, pra que
-- unilog_form_bot.py (que roda na maquina de quem for usar, SEM sessao
-- logada no GoDeploy) consiga ler sem precisar de login algum, igual o
-- titan_watcher.py ja faz pra infos_titan.

create table if not exists public.ocorrencias_unilog (
  protocolo text primary key,
  marca text,
  numero_pedido text,
  numero_nf text,
  cnpj text,
  nome_agente text,
  telefone text,
  email text,
  tipo_ocorrencia text,
  especificacao text,
  descricao text,
  endereco_entrega text,
  qtd_anexos integer default 0,
  -- rascunho = salvo pela Central, aguardando o bot abrir o formulario real
  -- preenchido = o bot ja abriu a aba preenchida (envio ainda e manual)
  status text not null default 'rascunho',
  criado_em timestamptz not null default now()
);

alter table public.ocorrencias_unilog enable row level security;

create policy "ocorrencias_unilog_leitura_anon" on public.ocorrencias_unilog
  for select to anon using (true);

create policy "ocorrencias_unilog_insercao_anon" on public.ocorrencias_unilog
  for insert to anon with check (true);

create policy "ocorrencias_unilog_atualizacao_anon" on public.ocorrencias_unilog
  for update to anon using (true) with check (true);
