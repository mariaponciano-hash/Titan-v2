/**
 * titan-watcher-worker (25/08/2026)
 *
 * Port do titan_watcher.py/titan_bi_scraper.py pra rodar como Cloudflare
 * Worker com Browser Rendering, em vez de ficar aberto no PC da Ivna.
 *
 * ESCOPO DESTA PRIMEIRA VERSAO: so o watcher (consulta avulsa, pedido a
 * pedido, dos itens 'pendente' em infos_titan) - equivalente exato do que
 * titan_watcher.py fazia. O backfill em massa (titan_backfill.py, que baixa
 * um .xlsx via "Exportar dados" e le com openpyxl) NAO foi portado aqui: em
 * Workers nao ha sistema de arquivos, e capturar o download de um Power BI
 * via CDP + parsear xlsx em JS puro e um projeto a parte. titan_backfill.py
 * continua rodando no PC da Ivna (Task Scheduler) por enquanto.
 *
 * ARQUITETURA: em vez do loop "while True: sleep(15)" do script Python, este
 * Worker acorda via Cron Trigger (ver wrangler.toml, a cada 5 min por
 * padrao), abre UM Chromium (Browser Rendering), loga UMA vez, processa
 * todos os itens 'pendente' da fila naquele momento, fecha o navegador e
 * termina - o proprio Cron acorda de novo depois.
 *
 * SEGREDOS NECESSARIOS (nunca no codigo - `wrangler secret put <NOME>` ou
 * pelo painel): TITAN_EMAIL, TITAN_SENHA, TITAN_SB_URL, TITAN_SB_KEY.
 */
import puppeteer, { Browser, Frame, Page, ElementHandle } from '@cloudflare/puppeteer';

export interface Env {
  MYBROWSER: Fetcher;
  TITAN_EMAIL: string;
  TITAN_SENHA: string;
  TITAN_SB_URL: string;
  TITAN_SB_KEY: string;
}

const TITAN_URL = 'https://www.titanbi.com.br/';
const DASHBOARD_URL = 'https://www.titanbi.com.br/embed/a1609652-2312-40a7-9cc8-124e25d6fcc3?page=0d62dad7c41ce11d7a3a';
const TABELA = 'infos_titan';
const WORKER_ID = 'titan-watcher-worker'; // = name em wrangler.toml - PK em worker_heartbeats

// RECHECK DE PEDIDOS NAO-FINALIZADOS (25/08/2026, a pedido da Ivna): sem
// isso, um pedido que ja tem status='concluido' no Supabase NUNCA mais era
// revisitado - mesmo que a situacao (ex: "AGUARDANDO_EMBARQUE") ainda nao
// fosse a final, nada o devolvia pra fila. O backfill diario (rolling
// window, ver rodar_titan_backfill_diario.ps1) cobre isso so pros ultimos N
// dias; pedidos mais antigos que isso e ainda presos num status
// intermediario ficavam travados pra sempre. Lista curta e conservadora -
// se aparecer uma situacao final nova que o Titan use e nao esteja aqui,
// ela so vai gerar recheck's a mais (gasto de tempo), nunca dado errado.
const STATUS_FINAIS = ['EMBARCADO', 'CANCELADO'];
// Nao rechecar de novo antes desse intervalo, pra nao gastar toda rodada do
// Cron so revisitando os mesmos pedidos presos - cada recheck bem-sucedido
// atualiza atualizado_em, o que naturalmente empurra o proximo recheck pra
// frente sozinho.
const RECHECK_INTERVALO_HORAS = 2;
const RECHECK_MAX_POR_RODADA = 30;

// RECHECK DE PEDIDOS COM ERRO (26/08/2026): simetrico ao recheck acima, mas
// pra status='erro' - sem isso, um erro transitorio (rede, Titan fora do ar
// por 1 minuto) marcava o pedido como erro pra sempre, sem nova tentativa
// automatica. Teto de tentativas pra nao ficar re-tentando pra sempre uma NF
// permanentemente quebrada (marca errada, NF que nunca vai existir etc.) -
// depois do teto, so um humano reativa via SQL.
const RECHECK_INTERVALO_ERRO_HORAS = 1;
const ERRO_RECHECK_MAX_POR_RODADA = 30;
const ERRO_RECHECK_MAX_TENTATIVAS = 5;

// TETO DE ITENS POR RODADA (27/08/2026): achado em producao - Cloudflare mata
// Cron scheduled events com "exceededWallTime" na marca de 15 min, sempre,
// sem excecao JS nenhuma (o finally do gravarHeartbeat nao roda - a rodada
// inteira desaparece sem deixar rastro). Com um backlog de ~14 mil itens
// (~15-30s cada), uma rodada sem teto nunca termina dentro da janela, nunca
// grava heartbeat, e ainda se sobrepoe com o proximo tick (5 min depois),
// competindo pelos mesmos itens. Processar um lote pequeno e fixo por rodada
// garante: a rodada sempre termina, o heartbeat sempre e gravado, e o
// backlog e consumido de forma constante e visivel ao longo de varios ticks.
// Reduzido de 20 pra 12 (28/08/2026) junto com o aumento do timeout de
// espera do painel do relatorio (30s -> 60s, ver getDashboardFrame) - com
// mais tempo por item, precisa de menos itens por rodada pra continuar
// cabendo dentro dos 15 min antes do exceededWallTime.
const PENDENTES_MAX_POR_RODADA = 12;

