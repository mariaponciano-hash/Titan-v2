// Backend da Central de Tickets — verificacao de Gmail + classificacao automatica via IA.
// Substitui o n8n inteiro nessa funcao: detecta emails novos direto via Gmail API
// (usando historyId para nao reprocessar o que ja foi visto), cruza com o Supabase
// pra saber se o ticket existe, e classifica via Gogroup AI Proxy quando ha resposta.
//
// Protegido por: precisa do header X-Trigger-Key batendo com env.CLASSIFY_TRIGGER_KEY,
// OU vir com o header X-Godeploy-Cron (chamadas do cron do proprio GoDeploy).
// Isso evita que qualquer visitante do app (que hoje e publico) consiga disparar
// chamadas pagas (Gmail API + IA) so por descobrir a URL do endpoint.
//
// MULTI-CONTA / MULTI-MARCA (04/08/2026): originalmente este backend so conhecia
// uma conta Gmail (Gobeaute) e uma tabela (tickets_gobeaute). A Gocase ja tinha
// tickets sendo criados numa tabela paralela (tickets_gocase) por outro pipeline,
// mas NUNCA teve a camada de deteccao de resposta + classificacao de IA rodando -
// zero linhas com tem_resposta=true, Ultima mensagem ou Motivo preenchidos, apesar
// de 20mil+ tickets acumulados. Causa raiz: os emails da Gocase saem de DUAS caixas
// Gmail diferentes (cx.tickets@gocase.com e cx.agentes@gocase.com, contas separadas,
// nao aliases), nenhuma delas com watch/Pub-Sub configurado neste app. A partir desta
// versao, o conceito de "conta Gmail" foi extraido pra um array (CONTAS), e toda
// funcao que falava com Gmail ou Supabase passou a receber a conta/tabela como
// parametro em vez de tabela/credencial fixa. Isso permite plugar novas contas so
// adicionando uma entrada em CONTAS + os 3 secrets de OAuth, sem duplicar codigo.

export interface Env {
  AI_PROXY_TOKEN: string;
  CLASSIFY_TRIGGER_KEY: string;
  PUBSUB_VERIFY_TOKEN: string;
  PUBSUB_VERIFY_TOKEN_GOCASE?: string;
  // Conta "gobeaute" (nome mantido sem sufixo por retrocompatibilidade - e a
  // conta original, ja em produção desde o inicio deste app)
  GMAIL_CLIENT_ID: string;
  GMAIL_CLIENT_SECRET: string;
  GMAIL_REFRESH_TOKEN: string;
  // Conta "gocase_tickets" (cx.tickets@gocase.com) - a configurar
  GMAIL_CLIENT_ID_GOCASE_TICKETS?: string;
  GMAIL_CLIENT_SECRET_GOCASE_TICKETS?: string;
  GMAIL_REFRESH_TOKEN_GOCASE_TICKETS?: string;
  // Conta "gocase_agentes" (cx.agentes@gocase.com) - a configurar
  GMAIL_CLIENT_ID_GOCASE_AGENTES?: string;
  GMAIL_CLIENT_SECRET_GOCASE_AGENTES?: string;
  GMAIL_REFRESH_TOKEN_GOCASE_AGENTES?: string;
  // Torre de Controle Logistica (17/08/2026) - uma chave Intelipost e um token
  // Shopify por marca. Ficam como secrets do app (nunca no codigo): varias
  // marcas compartilham a mesma chave Intelipost hoje, mas manter uma entrada
  // por marca permite rotacionar/trocar uma sem mexer nas outras.
  INTELIPOST_KEY_LESCENT?: string;
  INTELIPOST_KEY_KOKESHI?: string;
  INTELIPOST_KEY_BYSAMIA?: string;
  INTELIPOST_KEY_AUA?: string;
  INTELIPOST_KEY_BARBOURS?: string;
  INTELIPOST_KEY_RITUARIA?: string;
  INTELIPOST_KEY_APICE?: string;
  // GOCASE tem conta Intelipost propria (confirmado 24/08/2026, chave real) -
  // usada pela mesma buscarIntelipost() das outras marcas.
  INTELIPOST_KEY_GOCASE?: string;
  SHOPIFY_TOKEN_LESCENT?: string;
  SHOPIFY_TOKEN_KOKESHI?: string;
  SHOPIFY_TOKEN_BYSAMIA?: string;
  SHOPIFY_TOKEN_AUA?: string;
  SHOPIFY_TOKEN_BARBOURS?: string;
  SHOPIFY_TOKEN_RITUARIA?: string;
  SHOPIFY_TOKEN_APICE?: string;
  // Destino final do ticket de logistica (CX Hub / n8n). Enquanto nao existir,
  // o ticket fica gravado so localmente - ver /api/logistica/abrir-ticket.
  CXHUB_TICKET_WEBHOOK?: string;
  // GOCASE (24/08/2026): usa a Intelipost real (INTELIPOST_KEY_GOCASE acima)
  // pro rastreio, mas o pedido (NF/CPF/endereco/itens - papel da Shopify pras
  // outras marcas) ainda vem do ERP "Factory" via API do Metabase
  // (metabase.gocase.com.br), com a chave pessoal da Ivna (grupo "All Users")
  // - so leitura, nunca escrita. Ver buscarPedidoGocase.
  METABASE_KEY_GOCASE?: string;
  // EAN/GTIN por SKU (25/08/2026): a Intelipost so devolve o SKU dos itens do
  // pedido, nunca o EAN - sao campos diferentes no cadastro de produto (base
  // "Cosmos" do Metabase Gobeaute, tabela products: colunas sku e gtin
  // distintas, confirmado com um produto real da Rituaria). Chave gerada pela
  // Ivna especificamente pra isso - so leitura. Ver buscarEanPorSku.
  METABASE_KEY_GOBEAUTE?: string;
  DB: any; // SQLite embutido do GoDeploy - binding reservado, injetado automaticamente
}

const SB_URL = 'https://ozwcyrkzsqzmavjtsmsp.supabase.co';
const SB_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im96d2N5cmt6c3F6bWF2anRzbXNwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0ODcyMjksImV4cCI6MjA5NjA2MzIyOX0.UhdzBybx6q7TlWl7F6kvDWFxxEMOMze-b7vaxNgOt_Y';

const GB_SB_URL = 'https://syvnlauhwaafszcllvnd.supabase.co';
const GB_SB_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN5dm5sYXVod2FhZnN6Y2xsdm5kIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIyMjE3NDksImV4cCI6MjA5Nzc5Nzc0OX0.zklIi7V_bi2NqoDoQXAMZ8fe0vWuurb2oJQ78cR36VQ';
const GB_SB_SCHEMA = 'centralizacao';
const GB_FETCH_TIMEOUT_MS = 8000;

// TITAN BI (24/08/2026) - tabela "infos_titan" no MESMO projeto Supabase de
// SB_URL acima, so que com uma chave "publishable" separada (formato novo do
// Supabase) que a Ivna gerou so pra isso. Funciona como fila E cache ao mesmo
// tempo: a Torre insere status='pendente' quando o agente abre um ticket; um
// script rodando no PC da Ivna (nunca aqui - precisa login real no Titan, ver
// titan_bi_scraper.py) le essa fila, consulta o Titan e grava o resultado de
// volta com status='concluido'. A Torre so faz polling curto (GET) ate
// aparecer o resultado ou dar timeout.
const TITAN_SB_URL = SB_URL;
const TITAN_SB_KEY = 'sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ';

function titanHeaders() {
  return { apikey: TITAN_SB_KEY, Authorization: `Bearer ${TITAN_SB_KEY}`, 'Content-Type': 'application/json' };
}

const AI_PROXY_URL = 'https://ai-proxy.gogroupbr.com/v1/chat/completions';
const AI_PROXY_MODEL = 'gpt-5.4-mini';

// AUTOMACAO DE AVARIA (05/08/2026): mesmo webhook n8n que o botao "Enviar" do
// modal de resposta usa no frontend (index.html). Aqui e chamado direto do
// backend, sem intervencao de agente, quando um ticket da Gobeaute e
// classificado como AVARIA - ver notificarAvariaSeNecessario().
const WEBHOOK_ENVIAR = 'https://n8n-prod.gogroupgl.com/webhook/enviar-resposta-ticket';

// Texto fixo definido pela Maria (05/08/2026) pra resposta automatica de AVARIA.
// So Gobeaute, so uma vez por thread (ver tabela avarias_notificadas).
const TEXTO_AVARIA_AUTOMATICO = 'O procedimento ideal é que a transportadora atualize o status do pedido para Avaria na Intelipost e encaminhe a solicitação para análise.\nApós essa atualização, nossa equipe realizará a validação do caso para definir o tratamento mais adequado, determinando se o pedido seguirá para devolução ou ressarcimento por perda, conforme o cenário identificado.';

// REVERTIDO (11/08/2026): tinha sido temporariamente subido de 15 para 60 em
// 04/08/2026 pra acelerar o dreno do backlog historico de classificacao.
// Backlog real conferido em 11/08/2026 via /api/debug-pendentes: 8 tickets
// pendentes em tickets_gobeaute, 2 em tickets_gocase - praticamente zerado.
// Nao ha mais motivo pra manter o lote 4x maior (720/hora vs 180/hora), que so
// aumenta o consumo de IA e Gmail API por execucao sem necessidade agora.
const BATCH_SIZE = 15;
const SCAN_WINDOW = 500;

const CLASSIFICACAO_MAX_ESPERA_MS = 2 * 60 * 60 * 1000;

const AI_MIN_INTERVAL_MS = 600;
const AI_MAX_RETRIES = 4;
const AI_RETRY_BASE_MS = 1500;

// ============ CONTAS GMAIL / TABELAS ============

interface ContaGmail {
  id: string;
  tabela: string;
  envClientId: keyof Env;
  envClientSecret: keyof Env;
  envRefreshToken: keyof Env;
  topic: string; // topico Pub/Sub completo usado no watch()
  remetenteFiltro?: string;
}

const CONTAS: ContaGmail[] = [
  {
    id: 'gobeaute',
    tabela: 'tickets_gobeaute',
    envClientId: 'GMAIL_CLIENT_ID',
    envClientSecret: 'GMAIL_CLIENT_SECRET',
    envRefreshToken: 'GMAIL_REFRESH_TOKEN',
    topic: 'projects/tickets-468112/topics/gmail-tickets-notifications',
  },
  {
    id: 'gocase_tickets',
    tabela: 'tickets_gocase',
    envClientId: 'GMAIL_CLIENT_ID_GOCASE_TICKETS',
    envClientSecret: 'GMAIL_CLIENT_SECRET_GOCASE_TICKETS',
    envRefreshToken: 'GMAIL_REFRESH_TOKEN_GOCASE_TICKETS',
    topic: 'projects/tickets-automation-469820/topics/gmail-tickets-notifications-gocase-tickets',
    remetenteFiltro: '*cx.tickets@gocase.com*',
  },
  {
    id: 'gocase_agentes',
    tabela: 'tickets_gocase',
    envClientId: 'GMAIL_CLIENT_ID_GOCASE_AGENTES',
    envClientSecret: 'GMAIL_CLIENT_SECRET_GOCASE_AGENTES',
    envRefreshToken: 'GMAIL_REFRESH_TOKEN_GOCASE_AGENTES',
    topic: 'projects/tickets-automation-469820/topics/gmail-tickets-notifications-gocase-agentes',
    remetenteFiltro: '*cx.agentes@gocase.com*',
  },
];

const TABELAS_VALIDAS = new Set(CONTAS.map((c) => c.tabela));

function getConta(id: string): ContaGmail {
  const c = CONTAS.find((x) => x.id === id);
  if (!c) throw new Error(`conta gmail desconhecida: "${id}". Contas validas: ${CONTAS.map((x) => x.id).join(', ')}`);
  return c;
}

function tabelaOuPadrao(url: URL): string {
  const t = url.searchParams.get('tabela') || 'tickets_gobeaute';
  if (!TABELAS_VALIDAS.has(t)) throw new Error(`tabela desconhecida: "${t}". Validas: ${Array.from(TABELAS_VALIDAS).join(', ')}`);
  return t;
}

function inferirConta(tabela: string, remetente?: string | null): ContaGmail | null {
  const candidatas = CONTAS.filter((c) => c.tabela === tabela);
  if (candidatas.length === 1) return candidatas[0];
  if (!remetente) return null;
  const r = remetente.toLowerCase();
  for (const c of candidatas) {
    if (c.remetenteFiltro) {
      const agulha = c.remetenteFiltro.replace(/\*/g, '').toLowerCase();
      if (r.includes(agulha)) return c;
    }
  }
  return null;
}

