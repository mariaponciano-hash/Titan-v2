# titan-watcher-worker

Substitui o `titan_watcher.py` — em vez de ficar rodando aberto no seu PC, isso
roda como um Cloudflare Worker com Browser Rendering, acordando sozinho a cada
5 minutos (Cron Trigger) pra processar o que estiver `pendente` em
`infos_titan`.

**Não portei o `titan_backfill.py`** (a exportação em massa via .xlsx) — isso
precisa de sistema de arquivos e de uma biblioteca de leitura de Excel, que
não existem nesse ambiente. `titan_backfill.py` continua rodando no seu PC
(Task Scheduler) por enquanto.

## Passo a passo pra publicar (você mesma, no seu terminal — eu não tenho
## acesso à sua conta Cloudflare)

1. **Instalar dependências** (uma vez só), dentro desta pasta:
   ```powershell
   cd "C:\Users\Notebook\Desktop\central_tickets\titan_cf_worker"
   npm install
   ```

2. **Logar o wrangler na sua conta Cloudflare** (abre uma aba do navegador
   pra você autorizar — é você quem faz esse login, não eu):
   ```powershell
   npx wrangler login
   ```

3. **Guardar as credenciais como secrets** (cada comando abaixo pede o valor
   digitado, sem aparecer na tela — nunca fica salvo em nenhum arquivo aqui):
   ```powershell
   npx wrangler secret put TITAN_EMAIL
   npx wrangler secret put TITAN_SENHA
   npx wrangler secret put TITAN_SB_URL
   npx wrangler secret put TITAN_SB_KEY
   ```
   `TITAN_SB_URL` é `https://ozwcyrkzsqzmavjtsmsp.supabase.co` e `TITAN_SB_KEY`
   é a mesma chave "publishable" que já está no `titan_watcher.py` — pega
   direto de lá.

4. **Publicar:**
   ```powershell
   npx wrangler deploy
   ```
   Isso já cria o Worker com o Cron Trigger (a cada 5 min) e o binding do
   Browser Rendering, conforme `wrangler.toml`.

5. **Se der erro de permissão no Browser Rendering:** esse recurso pode
   precisar do plano pago do Workers (Workers Paid, ~US$5/mês) dependendo do
   volume — confira em **Workers & Pages → Browser Rendering** no painel.

## Testar sem esperar o Cron

O Worker responde a um GET normal (útil só pra testar o deploy inicial):
```powershell
curl https://titan-watcher-worker.<seu-subdominio>.workers.dev
```
(a URL exata aparece no terminal depois do `wrangler deploy`)

**Importante:** essa URL fica pública por padrão. Depois de confirmar que
funciona, vale restringir o acesso (Cloudflare Access, ou simplesmente não
divulgar a URL) — o Cron Trigger já cobre o uso normal, ninguém precisa
chamar essa URL manualmente no dia a dia.

## Acompanhar o que está rodando

**Workers & Pages → titan-watcher-worker → Logs** (em tempo real) mostra cada
rodada: quantos pendentes achou, o romaneio de cada um, e qualquer erro.

## Se algum seletor quebrar

O Titan pode mudar de layout a qualquer momento (é uma dashboard proprietária
sem API documentada — mesmo aviso que já valia pro script Python). Se um
passo começar a falhar, me manda a mensagem de erro exata dos Logs que eu
ajusto o seletor.