// ---------------------------------------------------------------------------
// Supabase (mesma tabela/chave que titan_watcher.py e o server.ts ja usam)
// ---------------------------------------------------------------------------
function titanHeaders(env: Env) {
  return {
    apikey: env.TITAN_SB_KEY,
    Authorization: `Bearer ${env.TITAN_SB_KEY}`,
    'Content-Type': 'application/json',
  };
}

class ErroSupabase extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

// Cloudflare Workers Logs corta a mensagem de um Error passado como
// argumento separado pro console.error (so mostra o stack, nunca
// e.message) - concatenar numa unica string evita perder o motivo real do
// erro (26/08/2026, achado depurando o proprio deploy de hoje).
function mensagemDeErro(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// Deteccao de pagina/navegador corrompido (27/08/2026): quando isso acontece
// no meio do loop de itens, TODO item seguinte falha do mesmo jeito (mesma
// referencia de frame morta) - continuar so desperdica o resto da janela de
// 15 min numa cascata garantida de falhas, e marcar cada um como 'erro'
// penaliza pedidos que nunca tiveram uma chance de verdade (gasta
// erro_recheck_tentativas por um problema que nao e deles). Ver rodarFila:
// ao detectar isso, para o loop sem marcar os itens restantes.
const PADROES_ERRO_CONEXAO = [
  'detached Frame',
  'Navigating frame was detached',
  'Execution context was destroyed',
  'Protocol error',
  'Connection closed',
  'Target closed',
];
function erroDeConexao(e: unknown): boolean {
  const msg = mensagemDeErro(e);
  return PADROES_ERRO_CONEXAO.some((padrao) => msg.includes(padrao));
}

async function verificarResposta(r: Response, contexto: string): Promise<void> {
  if (!r.ok) {
    const corpo = await r.text().catch(() => '');
    throw new ErroSupabase(`${contexto}: Supabase HTTP ${r.status} ${corpo.slice(0, 300)}`, r.status);
  }
}

// Retry com backoff pras chamadas ao Supabase (26/08/2026): antes, erro de
// rede/API numa leitura ficava indistinguivel de "sem trabalho" (silenciado
// com `return []`), e numa escrita era simplesmente engolido sem log. Agora
// tudo propaga como excecao depois de esgotar as tentativas, e tem onde
// pousar: heartbeat 'error' em scheduled() (ver executarRodadaComHeartbeat).
// So reexecuta em erro de rede, 5xx ou 429 - nunca em 4xx, que nao se
// resolve tentando de novo (ex: RLS negando, filtro invalido).
const SB_MAX_RETRIES = 2; // 3 tentativas totais
const SB_RETRY_BASE_MS = 500;

async function comRetry<T>(fn: () => Promise<T>): Promise<T> {
  let ultimoErro: any;
  for (let tentativa = 0; tentativa <= SB_MAX_RETRIES; tentativa++) {
    try {
      return await fn();
    } catch (e: any) {
      ultimoErro = e;
      if (e instanceof ErroSupabase && typeof e.status === 'number' && e.status < 500 && e.status !== 429) {
        throw e;
      }
    }
    if (tentativa < SB_MAX_RETRIES) {
      await new Promise((res) => setTimeout(res, SB_RETRY_BASE_MS * Math.pow(2, tentativa)));
    }
  }
  throw ultimoErro;
}

async function buscarPendentes(env: Env): Promise<any[]> {
  return comRetry(async (): Promise<any[]> => {
    const r = await fetch(
      `${env.TITAN_SB_URL}/rest/v1/${TABELA}?status=eq.pendente&select=numero_pedido,numero_nf,marca` +
        `&order=solicitado_em.asc&limit=${PENDENTES_MAX_POR_RODADA}`,
      { headers: titanHeaders(env) }
    );
    await verificarResposta(r, 'buscarPendentes');
    return (await r.json<any[]>().catch(() => [])) || [];
  });
}

async function buscarNaoFinalizadosParaRecheck(env: Env): Promise<any[]> {
  const cutoff = new Date(Date.now() - RECHECK_INTERVALO_HORAS * 3600 * 1000).toISOString();
  const listaFinais = STATUS_FINAIS.join(',');
  // "or" cobre situacao NULL tambem - "not.in" sozinho nunca bate NULL
  // (semantica de NULL do Postgres), o que deixaria pedidos sem situacao
  // nenhuma ainda escapando do recheck.
  const url =
    `${env.TITAN_SB_URL}/rest/v1/${TABELA}?status=eq.concluido` +
    `&atualizado_em=lt.${encodeURIComponent(cutoff)}` +
    `&or=(situacao.is.null,situacao.not.in.(${listaFinais}))` +
    `&select=numero_nf,marca&limit=${RECHECK_MAX_POR_RODADA}`;
  return comRetry(async (): Promise<any[]> => {
    const r = await fetch(url, { headers: titanHeaders(env) });
    await verificarResposta(r, 'buscarNaoFinalizadosParaRecheck');
    return (await r.json<any[]>().catch(() => [])) || [];
  });
}

async function buscarErrosParaRecheck(
  env: Env
): Promise<{ numero_nf: string; marca: string; erro_recheck_tentativas: number }[]> {
  const cutoff = new Date(Date.now() - RECHECK_INTERVALO_ERRO_HORAS * 3600 * 1000).toISOString();
  const url =
    `${env.TITAN_SB_URL}/rest/v1/${TABELA}?status=eq.erro` +
    `&atualizado_em=lt.${encodeURIComponent(cutoff)}` +
    `&erro_recheck_tentativas=lt.${ERRO_RECHECK_MAX_TENTATIVAS}` +
    `&select=numero_nf,marca,erro_recheck_tentativas&limit=${ERRO_RECHECK_MAX_POR_RODADA}`;
  return comRetry(async (): Promise<{ numero_nf: string; marca: string; erro_recheck_tentativas: number }[]> => {
    const r = await fetch(url, { headers: titanHeaders(env) });
    await verificarResposta(r, 'buscarErrosParaRecheck');
    return (await r.json<{ numero_nf: string; marca: string; erro_recheck_tentativas: number }[]>().catch(() => [])) || [];
  });
}

async function devolverParaFila(
  env: Env,
  itens: { numero_nf: string; marca: string; erro_recheck_tentativas?: number }[]
): Promise<{ ok: number; falhas: number }> {
  let ok = 0;
  let falhas = 0;
  for (const item of itens) {
    const body: Record<string, any> = { status: 'pendente' };
    if (typeof item.erro_recheck_tentativas === 'number') {
      body.erro_recheck_tentativas = item.erro_recheck_tentativas + 1;
    }
    try {
      await comRetry(async () => {
        const r = await fetch(
          `${env.TITAN_SB_URL}/rest/v1/${TABELA}?numero_nf=eq.${encodeURIComponent(item.numero_nf)}&marca=eq.${encodeURIComponent(item.marca)}`,
          {
            method: 'PATCH',
            headers: { ...titanHeaders(env), Prefer: 'return=minimal' },
            body: JSON.stringify(body),
          }
        );
        await verificarResposta(r, 'devolverParaFila');
      });
      ok++;
    } catch (e) {
      falhas++;
      console.error(`devolverParaFila: falha ao devolver NF ${item.numero_nf}/${item.marca}: ${mensagemDeErro(e)}`);
    }
  }
  return { ok, falhas };
}

async function marcarErro(env: Env, numeroNf: string, marca: string, mensagem: string) {
  try {
    await comRetry(async () => {
      const r = await fetch(`${env.TITAN_SB_URL}/rest/v1/${TABELA}?numero_nf=eq.${encodeURIComponent(numeroNf)}&marca=eq.${encodeURIComponent(marca)}`, {
        method: 'PATCH',
        headers: { ...titanHeaders(env), Prefer: 'return=minimal' },
        body: JSON.stringify({
          status: 'erro',
          erro: String(mensagem).slice(0, 500),
          atualizado_em: new Date().toISOString(),
        }),
      });
      await verificarResposta(r, 'marcarErro');
    });
  } catch (e) {
    console.error(`nao consegui nem marcar erro no Supabase: ${mensagemDeErro(e)}`);
  }
}

async function marcarConcluido(env: Env, numeroNf: string, marca: string, pedidoData: Record<string, any>, eventos: any[], itens: any[]) {
  await comRetry(async () => {
    const r = await fetch(`${env.TITAN_SB_URL}/rest/v1/${TABELA}?numero_nf=eq.${encodeURIComponent(numeroNf)}&marca=eq.${encodeURIComponent(marca)}`, {
      method: 'PATCH',
      headers: { ...titanHeaders(env), Prefer: 'return=minimal' },
      body: JSON.stringify({
        status: 'concluido',
        erro: null,
        erro_recheck_tentativas: 0,
        situacao: pedidoData['Situação'] ?? null,
        romaneio: pedidoData['Romaneio'] || null,
        valor_pedido: pedidoData['Valor Pedido'] ?? null,
        volume: pedidoData['Volume'] ?? null,
        observacao: pedidoData['Observação'] ?? null,
        nome_projeto: pedidoData['Nome Projeto'] ?? null,
        nome_projeto_antigo: pedidoData['Nome Projeto (Antigo)'] ?? null,
        data_importado: pedidoData['Data Importado'] ?? null,
        data_expedido: pedidoData['Data Expedido'] ?? null,
        data_conferido: pedidoData['Data Conferido'] ?? null,
        depositante: pedidoData['Depositante'] ?? null,
        cliente: pedidoData['Cliente'] ?? null,
        eventos: eventos || [],
        itens: itens || [],
        atualizado_em: new Date().toISOString(),
      }),
    });
    await verificarResposta(r, 'marcarConcluido');
  });
}

async function gravarHeartbeat(
  env: Env,
  status: 'ok' | 'error',
  detail: string,
  itensProcessados: number,
  itensComErro: number
): Promise<void> {
  try {
    await comRetry(async () => {
      const r = await fetch(`${env.TITAN_SB_URL}/rest/v1/worker_heartbeats`, {
        method: 'POST',
        headers: { ...titanHeaders(env), Prefer: 'resolution=merge-duplicates,return=minimal' },
        body: JSON.stringify({
          worker: WORKER_ID,
          last_run_at: new Date().toISOString(),
          status,
          detail: String(detail || '').slice(0, 2000),
          itens_processados: itensProcessados,
          itens_com_erro: itensComErro,
          // last_alert_at NAO vai aqui - e de uso exclusivo do watchdog
          // (worker_heartbeats_watchdog.sql). Omitir preserva o valor
          // existente no upsert (Prefer: resolution=merge-duplicates so
          // atualiza as colunas presentes no JSON).
        }),
      });
      await verificarResposta(r, 'gravarHeartbeat');
    });
  } catch (e) {
    console.error(`nao consegui gravar heartbeat no Supabase: ${mensagemDeErro(e)}`);
  }
}

// ---------------------------------------------------------------------------
// Helpers de scraping - porta de titan_bi_scraper.py pra Puppeteer
// ---------------------------------------------------------------------------

// Equivalente a elemento_visivel(): o Titan e um embed do Power BI, que cria
// um <p aria-hidden="true"> com o MESMO texto do titulo de cada visual (pra
// servir de tooltip) - qualquer busca por texto acha PELO MENOS 2 elementos,
// um deles escondido. Pega o primeiro REALMENTE visivel, nao so o primeiro
// do DOM (ver comentario identico e mais detalhado no titan_bi_scraper.py).
async function elementoVisivel(candidatos: ElementHandle[], timeoutMs = 30000): Promise<ElementHandle> {
  const limite = Date.now() + timeoutMs;
  while (Date.now() < limite) {
    for (const el of candidatos) {
      try {
        const visivel = await el.evaluate((node) => {
          const r = (node as Element).getBoundingClientRect();
          const s = getComputedStyle(node as Element);
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
        });
        if (visivel) return el;
      } catch {
        continue;
      }
    }
    await new Promise((res) => setTimeout(res, 300));
  }
  throw new Error(`Nenhum elemento visivel encontrado dentro de ${timeoutMs}ms`);
}

async function todosPorTexto(frame: Page | Frame, texto: string): Promise<ElementHandle[]> {
  return frame.$$(`::-p-text(${texto})`);
}

async function todosPorAria(frame: Page | Frame, nome: string, role?: string): Promise<ElementHandle[]> {
  const sel = role ? `aria/${nome}[role="${role}"]` : `aria/${nome}`;
  return frame.$$(sel);
}

async function login(page: Page, email: string, senha: string) {
  await page.goto(TITAN_URL, { waitUntil: 'networkidle0' });

  const campoEmail =
    (await page.$('input[type="email"]')) ||
    (await elementoVisivel(await todosPorAria(page, 'E-mail')).catch(() => null)) ||
    (await page.$('input[type="text"], input:not([type])'));
  if (!campoEmail) throw new Error('Campo de e-mail nao encontrado na tela de login do Titan.');
  await campoEmail.click({ clickCount: 3 });
  await campoEmail.type(email, { delay: 40 });

  const campoSenha = await page.$('input[type="password"]');
  if (!campoSenha) throw new Error('Campo de senha nao encontrado na tela de login do Titan.');
  await campoSenha.click({ clickCount: 3 });
  await campoSenha.type(senha, { delay: 40 });

  // type="submit" e preciso e sem ambiguidade (a tela tambem tem um botao
  // "Entrar com Microsoft", que nao e submit - so dispara um redirect OAuth).
  // ARIA por nome/role fica como fallback: o texto do botao real tem um
  // espaco em branco antes de "Entrar" no DOM, o que pode quebrar uma
  // comparacao exata de nome acessivel dependendo da versao do puppeteer.
  const botaoEntrar =
    (await page.$('button[type="submit"]')) ||
    (await elementoVisivel(await todosPorAria(page, 'Entrar', 'button')).catch(() => null));
  if (!botaoEntrar) throw new Error('Botao "Entrar" nao encontrado na tela de login do Titan.');
  await botaoEntrar.click();

  // "Autenticando no TitanBI..." fica visivel um tempo depois do clique -
  // esperar sumir antes de checar se deu certo (mesmo motivo do Python:
  // um check de rede parada sozinho dispara CEDO DEMAIS).
  await page
    .waitForFunction(() => !document.body.innerText.includes('Autenticando'), { timeout: 20000 })
    .catch(() => {});
  await page.waitForNetworkIdle({ timeout: 20000 }).catch(() => {});

  const aindaNoLogin = await page.$('input[type="password"]');
  if (aindaNoLogin) {
    const visivel = await aindaNoLogin.evaluate((n) => (n as HTMLElement).offsetParent !== null);
    if (visivel) {
      throw new Error(
        'Ainda na tela de login depois de tentar entrar - confira TITAN_EMAIL/TITAN_SENHA, ' +
          'ou pode ter aparecido um CAPTCHA/MFA que este script nao trata.'
      );
    }
  }

  await page.waitForFunction(() => location.href.includes('/home'), { timeout: 15000 }).catch(() => {});
  await page.waitForNetworkIdle({ timeout: 15000 }).catch(() => {});
}

// Diagnostico de texto (28/08/2026): a captura em base64 (imagem) exigia
// colar uma string enorme no chat pra eu decodificar, e a reconstrucao
// manual dos pedacos vinha corrompida (relay perde/altera caracteres em
// algum ponto) - impossivel decodificar de forma confiavel. Texto puro (URL
// atual, titulo, inicio do texto visivel da pagina) resolve o mesmo
// problema - dizer o que a pagina mostra de verdade no momento do timeout
// (bloqueio, captcha, tela em branco etc) - sem nenhum risco de corrupcao
// no caminho, e cabe direto numa linha de log. So uma vez por rodada.
//
// consoleErros/requisicoesFalhas (28/08/2026): dobrar o timeout pra 60s nao
// mudou nada (mesma falha 100% das vezes) - descarta "so precisa de mais
// tempo". O relatorio do Power BI carrega a casca da pagina mas nunca
// termina de renderizar o painel de verdade - hipotese agora e falha
// silenciosa (JS quebrando ou chamada de API do proprio Power BI sendo
// bloqueada) que nao gera nenhuma excecao pro nosso codigo. Escutar
// console/pageerror/response desde a criacao da pagina (nao so no momento
// do timeout) e a unica forma de pegar isso.
export interface Diagnostico {
  screenshotTirado: boolean;
  consoleErros: string[];
  requisicoesFalhas: string[];
}

function novoDiagnostico(): Diagnostico {
  return { screenshotTirado: false, consoleErros: [], requisicoesFalhas: [] };
}

const DIAGNOSTICO_MAX_ITENS = 15;
function registrarDiagnostico(lista: string[], item: string) {
  lista.push(item.slice(0, 200));
  if (lista.length > DIAGNOSTICO_MAX_ITENS) lista.shift();
}

// Anexa os listeners uma unica vez por pagina (25/08/2026 - o browser.newPage()
// de rodarFila e reusado por todos os itens da rodada, entao isso cobre a
// rodada inteira, nao so o item que disparar o diagnostico).
function monitorarPagina(page: Page, diagnostico: Diagnostico) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') registrarDiagnostico(diagnostico.consoleErros, `console.error: ${msg.text()}`);
  });
  page.on('pageerror', (err) => {
    registrarDiagnostico(diagnostico.consoleErros, `pageerror: ${String(err)}`);
  });
  page.on('requestfailed', (req) => {
    registrarDiagnostico(diagnostico.requisicoesFalhas, `FALHA ${req.method()} ${req.url()} -> ${req.failure()?.errorText || '?'}`);
  });
  page.on('response', (res) => {
    if (res.status() >= 400) registrarDiagnostico(diagnostico.requisicoesFalhas, `HTTP ${res.status()} ${res.url()}`);
  });
}