async function initThrottleTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS ai_throttle (k TEXT PRIMARY KEY, v TEXT)`, []);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function aguardarJanelaIA(env: Env): Promise<void> {
  await initThrottleTable(env);
  const res = await env.DB.query(`SELECT v FROM ai_throttle WHERE k = ?`, ['last_call_ts']);
  const last = res.rows && res.rows.length ? parseInt(res.rows[0].v, 10) || 0 : 0;
  const agora = Date.now();
  const jitter = Math.floor(Math.random() * 80);
  const espera = last + AI_MIN_INTERVAL_MS - agora + jitter;
  if (espera > 0) {
    await sleep(espera);
  }
  await env.DB.exec(
    `INSERT INTO ai_throttle (k, v) VALUES ('last_call_ts', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v`,
    [String(Date.now())]
  );
}

const CATEGORIAS = [
  'AVARIA — Transportadora confirmou avaria, vazamento ou dano no volume',
  'EXTRAVIO — Transportadora confirmou que o objeto foi extraviado ou roubado',
  'DEVOLUCAO — Objeto já retornou (ou retornará confirmadamente) ao remetente ou centro de distribuição',
  'ENTREGA_CONFIRMADA — Transportadora confirmou que o objeto foi entregue ao destinatário',
  'REENTREGA_CONFIRMADA — Transportadora confirmou nova tentativa de entrega com data ou prazo definido',
  'OBJETO_EM_TRANSITO — Objeto está em trânsito normal, sem problema reportado',
  'SOLICITACAO_DE_INFORMACAO — Transportadora pediu algo da gente: documento, romaneio, comprovante, ou indicou que este não é o canal correto',
  'OUTROS — Qualquer outra situação: aguardar prazo, investigação em andamento, solicitação apenas recebida sem novidade, tentativa de entrega frustrada ainda sem definição, endereço, ou mensagem vaga',
].join('\n- ');

const ORDEM_PRECEDENCIA = [
  'EXTRAVIO', 'AVARIA', 'DEVOLUCAO', 'SOLICITACAO_DE_INFORMACAO',
  'ENTREGA_CONFIRMADA', 'REENTREGA_CONFIRMADA', 'OBJETO_EM_TRANSITO', 'OUTROS',
].join(' > ');

interface Classificacao {
  precisa_responder: boolean;
  motivo: string;
  urgencia: string;
}

function buildPrompt(subject: string, ultimaMensagem: string): string {
  return `Você é um especialista em logística e suporte ao cliente de um e-commerce brasileiro. Analise a última mensagem recebida da transportadora em um thread de email de ocorrência logística.

Assunto: ${subject}
Última mensagem recebida da transportadora: ${ultimaMensagem}

TAREFA 1 — Classifique o motivo usando EXATAMENTE uma destas categorias:
- ${CATEGORIAS}

Critério de desempate quando mais de uma categoria parecer aplicável, nesta ordem: ${ORDEM_PRECEDENCIA}. Ou seja, se a mensagem confirma extravio, use EXTRAVIO mesmo que também mencione avaria ou pedido de documento.

TAREFA 2 — Decida se essa mensagem exige ação ou resposta do time de suporte (precisa_responder).
REGRAS precisa_responder:
- true: AVARIA, EXTRAVIO, SOLICITACAO_DE_INFORMACAO (exige que a gente envie o que foi pedido ou reencaminhe o contato)
- false: DEVOLUCAO (objeto já retornou/retornará ao remetente ou CD - fica com status Respondido, sem exigir ação de resposta ao contato), ENTREGA_CONFIRMADA, REENTREGA_CONFIRMADA, OBJETO_EM_TRANSITO (são confirmações de rotina, sem pendência nossa)
- Para OUTROS: avalie o conteúdo. false se for só confirmação/rotina (aguardar prazo, solicitação recebida sem novidade); true se pedir alguma ação nossa mesmo sem se encaixar nas outras categorias (ex.: pedido de correção de endereço, reclamação direta do destinatário)

TAREFA 3 — Defina a urgência.
REGRAS urgencia:
- alta: EXTRAVIO, AVARIA
- media: DEVOLUCAO, SOLICITACAO_DE_INFORMACAO
- baixa: ENTREGA_CONFIRMADA, REENTREGA_CONFIRMADA, OBJETO_EM_TRANSITO, e OUTROS quando for confirmação/rotina; media se OUTROS envolver algo que precisa de ação nossa

EXEMPLOS para os casos de fronteira mais comuns:

Exemplo 1 (ENTREGA_CONFIRMADA vs REENTREGA_CONFIRMADA):
Mensagem: "Informamos que o pedido foi entregue com sucesso em 15/07."
Resposta: {"motivo": "ENTREGA_CONFIRMADA — entrega confirmada em 15/07", "precisa_responder": false, "urgencia": "baixa"}

Mensagem: "Nova tentativa de entrega agendada para amanhã, 16/07."
Resposta: {"motivo": "REENTREGA_CONFIRMADA — nova tentativa agendada para 16/07", "precisa_responder": false, "urgencia": "baixa"}

Exemplo 2 (SOLICITACAO_DE_INFORMACAO vs OUTROS):
Mensagem: "O canal correto para essa demanda é: mgsac.gocase@jtexpress.com.br"
Resposta: {"motivo": "SOLICITACAO_DE_INFORMACAO — transportadora indicou outro canal de atendimento", "precisa_responder": true, "urgencia": "media"}

Mensagem: "Poderia enviar o romaneio de coleta para que possamos verificar o pedido?"
Resposta: {"motivo": "SOLICITACAO_DE_INFORMACAO — transportadora pediu romaneio de coleta", "precisa_responder": true, "urgencia": "media"}

Mensagem: "Sua solicitação foi recebida e está em análise."
Resposta: {"motivo": "OUTROS — confirmação de recebimento sem novidade de status", "precisa_responder": false, "urgencia": "baixa"}

Exemplo 3 (DEVOLUCAO vs OUTROS):
Mensagem: "Houve 2 tentativas de entrega sem sucesso, destinatário não localizado. Aguardando definição se o volume retorna ou nova tentativa é feita."
Resposta: {"motivo": "OUTROS — tentativas frustradas, retorno ainda não confirmado", "precisa_responder": true, "urgencia": "media"}
(Só vira DEVOLUCAO quando a transportadora confirma que o retorno JÁ ACONTECEU ou está definitivamente decidido, não quando ainda está em aberto.)

Mensagem: "O objeto retornou ao centro de distribuição após 2 tentativas frustradas."
Resposta: {"motivo": "DEVOLUCAO — objeto retornou ao CD após tentativas frustradas", "precisa_responder": false, "urgencia": "media"}

Exemplo 4 (AVARIA vs EXTRAVIO):
Mensagem: "Identificamos que o pedido apresentou avaria e, conforme procedimento interno, deve ser direcionado ao time de Ressarcimento."
Resposta: {"motivo": "AVARIA — avaria confirmada, direcionado para ressarcimento", "precisa_responder": true, "urgencia": "alta"}

Mensagem: "Acareação concluída, objeto confirmado como extraviado."
Resposta: {"motivo": "EXTRAVIO — acareação concluída, extravio confirmado", "precisa_responder": true, "urgencia": "alta"}

Responda SOMENTE com este JSON, sem markdown, sem texto adicional:
{"precisa_responder": true ou false, "motivo": "CATEGORIA — descrição breve em português com detalhes relevantes", "urgencia": "baixa | media | alta"}`;
}

async function classificarComProxy(env: Env, token: string, subject: string, ultimaMensagem: string): Promise<Classificacao> {
  let ultimoErro: any = null;
  for (let tentativa = 0; tentativa <= AI_MAX_RETRIES; tentativa++) {
    await aguardarJanelaIA(env);
    let r: Response;
    try {
      r = await fetch(AI_PROXY_URL, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          model: AI_PROXY_MODEL,
          temperature: 0,
          messages: [{ role: 'user', content: buildPrompt(subject, ultimaMensagem) }],
        }),
      });
    } catch (e: any) {
      ultimoErro = e;
      if (tentativa === AI_MAX_RETRIES) break;
      await sleep(AI_RETRY_BASE_MS * Math.pow(2, tentativa));
      continue;
    }
    if (r.status === 429) {
      const errText = await r.text().catch(() => '');
      ultimoErro = new Error(`AI Proxy HTTP 429: ${errText.slice(0, 300)}`);
      if (tentativa === AI_MAX_RETRIES) break;
      await sleep(AI_RETRY_BASE_MS * Math.pow(2, tentativa));
      continue;
    }
    if (!r.ok) {
      const errText = await r.text().catch(() => '');
      throw new Error(`AI Proxy HTTP ${r.status}: ${errText.slice(0, 300)}`);
    }
    const data: any = await r.json();
    const raw = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
    const clean = String(raw).replace(/```json|```/g, '').trim();
    let parsed: any;
    try {
      parsed = JSON.parse(clean);
    } catch (e) {
      throw new Error(`resposta da IA nao e JSON valido. raw="${String(raw).slice(0, 200)}" respostaCompleta=${JSON.stringify(data).slice(0, 300)}`);
    }
    const motivo = String(parsed.motivo || '').trim();
    if (!motivo) {
      throw new Error(`IA devolveu motivo vazio. parsed=${JSON.stringify(parsed).slice(0, 300)} raw="${String(raw).slice(0, 200)}"`);
    }
    const categoria = motivo.split('—')[0].trim().toUpperCase();
    const precisaResponder = categoria === 'DEVOLUCAO' ? false : !!parsed.precisa_responder;
    return {
      precisa_responder: precisaResponder,
      motivo,
      urgencia: String(parsed.urgencia || 'baixa'),
    };
  }
  throw new Error(`AI Proxy: esgotadas ${AI_MAX_RETRIES + 1} tentativas (rate limit persistente). Ultimo erro: ${String((ultimoErro && ultimoErro.message) || ultimoErro)}`);
}

async function buscarPendentes(tabela: string, desde?: string, ate?: string, limit: number = SCAN_WINDOW): Promise<any[]> {
  const SELECT_PENDENTES = [
    'id', 'thread_id', 'message_id', 'subject', 'data_email', 'remetente',
    'total_mensagens_thread', encodeURIComponent('"Ultima mensagem"'), 'updated_at',
  ].join(',');
  let url = `${SB_URL}/rest/v1/${tabela}?select=${SELECT_PENDENTES}&tem_resposta=eq.true&Motivo=is.null&order=data_email.asc&limit=${limit}`;
  if (desde) url += `&data_email=gte.${encodeURIComponent(desde)}`;
  if (ate) url += `&data_email=lte.${encodeURIComponent(ate)}`;
  const r = await fetch(url, { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } });
  if (!r.ok) throw new Error(`Supabase HTTP ${r.status}`);
  const rows: any[] = await r.json();
  return rows.filter((t) => !t['Motivo'] || String(t['Motivo']).trim() === '');
}

async function gravarClassificacao(tabela: string, threadId: string, cls: Classificacao): Promise<boolean> {
  const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
    method: 'PATCH',
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({
      'Precisa responder?': cls.precisa_responder,
      Motivo: cls.motivo,
      Urgência: cls.urgencia,
      updated_at: new Date().toISOString(),
    }),
  });
  if (!r.ok) return false;
  let ok = false;
  try {
    const rows: any[] = await r.json();
    ok = Array.isArray(rows) && rows.length > 0;
  } catch {
    ok = false;
  }
  if (ok) {
    await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}&Status_resposta_tickets=eq.aguardando`, {
      method: 'PATCH',
      headers: {
        apikey: SB_KEY,
        Authorization: `Bearer ${SB_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ Status_resposta_tickets: null }),
    }).catch(() => {});
  }
  return ok;
}

function autorizado(request: Request, env: Env): boolean {
  if (request.headers.get('x-godeploy-cron')) return true;
  const chave = request.headers.get('x-trigger-key');
  return !!chave && !!env.CLASSIFY_TRIGGER_KEY && chave === env.CLASSIFY_TRIGGER_KEY;
}

async function getGmailAccessToken(env: Env, conta: ContaGmail): Promise<string> {
  const clientId = env[conta.envClientId];
  const clientSecret = env[conta.envClientSecret];
  const refreshToken = env[conta.envRefreshToken];
  if (!clientId || !clientSecret || !refreshToken) {
    throw new Error(`Credenciais Gmail nao configuradas para a conta "${conta.id}" (esperado nos secrets: ${conta.envClientId}, ${conta.envClientSecret}, ${conta.envRefreshToken})`);
  }
  const r = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      refresh_token: refreshToken,
      grant_type: 'refresh_token',
    }).toString(),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error(`Falha ao renovar access token do Gmail (conta ${conta.id}): HTTP ${r.status} ${t.slice(0, 300)}`);
  }
  const data: any = await r.json();
  return data.access_token;
}

async function initSyncTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS gmail_sync (k TEXT PRIMARY KEY, v TEXT)`, []);
}
async function getLastHistoryId(env: Env, contaId: string): Promise<string | null> {
  await initSyncTable(env);
  const res = await env.DB.query(`SELECT v FROM gmail_sync WHERE k = ?`, [`last_history_id:${contaId}`]);
  if (res.rows && res.rows.length) return res.rows[0].v;
  return null;
}
async function setLastHistoryId(env: Env, contaId: string, v: string): Promise<void> {
  await initSyncTable(env);
  await env.DB.exec(
    `INSERT INTO gmail_sync (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v`,
    [`last_history_id:${contaId}`, v]
  );
}

async function gmailGet(accessToken: string, path: string): Promise<any> {
  const r = await fetch(`https://gmail.googleapis.com/gmail/v1/users/me${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    const err: any = new Error(`Gmail API HTTP ${r.status}: ${t.slice(0, 300)}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

function getHeader(message: any, name: string): string {
  const headers = (message.payload && message.payload.headers) || [];
  const h = headers.find((x: any) => (x.name || '').toLowerCase() === name.toLowerCase());
  return h ? h.value : '';
}

function decodeBase64Url(data: string): string {
  if (!data) return '';
  const b64 = data.replace(/-/g, '+').replace(/_/g, '/');
  try {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder('utf-8').decode(bytes);
  } catch {
    return '';
  }
}

function stripHtml(html: string): string {
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

function resumirEstruturaPayload(p: any, prof = 0): any {
  if (!p) return null;
  return {
    profundidade: prof,
    mimeType: p.mimeType || null,
    temBodyData: !!(p.body && p.body.data),
    bodySize: (p.body && typeof p.body.size === 'number') ? p.body.size : null,
    temAttachmentId: !!(p.body && p.body.attachmentId),
    filename: p.filename || null,
    qtdParts: Array.isArray(p.parts) ? p.parts.length : 0,
    parts: Array.isArray(p.parts) ? p.parts.map((pp: any) => resumirEstruturaPayload(pp, prof + 1)) : [],
  };
}

function extrairCorpoMensagem(payload: any): string {
  if (!payload) return '';
  let textoPlano = '';
  let textoHtml = '';
  function visitar(p: any) {
    if (!p) return;
    const mime = (p.mimeType || '').toLowerCase().split(';')[0].trim();
    if (p.body && p.body.data) {
      if (mime === 'text/plain' && !textoPlano) {
        textoPlano = decodeBase64Url(p.body.data);
      } else if (mime === 'text/html' && !textoHtml) {
        textoHtml = decodeBase64Url(p.body.data);
      }
    }
    (p.parts || []).forEach(visitar);
  }
  visitar(payload);
  if (textoPlano.trim()) return textoPlano.trim();
  if (textoHtml.trim()) return stripHtml(textoHtml);
  return '';
}

const MAX_ULTIMA_MENSAGEM_CHARS = 3000;

function processThread(thread: any) {
  const messages = thread.messages || [];
  const mensagensRecebidas = messages.filter((m: any) => !((m.labelIds || []).includes('SENT')));
  const temResposta = mensagensRecebidas.length > 0;
  const ultimaRecebida = mensagensRecebidas[mensagensRecebidas.length - 1];
  let ultimaMensagem = '';
  if (temResposta) {
    const corpo = extrairCorpoMensagem(ultimaRecebida.payload).slice(0, MAX_ULTIMA_MENSAGEM_CHARS);
    ultimaMensagem = corpo.trim() ? corpo : (ultimaRecebida.snippet || '').trim();
  }
  const lastMsg = messages[messages.length - 1];
  const subject = lastMsg ? getHeader(lastMsg, 'Subject') : '';
  const ultimaRecebidaFrom = temResposta ? getHeader(ultimaRecebida, 'From') : '';
  const ultimaMensagemId = lastMsg ? lastMsg.id : '';
  const ultimaMensagemEnviada = !!(lastMsg && (lastMsg.labelIds || []).includes('SENT'));
  return {
    tem_resposta: temResposta,
    total_mensagens_thread: messages.length,
    ultima_mensagem: ultimaMensagem,
    subject,
    ultima_recebida_from: ultimaRecebidaFrom,
    ultima_mensagem_id: ultimaMensagemId,
    ultima_mensagem_enviada: ultimaMensagemEnviada,
  };
}

function extrairEmailDeCampo(raw: any): string {
  if (!raw) return '';
  let obj: any = raw;
  if (typeof raw === 'string') {
    try { obj = JSON.parse(raw); } catch { obj = null; }
  }
  if (obj && typeof obj === 'object') {
    const entry = Array.isArray(obj.value) ? obj.value[0] : (obj.address ? obj : null);
    if (entry && entry.address) return String(entry.address);
  }
  const s = typeof raw === 'string' ? raw : '';
  const m = s.match(/<(.+?)>/);
  if (m) return m[1];
  const m2 = s.match(/\S+@\S+\.\S+/);
  return m2 ? m2[0] : '';
}

async function definirStatusManual(tabela: string, threadId: string, status: string): Promise<boolean> {
  const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
    method: 'PATCH',
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({ Status_resposta_tickets: status, updated_at: new Date().toISOString() }),
  });
  if (!r.ok) return false;
  try {
    const rows: any[] = await r.json();
    return Array.isArray(rows) && rows.length > 0;
  } catch {
    return false;
  }
}

async function refletirRespostaJaEnviada(tabela: string, threadId: string): Promise<boolean> {
  const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}&select=Status_resposta_tickets&limit=1`, {
    headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` },
  });
  if (!r.ok) return false;
  let atual: string | null = null;
  try {
    const rows: any[] = await r.json();
    atual = rows[0] ? rows[0].Status_resposta_tickets : null;
  } catch {
    return false;
  }
  const body: any = { 'Precisa responder?': false, updated_at: new Date().toISOString() };
  if (atual !== 'extravio' && atual !== 'concluido') body.Status_resposta_tickets = 'aguardando';
  const r2 = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
    method: 'PATCH',
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify(body),
  });
  if (!r2.ok) return false;
  try {
    const rows2: any[] = await r2.json();
    return Array.isArray(rows2) && rows2.length > 0;
  } catch {
    return false;
  }
}

async function initRecheckOffsetTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS recheck_tratativa_offset (tabela TEXT PRIMARY KEY, offset_val INTEGER)`, []);
}
async function getRecheckOffset(env: Env, tabela: string): Promise<number> {
  await initRecheckOffsetTable(env);
  const res = await env.DB.query(`SELECT offset_val FROM recheck_tratativa_offset WHERE tabela = ?`, [tabela]);
  if (res.rows && res.rows.length) return parseInt(res.rows[0].offset_val, 10) || 0;
  return 0;
}
async function setRecheckOffset(env: Env, tabela: string, offsetVal: number): Promise<void> {
  await initRecheckOffsetTable(env);
  await env.DB.exec(
    `INSERT INTO recheck_tratativa_offset (tabela, offset_val) VALUES (?, ?) ON CONFLICT(tabela) DO UPDATE SET offset_val = excluded.offset_val`,
    [tabela, offsetVal]
  );
}

async function recheckTratativaCxLote(env: Env, tabela: string, limit: number): Promise<any> {
  const candidatos: any[] = [];
  let offset = 0;
  const TETO_SCAN = 20000;
  while (offset < TETO_SCAN) {
    const r = await fetch(
      `${SB_URL}/rest/v1/${tabela}?select=*&Motivo=not.is.null&order=id.asc`,
      {
        headers: {
          apikey: SB_KEY,
          Authorization: `Bearer ${SB_KEY}`,
          'Range-Unit': 'items',
          Range: `${offset}-${offset + SCAN_WINDOW - 1}`,
        },
      }
    );
    if (!r.ok) {
      const errBody = await r.text().catch(() => '');
      throw new Error(`Supabase HTTP ${r.status} (${tabela}): ${errBody.slice(0, 300)}`);
    }
    const page: any[] = await r.json();
    candidatos.push(...page);
    if (page.length < SCAN_WINDOW) break;
    offset += SCAN_WINDOW;
  }

  const elegiveis = candidatos.filter(
    (t) =>
      t.thread_id &&
      t['Precisa responder?'] === true &&
      t.Status_resposta_tickets !== 'extravio' &&
      t.Status_resposta_tickets !== 'concluido'
  );
  if (!elegiveis.length) return { tabela, elegiveisTotal: 0, loteTentado: 0, verificados: 0, corrigidos: 0, erros: [] };

  let inicio = await getRecheckOffset(env, tabela);
  if (inicio >= elegiveis.length) inicio = 0;
  let lote = elegiveis.slice(inicio, inicio + limit);
  if (lote.length < limit && elegiveis.length > lote.length) {
    const faltam = Math.min(limit - lote.length, inicio);
    lote = lote.concat(elegiveis.slice(0, faltam));
  }
  const novoOffset = elegiveis.length ? (inicio + lote.length) % elegiveis.length : 0;
  await setRecheckOffset(env, tabela, novoOffset);

  const tokensPorConta = new Map<string, string | null>();
  async function tokenParaConta(conta: ContaGmail): Promise<string | null> {
    if (tokensPorConta.has(conta.id)) return tokensPorConta.get(conta.id)!;
    try {
      const tok = await getGmailAccessToken(env, conta);
      tokensPorConta.set(conta.id, tok);
      return tok;
    } catch {
      tokensPorConta.set(conta.id, null);
      return null;
    }
  }

  let verificados = 0;
  let corrigidos = 0;
  const erros: string[] = [];
  for (const item of lote) {
    try {
      const conta = inferirConta(tabela, item.remetente);
      if (!conta) {
        erros.push(`thread_id ${item.thread_id}: nao foi possivel inferir a conta Gmail`);
        continue;
      }
      const token = await tokenParaConta(conta);
      if (!token) {
        erros.push(`thread_id ${item.thread_id}: sem access token (conta ${conta.id})`);
        continue;
      }
      const thread = await gmailGet(token, `/threads/${encodeURIComponent(item.thread_id)}?format=full`);
      const info = processThread(thread);
      verificados++;
      if (info.ultima_mensagem_enviada) {
        const ok = await refletirRespostaJaEnviada(tabela, item.thread_id);
        if (ok) corrigidos++;
      }
    } catch (e: any) {
      erros.push(`thread_id ${item.thread_id}: ${String((e && e.message) || e)}`);
    }
  }

  return { tabela, elegiveisTotal: elegiveis.length, loteTentado: lote.length, verificados, corrigidos, erros };
}

async function marcarStatusConcluido(tabela: string, threadId: string): Promise<boolean> {
  return definirStatusManual(tabela, threadId, 'concluido');
}

async function initAvariaNotificadaTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS avarias_notificadas (thread_id TEXT PRIMARY KEY, enviado_em TEXT, para_email TEXT)`, []);
}

async function notificarAvariaSeNecessario(
  env: Env,
  tabela: string,
  threadId: string,
  categoria: string,
  subject: string,
  paraEmailBruto: string,
  messageId: string
): Promise<void> {
  if (tabela !== 'tickets_gobeaute') return;
  if (categoria !== 'AVARIA') return;
  if (!threadId) return;

  await initAvariaNotificadaTable(env);
  const existente = await env.DB.query(`SELECT thread_id FROM avarias_notificadas WHERE thread_id = ?`, [threadId]);
  if (existente.rows && existente.rows.length) return;

  const paraEmail = extrairEmailDeCampo(paraEmailBruto);
  if (!paraEmail) {
    console.error(`[notificarAvaria] sem email de destino pra thread ${threadId} - pulando envio automatico`);
    return;
  }

  const assunto = subject ? `Re: ${subject}` : 'Aviso de avaria';
  try {
    const r = await fetch(WEBHOOK_ENVIAR, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        para: paraEmail,
        assunto,
        mensagem: TEXTO_AVARIA_AUTOMATICO,
        thread_id: threadId,
        message_id: messageId || '',
        anexos: [],
      }),
    });
    if (!r.ok) {
      const t = await r.text().catch(() => '');
      console.error(`[notificarAvaria] webhook falhou pra thread ${threadId}: HTTP ${r.status} ${t.slice(0, 200)}`);
      return;
    }
  } catch (e: any) {
    console.error(`[notificarAvaria] erro de conexao pra thread ${threadId}: ${String((e && e.message) || e)}`);
    return;
  }

  const marcado = await marcarStatusConcluido(tabela, threadId);
  if (!marcado) {
    console.error(`[notificarAvaria] email enviado mas falhou ao marcar Status_resposta_tickets=concluido pra thread ${threadId}`);
  }

  await env.DB.exec(
    `INSERT INTO avarias_notificadas (thread_id, enviado_em, para_email) VALUES (?, ?, ?) ON CONFLICT(thread_id) DO NOTHING`,
    [threadId, new Date().toISOString(), paraEmail]
  );
}

