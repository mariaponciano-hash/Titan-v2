-- Rode isso UMA VEZ no SQL Editor, DEPOIS de worker_heartbeats_schema.sql E
-- depois que o Worker ja tiver gravado pelo menos UM heartbeat de verdade
-- (senao o watchdog dispara um alerta falso de "nunca rodou" assim que for
-- ligado - confira antes com "select * from public.worker_heartbeats;").
--
-- Watchdog independente da Cloudflare (26/08/2026, a pedido da Ivna): roda
-- inteiramente dentro do Supabase (pg_cron + pg_net), sem Edge Function -
-- decidido pra nao introduzir o Supabase CLI como ferramenta nova so pra
-- isso, e pra manter o mesmo padrao de arquivo .sql solto que o resto do
-- schema ja usa. Sobrevive mesmo se a conta Cloudflare inteira cair, ja que
-- nao depende de nada rodando la.
--
-- A URL do webhook do Slack NAO fica em texto puro aqui (arquivos .sql deste
-- repo ficam soltos no disco, sem controle de versao) - fica no Supabase
-- Vault. Rode isso ANTES do resto deste arquivo, com a URL de verdade do seu
-- Incoming Webhook do Slack (criar um em api.slack.com/apps se ainda nao
-- tiver):
--
--   select vault.create_secret(
--     'https://hooks.slack.com/services/REAL/WEBHOOK/URL',
--     'titan_watchdog_slack_webhook',
--     'Slack incoming webhook do canal de alertas do titan-watcher-worker'
--   );
--
-- Pra trocar a URL depois: select vault.update_secret(id, 'novo_valor')
-- from vault.secrets where name = 'titan_watchdog_slack_webhook';

create extension if not exists pg_cron with schema extensions;
create extension if not exists pg_net with schema extensions;
-- Vault ja vem habilitado na maioria dos projetos Supabase; se der erro
-- "extension does not exist" no create_secret acima, habilite pelo painel
-- (Database > Extensions > vault) antes de continuar.

create or replace function public.titan_watchdog_check()
returns void
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  hb record;
  webhook_url text;
  unhealthy boolean;
  motivo text;
begin
  select * into hb from public.worker_heartbeats where worker = 'titan-watcher-worker';

  if hb is null then
    unhealthy := true;
    motivo := 'titan-watcher-worker nunca gravou um heartbeat ainda.';
  elsif hb.last_run_at < now() - interval '20 minutes' then
    -- 20 min (nao os 15 min literais do pedido original) = 4x o intervalo
    -- normal do Cron (5 min), dando uma folga real pra variacao normal (cold
    -- start do Browser Rendering, fila mais cheia) sem deixar de pegar uma
    -- queda de verdade em ~35 min no pior caso (15 min ate o proximo tick
    -- deste watchdog + 20 min do limiar).
    unhealthy := true;
    motivo := format('ultimo heartbeat ha %s (esperado a cada 5 min).', now() - hb.last_run_at);
  elsif hb.status = 'error' then
    unhealthy := true;
    motivo := format('ultima rodada terminou em erro: %s', coalesce(hb.detail, '(sem detalhe)'));
  else
    unhealthy := false;
  end if;

  if not unhealthy then
    return;
  end if;

  -- Cooldown: continua checando a cada 15 min, mas so reenvia Slack se o
  -- ultimo alerta foi ha mais de 1h (ou nunca alertou) - evita floodar o
  -- canal durante uma queda longa, sem parar de lembrar de vez em quando.
  if hb is not null and hb.last_alert_at is not null and hb.last_alert_at > now() - interval '1 hour' then
    return;
  end if;

  select decrypted_secret into webhook_url
  from vault.decrypted_secrets
  where name = 'titan_watchdog_slack_webhook';

  if webhook_url is null then
    raise warning 'titan_watchdog_check: secret titan_watchdog_slack_webhook nao encontrado no Vault.';
    return;
  end if;

  perform net.http_post(
    url := webhook_url,
    headers := '{"Content-Type": "application/json"}'::jsonb,
    body := jsonb_build_object('text', format(':rotating_light: *titan-watcher-worker* travado/com erro. %s', motivo))
  );

  update public.worker_heartbeats set last_alert_at = now() where worker = 'titan-watcher-worker';
end;
$$;

select cron.schedule(
  'titan_watchdog_check_15min',
  '*/15 * * * *',
  $$select public.titan_watchdog_check();$$
);