async function tirarDiagnosticoDePagina(page: Page, diagnostico: Diagnostico) {
  if (diagnostico.screenshotTirado) return;
  diagnostico.screenshotTirado = true;
  try {
    const info = await page.evaluate(() => ({
      url: location.href,
      titulo: document.title,
      texto: (document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 800),
    }));
    console.log(`DIAGNOSTICO_TIMEOUT_DASHBOARD url=${info.url} titulo="${info.titulo}" texto="${info.texto}"`);
    console.log(
      `DIAGNOSTICO_TIMEOUT_DASHBOARD_CONSOLE (${diagnostico.consoleErros.length}): ${diagnostico.consoleErros.join(' | ') || '(nenhum)'}`
    );
    console.log(
      `DIAGNOSTICO_TIMEOUT_DASHBOARD_REDE (${diagnostico.requisicoesFalhas.length}): ${diagnostico.requisicoesFalhas.join(' | ') || '(nenhuma)'}`
    );
  } catch (diagErr) {
    console.error(`nao consegui pegar diagnostico da pagina: ${mensagemDeErro(diagErr)}`);
  }
}

async function getDashboardFrame(page: Page, diagnostico: Diagnostico): Promise<Frame> {
  for (let tentativa = 0; tentativa < 3; tentativa++) {
    try {
      // 'domcontentloaded' em vez de 'networkidle0' (27/08/2026): o Power BI
      // faz polling/telemetria continuo em segundo plano, entao a rede nunca
      // fica realmente parada - 'networkidle0' estava batendo timeout de 30s
      // em TODO item, sempre. A checagem real de "carregou de verdade" ja
      // acontece embaixo (espera o painel "Informacao Pedido" ficar visivel).
      await page.goto(DASHBOARD_URL, { waitUntil: 'domcontentloaded' });
      break;
    } catch (e: any) {
      if (String(e).includes('interrupted') && tentativa < 2) {
        await new Promise((res) => setTimeout(res, 1500));
        continue;
      }
      await tirarDiagnosticoDePagina(page, diagnostico);
      throw e;
    }
  }
  try {
    const frameEl = await page.waitForSelector('iframe', { timeout: 30000 });
    const frame = await frameEl!.contentFrame();
    if (!frame) throw new Error('iframe do dashboard nao expos um Frame acessivel.');
    // 30s -> 60s (28/08/2026): diagnostico mostrou a pagina certa carregando
    // rapido (titulo correto), so o relatorio do Power BI em si (dentro do
    // iframe) nunca aparecia a tempo - hipotese de que so precisa de mais
    // tempo pra renderizar nesse ambiente. Ver PENDENTES_MAX_POR_RODADA
    // (reduzido junto) pra manter a rodada dentro do limite de 15 min.
    await elementoVisivel(await todosPorTexto(frame, 'Informação Pedido'), 60000);
    return frame;
  } catch (e) {
    // Mesmo diagnostico da navegacao (acima): a falha aqui (esperando o
    // iframe ou o painel "Informacao Pedido" aparecer) e a que mais aparece
    // em producao desde que domcontentloaded resolveu a falha de navegacao
    // em si (27/08/2026) - o screenshot aqui mostra o que a pagina exibe DEPOIS
    // do DOM carregado, mas antes do relatorio do Power BI terminar de renderizar.
    await tirarDiagnosticoDePagina(page, diagnostico);
    throw e;
  }
}