async function initRegrasNegocioTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS regras_negocio (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT, updated_by TEXT)`, []);
}

async function getRegrasNegocio(env: Env): Promise<{ conteudo: Record<string, string>; updatedAt: string | null; updatedBy: string | null }> {
  await initRegrasNegocioTable(env);
  const res = await env.DB.query(`SELECT v, updated_at, updated_by FROM regras_negocio WHERE k = ?`, ['conteudo']);
  if (res.rows && res.rows.length) {
    let conteudo: Record<string, string> = {};
    try { conteudo = JSON.parse(res.rows[0].v) || {}; } catch {}
    return { conteudo, updatedAt: res.rows[0].updated_at || null, updatedBy: res.rows[0].updated_by || null };
  }
  return { conteudo: {}, updatedAt: null, updatedBy: null };
}

async function salvarRegrasNegocio(env: Env, conteudo: Record<string, string>, updatedBy: string): Promise<void> {
  await initRegrasNegocioTable(env);
  await env.DB.exec(
    `INSERT INTO regras_negocio (k, v, updated_at, updated_by) VALUES ('conteudo', ?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at, updated_by = excluded.updated_by`,
    [JSON.stringify(conteudo), new Date().toISOString(), updatedBy || '']
  );
}

async function ticketExiste(tabela: string, threadId: string): Promise<boolean> {
  const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}&select=thread_id&limit=1`, {
    headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` },
  });
  if (!r.ok) return false;
  const rows: any[] = await r.json();
  return Array.isArray(rows) && rows.length > 0;
}

async function gravarResposta(tabela: string, threadId: string, info: { tem_resposta: boolean; total_mensagens_thread: number; ultima_mensagem: string }): Promise<boolean> {
  const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
    method: 'PATCH',
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({
      tem_resposta: info.tem_resposta,
      total_mensagens_thread: info.total_mensagens_thread,
      'Ultima mensagem': info.ultima_mensagem,
      updated_at: new Date().toISOString(),
    }),
  });
  if (!r.ok) return false;
  try {
    const rows: any[] = await r.json();
    return Array.isArray(rows) && rows.length > 0;
  } catch {
    return false;
  }
}

async function ativarWatch(env: Env, conta: ContaGmail): Promise<any> {
  const accessToken = await getGmailAccessToken(env, conta);
  const r = await fetch('https://gmail.googleapis.com/gmail/v1/users/me/watch', {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ topicName: conta.topic, labelIds: ['INBOX'] }),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error(`Falha ao ativar watch do Gmail (conta ${conta.id}): HTTP ${r.status} ${t.slice(0, 300)}`);
  }
  return r.json();
}

async function verificarGmail(env: Env, conta: ContaGmail): Promise<any> {
  await sleep(2000);

  const accessToken = await getGmailAccessToken(env, conta);
  const lastHistoryId = await getLastHistoryId(env, conta.id);

  if (!lastHistoryId) {
    const profile = await gmailGet(accessToken, '/profile');
    await setLastHistoryId(env, conta.id, String(profile.historyId));
    return { conta: conta.id, inicializado: true, historyId: profile.historyId, mensagem: 'Primeira execucao: marcando ponto de partida. Nada processado ainda - a partir de agora, so email novo.' };
  }

  let history: any;
  try {
    history = await gmailGet(accessToken, `/history?startHistoryId=${encodeURIComponent(lastHistoryId)}&historyTypes=messageAdded&labelId=INBOX`);
  } catch (e: any) {
    if (e.status === 404) {
      const profile = await gmailGet(accessToken, '/profile');
      await setLastHistoryId(env, conta.id, String(profile.historyId));
      return { conta: conta.id, reinicializado: true, motivo: 'historyId expirado no Gmail (janela de ~7 dias estourada)', novoHistoryId: profile.historyId };
    }
    throw e;
  }

  const registros = history.history || [];
  const threadIdsNovos = new Set<string>();
  registros.forEach((h: any) => {
    (h.messagesAdded || []).forEach((ma: any) => {
      if (ma.message && ma.message.threadId) threadIdsNovos.add(ma.message.threadId);
    });
  });

  const resultado: any[] = [];
  for (const threadId of threadIdsNovos) {
    try {
      const existe = await ticketExiste(conta.tabela, threadId);
      if (!existe) {
        resultado.push({ threadId, acao: 'ignorado (nao e um ticket da Central de Tickets)' });
        continue;
      }

      const thread = await gmailGet(accessToken, `/threads/${encodeURIComponent(threadId)}?format=full`);
      const info = processThread(thread);
      const respostaGravada = await gravarResposta(conta.tabela, threadId, info);

      let classificado = false;
      if (info.ultima_mensagem_enviada) {
        const ok = await refletirRespostaJaEnviada(conta.tabela, threadId);
        resultado.push({ threadId, acao: 'ja respondemos - marcado como aguardando (sem reclassificar)', ok });
        continue;
      }
      if (info.tem_resposta && info.ultima_mensagem.trim()) {
        try {
          const cls = await classificarComProxy(env, env.AI_PROXY_TOKEN, info.subject, info.ultima_mensagem);
          classificado = await gravarClassificacao(conta.tabela, threadId, cls);
          if (classificado) {
            const categoria = cls.motivo.split('—')[0].trim().toUpperCase();
            await notificarAvariaSeNecessario(env, conta.tabela, threadId, categoria, info.subject, info.ultima_recebida_from, info.ultima_mensagem_id);
            if (categoria === 'EXTRAVIO') {
              await definirStatusManual(conta.tabela, threadId, 'extravio');
            }
          }
        } catch (e: any) {
          console.error(`[verificarGmail:${conta.id}] classificacao falhou pra thread ${threadId} (vai pro backlog do cron diario): ${String((e && e.message) || e)}`);
          resultado.push({ threadId, acao: 'resposta gravada, classificacao falhou (vai pro backlog)', erro: String((e && e.message) || e) });
          continue;
        }
      } else if (info.tem_resposta) {
        console.error(`[verificarGmail:${conta.id}] thread ${threadId}: resposta detectada mas corpo da ultima mensagem veio vazio (subject="${info.subject}") - aguardando reconciliacao do cron`);
        resultado.push({ threadId, acao: 'resposta gravada, mensagem vazia - aguardando reconciliacao do cron' });
        continue;
      }
      resultado.push({ threadId, acao: 'processado', temResposta: info.tem_resposta, respostaGravada, classificado });
    } catch (e: any) {
      console.error(`[verificarGmail:${conta.id}] erro geral processando thread ${threadId}: ${String((e && e.message) || e)}`);
      resultado.push({ threadId, acao: 'erro', erro: String((e && e.message) || e) });
    }
  }

  const novoHistoryId = history.historyId || lastHistoryId;
  await setLastHistoryId(env, conta.id, String(novoHistoryId));

  if (threadIdsNovos.size > 0) {
    console.log(`[verificarGmail:${conta.id}] ${threadIdsNovos.size} thread(s) novo(s): ${JSON.stringify(resultado.map((r) => ({ threadId: r.threadId, acao: r.acao })))}`);
  }

  return { conta: conta.id, threadsNovosDetectados: threadIdsNovos.size, resultado, novoHistoryId };
}

async function buscarTicketsPeriodo(tabela: string, remetenteFiltro: string | undefined, desde: string, ate: string, offset: number, limit: number): Promise<{ threadIds: string[]; total: number }> {
  let listUrl = `${SB_URL}/rest/v1/${tabela}?select=thread_id&data_email=gte.${encodeURIComponent(desde)}&data_email=lte.${encodeURIComponent(ate)}&${encodeURIComponent('Ultima mensagem')}=is.null&order=data_email.asc`;
  if (remetenteFiltro) listUrl += `&remetente=ilike.${encodeURIComponent(remetenteFiltro)}`;
  const r = await fetch(listUrl, {
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Range-Unit': 'items',
      Range: `${offset}-${offset + limit - 1}`,
      Prefer: 'count=exact',
    },
  });
  if (!r.ok) throw new Error(`Supabase HTTP ${r.status}`);
  const rows: any[] = await r.json();
  const contentRange = r.headers.get('Content-Range');
  let total = 0;
  if (contentRange && contentRange.indexOf('/') !== -1) {
    total = parseInt(contentRange.split('/')[1], 10) || 0;
  }
  return { threadIds: rows.map((x) => x.thread_id).filter(Boolean), total };
}

async function backfillPeriodo(env: Env, conta: ContaGmail, desde: string, ate: string, limit: number): Promise<any> {
  const accessToken = await getGmailAccessToken(env, conta);
  const { threadIds, total } = await buscarTicketsPeriodo(conta.tabela, conta.remetenteFiltro, desde, ate, 0, limit);

  const resultado: any[] = [];
  for (const threadId of threadIds) {
    try {
      const thread = await gmailGet(accessToken, `/threads/${encodeURIComponent(threadId)}?format=full`);
      const info = processThread(thread);
      const respostaGravada = await gravarResposta(conta.tabela, threadId, info);

      let classificado = false;
      if (info.ultima_mensagem_enviada) {
        const ok = await refletirRespostaJaEnviada(conta.tabela, threadId);
        resultado.push({ threadId, acao: 'ja respondemos - marcado como aguardando (sem reclassificar)', ok });
        continue;
      }
      if (info.tem_resposta && info.ultima_mensagem.trim()) {
        try {
          const cls = await classificarComProxy(env, env.AI_PROXY_TOKEN, info.subject, info.ultima_mensagem);
          classificado = await gravarClassificacao(conta.tabela, threadId, cls);
          if (classificado) {
            const categoria = cls.motivo.split('—')[0].trim().toUpperCase();
            await notificarAvariaSeNecessario(env, conta.tabela, threadId, categoria, info.subject, info.ultima_recebida_from, info.ultima_mensagem_id);
            if (categoria === 'EXTRAVIO') {
              await definirStatusManual(conta.tabela, threadId, 'extravio');
            }
          }
        } catch (e: any) {
          resultado.push({ threadId, acao: 'resposta gravada, classificacao falhou', erro: String((e && e.message) || e) });
          continue;
        }
      } else if (info.tem_resposta) {
        resultado.push({ threadId, acao: 'resposta gravada, mensagem vazia - aguardando reconciliacao do cron' });
        continue;
      }
      resultado.push({ threadId, acao: 'processado', temResposta: info.tem_resposta, respostaGravada, classificado });
    } catch (e: any) {
      resultado.push({ threadId, acao: 'erro', erro: String((e && e.message) || e) });
    }
  }

  return {
    conta: conta.id,
    totalPendenteAntesDoLote: total,
    processadosNesteLote: threadIds.length,
    totalPendenteDepoisEstimado: Math.max(0, total - threadIds.length),
    concluido: total <= threadIds.length,
    resultado,
  };
}

async function processarLotePendentes(env: Env, tabela: string, limit: number, desde?: string, ate?: string): Promise<any> {
  const pendentes = (await buscarPendentes(tabela, desde, ate, limit)).slice(0, limit);
  let processados = 0;
  let refeitos = 0;
  let aindaAguardando = 0;
  const erros: string[] = [];
  const threadIdsProcessados: string[] = [];
  const threadIdsFalharam: string[] = [];
  const threadIdsAguardando: string[] = [];

  const accessTokensPorConta = new Map<string, string | null>();
  async function accessTokenParaConta(conta: ContaGmail): Promise<string | null> {
    if (accessTokensPorConta.has(conta.id)) return accessTokensPorConta.get(conta.id)!;
    try {
      const tok = await getGmailAccessToken(env, conta);
      accessTokensPorConta.set(conta.id, tok);
      return tok;
    } catch (e: any) {
      erros.push(`falha ao obter access token do Gmail pra reconciliacao (conta ${conta.id}): ${String((e && e.message) || e)}`);
      accessTokensPorConta.set(conta.id, null);
      return null;
    }
  }

  for (const t of pendentes) {
    try {
      let ultimaMensagem = t['Ultima mensagem'] || t['Última mensagem'] || '';
      let subject = t.subject || '';
      let paraEmailBruto: any = '';
      let messageId: string = t.message_id || '';

      const conta = inferirConta(tabela, t.remetente);
      const accessToken = conta ? await accessTokenParaConta(conta) : null;

      if (accessToken && t.thread_id) {
        try {
          const thread = await gmailGet(accessToken, `/threads/${encodeURIComponent(t.thread_id)}?format=full`);
          const info = processThread(thread);
          if (info.total_mensagens_thread !== t.total_mensagens_thread || info.ultima_mensagem !== ultimaMensagem) {
            await gravarResposta(tabela, t.thread_id, info);
            refeitos++;
          }
          ultimaMensagem = info.ultima_mensagem;
          subject = info.subject || subject;
          if (info.ultima_recebida_from) paraEmailBruto = info.ultima_recebida_from;
          if (info.ultima_mensagem_id) messageId = info.ultima_mensagem_id;
        } catch (e: any) {
          // Rebusca falhou - segue com o dado que ja tinha.
        }
      }

      const dataEmailMs = t.data_email ? new Date(t.data_email).getTime() : NaN;
      const tempoDesdeEmail = isNaN(dataEmailMs) ? Infinity : (Date.now() - dataEmailMs);
      const podeClassificarComVazio = tempoDesdeEmail > CLASSIFICACAO_MAX_ESPERA_MS;

      if (!ultimaMensagem.trim() && !podeClassificarComVazio) {
        aindaAguardando++;
        threadIdsAguardando.push(t.thread_id);
        continue;
      }

      const cls = await classificarComProxy(env, env.AI_PROXY_TOKEN, subject, ultimaMensagem);
      const ok = await gravarClassificacao(tabela, t.thread_id, cls);
      if (ok) {
        processados++; threadIdsProcessados.push(t.thread_id);
        const categoria = cls.motivo.split('—')[0].trim().toUpperCase();
        await notificarAvariaSeNecessario(env, tabela, t.thread_id, categoria, subject, paraEmailBruto, messageId);
        if (categoria === 'EXTRAVIO') {
          await definirStatusManual(tabela, t.thread_id, 'extravio');
        }
      }
      else { erros.push(`thread_id ${t.thread_id}: PATCH nao casou nenhuma linha (0 rows affected)`); threadIdsFalharam.push(t.thread_id); }
    } catch (e: any) {
      erros.push(`thread_id ${t.thread_id}: ${String((e && e.message) || e)}`);
    }
  }

  return { tabela, processados, refeitos, aindaAguardando, encontradosNoLote: pendentes.length, threadIdsProcessados, threadIdsFalharam, threadIdsAguardando, erros };
}

async function initRessarcimentoTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS ressarcimentos_solicitados (thread_id TEXT PRIMARY KEY, solicitado_em TEXT)`, []);
}

async function marcarRessarcimento(env: Env, threadIds: string[]): Promise<number> {
  await initRessarcimentoTable(env);
  const agora = new Date().toISOString();
  let marcados = 0;
  for (const threadId of threadIds) {
    if (!threadId) continue;
    await env.DB.exec(
      `INSERT INTO ressarcimentos_solicitados (thread_id, solicitado_em) VALUES (?, ?) ON CONFLICT(thread_id) DO UPDATE SET solicitado_em = excluded.solicitado_em`,
      [threadId, agora]
    );
    marcados++;
  }
  return marcados;
}

async function buscarRessarcimentos(env: Env, threadIds: string[]): Promise<Record<string, string>> {
  await initRessarcimentoTable(env);
  if (!threadIds.length) return {};
  const placeholders = threadIds.map(() => '?').join(',');
  const res = await env.DB.query(
    `SELECT thread_id, solicitado_em FROM ressarcimentos_solicitados WHERE thread_id IN (${placeholders})`,
    threadIds
  );
  const out: Record<string, string> = {};
  (res.rows || []).forEach((r: any) => { out[r.thread_id] = r.solicitado_em; });
  return out;
}

async function initTratativaTables(env: Env): Promise<void> {
  await env.DB.exec(
    `CREATE TABLE IF NOT EXISTS tratativas (
      id INTEGER PRIMARY KEY,
      numero_pedido TEXT,
      tipo TEXT,
      data_solicitacao TEXT,
      valor_nf_tratado REAL,
      chave_nf TEXT,
      cod_nf_original TEXT
    )`,
    []
  );
  await env.DB.exec(`CREATE INDEX IF NOT EXISTS idx_tratativas_pedido ON tratativas(numero_pedido)`, []);
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS tratativa_sync (k TEXT PRIMARY KEY, v TEXT)`, []);
}

async function getTratativaOffset(env: Env): Promise<number> {
  await initTratativaTables(env);
  const res = await env.DB.query(`SELECT v FROM tratativa_sync WHERE k = ?`, ['offset']);
  if (res.rows && res.rows.length) return parseInt(res.rows[0].v, 10) || 0;
  return 0;
}

async function fetchComTimeout(url: string, options: any, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function buscarTratativasAoVivo(pedidos: string[]): Promise<any[]> {
  if (!pedidos.length) return [];
  const valores = pedidos.map((p) => `"${String(p).replace(/"/g, '\\"')}"`).join(',');
  const filtro = `in.(${valores})`;
  const url = `${GB_SB_URL}/rest/v1/base_reenvio_reembolso?select=id,numero_pedido,tipo,data_solicitacao,valor_nf_tratado,chave_nf,cod_nf_original&numero_pedido=${encodeURIComponent(filtro)}`;
  const r = await fetchComTimeout(
    url,
    { headers: { apikey: GB_SB_KEY, Authorization: `Bearer ${GB_SB_KEY}`, 'Accept-Profile': GB_SB_SCHEMA } },
    GB_FETCH_TIMEOUT_MS
  );
  if (!r.ok) {
    const errBody = await r.text().catch(() => '');
    throw new Error(`Supabase HTTP ${r.status}: ${errBody.slice(0, 300)}`);
  }
  return r.json();
}

async function salvarTratativasLocal(env: Env, rows: any[]): Promise<void> {
  if (!rows.length) return;
  await initTratativaTables(env);
  for (const row of rows) {
    if (row.id === undefined || row.id === null) continue;
    await env.DB.exec(
      `INSERT INTO tratativas (id, numero_pedido, tipo, data_solicitacao, valor_nf_tratado, chave_nf, cod_nf_original)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         numero_pedido = excluded.numero_pedido,
         tipo = excluded.tipo,
         data_solicitacao = excluded.data_solicitacao,
         valor_nf_tratado = excluded.valor_nf_tratado,
         chave_nf = excluded.chave_nf,
         cod_nf_original = excluded.cod_nf_original`,
      [
        row.id,
        row.numero_pedido ?? null,
        row.tipo ?? null,
        row.data_solicitacao ?? null,
        row.valor_nf_tratado ?? null,
        row.chave_nf ?? null,
        row.cod_nf_original ?? null,
      ]
    );
  }
}