async function abrirDropdownEDigitar(frame: Frame, rotulo: string, valor: string, tentativas = 3) {
  let ultimoErro: any = null;
  for (let t = 0; t < tentativas; t++) {
    const titulo = await elementoVisivel(await todosPorTexto(frame, rotulo));
    const caixa = await titulo.boundingBox();
    if (!caixa) throw new Error(`Rotulo '${rotulo}' visivel mas sem bounding box.`);
    await frame.page().mouse.click(caixa.x + caixa.width / 2, caixa.y + caixa.height + 15);
    await new Promise((res) => setTimeout(res, 800 + t * 500));
    try {
      const campo = await elementoVisivel(await frame.$$('[placeholder*="Pesquisar"]'), 10000);
      await campo.click({ clickCount: 3 });
      await frame.page().keyboard.press('Delete');
      await campo.type(String(valor), { delay: 60 });
      return campo;
    } catch (e) {
      ultimoErro = e;
      await frame.page().keyboard.press('Escape');
      await new Promise((res) => setTimeout(res, 500));
    }
  }
  throw ultimoErro;
}

async function marcarItemDaLista(frame: Frame, campo: ElementHandle, valor: string) {
  await frame.page().keyboard.press('Enter');
  await new Promise((res) => setTimeout(res, 1000));
  const opcao = await elementoVisivel(await todosPorAria(frame, String(valor)));
  await opcao.click();
}

async function filtrar(frame: Frame, nf?: string, numeroPedido?: string) {
  if (nf) {
    const campo = await abrirDropdownEDigitar(frame, 'Nota Fiscal de Saída', nf);
    await new Promise((res) => setTimeout(res, 1000));
    await marcarItemDaLista(frame, campo, nf);
    await new Promise((res) => setTimeout(res, 1000));
    await frame.page().keyboard.press('Escape');
  }
  if (numeroPedido) {
    const campo = await abrirDropdownEDigitar(frame, 'Número do pedido', numeroPedido);
    await new Promise((res) => setTimeout(res, 1000));
    await marcarItemDaLista(frame, campo, numeroPedido);
    await new Promise((res) => setTimeout(res, 1000));
    await frame.page().keyboard.press('Escape');
  }
  await new Promise((res) => setTimeout(res, 1000));
}

async function localizarPainel(frame: Frame, titulo: string): Promise<ElementHandle> {
  const painel = await frame.$(`[role="group"][aria-label^="${titulo}"]`);
  if (!painel) throw new Error(`Painel '${titulo}' nao encontrado.`);
  return painel;
}

async function celulasDaLinha(linha: ElementHandle): Promise<string[]> {
  const celulas = await linha.$$(
    "xpath/.//*[self::td or @role='cell' or @role='gridcell' or @role='columnheader']"
  );
  if (celulas.length) {
    const valores = [];
    for (const c of celulas) valores.push((await c.evaluate((n) => (n as HTMLElement).innerText)).trim());
    return valores;
  }
  const bruto = await linha.evaluate((n) => (n as HTMLElement).innerText);
  return bruto
    .replace(/\t/g, '\n')
    .split('\n')
    .map((p) => p.trim())
    .filter(Boolean);
}