const SELECT_TICKETS_COLS = [
  'id', 'thread_id', 'message_id', 'numero_nf', 'chave_danfe', 'tipo_ocorrencia', 'marca',
  'data_email', 'remetente', 'subject', 'tem_resposta', 'total_mensagens_thread', 'updated_at',
  'transportadora', 'previsao_entrega', 'Status_resposta_tickets', 'Precisa responder?',
  'Motivo', 'Urgência', 'Número do pedido', 'Último Status', 'Rastreio',
];
const SELECT_TICKETS_PARAM = SELECT_TICKETS_COLS
  .map((c) => encodeURIComponent('"' + c.replace(/"/g, '""') + '"'))
  .join(',');

const CACHE_TTL_SEGUNDOS = 30;

async function initTicketListCacheTable(env: Env): Promise<void> {
  await env.DB.exec(`CREATE TABLE IF NOT EXISTS ticket_list_cache (cache_key TEXT PRIMARY KEY, payload TEXT, cached_at TEXT)`, []);
}

async function getTicketListCache(env: Env, key: string): Promise<any[] | null> {
  await initTicketListCacheTable(env);
  const res = await env.DB.query(`SELECT payload, cached_at FROM ticket_list_cache WHERE cache_key = ?`, [key]);
  if (!res.rows || !res.rows.length) return null;
  const cachedAtMs = new Date(res.rows[0].cached_at).getTime();
  if (!cachedAtMs || Date.now() - cachedAtMs > CACHE_TTL_SEGUNDOS * 1000) return null;
  try {
    return JSON.parse(res.rows[0].payload);
  } catch {
    return null;
  }
}

async function setTicketListCache(env: Env, key: string, payload: any[]): Promise<void> {
  await initTicketListCacheTable(env);
  await env.DB.exec(
    `INSERT INTO ticket_list_cache (cache_key, payload, cached_at) VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, cached_at = excluded.cached_at`,
    [key, JSON.stringify(payload), new Date().toISOString()]
  );
}

async function buscarTicketsPaginado(url: string): Promise<any[]> {
  const pageSize = 1000;
  let all: any[] = [];
  let page = 0;
  const MAX_PAGINAS = 200;
  const MAX_BYTES = 20 * 1024 * 1024;
  let bytesAcumulados = 0;
  while (page < MAX_PAGINAS) {
    const from = page * pageSize;
    const to = from + pageSize - 1;
    const r = await fetch(url, {
      headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, 'Range-Unit': 'items', Range: `${from}-${to}` },
    });
    if (!r.ok) {
      const t = await r.text().catch(() => '');
      throw new Error(`Supabase HTTP ${r.status}: ${t.slice(0, 300)}`);
    }
    const textoPagina = await r.text();
    bytesAcumulados += textoPagina.length;
    let rows: any[];
    try {
      rows = JSON.parse(textoPagina);
    } catch {
      throw new Error('Supabase devolveu JSON invalido');
    }
    all = all.concat(rows);
    if (rows.length < pageSize) break;
    if (bytesAcumulados >= MAX_BYTES) break;
    page++;
  }
  return all;
}

async function buscarTicketsComLimiteFixo(url: string, limite: number): Promise<any[]> {
  const pageSize = 1000;
  const MAX_BYTES = 20 * 1024 * 1024;
  let all: any[] = [];
  let bytesAcumulados = 0;
  let offset = 0;
  while (all.length < limite) {
    const from = offset;
    const to = Math.min(offset + pageSize, limite) - 1;
    const r = await fetch(url, {
      headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, 'Range-Unit': 'items', Range: `${from}-${to}` },
    });
    if (!r.ok) {
      const t = await r.text().catch(() => '');
      throw new Error(`Supabase HTTP ${r.status}: ${t.slice(0, 300)}`);
    }
    const texto = await r.text();
    bytesAcumulados += texto.length;
    let page: any[];
    try {
      page = JSON.parse(texto);
    } catch {
      throw new Error('Supabase devolveu JSON invalido');
    }
    all = all.concat(page);
    if (page.length < to - from + 1) break;
    if (bytesAcumulados >= MAX_BYTES) break;
    offset += pageSize;
  }
  return all;
}

interface OpcoesListaTickets {
  termo?: string;
  ini?: string;
  fim?: string;
  statusExato?: string;
  heuristicaExtravio?: boolean;
}

async function buscarTicketsProxy(tabela: string, opts: OpcoesListaTickets): Promise<any[]> {
  const base = `${SB_URL}/rest/v1/${tabela}?select=${SELECT_TICKETS_PARAM}`;
  if (opts.statusExato) {
    const url = `${base}&Status_resposta_tickets=eq.${encodeURIComponent(opts.statusExato)}&order=updated_at.desc`;
    return buscarTicketsComLimiteFixo(url, 5000);
  }
  if (opts.heuristicaExtravio) {
    const hoje = new Date();
    const dataCorte = new Date(hoje.getTime() - 10 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
    const url = `${base}&previsao_entrega=not.is.null&previsao_entrega=lte.${dataCorte}&tem_resposta=not.is.true&Motivo=not.is.null&order=previsao_entrega.asc`;
    return buscarTicketsComLimiteFixo(url, 5000);
  }
  if (opts.termo) {
    const termoEscapado = opts.termo.replace(/[,()]/g, '');
    const padrao = '*' + termoEscapado + '*';
    const url = `${base}&or=(${[
      'numero_nf.ilike.' + encodeURIComponent(padrao),
      encodeURIComponent('"Número do pedido"') + '.ilike.' + encodeURIComponent(padrao),
      'subject.ilike.' + encodeURIComponent(padrao),
    ].join(',')})&order=id.asc&limit=500`;
    const r = await fetch(url, { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } });
    if (!r.ok) throw new Error(`Supabase HTTP ${r.status}`);
    return r.json();
  }
  // FIX TIMEOUT (12/08/2026, achado real em producao - Maria reportou erro
  // 57014 "canceling statement due to statement timeout" na tela sem filtro
  // de status, so com periodo): esta era a UNICA rota de buscarTicketsProxy
  // que ainda ordenava por `id` enquanto filtrava por `data_email`. Foi
  // criado o indice idx_tickets_gocase_data_email / idx_tickets_gobeaute_data_email
  // (antes nao existia NENHUM indice em data_email nas duas tabelas), mas
  // EXPLAIN ANALYZE mostrou que a combinacao "ORDER BY id + LIMIT/OFFSET"
  // (usada pela paginacao de buscarTicketsPaginado) faz o planner do
  // Postgres preferir o indice de id (tickets_gocase_pkey) e filtrar linha a
  // linha por data_email - pra um periodo de 30 dias (7826 linhas de match
  // em 85840), so a primeira pagina (LIMIT 1000) levou 1868ms escaneando
  // 11277 linhas (Rows Removed by Filter: 10277), quase batendo no
  // statement_timeout de 3s da role anon. Trocando pra ORDER BY data_email
  // (mesma coluna do filtro e do indice novo), a mesma pagina caiu pra
  // 109ms via Index Scan direto, sem descartar nada. So se aplica quando ha
  // filtro de periodo - a busca sem filtro nenhum (cacheada, ver
  // ticketsListComCache) continua ordenando por id, que e o que faz sentido
  // pra ela.
  const ordenacao = (opts.ini && opts.fim) ? 'data_email.asc' : 'id.asc';
  let url = `${base}&order=${ordenacao}`;
  if (opts.ini && opts.fim) {
    url += `&data_email=gte.${encodeURIComponent(opts.ini)}&data_email=lte.${encodeURIComponent(opts.fim + 'T23:59:59.999')}`;
  }
  return buscarTicketsPaginado(url);
}

async function ticketsListComCache(env: Env, tabela: string, opts: OpcoesListaTickets): Promise<any[]> {
  const cacheavel = !opts.termo && (!!opts.statusExato || !!opts.heuristicaExtravio || (!opts.ini && !opts.fim));
  const cacheKey = `${tabela}:${opts.statusExato || ''}:${opts.heuristicaExtravio ? 'heur' : ''}:${opts.ini || ''}:${opts.fim || ''}`;
  if (cacheavel) {
    const cached = await getTicketListCache(env, cacheKey);
    if (cached) return cached;
  }
  const rows = await buscarTicketsProxy(tabela, opts);
  if (cacheavel) await setTicketListCache(env, cacheKey, rows);
  return rows;
}

async function migrarDevolucaoNaoPrecisaResponder(tabela: string): Promise<{ tabela: string; corrigidosPrecisaResponder: number; corrigidosStatusManual: number }> {
  const filtro1 = `Motivo=ilike.DEVOLUCAO*`;
  const r1 = await fetch(`${SB_URL}/rest/v1/${tabela}?${filtro1}`, {
    method: 'PATCH',
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({ 'Precisa responder?': false, updated_at: new Date().toISOString() }),
  });
  if (!r1.ok) {
    const errBody = await r1.text().catch(() => '');
    throw new Error(`Supabase HTTP ${r1.status}: ${errBody.slice(0, 300)}`);
  }
  const rows1: any[] = await r1.json();

  const filtro2 = `Motivo=ilike.DEVOLUCAO*&Status_resposta_tickets=eq.pendente`;
  const r2 = await fetch(`${SB_URL}/rest/v1/${tabela}?${filtro2}`, {
    method: 'PATCH',
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({ Status_resposta_tickets: null, updated_at: new Date().toISOString() }),
  });
  if (!r2.ok) {
    const errBody = await r2.text().catch(() => '');
    throw new Error(`Supabase HTTP ${r2.status}: ${errBody.slice(0, 300)}`);
  }
  const rows2: any[] = await r2.json();

  return {
    tabela,
    corrigidosPrecisaResponder: Array.isArray(rows1) ? rows1.length : 0,
    corrigidosStatusManual: Array.isArray(rows2) ? rows2.length : 0,
  };
}

// ============================================================================
// TORRE DE CONTROLE LOGISTICA (17/08/2026)
// ============================================================================
// Objetivo: acabar com o pula-pula de abas do agente. Hoje, pra tratar um
// pedido atrasado ele abre a Intelipost (rastreio), a Shopify (dados do
// pedido/cliente), a Central de Tickets (ver se ja tem ticket) e o CX Hub
// (abrir chamado). Aqui isso vira UMA busca.
//
// COMO A BUSCA RESOLVE (descoberto testando as APIs reais em 17/08/2026, nao
// e chute - ver comentarios de cada etapa):
//   - A Intelipost tem TRES formas de achar o mesmo pedido, e cada marca usa
//     uma convencao diferente. Por isso tentamos em cascata em vez de fixar:
//       1. /shipment_order/{X}                     -> X = order_number nativo
//          (formato composto "SH1016912RT0789656" = nº do pedido + NF com
//          zeros a esquerda ate 18 chars. E exatamente o que a coluna
//          "Rastreio" da tabela de tickets ja guarda.)
//       2. /shipment_order/sales_order_number/{X}  -> X = nº do e-commerce
//          ("SH1016912RT"). Funciona pra Rituaria/Kokeshi/Lescent/Barbours.
//       3. /shipment_order/tracking_code/{X}       -> X = codigo da
//          transportadora. E o unico caminho que funciona pra APICE, cujos
//          pedidos nao estao registrados sob a chave externa "sales".
//   - NF pura nao e buscavel na Intelipost. Resolvemos pela tabela de tickets
//     do Supabase (numero_nf -> "Número do pedido") e caimos na cascata acima.
//   - CPF nao e indexado na busca de PEDIDOS da Shopify (testado: GraphQL
//     orders(query:"<cpf>") volta vazio), mas E indexado na busca de CLIENTES,
//     porque o CPF vem gravado em shipping_address.company. Entao: CPF ->
//     customers/search.json -> customer_id -> orders.json.
//   - Se a Shopify achou o pedido mas a Intelipost nao, usamos o
//     fulfillments[].tracking_number da Shopify pra tentar a etapa 3. E o que
//     salva o caso da Apice.

interface MarcaLogistica {
  id: string;
  nome: string;
  marcaSupabase: string; // valor exato da coluna "marca" na tabela de tickets
  // Intelipost/Shopify: usados pelas 7 marcas Gobeaute. Gocase busca via
  // Metabase (Factory/Shazam) em vez disso - ver torreBuscarNaMarca - por
  // isso esses tres campos sao opcionais.
  shopifyDomain?: string;
  envIntelipost?: string;
  envShopify?: string;
  sufixoPedido?: string; // usado so pra adivinhar a marca quando vem "auto"
}

const MARCAS_LOGISTICA: MarcaLogistica[] = [
  { id: 'lescent',  nome: 'Lescent',  marcaSupabase: 'LESCENT',  shopifyDomain: '568499-ef.myshopify.com',            envIntelipost: 'INTELIPOST_KEY_LESCENT',  envShopify: 'SHOPIFY_TOKEN_LESCENT',  sufixoPedido: 'LC' },
  { id: 'kokeshi',  nome: 'Kokeshi',  marcaSupabase: 'KOKESHI',  shopifyDomain: 'kokeshi-loja.myshopify.com',         envIntelipost: 'INTELIPOST_KEY_KOKESHI',  envShopify: 'SHOPIFY_TOKEN_KOKESHI',  sufixoPedido: 'KS' },
  { id: 'bysamia',  nome: 'BySamia',  marcaSupabase: 'BYSAMIA',  shopifyDomain: 'gcpf8a-ki.myshopify.com',            envIntelipost: 'INTELIPOST_KEY_BYSAMIA',  envShopify: 'SHOPIFY_TOKEN_BYSAMIA' },
  { id: 'aua',      nome: 'Auá',      marcaSupabase: 'AUA',      shopifyDomain: 'ca0rd1-ft.myshopify.com',            envIntelipost: 'INTELIPOST_KEY_AUA',      envShopify: 'SHOPIFY_TOKEN_AUA' },
  { id: 'barbours', nome: 'Barbours', marcaSupabase: 'BARBOURS', shopifyDomain: '742eae-3.myshopify.com',             envIntelipost: 'INTELIPOST_KEY_BARBOURS', envShopify: 'SHOPIFY_TOKEN_BARBOURS', sufixoPedido: 'BB' },
  { id: 'rituaria', nome: 'Rituária', marcaSupabase: 'RITUARIA', shopifyDomain: 'uyckuc-fw.myshopify.com',            envIntelipost: 'INTELIPOST_KEY_RITUARIA', envShopify: 'SHOPIFY_TOKEN_RITUARIA', sufixoPedido: 'RT' },
  { id: 'apice',    nome: 'Apice',    marcaSupabase: 'APICE',    shopifyDomain: 'apse-cosmetics-dev.myshopify.com',   envIntelipost: 'INTELIPOST_KEY_APICE',    envShopify: 'SHOPIFY_TOKEN_APICE' },
  // GOCASE (24/08/2026): tem Intelipost propria (confirmado - chave real),
  // mas sem Shopify - ver buscarPedidoGocase (Factory faz o papel da Shopify).
  { id: 'gocase',   nome: 'Gocase',   marcaSupabase: 'GOCASE', envIntelipost: 'INTELIPOST_KEY_GOCASE' },
];

const LOG_TIMEOUT_MS = 12000;

function getMarcaLogistica(id: string): MarcaLogistica | null {
  const alvo = (id || '').trim().toLowerCase();
  return MARCAS_LOGISTICA.find((m) => m.id === alvo) || null;
}

// Descobre a marca pelo proprio numero do pedido quando o agente deixa o
// dropdown em "Detectar automaticamente". Os pedidos da Gobeaute seguem o
// padrao SH<numero><sigla da marca> (SH1016912RT = Rituaria). Marcas sem
// sufixo conhecido (BySamia, Aua, Apice) caem no fallback de varrer todas.
function inferirMarcasPorPedido(termo: string): MarcaLogistica[] {
  const t = (termo || '').trim().toUpperCase();
  const m = t.match(/^SH\d+([A-Z]{2})$/);
  if (m) {
    const porSufixo = MARCAS_LOGISTICA.filter((x) => x.sufixoPedido === m[1]);
    if (porSufixo.length) return porSufixo;
  }
  return MARCAS_LOGISTICA;
}

function classificarTermo(termo: string): 'cpf' | 'nf' | 'pedido' {
  const limpo = (termo || '').trim();
  const soDigitos = limpo.replace(/\D/g, '');
  // CPF: 11 digitos. Checamos o tamanho do termo original tambem pra nao
  // confundir um numero de pedido numerico longo com CPF.
  if (soDigitos.length === 11 && /^[\d.\-\s]+$/.test(limpo)) return 'cpf';
  // NF: so digitos, ate 9 posicoes (as NFs observadas tem 5 a 7 digitos).
  if (/^\d{1,9}$/.test(limpo)) return 'nf';
  return 'pedido';
}

async function fetchJsonComTimeout(url: string, options: any, timeoutMs: number): Promise<any | null> {
  try {
    const r = await fetchComTimeout(url, options, timeoutMs);
    if (!r.ok) return null;
    return await r.json().catch(() => null);
  } catch {
    return null;
  }
}

// Tenta os tres formatos de busca da Intelipost e devolve o primeiro que
// responder status OK. Cada endpoint devolve o pedido numa posicao diferente
// do JSON (objeto direto / dentro de shipment_orders[] / dentro de um array),
// entao normalizamos aqui mesmo.
// Traducao dos codigos crus de status. Necessario porque nem toda resposta da
// Intelipost vem localizada (ver comentario em buscarIntelipost) - sem isso a
// tela mostraria "IN_TRANSIT" pro agente em vez de "Em trânsito".
const STATUS_PT: Record<string, string> = {
  NEW: 'Criado',
  READY_FOR_SHIPPING: 'Pronto para envio',
  PRE_SHIPMENT_LIST_SUCCEEDED: 'Pré-lista de postagem enviada',
  SHIPPED: 'Despachado',
  IN_TRANSIT: 'Em trânsito',
  OUT_FOR_DELIVERY: 'Saiu para entrega',
  DELIVERED: 'Entregue',
  RETURNING: 'Em devolução',
  RETURNED: 'Devolvido',
  CANCELLED: 'Cancelado',
  TO_BE_RECOVERED: 'A recuperar',
  LOST: 'Extraviado',
  STOLEN: 'Roubado',
  DAMAGED: 'Avariado',
  DELIVERY_FAILED: 'Falha na entrega',
  WAITING_WITHDRAWAL: 'Aguardando retirada',
};
function traduzirStatus(v: any): string {
  const s = String(v || '').trim();
  if (!s) return '';
  // Se ja veio localizado (tem minuscula/acento), devolve como esta.
  if (!/^[A-Z_]+$/.test(s)) return s;
  return STATUS_PT[s] || s;
}

function ehPayloadRico(node: any): boolean {
  return !!(node && (node.logistic_provider_name || node.delivery_method_name));
}

async function ipBuscarEm(headers: any, endpoint: string): Promise<any | null> {
  const j = await fetchJsonComTimeout(endpoint, { headers }, LOG_TIMEOUT_MS);
  if (!j || j.status !== 'OK' || !j.content) return null;
  const c = j.content;
  let node: any = null;
  if (Array.isArray(c)) node = c[0];
  else if (Array.isArray(c.shipment_orders)) node = c.shipment_orders[0];
  else node = c;
  if (node && (node.order_number || node.shipment_order_volume_array)) return node;
  return null;
}

async function buscarIntelipost(env: any, marca: MarcaLogistica, termo: string): Promise<any | null> {
  const apiKey = env[marca.envIntelipost];
  if (!apiKey) return null;
  const headers = { 'api-key': apiKey, 'Content-Type': 'application/json' };
  const termoUrl = encodeURIComponent(termo.trim());
  const base = 'https://api.intelipost.com.br/api/v1/shipment_order';

  const tentativas = [
    `${base}/${termoUrl}`,
    `${base}/sales_order_number/${termoUrl}`,
    `${base}/tracking_code/${termoUrl}`,
  ];

  for (const endpoint of tentativas) {
    const node = await ipBuscarEm(headers, endpoint);
    if (!node) continue;

    // PAYLOAD ENXUTO x RICO (achado testando com pedido real em 17/08/2026):
    // /sales_order_number devolve uma versao REDUZIDA do pedido - sem
    // logistic_provider_name, sem delivery_method_name, sem tracking_url, e
    // com os status em codigo cru ("IN_TRANSIT") em vez de localizados
    // ("Em trânsito"). Ja /shipment_order/{order_number} devolve tudo.
    // Como a resposta enxuta ENTREGA o order_number canonico, fazemos uma
    // segunda chamada com ele pra buscar a versao completa. Se essa segunda
    // chamada falhar, seguimos com a enxuta (traduzirStatus cobre o resto).
    if (!ehPayloadRico(node) && node.order_number && node.order_number !== termo.trim()) {
      const rico = await ipBuscarEm(headers, `${base}/${encodeURIComponent(node.order_number)}`);
      if (rico && ehPayloadRico(rico)) return rico;
    }
    return node;
  }
  return null;
}

// GET /shipment_order/invoice/{nf} - resolve pedido a partir da NF (achado
// 26/08/2026, ver comentario grande em torreBuscarNaMarca, tipo==='nf').
// `content` vem como array (ver ipBuscarEm - ja trata isso), diferente do
// endpoint por pedido/rastreio que devolve objeto direto.
async function buscarIntelipostPorNF(env: any, marca: MarcaLogistica, nf: string): Promise<any | null> {
  const apiKey = env[marca.envIntelipost];
  if (!apiKey) return null;
  const headers = { 'api-key': apiKey, 'Content-Type': 'application/json' };
  const nfUrl = encodeURIComponent(nf.trim());
  return ipBuscarEm(headers, `https://api.intelipost.com.br/api/v1/shipment_order/invoice/${nfUrl}`);
}

async function buscarShopifyPorNome(env: any, marca: MarcaLogistica, nome: string): Promise<any | null> {
  const token = env[marca.envShopify];
  if (!token) return null;
  // A Shopify guarda o nome com "#" em algumas lojas (Apice: "#1475900") e sem
  // em outras (Rituaria: "SH1016912RT"). O filtro name= casa os dois jeitos.
  const alvo = encodeURIComponent(nome.trim().replace(/^#/, ''));
  const u = `https://${marca.shopifyDomain}/admin/api/2025-01/orders.json?status=any&limit=5&name=${alvo}`;
  const j = await fetchJsonComTimeout(u, { headers: { 'X-Shopify-Access-Token': token } }, LOG_TIMEOUT_MS);
  const orders = j && Array.isArray(j.orders) ? j.orders : [];
  return orders.length ? orders[0] : null;
}

async function buscarShopifyPorCpf(env: any, marca: MarcaLogistica, cpf: string): Promise<any[]> {
  const token = env[marca.envShopify];
  if (!token) return [];
  const headers = { 'X-Shopify-Access-Token': token };
  const soDigitos = cpf.replace(/\D/g, '');
  const busca = await fetchJsonComTimeout(
    `https://${marca.shopifyDomain}/admin/api/2025-01/customers/search.json?query=${encodeURIComponent(soDigitos)}&limit=5`,
    { headers },
    LOG_TIMEOUT_MS
  );
  const clientes = busca && Array.isArray(busca.customers) ? busca.customers : [];
  if (!clientes.length) return [];
  const pedidos = await fetchJsonComTimeout(
    `https://${marca.shopifyDomain}/admin/api/2025-01/orders.json?status=any&limit=20&customer_id=${clientes[0].id}`,
    { headers },
    LOG_TIMEOUT_MS
  );
  return pedidos && Array.isArray(pedidos.orders) ? pedidos.orders : [];
}

function extrairCpfShopify(order: any): string {
  if (!order) return '';
  const attrs = Array.isArray(order.note_attributes) ? order.note_attributes : [];
  for (const a of attrs) {
    const n = String((a && a.name) || '').toLowerCase();
    if (n === 'customer_document' || n === 'payment_additional_taxid' || n === 'cpf') {
      const v = String((a && a.value) || '').replace(/\D/g, '');
      if (v.length >= 11) return v;
    }
  }
  // Fallback: nas lojas BR da Gobeaute o CPF tambem vai no campo "company" do
  // endereco (e por isso que a busca de clientes por CPF funciona).
  const cands = [order.shipping_address && order.shipping_address.company, order.billing_address && order.billing_address.company];
  for (const c of cands) {
    const v = String(c || '').replace(/\D/g, '');
    if (v.length === 11 || v.length === 14) return v;
  }
  return '';
}

function formatarCpf(v: string): string {
  const d = String(v || '').replace(/\D/g, '');
  if (d.length === 11) return `${d.slice(0, 3)}.${d.slice(3, 6)}.${d.slice(6, 9)}-${d.slice(9)}`;
  if (d.length === 14) return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`;
  return d || '';
}

function soData(iso: any): string {
  if (!iso) return '';
  const s = String(iso);
  return s.length >= 10 ? s.slice(0, 10) : '';
}

// Dias de atraso = previsao de entrega x realidade. Se ja entregou, congela no
// atraso que houve na entrega (nao fica crescendo pra sempre). Se nao entregou,
// conta ate hoje. Negativo = ainda dentro do prazo.
function calcularAtraso(previsaoIso: any, entregue: boolean, dataEntregaIso: any): { dias: number; base: string } | null {
  const p = soData(previsaoIso);
  if (!p) return null;
  const prev = Date.parse(p + 'T00:00:00Z');
  if (isNaN(prev)) return null;
  const refIso = entregue ? soData(dataEntregaIso) : new Date().toISOString().slice(0, 10);
  if (!refIso) return null;
  const ref = Date.parse(refIso + 'T00:00:00Z');
  if (isNaN(ref)) return null;
  return { dias: Math.round((ref - prev) / 86400000), base: entregue ? 'entrega' : 'hoje' };
}

// Transforma o JSON gigante da Intelipost (35 KB, 26 eventos aninhados) nos 4
// blocos + timeline que a tela desenha. Tudo defensivo: cada endpoint da
// Intelipost devolve um subconjunto levemente diferente dos campos.
function normalizarIntelipost(node: any): any {
  const vol = (node.shipment_order_volume_array && node.shipment_order_volume_array[0]) || {};
  const inv = vol.shipment_order_volume_invoice || {};
  const ext = node.external_order_numbers || {};
  const cli = node.end_customer || {};
  const hist = Array.isArray(vol.shipment_order_volume_state_history_array) ? vol.shipment_order_volume_state_history_array : [];

  const entregue = !!vol.delivered;
  const previsao = vol.estimated_delivery_date_iso || node.estimated_delivery_date_iso || vol.estimated_delivery_date_lp_iso || node.estimated_delivery_date_lp_iso;
  const atraso = calcularAtraso(previsao, entregue, vol.delivered_date_iso || vol.delivered_date);

  const timeline = hist
    .map((e: any) => {
      const micro = e.shipment_volume_micro_state || {};
      const quando = e.event_date_iso || e.created_iso || '';
      // Comprovante de entrega (POD = Proof Of Delivery): a transportadora
      // anexa a foto/assinatura no evento em que marca a entrega. Guardamos
      // em CADA evento do timeline (nao so no pedido) porque e informativo
      // ver exatamente qual evento trouxe o anexo, mas o campo pedido.anexos
      // abaixo e o que a tela realmente usa pra mostrar "Ver comprovante".
      const anexos = (Array.isArray(e.attachments) ? e.attachments : []).map((a: any) => ({
        url: a.url || '',
        tipo: a.type || '',
        nome_arquivo: a.file_name || '',
        nome_pessoa: (a.additional_information && a.additional_information.nome) || '',
        documento_pessoa: (a.additional_information && a.additional_information.rg) || '',
      }));
      return {
        iso: quando,
        data: quando ? String(quando).slice(0, 10).split('-').reverse().join('/') : '',
        hora: quando ? String(quando).slice(11, 16) : '',
        status: traduzirStatus(e.shipment_order_volume_state_localized || micro.shipment_volume_state_localized || e.shipment_order_volume_state),
        micro_status: micro.name || micro.default_name || '',
        mensagem: e.esprinter_message || e.provider_message || micro.description || '',
        fonte: e.request_origin === 'EXTERNAL' ? 'Transportadora' : (e.request_origin || 'Intelipost'),
        anexos,
      };
    })
    .sort((a: any, b: any) => String(b.iso).localeCompare(String(a.iso)));

  // Busca em TODOS os eventos (nao so no mais recente) por um anexo tipo POD -
  // testado com pedido real (SH1099815RT0940380): o anexo vem no evento
  // DELIVERED, com a foto do canhoto assinado + nome/RG de quem recebeu.
  // Nao assumimos que o comprovante esteja sempre no primeiro evento da lista
  // (pode haver eventos posteriores, tipo recheck/conciliacao).
  let comprovanteEntrega: any = null;
  for (const evento of timeline) {
    const pod = evento.anexos.find((a: any) => a.tipo === 'POD' && a.url);
    if (pod) { comprovanteEntrega = { ...pod, data_evento: evento.data, hora_evento: evento.hora }; break; }
  }

  const produtos = (Array.isArray(vol.products) ? vol.products : []).map((p: any) => ({
    descricao: p.description || p.category || '',
    sku: p.sku || '',
    quantidade: p.quantity || 0,
  }));

  return {
    encontrado: true,
    pedido: {
      numero_pedido: ext.ecommerce_number || node.sales_order_number || ext.sales || '',
      numero_intelipost: node.order_number || '',
      id_intelipost: node.id || null,
      numero_nf: inv.invoice_number || ext.erp_number || '',
      serie_nf: inv.invoice_series || '',
      chave_nf: inv.invoice_key || '',
      // PLP: numero do lote de postagem do lado da Intelipost. Confirmado
      // batendo com o valor real da API.
      plp_intelipost: vol.pre_shipment_list_id != null ? String(vol.pre_shipment_list_id) : '',
      // ROMANEIO (18/08/2026 - CUIDADO, NAO CONFIRMADO): a hipotese original
      // era que logistic_provider_pre_shipment_list_id fosse "o numero de
      // romaneio que a transportadora usa". A Ivna conferiu contra o sistema
      // de armazem real (pedido SH1099815RT0940380) e o romaneio de la e
      // 173827 - um numero que NAO EXISTE em lugar nenhum da resposta da
      // Intelipost pra esse pedido (nem em external_order_numbers, nem em
      // nenhum campo do volume; foi conferido buscando a string inteira no
      // JSON). Ou seja, esse campo aqui e so um ID interno da Intelipost pro
      // lado da transportadora - nao necessariamente o romaneio operacional
      // que a transportadora reconhece. Por isso o frontend agora trata isso
      // como SUGESTAO editavel, nunca como fato (ver campoEditavel no
      // renderTorreForm) - nao da pra automatizar isso sem achar de onde vem
      // o "173827" (provavelmente outro sistema, tipo WMS/Cosmos, que este
      // app nao tem acesso).
      romaneio_transportadora: vol.logistic_provider_pre_shipment_list_id != null ? String(vol.logistic_provider_pre_shipment_list_id) : '',
      valor_total: inv.invoice_total_value != null ? inv.invoice_total_value : null,
      valor_produtos: inv.invoice_products_value != null ? inv.invoice_products_value : null,
      canal_venda: node.sales_channel || '',
      tipo: node.shipment_order_type || '',
      data_pedido: node.created_iso || '',
      data_faturamento: inv.invoice_date_iso_iso || '',
      data_envio: node.shipped_date_iso || vol.shipped_date_iso || '',
      previsao_entrega: previsao || '',
      previsao_original: vol.original_estimated_delivery_date_iso || '',
      data_entrega: vol.delivered_date_iso || vol.delivered_date || '',
      entregue,
      entregue_com_atraso: !!vol.delivered_late,
      status: traduzirStatus(vol.shipment_order_volume_state_localized || node.shipment_order_volume_state),
      status_code: node.shipment_order_volume_state || vol.shipment_order_volume_state || '',
      peso: vol.weight != null ? vol.weight : null,
      volumes: (node.shipment_order_volume_array || []).length,
      produtos,
      // Comprovante de entrega (foto do canhoto/assinatura), quando a
      // transportadora anexou um na Intelipost - so existe pra pedidos ja
      // entregues, e nem toda transportadora anexa.
      comprovante_entrega: comprovanteEntrega,
    },
    transportadora: {
      nome: node.logistic_provider_name || '',
      metodo: node.delivery_method_name || '',
      codigo_rastreio: vol.tracking_code || vol.logistic_provider_tracking_code || '',
      url_rastreio: node.tracking_url || '',
      prazo_dias: node.estimated_delivery_days_lp != null ? node.estimated_delivery_days_lp : vol.estimated_delivery_days_lp,
      custo_cliente: node.customer_shipping_costs != null ? node.customer_shipping_costs : null,
      custo_provedor: node.provider_shipping_cost != null ? node.provider_shipping_cost : node.provider_shipping_costs,
    },
    remetente: {
      nome: node.origin_name || node.origin_official_name || '',
      cnpj: formatarCpf(node.origin_federal_tax_payer_id || ''),
      endereco: node.origin_street || '',
      numero: node.origin_number || '',
      bairro: node.origin_quarter || '',
      cidade: node.origin_city || '',
      uf: node.origin_state_code || '',
      cep: node.origin_zip_code || '',
    },
    destinatario: {
      nome: [cli.first_name, cli.last_name].filter(Boolean).join(' ').trim(),
      email: cli.email || '',
      telefone: cli.cellphone || cli.phone || '',
      cpf: formatarCpf(cli.federal_tax_payer_id || ''),
      endereco: cli.shipping_address || '',
      numero: cli.shipping_number || '',
      complemento: cli.shipping_additional || '',
      bairro: cli.shipping_quarter || '',
      cidade: cli.shipping_city || '',
      uf: cli.shipping_state_code || cli.shipping_state || '',
      cep: cli.shipping_zip_code || '',
    },
    kpi: {
      dias_atraso: atraso ? atraso.dias : null,
      base_atraso: atraso ? atraso.base : '',
      entregue,
      status: traduzirStatus(vol.shipment_order_volume_state_localized),
      total_eventos: timeline.length,
      ultimo_evento: timeline.length ? timeline[0] : null,
    },
    timeline,
  };
}

// ============ GOCASE ============
// A Gocase NAO usa Shopify (nao tem loja Shopify) mas TEM conta Intelipost
// propria (confirmado em 24/08/2026 - chave real, mesma API/endpoint das
// outras 7 marcas, ver INTELIPOST_KEY_GOCASE) - por isso buscarIntelipost()
// e normalizarIntelipost() sao REAPROVEITADOS sem nenhuma mudanca pra Gocase,
// exatamente como pras marcas Gobeaute.
//
// O que falta pra completar o pedido (achar o numero certo a partir de
// NF/CPF/rastreio, endereco, itens) faz o papel que a Shopify faz pras outras
// marcas - so que a Gocase nao tem uma API assim disponivel ainda (o endpoint
// REST que a Ivna indicou, factory.gocase.com.br/api/v1/gomagics/orders,
// devolveu "Usuario nao encontrado" nos testes - parece exigir mais contexto
// alem do token+reference, a confirmar com quem forneceu o token). Enquanto
// isso, usamos consulta direta (SQL) na tabela "orders" do ERP "Factory" via
// API do Metabase, com a chave pessoal da Ivna (METABASE_KEY_GOCASE, grupo
// "All Users" - sempre leitura, nunca escrita, confirmado com ela que estava
// de acordo em usar assim num app de teste).
const METABASE_URL_GOCASE = 'https://metabase.gocase.com.br';
const METABASE_DB_FACTORY = 3;

// Consulta nativa parametrizada no Factory. Usa template-tag do Metabase (nao
// concatena o termo na string do SQL) pra nao abrir brecha de injecao - o
// termo vem direto da busca do agente.
async function metabaseQueryFactory(env: any, sql: string, termo: string): Promise<any[] | null> {
  const key = (env as any).METABASE_KEY_GOCASE;
  if (!key) return null;
  const body = {
    type: 'native',
    native: { query: sql, 'template-tags': { termo: { type: 'text', name: 'termo', id: 'termo', 'display-name': 'termo' } } },
    database: METABASE_DB_FACTORY,
    parameters: [{ type: 'category', target: ['variable', ['template-tag', 'termo']], value: termo }],
  };
  try {
    const r = await fetchComTimeout(`${METABASE_URL_GOCASE}/api/dataset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': key },
      body: JSON.stringify(body),
    }, LOG_TIMEOUT_MS);
    if (!r.ok) return null;
    const j: any = await r.json().catch(() => null);
    if (!j || !j.data || j.error) return null;
    const cols = (j.data.cols || []).map((c: any) => c.name);
    return (j.data.rows || []).map((row: any[]) => {
      const obj: any = {};
      cols.forEach((c: string, i: number) => { obj[c] = row[i]; });
      return obj;
    });
  } catch {
    return null;
  }
}