async function extrairRegistrosDoPainel(painel: ElementHandle, marcadoresCabecalho: string[]): Promise<Record<string, string>[]> {
  const linhas = await painel.$$("xpath/.//*[self::tr or @role='row']");
  if (!linhas.length) return [];

  let cabecalho: ElementHandle | null = null;
  let textoCabecalho = '';
  for (const linha of linhas) {
    const texto = (await linha.evaluate((n) => (n as HTMLElement).innerText)).trim();
    if (texto && marcadoresCabecalho.every((m) => texto.includes(m))) {
      cabecalho = linha;
      textoCabecalho = texto;
      break;
    }
  }
  if (!cabecalho) return [];

  const rotulos = await celulasDaLinha(cabecalho);
  if (!rotulos.length) return [];

  const registros: Record<string, string>[] = [];
  for (const linha of linhas) {
    const texto = (await linha.evaluate((n) => (n as HTMLElement).innerText)).trim();
    if (!texto || texto === textoCabecalho) continue;
    const valores = await celulasDaLinha(linha);
    const n = Math.min(rotulos.length, valores.length);
    const registro: Record<string, string> = {};
    for (let i = 0; i < n; i++) registro[rotulos[i]] = valores[i];
    registros.push(registro);
  }
  return registros;
}

function bateMarca(registro: Record<string, string>, marcaEsperada?: string): boolean {
  if (!marcaEsperada) return true;
  const projeto = String(registro['Nome Projeto'] || '').trim().toUpperCase();
  return projeto === marcaEsperada.trim().toUpperCase();
}