// Busca o(s) pedido(s) no Factory. NF/pedido/rastreio cabem na mesma consulta
// (o Factory indexa os tres direto na tabela "orders", ao contrario da
// Intelipost, que so aceita numero de pedido/rastreio, nunca NF). CPF fica
// numa consulta separada porque vive dentro do campo hstore "user".
async function buscarPedidoGocase(env: any, termo: string, tipo: string): Promise<any[]> {
  const sql = tipo === 'cpf'
    ? `SELECT * FROM orders WHERE regexp_replace("user" -> 'cpf', '\\D', '', 'g') = {{termo}} ORDER BY created_at DESC LIMIT 10`
    : `SELECT * FROM orders WHERE reference = {{termo}} OR track_code = {{termo}} OR nfe_number = {{termo}} ORDER BY created_at DESC LIMIT 10`;
  const linhas = await metabaseQueryFactory(env, sql, termo);
  return linhas || [];
}

// EAN/GTIN por SKU (25/08/2026, a pedido da Ivna): SKU e EAN sao campos
// DIFERENTES no cadastro de produto das marcas Gobeaute - confirmado com um
// produto real (SKU "RT01020" = EAN "7908407004412", ANTIOXI NUTRI 30 CAPS -
// RITUARIA). A Intelipost so devolve o SKU (ver "produtos" em
// normalizarIntelipost) - isto cruza pelo SKU na base "Cosmos" (mesmo
// Metabase da Gobeaute, banco/tabela "products") pra trazer o EAN sem
// precisar de nenhuma consulta ao Titan.
const METABASE_URL_GOBEAUTE = 'https://metabase.gobeaute.com.br';
const METABASE_DB_COSMOS = 38;

async function metabaseQueryCosmos(env: any, sql: string): Promise<any[] | null> {
  const key = (env as any).METABASE_KEY_GOBEAUTE;
  if (!key) return null;
  const body = { type: 'native', native: { query: sql }, database: METABASE_DB_COSMOS };
  try {
    const r = await fetchComTimeout(`${METABASE_URL_GOBEAUTE}/api/dataset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': key },
      body: JSON.stringify(body),
    }, LOG_TIMEOUT_MS);
    if (!r.ok) return null;
    const j: any = await r.json().catch(() => null);
    if (!j || !j.data || j.error) return null;
    const cols = (j.data.cols || []).map((c: any) => c.name);
    return (j.data.rows || []).map((row: any[]) => {
      const obj: any = {};
      cols.forEach((c: string, i: number) => { obj[c] = row[i]; });
      return obj;
    });
  } catch {
    return null;
  }
}

// SKUs vem de dados JA estruturados da Intelipost, nunca digitados livre pelo
// agente - mesmo assim, valida um charset seguro antes de colar no SQL: a
// consulta multi-valor (WHERE sku IN (...)) do Metabase nao tem um jeito
// limpo de usar template-tag pra lista (so suporta valor unico ou "field
// filter" com dimension, mais complexo do que vale a pena aqui).
function skuSeguro(sku: string): boolean {
  return /^[A-Za-z0-9_-]{1,40}$/.test(sku);
}

async function buscarEanPorSku(env: any, skus: string[]): Promise<Record<string, string>> {
  const validos = Array.from(new Set(skus.filter(skuSeguro)));
  if (!validos.length) return {};
  const lista = validos.map((s) => `'${s}'`).join(',');
  const linhas = await metabaseQueryCosmos(env, `SELECT sku, gtin FROM products WHERE sku IN (${lista})`);
  const mapa: Record<string, string> = {};
  (linhas || []).forEach((r: any) => { if (r.sku && r.gtin) mapa[r.sku] = r.gtin; });
  return mapa;
}

// Procura tickets ja abertos pro pedido nas DUAS tabelas (Gobeaute e Gocase).
// E o que evita o agente abrir um chamado duplicado - hoje ele so descobre
// isso indo na mao na Central.
async function buscarTicketsDoPedido(pedido: string, nf: string): Promise<any[]> {
  const cols = SELECT_TICKETS_PARAM;
  const filtros: string[] = [];
  if (pedido) filtros.push(encodeURIComponent('"Número do pedido"') + '.eq.' + encodeURIComponent(pedido));
  if (nf) filtros.push('numero_nf.eq.' + encodeURIComponent(nf));
  if (!filtros.length) return [];

  const achados: any[] = [];
  for (const tabela of ['tickets_gobeaute', 'tickets_gocase']) {
    const u = `${SB_URL}/rest/v1/${tabela}?select=${cols}&or=(${filtros.join(',')})&order=data_email.desc&limit=20`;
    const j = await fetchJsonComTimeout(u, { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } }, LOG_TIMEOUT_MS);
    if (Array.isArray(j)) for (const row of j) achados.push({ ...row, _tabela: tabela });
  }
  return achados;
}

async function initTicketsLogisticaTable(env: Env): Promise<void> {
  await env.DB.exec(
    `CREATE TABLE IF NOT EXISTS tickets_logistica (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       protocolo TEXT,
       marca TEXT,
       numero_pedido TEXT,
       numero_nf TEXT,
       problema TEXT,
       transportadora TEXT,
       codigo_rastreio TEXT,
       previsao_entrega TEXT,
       dias_atraso INTEGER,
       destinatario TEXT,
       observacao TEXT,
       responsavel TEXT,
       encaminhado INTEGER DEFAULT 0,
       resposta_webhook TEXT,
       criado_em TEXT
     )`,
    []
  );
  // DADOS DE LOCALIZACAO DA ENTREGA (17/08/2026, a pedido da Ivna): a
  // transportadora nao acha o volume so com o nº do pedido - ela pede CPF,
  // NF, chave da DANFE e o PLP/romaneio do lote de postagem. Sem isso o
  // chamado volta com "informar dados" e a tratativa perde um dia.
  // Adicionadas por ALTER pra nao perder o que ja foi gravado na tabela; o
  // SQLite nao tem "ADD COLUMN IF NOT EXISTS", entao ignoramos o erro de
  // coluna duplicada (que e o caso normal a partir da segunda execucao).
  const novasColunas = [
    'cpf_destinatario TEXT',
    'chave_danfe TEXT',
    'plp_intelipost TEXT',
    'romaneio_transportadora TEXT',
    'endereco_entrega TEXT',
    // ENVIO REAL DE E-MAIL (18/08/2026): quando o ticket nao tem thread de
    // Gmail existente (pedido que a transportadora nunca respondeu), o botao
    // "Responder" da Central nao serve - ele so responde DENTRO de uma thread
    // que ja existe. Pra esse caso, o frontend manda um e-mail NOVO direto pro
    // mesmo WEBHOOK_ENVIAR (n8n) que a Central ja usa, e so DEPOIS chama este
    // endpoint informando o resultado - por isso essas colunas guardam o que
    // foi enviado e pra onde, e nao apenas se foi encaminhado pro CX Hub.
    'email_para TEXT',
    'email_cc TEXT',
    'email_bcc TEXT',
    'email_assunto TEXT',
    'email_enviado INTEGER DEFAULT 0',
    'email_resposta TEXT',
    // "Alterar Endereço" (18/08/2026): unico problema com um dado extra -
    // endereco_novo e o que o agente digitou como correto, maps_url e o link
    // de busca do Google Maps gerado a partir dele (sem geocodificacao real,
    // so um deep link - ver gerarUrlGoogleMaps no frontend).
    'endereco_novo TEXT',
    'maps_url TEXT',
  ];
  for (const coluna of novasColunas) {
    try {
      await env.DB.exec(`ALTER TABLE tickets_logistica ADD COLUMN ${coluna}`, []);
    } catch {
      /* coluna ja existe */
    }
  }
}

// UNILOG CD (25/08/2026, a pedido da Ivna): tela irma da Torre, so que em vez
// de contatar a transportadora, prepara a abertura de uma ocorrencia no
// formulario publico da Unilog (Bitrix24 Site, fora deste sistema - nao existe
// API oficial pra ele, so o form web). Por isso este registro e sempre um
// RASCUNHO: nao existe nenhuma automacao daqui que preencha ou envie o
// formulario real da Unilog - isso e responsabilidade de quem abrir o
// formulario (ver unilog_form_bot.py, que roda na maquina de quem for usar).
//
// Fica no MESMO projeto Supabase que infos_titan ja usa (TITAN_SB_URL/
// titanHeaders acima) - NAO no D1/env.DB deste app, de proposito: o app fica
// atras do SSO do GoDeploy ("authenticated"), e o unilog_form_bot.py roda
// sem nenhuma sessao logada (mesmo motivo, ver comentario grande em
// TITAN_SB_URL). Uma tentativa anterior guardou isso em env.DB e teve que
// ser corrigida pra Supabase depois de travar exatamente nesse ponto.

// Executa a busca completa pra UMA marca. Devolve null se nada bateu, pra que
// o modo "auto" possa seguir tentando as outras marcas.
async function torreBuscarNaMarca(env: any, marca: MarcaLogistica, termo: string, tipo: string): Promise<any | null> {
  let intelipost: any = null;
  let shopify: any = null;
  let termoIntelipost = termo;
  let outrosPedidos: any[] = [];
  // So true quando o achado veio do endpoint exato /invoice/{nf} - usado pra
  // saber se a validacao de seguranca abaixo se aplica (ver tipo==='nf').
  let resolvidoPorNf = false;
  // Preenche o papel que a Shopify faz pras outras marcas (achar o pedido a
  // partir de NF/CPF/rastreio, endereco, itens) - a Gocase nao tem Shopify,
  // entao usa o Factory (ver buscarPedidoGocase). O rastreio em si vem da
  // Intelipost de verdade, igual as outras marcas (ver bloco abaixo).
  let fatorGocase: any = null;

  if (marca.id === 'gocase') {
    const linhas = await buscarPedidoGocase(env, termo, tipo);
    if (linhas.length) {
      fatorGocase = linhas[0];
      termoIntelipost = fatorGocase.reference || fatorGocase.track_code || termo;
      intelipost = await buscarIntelipost(env, marca, String(termoIntelipost));
      if (tipo === 'cpf') {
        outrosPedidos = linhas.slice(1, 20).map((o: any) => ({
          numero_pedido: o.reference || '',
          data: o.created_at || '',
          total: o.total_value != null ? o.total_value : '',
          status_pagamento: o.payment_state || '',
          status_envio: o.shipment_state || '',
        }));
      }
    }
    if (!intelipost && !fatorGocase) return null;
  } else if (tipo === 'cpf') {
    const pedidos = await buscarShopifyPorCpf(env, marca, termo);
    if (!pedidos.length) return null;
    shopify = pedidos[0];
    termoIntelipost = String(shopify.name || '').replace(/^#/, '');
    intelipost = await buscarIntelipost(env, marca, termoIntelipost);
    // Guarda os outros pedidos do mesmo CPF pra tela oferecer a escolha.
    outrosPedidos = pedidos.slice(0, 20).map((o: any) => ({
      numero_pedido: String(o.name || '').replace(/^#/, ''),
      data: o.created_at || '',
      total: o.total_price || '',
      status_pagamento: o.financial_status || '',
      status_envio: o.fulfillment_status || '',
    }));
    outrosPedidos = outrosPedidos.filter((p) => p.numero_pedido !== termoIntelipost);
  } else if (tipo === 'nf') {
    // So chega aqui pra Apice (o handler de /api/logistica/buscar bloqueia
    // tipo==='nf' pra qualquer outra marca/"auto" - NF colide entre marcas
    // diferentes, ver comentario la). NF E buscavel direto na Intelipost via
    // GET /shipment_order/invoice/{nf} (achado 26/08/2026 na documentacao
    // oficial - docs.intelipost.com.br - depois de pesquisar por "nota
    // fiscal": o comentario antigo aqui dizia que NF nao era buscavel porque
    // so tinham sido testados os 3 endpoints por pedido/rastreio, nunca
    // esse). Tenta a NF primeiro (exato, sem chute). Apice tambem e a unica
    // marca cujo numero de pedido e so digitos (sem sufixoPedido em
    // MARCAS_LOGISTICA) - o mesmo termo pode ser um NUMERO DE PEDIDO em vez
    // de NF, entao se a NF nao achar nada tenta como pedido antes de
    // desistir (bug real, 26/08/2026: sem esse fallback, buscar um pedido
    // Apice pelo proprio numero simplesmente nunca achava nada).
    intelipost = await buscarIntelipostPorNF(env, marca, termo);
    if (intelipost && intelipost.order_number) {
      resolvidoPorNf = true;
      termoIntelipost = String(intelipost.order_number);
      shopify = await buscarShopifyPorNome(env, marca, termoIntelipost);
    } else {
      intelipost = await buscarIntelipost(env, marca, termo);
      shopify = await buscarShopifyPorNome(env, marca, termo);
    }
    if (!intelipost && !shopify) return null;
  } else {
    intelipost = await buscarIntelipost(env, marca, termo);
    shopify = await buscarShopifyPorNome(env, marca, termo);
    if (!intelipost && !shopify) return null;
  }

  // Ponte que salva a Apice: a Intelipost nao acha o pedido pela chave "sales",
  // mas a Shopify tem o tracking_number do fulfillment, e por tracking_code a
  // Intelipost responde.
  if (!intelipost && shopify) {
    const fulfillments = Array.isArray(shopify.fulfillments) ? shopify.fulfillments : [];
    for (const f of fulfillments) {
      if (!f || !f.tracking_number) continue;
      intelipost = await buscarIntelipost(env, marca, String(f.tracking_number));
      if (intelipost) break;
    }
  }

  if (!intelipost && !shopify && !fatorGocase) return null;

  const base = intelipost
    ? normalizarIntelipost(intelipost)
    : { encontrado: false, pedido: {}, transportadora: {}, remetente: {}, destinatario: {}, kpi: {}, timeline: [] };

  // VALIDACAO DE SEGURANCA pra busca por NF - so entra quando o achado veio
  // mesmo do endpoint exato /invoice/{nf} (resolvidoPorNf), NUNCA quando
  // tipo==='nf' caiu no fallback por numero de pedido (achado real,
  // 26/08/2026: um pedido Apice achado pelo PROPRIO numero de pedido quase
  // sempre tem uma NF DIFERENTE desse numero - comparar contra o termo
  // rejeitaria toda busca por pedido legitima). Mantida como
  // cinto-e-suspensorio barato pro caminho que ainda importa: ate 25/08/2026
  // o fallback antigo (mandava a NF pra Intelipost como se fosse pedido
  // quando nao achava ticket historico) causou um bug real de PEDIDO ERRADO
  // por coincidencia numerica (NF 886691 sem ticket bateu num pedido real da
  // Apice cuja NF de verdade era 231787). So valida quando o pedido
  // encontrado TEM uma NF preenchida (pedido novo sem NF ainda emitida nao
  // tem o que comparar).
  if (resolvidoPorNf && base.pedido && base.pedido.numero_nf && String(base.pedido.numero_nf).trim() !== String(termo).trim()) {
    return null;
  }

  // Completa os buracos com o que a Shopify sabe. A Intelipost so conhece o
  // pedido depois que ele e faturado/despachado, entao pedido novo so tem
  // Shopify - e a tela precisa mostrar isso em vez de dizer "nao encontrado".
  if (shopify) {
    const p: any = base.pedido;
    if (!p.numero_pedido) p.numero_pedido = String(shopify.name || '').replace(/^#/, '');
    if (!p.data_pedido) p.data_pedido = shopify.created_at || '';
    if (p.valor_total == null && shopify.total_price != null) p.valor_total = Number(shopify.total_price);
    if (!p.produtos || !p.produtos.length) {
      p.produtos = (Array.isArray(shopify.line_items) ? shopify.line_items : []).map((li: any) => ({
        descricao: li.name || '',
        sku: li.sku || '',
        quantidade: li.quantity || 0,
      }));
    }
    p.status_pagamento = shopify.financial_status || '';
    p.status_envio_shopify = shopify.fulfillment_status || '';
    p.cancelado_em = shopify.cancelled_at || '';

    const d: any = base.destinatario;
    const end = shopify.shipping_address || {};
    if (!d.nome) d.nome = end.name || [shopify.customer && shopify.customer.first_name, shopify.customer && shopify.customer.last_name].filter(Boolean).join(' ');
    if (!d.email) d.email = (shopify.customer && shopify.customer.email) || shopify.email || '';
    if (!d.telefone) d.telefone = end.phone || (shopify.customer && shopify.customer.phone) || '';
    if (!d.cpf) d.cpf = formatarCpf(extrairCpfShopify(shopify));
    if (!d.endereco) d.endereco = end.address1 || '';
    if (!d.complemento) d.complemento = end.address2 || '';
    if (!d.cidade) d.cidade = end.city || '';
    if (!d.uf) d.uf = end.province_code || end.province || '';
    if (!d.cep) d.cep = end.zip || '';

    const t: any = base.transportadora;
    const f0 = (Array.isArray(shopify.fulfillments) ? shopify.fulfillments : [])[0];
    if (f0) {
      if (!t.codigo_rastreio) t.codigo_rastreio = f0.tracking_number || '';
      if (!t.url_rastreio) t.url_rastreio = f0.tracking_url || '';
      if (!t.nome) t.nome = f0.tracking_company || '';
    }
  }

  // Completa os buracos com o que o Factory sabe (papel equivalente ao bloco
  // da Shopify acima) - NF/CPF/endereco/itens que a Intelipost nao devolve
  // (ou pedido tao novo que ainda nem chegou na Intelipost).
  if (fatorGocase) {
    const p: any = base.pedido;
    const user = fatorGocase.user || {};
    const end = fatorGocase.shipping_address || {};
    if (!p.numero_pedido) p.numero_pedido = fatorGocase.reference || '';
    if (!p.numero_nf) p.numero_nf = fatorGocase.nfe_number || '';
    if (!p.chave_nf) p.chave_nf = fatorGocase.nfe_key || '';
    if (!p.data_pedido) p.data_pedido = fatorGocase.created_at || '';
    if (!p.data_faturamento) p.data_faturamento = fatorGocase.nfe_date || '';
    if (p.valor_total == null && fatorGocase.total_value != null) p.valor_total = Number(fatorGocase.total_value);
    if (!p.produtos || !p.produtos.length) {
      let itensBrutos: any[] = [];
      try {
        const raw = fatorGocase.abridged_line_items;
        itensBrutos = Array.isArray(raw) ? raw : (typeof raw === 'string' ? JSON.parse(raw) : []);
      } catch {
        itensBrutos = [];
      }
      p.produtos = itensBrutos.map((li: any) => ({ descricao: (li && li.sku) || '', sku: (li && li.sku) || '', quantidade: 1 }));
    }

    const d: any = base.destinatario;
    if (!d.nome) d.nome = [user.first_name, user.last_name].filter(Boolean).join(' ') || [end.first_name, end.last_name].filter(Boolean).join(' ');
    if (!d.email) d.email = fatorGocase.email || user.email || '';
    if (!d.telefone) d.telefone = end.phone || '';
    if (!d.cpf) d.cpf = formatarCpf(user.cpf || '');
    if (!d.endereco) d.endereco = end.address1 || '';
    if (!d.complemento) d.complemento = end.address2 || '';
    if (!d.cidade) d.cidade = end.city || '';
    if (!d.uf) d.uf = end.state_code || end.state || '';
    if (!d.cep) d.cep = end.zipcode || '';

    const t2: any = base.transportadora;
    if (!t2.codigo_rastreio) t2.codigo_rastreio = fatorGocase.track_code || '';
    if (!t2.nome) t2.nome = fatorGocase.delivery_service || '';
  }

  // EAN via Metabase Gobeaute - so pras marcas Gobeaute (Gocase nao usa o
  // cadastro "Cosmos"; ver comentario grande em buscarEanPorSku).
  if (marca.id !== 'gocase' && base.pedido && Array.isArray(base.pedido.produtos) && base.pedido.produtos.length) {
    const skus = base.pedido.produtos.map((p: any) => p.sku).filter(Boolean);
    if (skus.length) {
      const eanPorSku = await buscarEanPorSku(env, skus);
      base.pedido.produtos = base.pedido.produtos.map((p: any) => ({ ...p, ean: eanPorSku[p.sku] || '' }));
    }
  }

  const tickets = await buscarTicketsDoPedido(base.pedido.numero_pedido || '', base.pedido.numero_nf || '');

  return {
    ...base,
    marca: { id: marca.id, nome: marca.nome, supabase: marca.marcaSupabase },
    fontes: { intelipost: !!intelipost, shopify: !!shopify, metabase: !!fatorGocase },
    outros_pedidos_cliente: typeof outrosPedidos !== 'undefined' ? outrosPedidos : [],
    tickets_existentes: tickets.map((t: any) => {
      // Alem dos campos "amigaveis", devolvemos a LINHA CRUA (raw) com os nomes
      // de coluna originais. E o que permite a Torre reaproveitar calcStatus()/
      // statusBadge() do frontend - as mesmas funcoes que a tela inicial usa -
      // em vez de reimplementar a regra de status num segundo lugar e correr o
      // risco das duas telas divergirem.
      const raw = { ...t };
      delete raw._tabela;
      return {
        thread_id: t.thread_id,
        id: t.id,
        message_id: t.message_id,
        tabela: t._tabela,
        marca: t.marca,
        assunto: t.subject,
        motivo: t.Motivo,
        status: t.Status_resposta_tickets,
        ultimo_status: t['Último Status'],
        transportadora: t.transportadora,
        precisa_responder: t['Precisa responder?'],
        tem_resposta: t.tem_resposta,
        total_mensagens: t.total_mensagens_thread,
        remetente: t.remetente,
        urgencia: t['Urgência'],
        tipo_ocorrencia: t.tipo_ocorrencia,
        data_email: t.data_email,
        updated_at: t.updated_at,
        numero_nf: t.numero_nf,
        numero_pedido: t['Número do pedido'],
        raw,
      };
    }),
  };
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // ---- Torre de Controle: lista de marcas pro dropdown ----
    if (url.pathname === '/api/logistica/marcas' && request.method === 'GET') {
      return Response.json({
        marcas: MARCAS_LOGISTICA.map((m) => ({
          id: m.id,
          nome: m.nome,
          configurada: m.id === 'gocase'
            ? !!(env as any).INTELIPOST_KEY_GOCASE && !!(env as any).METABASE_KEY_GOCASE
            : !!(env as any)[m.envIntelipost as string] && !!(env as any)[m.envShopify as string],
        })),
      });
    }

    // ---- Torre de Controle: busca unificada ----
    if (url.pathname === '/api/logistica/buscar' && request.method === 'GET') {
      try {
        const termo = (url.searchParams.get('termo') || '').trim();
        const marcaId = (url.searchParams.get('marca') || 'auto').trim().toLowerCase();
        if (!termo) return Response.json({ error: 'informe um termo de busca' }, { status: 400 });
        if (termo.length > 60) return Response.json({ error: 'termo muito longo' }, { status: 400 });

        const tipo = classificarTermo(termo);
        // NF so e segura quando escopada a UMA marca por vez - NF nao e
        // unica entre marcas diferentes (cada uma numera do zero na sua
        // propria conta Intelipost; confirmado por Ivna, 26/08/2026: a NF
        // 150956 em modo "auto" bateu num pedido real da Lescent sem nenhuma
        // relacao, so por a mesma NF existir por coincidencia nas duas
        // contas). Restrito a Apice por pedido dela: as outras 6 marcas com
        // sufixo "SH..." ja tem busca por numero do pedido 100% confiavel,
        // entao nao precisam de NF; Apice e a excecao (numero de pedido dela
        // diverge do numero do pedido no Titan, entao NF e a unica chave em
        // comum - ver torreBuscarNaMarca, tipo==='nf').
        if (tipo === 'nf' && marcaId !== 'apice') {
          return Response.json({
            encontrado: false,
            termo,
            tipo_busca: tipo,
            marcas_tentadas: [],
            error: `Busca por NF só é suportada pra Apice (NF pode coincidir entre marcas diferentes) - selecione "Apice", ou use o número do pedido para as demais marcas.`,
          }, { status: 400 });
        }
        let candidatas: MarcaLogistica[];
        if (marcaId && marcaId !== 'auto') {
          const m = getMarcaLogistica(marcaId);
          if (!m) return Response.json({ error: `marca desconhecida: ${marcaId}` }, { status: 400 });
          candidatas = [m];
        } else {
          candidatas = tipo === 'pedido' ? inferirMarcasPorPedido(termo) : MARCAS_LOGISTICA;
        }

        const tentadas: string[] = [];
        for (const marca of candidatas) {
          tentadas.push(marca.id);
          const achado = await torreBuscarNaMarca(env, marca, termo, tipo);
          if (achado) return Response.json({ ...achado, termo, tipo_busca: tipo, marcas_tentadas: tentadas });
        }

        return Response.json({
          encontrado: false,
          termo,
          tipo_busca: tipo,
          marcas_tentadas: tentadas,
          error: `Nada encontrado para "${termo}" (${tipo}) em: ${tentadas.join(', ')}.`,
        }, { status: 404 });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    // ---- Torre de Controle: pede pro Titan BI consultar o romaneio de um
    // pedido (fila - ver comentario grande em TITAN_SB_URL acima). A CHAVE E
    // (NOTA FISCAL, MARCA) - migrado de so-NF em 24/08/2026 depois de achar,
    // com print real da tela do Titan, que a MESMA NF aparece em mais de uma
    // linha (uma por marca), CADA UMA com romaneio diferente (ex: NF 940380:
    // Kokeshi 169114, Rituaria 173827) - o risco que tinhamos combinado como
    // "raro, pode arriscar" aconteceu de verdade. "marca" aqui e o id da
    // Torre (ex: "rituaria") - o scraper compara sem diferenciar caixa contra
    // o "Nome Projeto" do Titan (ex: "RITUARIA"). So insere se ainda nao
    // existir NENHUMA linha pra essa combinacao, pra nao voltar um
    // "concluido" antigo (ou um resultado ja trazido pelo backfill) pra
    // 'pendente' de novo a cada clique repetido.
    if (url.pathname === '/api/logistica/titan-solicitar' && request.method === 'POST') {
      try {
        const body: any = await request.json().catch(() => ({}));
        const numeroNf = String(body.numero_nf || '').trim();
        const numeroPedido = String(body.numero_pedido || '').trim();
        const marca = String(body.marca || '').trim();
        if (!numeroNf) return Response.json({ error: 'numero_nf obrigatorio (Titan BI so e buscavel por Nota Fiscal)' }, { status: 400 });
        if (!marca) return Response.json({ error: 'marca obrigatoria (NF sozinha pode ser de mais de uma marca - ver comentario acima)' }, { status: 400 });

        const jaExiste = await fetchJsonComTimeout(
          `${TITAN_SB_URL}/rest/v1/infos_titan?numero_nf=eq.${encodeURIComponent(numeroNf)}&marca=eq.${encodeURIComponent(marca)}&select=numero_nf,status`,
          { headers: titanHeaders() },
          LOG_TIMEOUT_MS
        );
        if (Array.isArray(jaExiste) && jaExiste.length) {
          const statusAtual = jaExiste[0].status;
          // 'erro' vale re-enfileirar (ex: o script no PC da Ivna nao estava
          // rodando na primeira tentativa) - 'pendente'/'concluido' ficam como
          // estao (nao interrompe um processamento em andamento nem refaz uma
          // consulta que ja deu certo, seja de um pedido avulso ou do backfill).
          if (statusAtual === 'erro') {
            await fetchComTimeout(`${TITAN_SB_URL}/rest/v1/infos_titan?numero_nf=eq.${encodeURIComponent(numeroNf)}&marca=eq.${encodeURIComponent(marca)}`, {
              method: 'PATCH',
              headers: { ...titanHeaders(), Prefer: 'return=minimal' },
              body: JSON.stringify({ status: 'pendente', erro: null, numero_pedido: numeroPedido || undefined }),
            }, LOG_TIMEOUT_MS);
            return Response.json({ ok: true, ja_existia: true, status: 'pendente' });
          }
          return Response.json({ ok: true, ja_existia: true, status: statusAtual });
        }

        const r = await fetchComTimeout(`${TITAN_SB_URL}/rest/v1/infos_titan`, {
          method: 'POST',
          headers: { ...titanHeaders(), Prefer: 'return=minimal' },
          body: JSON.stringify({
            numero_nf: numeroNf,
            numero_pedido: numeroPedido || null,
            marca,
            status: 'pendente',
          }),
        }, LOG_TIMEOUT_MS);
        if (!r.ok) {
          const t = await r.text().catch(() => '');
          return Response.json({ error: `Supabase respondeu ${r.status}: ${t.slice(0, 300)}` }, { status: 502 });
        }
        return Response.json({ ok: true, ja_existia: false, status: 'pendente' });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    // ---- Torre de Controle: consulta o status/resultado da fila do Titan
    // (chave = NF+marca, mesmo motivo do endpoint acima) ----
    if (url.pathname === '/api/logistica/titan-status' && request.method === 'GET') {
      try {
        const numeroNf = (url.searchParams.get('nf') || '').trim();
        const marca = (url.searchParams.get('marca') || '').trim();
        if (!numeroNf) return Response.json({ error: 'nf obrigatorio' }, { status: 400 });
        if (!marca) return Response.json({ error: 'marca obrigatoria' }, { status: 400 });
        const linhas = await fetchJsonComTimeout(
          `${TITAN_SB_URL}/rest/v1/infos_titan?numero_nf=eq.${encodeURIComponent(numeroNf)}&marca=eq.${encodeURIComponent(marca)}&select=*`,
          { headers: titanHeaders() },
          LOG_TIMEOUT_MS
        );
        const linha = Array.isArray(linhas) && linhas.length ? linhas[0] : null;
        if (!linha) return Response.json({ status: 'inexistente' });
        return Response.json(linha);
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    // ---- Torre de Controle: abrir ticket de logistica ----
    if (url.pathname === '/api/logistica/abrir-ticket' && request.method === 'POST') {
      try {
        const body: any = await request.json().catch(() => ({}));
        const problemasValidos = ['Acareação', 'Pedido Atrasado', 'Alterar Endereço', 'Barra Entrega'];
        const problema = String(body.problema || '');
        if (!problemasValidos.includes(problema)) {
          return Response.json({ error: `problema invalido. Validos: ${problemasValidos.join(', ')}` }, { status: 400 });
        }
        if (!body.numero_pedido) return Response.json({ error: 'numero_pedido obrigatorio' }, { status: 400 });
        if (!body.marca) return Response.json({ error: 'marca obrigatoria' }, { status: 400 });

        await initTicketsLogisticaTable(env);
        const protocolo = 'LOG-' + Date.now().toString(36).toUpperCase();
        const criadoEm = new Date().toISOString();

        // ENCAMINHAMENTO PRO DESTINO REAL (CX Hub / n8n): fica DESLIGADO enquanto
        // o secret CXHUB_TICKET_WEBHOOK nao existir. Foi feito assim de proposito
        // - nao da pra inventar o endpoint de um sistema de producao que eu nunca
        // vi. Sem o secret, o ticket e gravado localmente (env.DB) e a tela avisa
        // que ficou em rascunho. Com o secret configurado, o mesmo payload e
        // encaminhado e a resposta fica registrada em resposta_webhook.
        const webhook = (env as any).CXHUB_TICKET_WEBHOOK;
        let encaminhado = 0;
        let respostaWebhook = '';
        const payload = {
          protocolo,
          marca: body.marca,
          problema,
          numero_pedido: body.numero_pedido,
          numero_nf: body.numero_nf || '',
          transportadora: body.transportadora || '',
          codigo_rastreio: body.codigo_rastreio || '',
          previsao_entrega: body.previsao_entrega || '',
          dias_atraso: body.dias_atraso != null ? body.dias_atraso : null,
          destinatario: body.destinatario || '',
          // Bloco que a transportadora usa pra localizar a entrega.
          cpf_destinatario: body.cpf_destinatario || '',
          chave_danfe: body.chave_danfe || '',
          plp_intelipost: body.plp_intelipost || '',
          romaneio_transportadora: body.romaneio_transportadora || '',
          endereco_entrega: body.endereco_entrega || '',
          // So preenchido quando problema === 'Alterar Endereço' (ver frontend).
          endereco_novo: body.endereco_novo || '',
          maps_url: body.maps_url || '',
          observacao: String(body.observacao || '').slice(0, 2000),
          responsavel: String(body.responsavel || '').slice(0, 120),
          criado_em: criadoEm,
          // Preenchidos pelo frontend quando o agente manda um e-mail real pela
          // previa (torreEnviarTicket com emailInfo) - o envio em si acontece
          // no navegador, direto pro WEBHOOK_ENVIAR (mesmo caminho n8n/Gmail do
          // botao "Responder"); aqui so registramos o resultado.
          email_para: body.email_para || '',
          email_cc: body.email_cc || '',
          email_bcc: body.email_bcc || '',
          email_assunto: body.email_assunto || '',
          email_enviado: body.email_enviado ? 1 : 0,
          email_resposta: String(body.email_resposta || '').slice(0, 500),
        };

        if (webhook) {
          try {
            const r = await fetchComTimeout(webhook, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            }, LOG_TIMEOUT_MS);
            encaminhado = r.ok ? 1 : 0;
            respostaWebhook = `HTTP ${r.status}: ${(await r.text().catch(() => '')).slice(0, 300)}`;
          } catch (e: any) {
            respostaWebhook = 'falha: ' + String((e && e.message) || e);
          }
        }

        await env.DB.exec(
          `INSERT INTO tickets_logistica
             (protocolo, marca, numero_pedido, numero_nf, problema, transportadora, codigo_rastreio,
              previsao_entrega, dias_atraso, destinatario, observacao, responsavel, encaminhado,
              resposta_webhook, criado_em, cpf_destinatario, chave_danfe, plp_intelipost,
              romaneio_transportadora, endereco_entrega, email_para, email_assunto, email_enviado,
              email_resposta, endereco_novo, maps_url, email_cc, email_bcc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
          [protocolo, payload.marca, payload.numero_pedido, payload.numero_nf, problema, payload.transportadora,
           payload.codigo_rastreio, payload.previsao_entrega, payload.dias_atraso, payload.destinatario,
           payload.observacao, payload.responsavel, encaminhado, respostaWebhook, criadoEm,
           payload.cpf_destinatario, payload.chave_danfe, payload.plp_intelipost,
           payload.romaneio_transportadora, payload.endereco_entrega, payload.email_para,
           payload.email_assunto, payload.email_enviado, payload.email_resposta,
           payload.endereco_novo, payload.maps_url, payload.email_cc, payload.email_bcc]
        );

        return Response.json({
          ok: true,
          protocolo,
          encaminhado: !!encaminhado,
          destino: webhook ? 'webhook configurado' : 'rascunho local (CXHUB_TICKET_WEBHOOK nao configurado)',
          resposta_webhook: respostaWebhook,
        });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    // ---- Unilog CD: lista TODOS os candidatos (marca/situacao/pedido) que o
    // Titan ja tem cacheados em infos_titan pra uma NF, sem exigir marca - so
    // LEITURA do que ja esta la, nunca dispara consulta nova. Existe pra
    // resolver um bug real (25/08/2026, achado pela Ivna): a mesma NF pode
    // aparecer em mais de uma marca (ver comentario grande no endpoint
    // titan-solicitar acima), e a busca da Torre (Intelipost/Shopify, marca
    // "auto") pode resolver pra uma marca que nem bate com o que o Titan
    // realmente tem cadastrado pra essa NF - o frontend usa isto pra deixar o
    // agente escolher o pedido certo ANTES de fixar a marca.
    if (url.pathname === '/api/logistica/titan-candidatos' && request.method === 'GET') {
      try {
        const numeroNf = (url.searchParams.get('nf') || '').trim();
        if (!numeroNf) return Response.json({ error: 'nf obrigatorio' }, { status: 400 });
        const linhas = await fetchJsonComTimeout(
          `${TITAN_SB_URL}/rest/v1/infos_titan?numero_nf=eq.${encodeURIComponent(numeroNf)}&select=marca,numero_pedido,status,situacao,romaneio,nome_projeto`,
          { headers: titanHeaders() },
          LOG_TIMEOUT_MS
        );
        return Response.json({ candidatos: Array.isArray(linhas) ? linhas : [] });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    // ---- Torre de Controle: tickets de logistica ja registrados aqui ----
    if (url.pathname === '/api/logistica/tickets-abertos' && request.method === 'GET') {
      try {
        await initTicketsLogisticaTable(env);
        const pedido = (url.searchParams.get('pedido') || '').trim();
        const res = pedido
          ? await env.DB.query(`SELECT * FROM tickets_logistica WHERE numero_pedido = ? ORDER BY id DESC LIMIT 50`, [pedido])
          : await env.DB.query(`SELECT * FROM tickets_logistica ORDER BY id DESC LIMIT 50`, []);
        return Response.json({ tickets: res.rows || [] });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-fetch-tickets-timing' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const tabela = tabelaOuPadrao(url);
        const probeParam = url.searchParams.get('probe') || 'todas';
        const probesDisponiveis: Record<string, { nome: string; url: string; headers?: Record<string, string>; apikey?: string }> = {
          simples: { nome: 'select=id limit=1 (sem order, sem range)', url: `${SB_URL}/rest/v1/${tabela}?select=id&limit=1` },
          order: { nome: 'select=id limit=1 order=id.asc (sem range)', url: `${SB_URL}/rest/v1/${tabela}?select=id&order=id.asc&limit=1` },
          range: { nome: 'select=id order=id.asc com Range 0-999', url: `${SB_URL}/rest/v1/${tabela}?select=id&order=id.asc`, headers: { 'Range-Unit': 'items', Range: '0-999' } },
          estrela: { nome: 'select=* limit=1 (sem order)', url: `${SB_URL}/rest/v1/${tabela}?select=*&limit=1` },
          raiz: { nome: 'raiz do Supabase (sem tabela, so healthcheck do REST)', url: `${SB_URL}/rest/v1/` },
          outroProjeto: { nome: 'outro projeto Supabase (GB_SB_URL) - base_reenvio_reembolso limit=1', url: `${GB_SB_URL}/rest/v1/base_reenvio_reembolso?select=id&limit=1`, headers: { 'Accept-Profile': GB_SB_SCHEMA }, apikey: GB_SB_KEY },
        };
        const executarProbe = async (key: string) => {
          const probe = probesDisponiveis[key];
          const chave = probe.apikey || SB_KEY;
          const t0 = Date.now();
          try {
            const r = await fetchComTimeout(probe.url, { headers: { apikey: chave, Authorization: `Bearer ${chave}`, ...(probe.headers || {}) } }, 12000);
            const tempoMs = Date.now() - t0;
            if (!r.ok) {
              const t = await r.text().catch(() => '');
              return { probe: probe.nome, ok: false, status: r.status, tempoMs, erro: t.slice(0, 300) };
            }
            const rows: any = await r.json();
            return { probe: probe.nome, ok: true, status: r.status, tempoMs, linhas: Array.isArray(rows) ? rows.length : null };
          } catch (e: any) {
            return { probe: probe.nome, ok: false, tempoMs: Date.now() - t0, erro: String((e && e.message) || e) };
          }
        };
        if (probeParam !== 'todas') {
          if (!probesDisponiveis[probeParam]) return Response.json({ error: `probe desconhecida: ${probeParam}. Use: ${Object.keys(probesDisponiveis).join(', ')}` }, { status: 400 });
          const resultado = await executarProbe(probeParam);
          return Response.json({ tabela, resultado });
        }
        const resultados = [];
        for (const key of Object.keys(probesDisponiveis)) resultados.push(await executarProbe(key));
        return Response.json({ tabela, resultados });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/migrar-devolucao-precisa-responder' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabelasParam: string[] = Array.isArray(body.tabelas) && body.tabelas.length
          ? body.tabelas.filter((t: any) => TABELAS_VALIDAS.has(t))
          : Array.from(TABELAS_VALIDAS);
        const resultados = [];
        for (const tabela of tabelasParam) {
          resultados.push(await migrarDevolucaoNaoPrecisaResponder(tabela));
        }
        return Response.json({ resultados });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname.startsWith('/api/gmail-watch/') && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const conta = getConta(url.pathname.slice('/api/gmail-watch/'.length));
        const resultado = await ativarWatch(env, conta);
        return Response.json({ ok: true, conta: conta.id, ...resultado });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname.startsWith('/api/verificar-gmail/') && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const conta = getConta(url.pathname.slice('/api/verificar-gmail/'.length));
        const resultado = await verificarGmail(env, conta);
        return Response.json(resultado);
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname.startsWith('/api/classificar-pendentes/') && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      if (!env.AI_PROXY_TOKEN) return Response.json({ error: 'AI_PROXY_TOKEN nao configurado nos secrets do app' }, { status: 500 });
      try {
        const alias = url.pathname.slice('/api/classificar-pendentes/'.length);
        const tabela = alias === 'gocase' ? 'tickets_gocase' : alias === 'gobeaute' ? 'tickets_gobeaute' : null;
        if (!tabela) return Response.json({ error: `alias desconhecido: "${alias}". Use "gobeaute" ou "gocase".` }, { status: 400 });
        const resultado = await processarLotePendentes(env, tabela, BATCH_SIZE);
        console.log('[classificar-pendentes/' + alias + ']', JSON.stringify({ ...resultado, erros: resultado.erros.slice(0, 5) }));
        return Response.json(resultado);
      } catch (e: any) {
        console.error('[classificar-pendentes alias] erro fatal:', String((e && e.message) || e));
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname.startsWith('/api/backfill-periodo/') && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const suffix = url.pathname.slice('/api/backfill-periodo/'.length);
        const historico = suffix.endsWith('-historico');
        const contaId = historico ? suffix.slice(0, -'-historico'.length) : suffix;
        const conta = getConta(contaId);
        const agora = new Date();
        const desde = historico ? '2026-01-01' : new Date(agora.getTime() - 45 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
        const ate = agora.toISOString();
        const limit = historico ? 40 : 10;
        const resultado = await backfillPeriodo(env, conta, desde, ate, limit);
        return Response.json(resultado);
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-payload-estrutura' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const threadId = url.searchParams.get('thread_id');
        if (!threadId) return Response.json({ error: 'thread_id obrigatorio' }, { status: 400 });
        const conta = getConta(url.searchParams.get('conta') || 'gobeaute');
        const accessToken = await getGmailAccessToken(env, conta);
        const thread = await gmailGet(accessToken, `/threads/${encodeURIComponent(threadId)}?format=full`);
        const messages = thread.messages || [];
        const mensagensRecebidas = messages.filter((m: any) => !((m.labelIds || []).includes('SENT')));
        const ultimaRecebida = mensagensRecebidas[mensagensRecebidas.length - 1];
        return Response.json({
          threadId,
          totalMensagens: messages.length,
          totalRecebidas: mensagensRecebidas.length,
          snippetUltimaRecebida: ultimaRecebida ? ultimaRecebida.snippet : null,
          estruturaUltimaRecebida: ultimaRecebida ? resumirEstruturaPayload(ultimaRecebida.payload) : null,
          corpoExtraidoAtualmente: ultimaRecebida ? extrairCorpoMensagem(ultimaRecebida.payload).slice(0, 300) : null,
        });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/backfill-periodo' && request.method === 'POST') {
      if (!autorizado(request, env)) {
        return Response.json({ error: 'nao autorizado' }, { status: 401 });
      }
      try {
        const conta = getConta(url.searchParams.get('conta') || 'gobeaute');
        const body: any = await request.json().catch(() => ({}));
        const agora = new Date();
        const quarentaCincoDiasAtras = new Date(agora.getTime() - 45 * 24 * 60 * 60 * 1000);
        const desde = url.searchParams.get('desde') || body.desde || quarentaCincoDiasAtras.toISOString().slice(0, 10);
        const ate = url.searchParams.get('ate') || body.ate || agora.toISOString();
        const limitRaw = url.searchParams.get('limit') || body.limit;
        const limit = Math.min(Number(limitRaw) || 10, 20);
        const resultado = await backfillPeriodo(env, conta, desde, ate, limit);
        return Response.json(resultado);
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/gmail-watch' && request.method === 'POST') {
      if (!autorizado(request, env)) {
        return Response.json({ error: 'nao autorizado' }, { status: 401 });
      }
      try {
        const conta = getConta(url.searchParams.get('conta') || 'gobeaute');
        const resultado = await ativarWatch(env, conta);
        return Response.json({ ok: true, conta: conta.id, ...resultado });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/gmail-push' && request.method === 'POST') {
      const token = url.searchParams.get('token');
      const tokensValidos = [env.PUBSUB_VERIFY_TOKEN, env.PUBSUB_VERIFY_TOKEN_GOCASE].filter(Boolean);
      if (!token || !tokensValidos.includes(token)) {
        return Response.json({ error: 'token invalido' }, { status: 401 });
      }
      let conta: ContaGmail;
      try {
        conta = getConta(url.searchParams.get('conta') || 'gobeaute');
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 400 });
      }
      return Response.json({ ok: true, conta: conta.id, ack: true });
    }

    if (url.pathname === '/api/verificar-gmail' && request.method === 'POST') {
      if (!autorizado(request, env)) {
        return Response.json({ error: 'nao autorizado' }, { status: 401 });
      }
      try {
        const conta = getConta(url.searchParams.get('conta') || 'gobeaute');
        const resultado = await verificarGmail(env, conta);
        return Response.json(resultado);
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/classificar-pendentes' && request.method === 'POST') {
      if (!autorizado(request, env)) {
        return Response.json({ error: 'nao autorizado' }, { status: 401 });
      }
      if (!env.AI_PROXY_TOKEN) {
        return Response.json({ error: 'AI_PROXY_TOKEN nao configurado nos secrets do app' }, { status: 500 });
      }
      try {
        const tabela = tabelaOuPadrao(url);
        const resultado = await processarLotePendentes(env, tabela, BATCH_SIZE);
        console.log('[classificar-pendentes]', JSON.stringify({ ...resultado, erros: resultado.erros.slice(0, 5) }));
        return Response.json(resultado);
      } catch (e: any) {
        console.error('[classificar-pendentes] erro fatal:', String((e && e.message) || e));
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/reclassificar-lote-manual' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      if (!env.AI_PROXY_TOKEN) {
        return Response.json({ error: 'AI_PROXY_TOKEN nao configurado nos secrets do app' }, { status: 500 });
      }
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
        const limit = Math.min(Math.max(parseInt(body.limit, 10) || 50, 1), 150);
        const desde = typeof body.desde === 'string' && body.desde.trim() ? body.desde.trim() : undefined;
        const ate = typeof body.ate === 'string' && body.ate.trim() ? body.ate.trim() : undefined;
        const resultado = await processarLotePendentes(env, tabela, limit, desde, ate);
        console.log('[reclassificar-lote-manual]', JSON.stringify({ tabela, desde, ate, ...resultado, erros: resultado.erros.slice(0, 5) }));
        return Response.json({ desde: desde || null, ate: ate || null, ...resultado });
      } catch (e: any) {
        console.error('[reclassificar-lote-manual] erro fatal:', String((e && e.message) || e));
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/reprocessar-ticket' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const threadId = body.thread_id;
        const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
        if (!threadId) return Response.json({ error: 'thread_id obrigatorio' }, { status: 400 });
        if (!env.AI_PROXY_TOKEN) return Response.json({ error: 'AI_PROXY_TOKEN nao configurado' }, { status: 500 });

        let conta: ContaGmail | null = body.conta ? getConta(body.conta) : null;
        if (!conta) {
          const r0 = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}&select=remetente&limit=1`, {
            headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` },
          });
          const rows0: any[] = r0.ok ? await r0.json() : [];
          const remetente = rows0[0] && rows0[0].remetente;
          conta = inferirConta(tabela, remetente);
        }
        if (!conta) {
          return Response.json({ error: `nao foi possivel inferir a conta Gmail para este ticket (tabela=${tabela}). Passe "conta" explicitamente no body.` }, { status: 400 });
        }

        const accessToken = await getGmailAccessToken(env, conta);
        const thread = await gmailGet(accessToken, `/threads/${encodeURIComponent(threadId)}?format=full`);
        const info = processThread(thread);
        const respostaGravada = await gravarResposta(tabela, threadId, info);

        let classificado = false;
        let cls: Classificacao | null = null;
        let jaRespondemos = false;
        if (info.ultima_mensagem_enviada) {
          jaRespondemos = await refletirRespostaJaEnviada(tabela, threadId);
        } else if (info.tem_resposta && info.ultima_mensagem.trim()) {
          cls = await classificarComProxy(env, env.AI_PROXY_TOKEN, info.subject, info.ultima_mensagem);
          classificado = await gravarClassificacao(tabela, threadId, cls);
          if (classificado) {
            const categoria = cls.motivo.split('—')[0].trim().toUpperCase();
            await notificarAvariaSeNecessario(env, tabela, threadId, categoria, info.subject, info.ultima_recebida_from, info.ultima_mensagem_id);
            if (categoria === 'EXTRAVIO') {
              await definirStatusManual(tabela, threadId, 'extravio');
            }
          }
        }
        return Response.json({
          threadId,
          tabela,
          conta: conta.id,
          totalMensagensThread: info.total_mensagens_thread,
          temResposta: info.tem_resposta,
          ultimaMensagemEnviada: info.ultima_mensagem_enviada,
          jaRespondemosAplicado: jaRespondemos,
          ultimaMensagemPreview: info.ultima_mensagem.slice(0, 200),
          respostaGravada,
          classificado,
          novaClassificacao: cls,
        });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-mensagem' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const tabela = tabelaOuPadrao(url);
        const threadId = url.searchParams.get('thread_id');
        const numeroNf = url.searchParams.get('numero_nf');
        const filtro = threadId
          ? `thread_id=eq.${encodeURIComponent(threadId)}`
          : `numero_nf=eq.${encodeURIComponent(numeroNf || '')}`;
        const r = await fetch(`${SB_URL}/rest/v1/${tabela}?${filtro}&select=*`, {
          headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` },
        });
        const rows: any[] = await r.json();
        return Response.json({ tabela, tickets: rows });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-tipos-ocorrencia' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const tabela = tabelaOuPadrao(url);
        const contagem: Record<string, number> = {};
        let offset = 0;
        const TETO_SCAN = 60000;
        while (offset < TETO_SCAN) {
          const r = await fetch(
            `${SB_URL}/rest/v1/${tabela}?select=tipo_ocorrencia`,
            { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, 'Range-Unit': 'items', Range: `${offset}-${offset + SCAN_WINDOW - 1}` } }
          );
          if (!r.ok) {
            const errBody = await r.text().catch(() => '');
            throw new Error(`Supabase HTTP ${r.status}: ${errBody.slice(0, 300)}`);
          }
          const page: any[] = await r.json();
          page.forEach((row) => {
            const v = (row.tipo_ocorrencia || '').trim() || '(vazio)';
            contagem[v] = (contagem[v] || 0) + 1;
          });
          if (page.length < SCAN_WINDOW) break;
          offset += SCAN_WINDOW;
        }
        const ordenado = Object.entries(contagem).sort((a, b) => b[1] - a[1]).map(([tipo_ocorrencia, count]) => ({ tipo_ocorrencia, count }));
        return Response.json({ tabela, totalDistintos: ordenado.length, tipos: ordenado });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-pendentes' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const tabela = tabelaOuPadrao(url);
        const desde = url.searchParams.get('desde') || undefined;
        const ate = url.searchParams.get('ate') || undefined;
        const pendentes = await buscarPendentes(tabela, desde, ate);
        return Response.json({
          tabela,
          desde: desde || null,
          ate: ate || null,
          totalPendentes: pendentes.length,
          amostra: pendentes.slice(0, 10).map((t) => ({
            thread_id: t.thread_id,
            tipoThreadId: typeof t.thread_id,
            subject: t.subject,
            data_email: t.data_email,
            tem_resposta: t.tem_resposta,
            Motivo: t['Motivo'],
            updated_at: t.updated_at,
          })),
        });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-status-extravio' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const tabela = tabelaOuPadrao(url);
        const r = await fetch(
          `${SB_URL}/rest/v1/${tabela}?select=id,numero_nf,thread_id,marca,${encodeURIComponent('Motivo')},updated_at,Status_resposta_tickets&Status_resposta_tickets=eq.extravio&order=updated_at.desc&limit=20`,
          { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } }
        );
        if (!r.ok) {
          const errBody = await r.text().catch(() => '');
          throw new Error(`Supabase HTTP ${r.status}: ${errBody.slice(0, 300)}`);
        }
        const rows: any[] = await r.json();
        return Response.json({ tabela, tickets: rows });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/reverter-status-extravio' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
        const threadIds: string[] = Array.isArray(body.thread_ids) ? body.thread_ids.filter(Boolean).slice(0, 50) : [];
        if (!threadIds.length) return Response.json({ error: 'thread_ids vazio' }, { status: 400 });
        const revertidos: string[] = [];
        const falhas: string[] = [];
        for (const threadId of threadIds) {
          const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
            method: 'PATCH',
            headers: {
              apikey: SB_KEY,
              Authorization: `Bearer ${SB_KEY}`,
              'Content-Type': 'application/json',
              Prefer: 'return=representation',
            },
            body: JSON.stringify({ Status_resposta_tickets: null, updated_at: new Date().toISOString() }),
          });
          if (r.ok) {
            try {
              const rows: any[] = await r.json();
              if (Array.isArray(rows) && rows.length > 0) revertidos.push(threadId);
              else falhas.push(threadId);
            } catch { falhas.push(threadId); }
          } else {
            falhas.push(threadId);
          }
        }
        return Response.json({ tabela, revertidos, falhas });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/ultimos-classificados' && request.method === 'GET') {
      try {
        const tabela = tabelaOuPadrao(url);
        const r = await fetch(
          `${SB_URL}/rest/v1/${tabela}?select=*&${encodeURIComponent('Motivo')}=not.is.null&order=updated_at.desc&limit=10`,
          { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } }
        );
        if (!r.ok) {
          const errBody = await r.text().catch(() => '');
          throw new Error(`Supabase HTTP ${r.status}: ${errBody.slice(0, 300)}`);
        }
        const rows: any[] = await r.json();
        const tickets = rows.map((t) => ({
          numero_nf: t.numero_nf,
          marca: t.marca,
          Motivo: t['Motivo'],
          'Urgência': t['Urgência'],
          'Precisa responder?': t['Precisa responder?'],
          updated_at: t.updated_at,
          data_email: t.data_email,
          thread_id: t.thread_id,
        }));
        return Response.json({ tabela, tickets });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/status-classificacao' && request.method === 'GET') {
      try {
        const tabela = tabelaOuPadrao(url);
        const desde = url.searchParams.get('desde') || '2026-06-01';
        const ate = url.searchParams.get('ate') || '2026-07-02T23:59:59';
        const r = await fetch(
          `${SB_URL}/rest/v1/${tabela}?select=id&${encodeURIComponent('Ultima mensagem')}=is.null&data_email=gte.${encodeURIComponent(desde)}&data_email=lte.${encodeURIComponent(ate)}`,
          { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, 'Range-Unit': 'items', Range: '0-0', Prefer: 'count=exact' } }
        );
        const contentRange = r.headers.get('Content-Range');
        let pendentes = 0;
        if (contentRange && contentRange.indexOf('/') !== -1) {
          pendentes = parseInt(contentRange.split('/')[1], 10) || 0;
        }
        return Response.json({ tabela, pendentes, desde, ate });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-ticket' && request.method === 'POST') {
      if (!autorizado(request, env)) {
        return Response.json({ error: 'nao autorizado' }, { status: 401 });
      }
      const body: any = await request.json().catch(() => ({}));
      const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
      const filtro = body.numero_nf
        ? `numero_nf=eq.${encodeURIComponent(body.numero_nf)}`
        : `thread_id=eq.${encodeURIComponent(body.thread_id || '19f1ec5ddfefcf66')}`;
      const r = await fetch(`${SB_URL}/rest/v1/${tabela}?${filtro}&select=*`, {
        headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` },
      });
      const rows = await r.json();
      return Response.json({ status: r.status, tabela, rows });
    }

    if (url.pathname === '/api/sync-tratativas' && request.method === 'POST') {
      return Response.json(
        { info: 'sync em lote descontinuado - as pills e detalhes agora buscam ao vivo sob demanda (ver /api/tratativas-pills e /api/tratativas-detalhe), com cache local automatico.' },
        { status: 200 }
      );
    }

    if (url.pathname === '/api/tratativas-status' && request.method === 'GET') {
      try {
        await initTratativaTables(env);
        const countRes = await env.DB.query(`SELECT COUNT(*) as c FROM tratativas`, []);
        const totalSincronizado = countRes.rows && countRes.rows.length ? countRes.rows[0].c : 0;
        const offsetAtual = await getTratativaOffset(env);
        let ultimoResultado: any = null;
        const ultRes = await env.DB.query(`SELECT v FROM tratativa_sync WHERE k = ?`, ['ultimo_resultado']);
        if (ultRes.rows && ultRes.rows.length) {
          try { ultimoResultado = JSON.parse(ultRes.rows[0].v); } catch {}
        }
        return Response.json({ totalSincronizado, offsetAtual, ultimoResultado, modo: 'busca ao vivo sob demanda (com cache local) - restaurada em 17/07/2026' });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/tickets-list' && request.method === 'GET') {
      try {
        const tabela = tabelaOuPadrao(url);
        const termo = url.searchParams.get('termo') || undefined;
        const ini = url.searchParams.get('ini') || undefined;
        const fim = url.searchParams.get('fim') || undefined;
        const statusExato = url.searchParams.get('status') || undefined;
        const heuristicaExtravio = url.searchParams.get('heuristicaExtravio') === '1';
        const rows = await ticketsListComCache(env, tabela, { termo, ini, fim, statusExato, heuristicaExtravio });
        return Response.json(rows);
      } catch (e: any) {
        return Response.json({ __error: String((e && e.message) || e) });
      }
    }

    if (url.pathname === '/api/tickets-update-status' && request.method === 'POST') {
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
        const id = body.id;
        const status = body.status;
        if (!id || !status) return Response.json({ ok: false, error: 'id e status obrigatorios' }, { status: 400 });
        const r = await fetch(`${SB_URL}/rest/v1/${tabela}?id=eq.${encodeURIComponent(String(id))}`, {
          method: 'PATCH',
          headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, 'Content-Type': 'application/json', Prefer: 'return=minimal' },
          body: JSON.stringify({ Status_resposta_tickets: status }),
        });
        return Response.json({ ok: r.ok });
      } catch (e: any) {
        return Response.json({ ok: false, error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/tickets-tem-resposta' && request.method === 'POST') {
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
        const threadId = body.thread_id;
        if (!threadId) return Response.json({ ok: false, error: 'thread_id obrigatorio' }, { status: 400 });
        const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
          method: 'PATCH',
          headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}`, 'Content-Type': 'application/json', Prefer: 'return=minimal' },
          body: JSON.stringify({ tem_resposta: true, updated_at: new Date().toISOString() }),
        });
        return Response.json({ ok: r.ok });
      } catch (e: any) {
        return Response.json({ ok: false, error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/tickets-ultima-mensagem' && request.method === 'GET') {
      try {
        const tabela = tabelaOuPadrao(url);
        const threadId = url.searchParams.get('thread_id');
        if (!threadId) return Response.json({ texto: '' });
        const r = await fetch(
          `${SB_URL}/rest/v1/${tabela}?select=${encodeURIComponent('"Ultima mensagem"')}&thread_id=eq.${encodeURIComponent(threadId)}&limit=1`,
          { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } }
        );
        if (!r.ok) return Response.json({ texto: '' });
        const rows: any[] = await r.json();
        const texto = (rows[0] && (rows[0]['Ultima mensagem'] || rows[0]['Última mensagem'])) || '';
        return Response.json({ texto });
      } catch (e: any) {
        return Response.json({ texto: '' });
      }
    }

    if (url.pathname === '/api/tratativas-pills' && request.method === 'GET') {
      try {
        const pedidosParam = url.searchParams.get('pedidos') || '';
        const pedidos = pedidosParam.split(',').map((s) => s.trim()).filter(Boolean).slice(0, 200);
        if (!pedidos.length) return Response.json({ pills: {} });
        await initTratativaTables(env);
        const placeholders = pedidos.map(() => '?').join(',');
        const localRes = await env.DB.query(`SELECT numero_pedido, tipo FROM tratativas WHERE numero_pedido IN (${placeholders})`, pedidos);
        const localRows: any[] = localRes.rows || [];
        const encontradosLocal = new Set(localRows.map((r: any) => String(r.numero_pedido)));
        const faltando = pedidos.filter((p) => !encontradosLocal.has(String(p)));

        let liveRows: any[] = [];
        let avisoAoVivo: string | null = null;
        if (faltando.length) {
          try {
            liveRows = await buscarTratativasAoVivo(faltando);
            if (liveRows.length) await salvarTratativasLocal(env, liveRows);
          } catch (e: any) {
            avisoAoVivo = `busca ao vivo indisponivel no momento (mostrando so copia local): ${String((e && e.message) || e)}`;
          }
        }

        const todasLinhas = localRows.concat(liveRows);
        const pills: any = {};
        todasLinhas.forEach((row: any) => {
          const key = String(row.numero_pedido || '').trim();
          if (!key) return;
          if (!pills[key]) pills[key] = { reembolso: false, reenvio: false, count: 0 };
          const tipo = String(row.tipo || '').toUpperCase();
          if (tipo === 'REEMBOLSO') pills[key].reembolso = true;
          if (tipo === 'REENVIO') pills[key].reenvio = true;
          pills[key].count++;
        });
        return Response.json({ pills, ...(avisoAoVivo ? { avisoAoVivo } : {}) });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/tratativas-detalhe' && request.method === 'GET') {
      try {
        const pedido = (url.searchParams.get('pedido') || '').trim();
        if (!pedido) return Response.json({ registros: [] });
        await initTratativaTables(env);
        let res = await env.DB.query(
          `SELECT id, numero_pedido, tipo, data_solicitacao, valor_nf_tratado, chave_nf, cod_nf_original FROM tratativas WHERE numero_pedido = ? ORDER BY data_solicitacao DESC`,
          [pedido]
        );
        let registros = res.rows || [];
        let avisoAoVivo: string | null = null;

        if (!registros.length) {
          try {
            const liveRows = await buscarTratativasAoVivo([pedido]);
            if (liveRows.length) {
              await salvarTratativasLocal(env, liveRows);
              registros = liveRows.sort((a: any, b: any) => String(b.data_solicitacao || '').localeCompare(String(a.data_solicitacao || '')));
            }
          } catch (e: any) {
            avisoAoVivo = `busca ao vivo indisponivel no momento (mostrando so copia local): ${String((e && e.message) || e)}`;
          }
        }

        return Response.json({ registros, ...(avisoAoVivo ? { avisoAoVivo } : {}) });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/marcar-ressarcimento' && request.method === 'POST') {
      try {
        const body: any = await request.json().catch(() => ({}));
        const threadIds: string[] = Array.isArray(body.thread_ids) ? body.thread_ids.filter(Boolean).slice(0, 500) : [];
        if (!threadIds.length) return Response.json({ error: 'thread_ids vazio' }, { status: 400 });
        const marcados = await marcarRessarcimento(env, threadIds);
        return Response.json({ marcados });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/reverter-avarias-notificadas' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabela = typeof body.tabela === 'string' && TABELAS_VALIDAS.has(body.tabela) ? body.tabela : 'tickets_gobeaute';
        const threadIds: string[] = Array.isArray(body.thread_ids) ? body.thread_ids.filter(Boolean).slice(0, 500) : [];
        if (!threadIds.length) return Response.json({ error: 'thread_ids vazio' }, { status: 400 });

        const revertidosStatus: string[] = [];
        const falhasStatus: string[] = [];
        for (const threadId of threadIds) {
          const r = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=eq.${encodeURIComponent(threadId)}`, {
            method: 'PATCH',
            headers: {
              apikey: SB_KEY,
              Authorization: `Bearer ${SB_KEY}`,
              'Content-Type': 'application/json',
              Prefer: 'return=representation',
            },
            body: JSON.stringify({ Status_resposta_tickets: null, updated_at: new Date().toISOString() }),
          });
          if (r.ok) {
            try {
              const rows: any[] = await r.json();
              if (Array.isArray(rows) && rows.length > 0) revertidosStatus.push(threadId);
              else falhasStatus.push(threadId);
            } catch { falhasStatus.push(threadId); }
          } else {
            falhasStatus.push(threadId);
          }
        }

        await initAvariaNotificadaTable(env);
        const placeholders = threadIds.map(() => '?').join(',');
        const del = await env.DB.exec(`DELETE FROM avarias_notificadas WHERE thread_id IN (${placeholders})`, threadIds);

        return Response.json({
          tabela,
          revertidosStatus,
          falhasStatus,
          removidosDoDedup: del && del.rowsWritten ? del.rowsWritten : 0,
        });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/recheck-tratativa-cx' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const limit = Math.min(Math.max(parseInt(body.limit, 10) || 80, 1), 200);
        const resultados: any[] = [];
        for (const tabela of Array.from(TABELAS_VALIDAS)) {
          resultados.push(await recheckTratativaCxLote(env, tabela, limit));
        }
        console.log('[recheck-tratativa-cx]', JSON.stringify(resultados));
        return Response.json({ resultados });
      } catch (e: any) {
        console.error('[recheck-tratativa-cx] erro fatal:', String((e && e.message) || e));
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/migrar-extravio-sempre' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const dryRun = body.dryRun !== false;
        const tabelasParam: string[] = Array.isArray(body.tabelas) && body.tabelas.length
          ? body.tabelas.filter((t: any) => TABELAS_VALIDAS.has(t))
          : Array.from(TABELAS_VALIDAS);

        const resultadosPorTabela: any[] = [];
        for (const tabela of tabelasParam) {
          const candidatos: any[] = [];
          let offset = 0;
          const TETO_SCAN = 10000;
          while (offset < TETO_SCAN) {
            const r = await fetch(
              `${SB_URL}/rest/v1/${tabela}?select=thread_id,Status_resposta_tickets&Motivo=ilike.EXTRAVIO*&order=id.asc`,
              {
                headers: {
                  apikey: SB_KEY,
                  Authorization: `Bearer ${SB_KEY}`,
                  'Range-Unit': 'items',
                  Range: `${offset}-${offset + SCAN_WINDOW - 1}`,
                },
              }
            );
            if (!r.ok) {
              const errBody = await r.text().catch(() => '');
              throw new Error(`Supabase HTTP ${r.status} (${tabela}): ${errBody.slice(0, 300)}`);
            }
            const page: any[] = await r.json();
            candidatos.push(...page);
            if (page.length < SCAN_WINDOW) break;
            offset += SCAN_WINDOW;
          }

          const paraCorrigir = candidatos
            .filter((t) => t.Status_resposta_tickets !== 'extravio' && t.Status_resposta_tickets !== 'concluido')
            .map((t) => t.thread_id)
            .filter(Boolean);

          if (dryRun) {
            resultadosPorTabela.push({ tabela, totalClassificadosExtravio: candidatos.length, pendentesParaMarcar: paraCorrigir.length });
            continue;
          }

          let corrigidos = 0;
          for (let i = 0; i < paraCorrigir.length; i += 200) {
            const chunk = paraCorrigir.slice(i, i + 200);
            const idsFiltro = chunk.map((id) => `"${id}"`).join(',');
            const r2 = await fetch(`${SB_URL}/rest/v1/${tabela}?thread_id=in.(${idsFiltro})`, {
              method: 'PATCH',
              headers: {
                apikey: SB_KEY,
                Authorization: `Bearer ${SB_KEY}`,
                'Content-Type': 'application/json',
                Prefer: 'return=representation',
              },
              body: JSON.stringify({ Status_resposta_tickets: 'extravio', updated_at: new Date().toISOString() }),
            });
            if (!r2.ok) {
              const errBody = await r2.text().catch(() => '');
              throw new Error(`Supabase HTTP ${r2.status} (PATCH ${tabela}): ${errBody.slice(0, 300)}`);
            }
            const rows: any[] = await r2.json();
            corrigidos += Array.isArray(rows) ? rows.length : 0;
          }
          resultadosPorTabela.push({ tabela, totalClassificadosExtravio: candidatos.length, corrigidos });
        }

        return Response.json({ dryRun, resultadosPorTabela });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/enviar-avarias-pendentes' && request.method === 'POST') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        const body: any = await request.json().catch(() => ({}));
        const tabela = 'tickets_gobeaute';
        const dryRun = body.dryRun !== false;
        const limit = Math.min(Math.max(parseInt(body.limit, 10) || 50, 1), 300);

        const candidatos: any[] = [];
        let offset = 0;
        const TETO_SCAN = 5000;
        while (offset < TETO_SCAN) {
          const r = await fetch(
            `${SB_URL}/rest/v1/${tabela}?select=thread_id,remetente,message_id,subject,Status_resposta_tickets,Motivo&Motivo=ilike.AVARIA*&order=data_email.asc`,
            {
              headers: {
                apikey: SB_KEY,
                Authorization: `Bearer ${SB_KEY}`,
                'Range-Unit': 'items',
                Range: `${offset}-${offset + SCAN_WINDOW - 1}`,
              },
            }
          );
          if (!r.ok) throw new Error(`Supabase HTTP ${r.status}`);
          const page: any[] = await r.json();
          candidatos.push(...page);
          if (page.length < SCAN_WINDOW) break;
          offset += SCAN_WINDOW;
        }

        const elegveis = candidatos.filter((t) => t.Status_resposta_tickets !== 'concluido' && t.Status_resposta_tickets !== 'extravio');
        const puladosPorStatusTerminal = candidatos.length - elegveis.length;

        await initAvariaNotificadaTable(env);
        const jaNotificados = new Set<string>();
        {
          const res = await env.DB.query(`SELECT thread_id FROM avarias_notificadas`, []);
          (res.rows || []).forEach((r: any) => jaNotificados.add(r.thread_id));
        }
        const pendentes = elegveis.filter((t) => t.thread_id && !jaNotificados.has(t.thread_id));

        if (dryRun) {
          return Response.json({
            dryRun: true,
            tabela,
            totalClassificadosAvaria: candidatos.length,
            puladosPorStatusTerminal,
            jaNotificadosAntes: elegveis.length - pendentes.length,
            pendentesParaEnviar: pendentes.length,
            amostra: pendentes.slice(0, 10).map((t) => ({ thread_id: t.thread_id, subject: t.subject })),
          });
        }

        const lote = pendentes.slice(0, limit);
        let enviados = 0;
        const erros: string[] = [];
        const conta = getConta('gobeaute');
        let accessTokenGobeaute: string | null = null;
        try {
          accessTokenGobeaute = await getGmailAccessToken(env, conta);
        } catch (e: any) {
          return Response.json({ error: `nao foi possivel obter access token do Gmail (conta gobeaute): ${String((e && e.message) || e)}` }, { status: 500 });
        }

        for (const t of lote) {
          try {
            const thread = await gmailGet(accessTokenGobeaute!, `/threads/${encodeURIComponent(t.thread_id)}?format=full`);
            const info = processThread(thread);
            if (!info.ultima_recebida_from) {
              erros.push(`thread_id ${t.thread_id}: sem remetente de mensagem recebida na thread - pulado (nao enviado com destinatario incerto)`);
              continue;
            }
            const antesDoEnvio = jaNotificados.has(t.thread_id);
            await notificarAvariaSeNecessario(env, tabela, t.thread_id, 'AVARIA', info.subject || t.subject || '', info.ultima_recebida_from, info.ultima_mensagem_id || t.message_id || '');
            const check = await env.DB.query(`SELECT thread_id FROM avarias_notificadas WHERE thread_id = ?`, [t.thread_id]);
            if (!antesDoEnvio && check.rows && check.rows.length) enviados++;
            else if (!(check.rows && check.rows.length)) erros.push(`thread_id ${t.thread_id}: nao enviado (falha no webhook - ver logs)`);
          } catch (e: any) {
            erros.push(`thread_id ${t.thread_id}: ${String((e && e.message) || e)}`);
          }
          await sleep(300);
        }

        return Response.json({
          dryRun: false,
          tabela,
          totalClassificadosAvaria: candidatos.length,
          puladosPorStatusTerminal,
          loteTentado: lote.length,
          enviados,
          erros,
          restantes: pendentes.length - lote.length,
        });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/debug-avarias-notificadas' && request.method === 'GET') {
      if (!autorizado(request, env)) return Response.json({ error: 'nao autorizado' }, { status: 401 });
      try {
        await initAvariaNotificadaTable(env);
        const limit = Math.min(Math.max(parseInt(url.searchParams.get('limit') || '50', 10) || 50, 1), 500);
        const res = await env.DB.query(`SELECT thread_id, enviado_em, para_email FROM avarias_notificadas ORDER BY enviado_em DESC LIMIT ?`, [limit]);
        return Response.json({ total: res.rows ? res.rows.length : 0, notificacoes: res.rows || [] });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/ressarcimentos' && request.method === 'GET') {
      try {
        const param = url.searchParams.get('thread_ids') || '';
        const threadIds = param.split(',').map((s) => s.trim()).filter(Boolean).slice(0, 500);
        if (!threadIds.length) return Response.json({ marcados: {} });
        const marcados = await buscarRessarcimentos(env, threadIds);
        return Response.json({ marcados });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/regras-negocio' && request.method === 'GET') {
      try {
        const dados = await getRegrasNegocio(env);
        return Response.json(dados);
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    if (url.pathname === '/api/regras-negocio' && request.method === 'POST') {
      try {
        const body: any = await request.json().catch(() => ({}));
        const conteudo = body.conteudo && typeof body.conteudo === 'object' ? body.conteudo : {};
        const updatedBy = typeof body.updatedBy === 'string' ? body.updatedBy.slice(0, 100) : '';
        await salvarRegrasNegocio(env, conteudo, updatedBy);
        return Response.json({ ok: true });
      } catch (e: any) {
        return Response.json({ error: String((e && e.message) || e) }, { status: 500 });
      }
    }

    return new Response('Not found', { status: 404 });
  },
};