async function acharLinhaPedido(
  frame: Frame,
  numeroPedido: string,
  marcaEsperada?: string
): Promise<{ registro: Record<string, string>; linha: ElementHandle } | null> {
  const painel = await localizarPainel(frame, 'Informação Pedido');
  const linhas = await painel.$$("xpath/.//*[self::tr or @role='row']");
  if (!linhas.length) return null;

  let cabecalho: ElementHandle | null = null;
  let textoCabecalho = '';
  for (const linha of linhas) {
    const texto = (await linha.evaluate((n) => (n as HTMLElement).innerText)).trim();
    if (texto && texto.includes('Romaneio') && texto.includes('Número do Pedido')) {
      cabecalho = linha;
      textoCabecalho = texto;
      break;
    }
  }
  if (!cabecalho) return null;
  const rotulos = await celulasDaLinha(cabecalho);
  if (!rotulos.length) return null;

  const candidatos: { registro: Record<string, string>; linha: ElementHandle }[] = [];
  for (const linha of linhas) {
    const texto = (await linha.evaluate((n) => (n as HTMLElement).innerText)).trim();
    if (!texto || texto === textoCabecalho || !texto.includes(numeroPedido)) continue;
    const valores = await celulasDaLinha(linha);
    const n = Math.min(rotulos.length, valores.length);
    const registro: Record<string, string> = {};
    for (let i = 0; i < n; i++) registro[rotulos[i]] = valores[i];
    candidatos.push({ registro, linha });
  }
  if (!candidatos.length) return null;

  if (marcaEsperada) {
    const batem = candidatos.filter((c) => bateMarca(c.registro, marcaEsperada));
    // So aceita a linha da marca certa - NF repetida entre marcas e um risco
    // real e ja confirmado (ver comentario grande em titan_bi_scraper.py).
    return batem[0] || null;
  }
  return candidatos[0];
}

async function extrairEventos(frame: Frame): Promise<Record<string, string>[]> {
  const painel = await localizarPainel(frame, 'Eventos');
  return extrairRegistrosDoPainel(painel, ['Situação']);
}

async function extrairItensPedido(frame: Frame): Promise<Record<string, string>[]> {
  const painel = await localizarPainel(frame, 'Itens do pedido');
  return extrairRegistrosDoPainel(painel, ['Código', 'Ean']);
}

// ---------------------------------------------------------------------------
// Fluxo por pedido (equivalente a processar_pedido em titan_watcher.py)
// ---------------------------------------------------------------------------
async function processarPedido(frame: Frame, item: { numero_nf: string; marca: string; numero_pedido?: string }) {
  const { numero_nf: numeroNf, marca } = item;
  await filtrar(frame, numeroNf);

  const achado = await acharLinhaPedido(frame, numeroNf, marca);
  if (!achado) {
    throw new Error(`NF ${numeroNf} nao encontrada no Titan BI para a marca ${marca}.`);
  }
  await achado.linha.click();
  await new Promise((res) => setTimeout(res, 1000));

  const eventos = await extrairEventos(frame);
  const itens = await extrairItensPedido(frame);
  return { pedidoData: achado.registro, eventos, itens };
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------
async function rodarFila(env: Env): Promise<{ log: string; processados: number; comErro: number }> {
  const linhasResumo: string[] = [];

  const naoFinalizados = await buscarNaoFinalizadosParaRecheck(env);
  if (naoFinalizados.length) {
    const { ok, falhas } = await devolverParaFila(env, naoFinalizados);
    linhasResumo.push(
      `${ok} pedido(s) nao-finalizado(s) devolvido(s) pra fila pra recheck${falhas ? ` (${falhas} falha(s))` : ''}.`
    );
  }

  const errosParaRecheck = await buscarErrosParaRecheck(env);
  if (errosParaRecheck.length) {
    const { ok, falhas } = await devolverParaFila(env, errosParaRecheck);
    linhasResumo.push(
      `${ok} pedido(s) com erro devolvido(s) pra nova tentativa${falhas ? ` (${falhas} falha(s))` : ''}.`
    );
  }

  const pendentes = await buscarPendentes(env);
  if (!pendentes.length) {
    return { log: [...linhasResumo, 'Nenhum pendente.'].join('\n'), processados: 0, comErro: 0 };
  }

  const browser: Browser = await puppeteer.launch(env.MYBROWSER);
  const page = await browser.newPage();
  const linhas: string[] = [];
  let comErro = 0;
  const diagnostico = novoDiagnostico();
  monitorarPagina(page, diagnostico);
  try {
    await login(page, env.TITAN_EMAIL, env.TITAN_SENHA);

    for (const item of pendentes) {
      const numeroNf = String(item.numero_nf || '').trim();
      const marca = String(item.marca || '').trim();
      if (!numeroNf || !marca) continue;
      try {
        // Recarrega o dashboard do zero a cada pedido - reseta os filtros
        // (mesma cautela do titan_watcher.py: o slicer do Power BI pode
        // acumular selecao entre consultas em vez de substituir).
        const frame = await getDashboardFrame(page, diagnostico);
        const { pedidoData, eventos, itens } = await processarPedido(frame, { numero_nf: numeroNf, marca });
        await marcarConcluido(env, numeroNf, marca, pedidoData, eventos, itens);
        linhas.push(`[NF ${numeroNf}/${marca}] ok - Romaneio: ${pedidoData['Romaneio'] || '(nao veio)'}`);
      } catch (e: any) {
        if (erroDeConexao(e)) {
          // Nao marca 'erro' (o item nao teve uma chance de verdade, e o
          // proximo tick pega ele de novo com um browser/pagina novos) e para
          // aqui - continuar so bateria na mesma parede pra cada item restante.
          linhas.push(
            `[NF ${numeroNf}/${marca}] pagina/navegador corrompido (${mensagemDeErro(e)}) - parando a rodada, restante fica pendente pro proximo tick.`
          );
          break;
        }
        comErro++;
        await marcarErro(env, numeroNf, marca, mensagemDeErro(e));
        linhas.push(`[NF ${numeroNf}/${marca}] erro: ${mensagemDeErro(e)}`);
      }
    }
  } finally {
    // Sem try/catch aqui, uma falha ao FECHAR uma conexao ja morta (ex: o
    // navegador caiu no meio do processamento de um item, que ja foi
    // corretamente marcado 'erro' individualmente) escapava sem protecao e
    // mascarava o resultado real da rodada com um erro generico de fechamento
    // (ver "Attempted to use detached Frame" chegando no topo em vez de ficar
    // contido no catch por item, 27/08/2026).
    await browser.close().catch((e) => console.error(`falha ao fechar o browser (ignorada): ${mensagemDeErro(e)}`));
  }
  return { log: [...linhasResumo, ...linhas].join('\n'), processados: pendentes.length, comErro };
}

// Sempre grava um heartbeat no fim da rodada, sucesso ou falha (26/08/2026):
// antes, se login() (ou qualquer coisa antes do loop de itens) falhasse, a
// excecao subia direto pro catch() do scheduled() - nenhum registro no
// Supabase era tocado, so um console.error que ninguem via. Agora isso vira
// status='error' em worker_heartbeats, que o watchdog externo monitora (ver
// worker_heartbeats_watchdog.sql) mesmo se o Worker travar antes de processar
// qualquer item.
async function executarRodadaComHeartbeat(env: Env): Promise<void> {
  let status: 'ok' | 'error' = 'ok';
  let detail = '';
  let processados = 0;
  let comErro = 0;
  try {
    const resultado = await rodarFila(env);
    detail = resultado.log;
    processados = resultado.processados;
    comErro = resultado.comErro;
    console.log(detail);
  } catch (e: any) {
    status = 'error';
    detail = mensagemDeErro(e).slice(0, 2000);
    console.error(`rodarFila falhou: ${detail}`);
  } finally {
    await gravarHeartbeat(env, status, detail, processados, comErro);
  }
}

export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(executarRodadaComHeartbeat(env));
  },

  // GET manual pra testar sem esperar o Cron (ex: durante o deploy inicial) -
  // roda a MESMA fila. Sem autenticacao propria: o Worker inteiro deveria
  // ficar com acesso restrito (ver instrucoes de deploy) ja que qualquer um
  // com a URL poderia disparar isso.
  async fetch(_req: Request, env: Env) {
    const resultado = await rodarFila(env);
    return new Response(resultado.log || 'ok', { status: 200 });
  },
};
