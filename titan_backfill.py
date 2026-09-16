"""
titan_backfill.py

Roda UMA VEZ (ou de tempos em tempos) pra carregar em MASSA a tabela
infos_titan a partir de um periodo de datas no Titan BI, em vez de esperar
cada pedido ser consultado avulso pelo titan_watcher.py. Depois disso, a
maioria das buscas na Torre ja acha o resultado pronto no Supabase - o
titan_watcher.py continua rodando so pra cobrir os pedidos NOVOS que ainda
nao entraram em nenhum backfill (ver conversa de 24/08/2026 com a Ivna).

QUEM RODA ISSO: voce, na sua maquina - nunca eu.

USO
    set TITAN_EMAIL=sacgobeauty@unilog.com
    set TITAN_SENHA=sua-senha-aqui
    python titan_backfill.py --data-inicial 01/06/2026 --data-final 24/08/2026

VALIDADO COM TESTE REAL (24/08/2026, periodo 01/06-23/08/2026 - 116 pedidos
encontrados, 98 gravados, 18 ignorados por falta de NF/marca): duas
descobertas corrigiram um "0 pedido(s) encontrados" que parecia bug de
seletor mas nao era:
1. O clique-e-digita do periodo ACERTAVA os campos desde o inicio - o
   problema real era ler a tabela CEDO DEMAIS, antes da query terminar
   (corrigido esperando a 1a linha de verdade aparecer, nao um sleep fixo).
2. O popup do calendario e um overlay do Angular Material (CDK) - um Escape
   sozinho nem sempre fecha ele, deixando um <div class="cdk-overlay-backdrop">
   transparente que intercepta todo clique/hover seguinte. Precisa clicar no
   proprio backdrop (ver fechar_popup_calendario) e confirmar que sumiu.

EVENTOS/ITENS - EXPORT + WORKERS EM FATIA FIXA, UM POR VEZ (13-14/09/2026):
a exportacao em massa de "Informacao Pedido" (Romaneio, Situacao, datas
etc.) continua um passo UNICO e rapido (ver exportar_e_gravar_periodo).
Completar Eventos/Itens (clicar pedido por pedido, ~11s cada - ver
_ler_eventos_itens_via_filtro_cruzado) NAO roda dentro deste mesmo passo -
exportar_e_gravar_periodo levanta a lista de pendentes DESTA janela
(eventos OU itens nulos - ver _buscar_pendentes_eventos_itens) e grava num
arquivo (ARQUIVO_PENDENTES_EVENTOS_ITENS); esse arquivo viaja via artifact
pro job completar-eventos, que roda --completar-eventos-worker em N
"instancias" (ver strategy.matrix no workflow, hoje N=20) - cada uma
processando so a sua FATIA fixa da lista por posicao (ver
_fatia_do_worker), sem round-trip nenhum no banco pra "pegar o proximo
lote" e sem risco de duas instancias pegarem o mesmo pedido (fatias
disjuntas por construcao).

Apesar do nome "workers", elas rodam em SEQUENCIA (strategy.max-parallel:
1), nao em paralelo - 3a versao deste desenho, depois de duas tentativas
de paralelismo de verdade que esbarraram no mesmo tipo de problema:
1. (13/09/2026, pedido direto da Maria: "preciso que todos os pedidos
   sejam processados em uma unica rodada") 20 instancias rodando ao mesmo
   tempo, cada uma logada separadamente - 20 logins simultaneos com a
   MESMA conta derrubavam a maioria com "Ainda na tela de login" (run
   #45). Corrigido com um lock de login (so o login esperava a vez, o
   processamento continuava paralelo).
2. Mesmo com o lock, a run #50 manual mostrou 17 de 20 workers morrendo
   logo apos o login, redirecionados sozinhos pra "/login?reason=noUser" -
   o Titan parece derrubar a sessao ANTERIOR assim que uma NOVA sessao
   loga com a mesma conta (nao so rejeitar logins simultaneos). So
   sobrevivia quem abria o painel antes do proximo da fila logar (~3 de
   20 processavam algo de verdade - os outros 17 so gastavam minutos de
   Actions a toa).
A Maria decidiu (14/09/2026) rodar 1 worker por vez (strategy.max-
parallel: 1) - elimina o problema de raiz (nunca ha uma 2a sessao ativa
pra derrubar a 1a), ciente do trade-off real: throughput volta ao teto de
~11s/pedido de uma unica instancia, nao mais paralelizado - nao da mais
pra zerar uma janela de dezenas de milhares de pedidos numa unica rodada,
mas usa o tempo de Actions de verdade em vez de desperdicar a maioria em
logins que nunca chegam a processar nada. O lock de login (titan_login_
lock/_adquirir_lock_login/_liberar_lock_login) virou redundante com
max-parallel:1 e foi removido.

O escopo tambem ja tinha voltado a ser so esta janela antes disso -
a 1a versao do desenho (13/09/2026) reivindicava lotes via uma RPC
(reivindicar_recheck_eventos, SELECT FOR UPDATE SKIP LOCKED) que cobria o
backlog INTEIRO de pedidos concluidos com eventos nulo (qualquer data),
bom pra tambem zerar backlog antigo como efeito colateral, mas exigia o
worker configurar um periodo de datas amplo no Titan pra achar pedidos
antigos, o que nunca foi implementado (achado real na run #48 manual -
"Nenhum elemento visivel encontrado" em quase todo pedido, porque o
periodo padrao do Titan pos-login e estreito/recente). A Maria decidiu
voltar a escopar isso so pra esta janela (14/09/2026) - o backlog antigo
fica de fora do escopo automatico, mesma situacao de antes de 13/09
(titan_recheck_eventos.yml, que cobre esse caso, continua desativado).

ITENS VIA METABASE, EVENTOS SAIU DO ESCOPO (16/09/2026, pedido direto da
Maria): "backfill nao precisa mais buscar itens e eventos no titan" - todo
o mecanismo de clique-por-pedido acima (job completar-eventos, 20
"workers" em sequencia, _ler_eventos_itens_via_filtro_cruzado e toda a
infraestrutura ao redor) foi REMOVIDO. Eventos deixou de ser
responsabilidade deste script (nao entrou na lista de colunas que a Maria
pediu pra manter) - quem ainda tiver eventos=NULL fica assim ate outro
mecanismo cobrir isso (nenhum decidido ainda). Itens continua sendo
preenchido, mas agora vem do Metabase (gold.shopify_order_items, banco
"Data Mart"/database_id 43 - mesma fonte e mesmo schema de JSON usados no
backfill manual de itens feito em 14-15/09/2026 pra rituaria/barbours/
kokeshi/apice - ver _buscar_itens_via_metabase) em vez de clicar pedido
por pedido no Titan: mais rapido (uma query em lote por marca, no mesmo
job, sem precisar de workers/matrix/artifact) e nao depende mais de
sessao/navegacao instavel no Power BI pra esse dado especifico. Autenticado
via API Key (METABASE_API_KEY, Secret do GitHub) - a conta da Maria no
Metabase nao tinha admin pra criar isso sozinha, uma pessoa com acesso de
admin gerou a chave e ela foi cadastrada como secret.

numero_pedido virou fill-only-if-empty (16/09/2026, mesmo pedido): antes
sempre sobrescrevia quando dava pra derivar (ver historico anterior desta
secao, 04/09/2026); agora so entra no payload quando a linha AINDA nao tem
numero_pedido no Supabase (ver _buscar_numero_pedido_ja_preenchido) - nunca
sobrescreve um valor ja gravado por outra fonte (ex: consulta avulsa da
Torre via titan_watcher.py).

COLETA VIA EXPORTACAO NATIVA, NAO SCROLL (25/08/2026, achado real pela
Ivna): a versao anterior lia a tabela "Informacao Pedido" rolando (ela e
virtualizada pelo Power BI - so ~16-20 linhas no DOM por vez) e acumulava por
Romaneio. Isso SUBESTIMAVA muito o total: pra um periodo onde a exportacao
nativa do Power BI ("..." -> "Exportar dados" -> "Exportar", opcao "Dados
com layout atual") trouxe 150.003 linhas de verdade, o scroll so vinha
salvando ~90. Agora o backfill usa scraper.exportar_dados_do_painel (baixa o
.xlsx completo) + scraper.ler_export_xlsx (le com openpyxl, streaming) em vez
de rolar - ver esses dois em titan_bi_scraper.py pro porque de cada escolha.
Bonus: essa exportacao tambem trouxe "Depositante"/"Cliente" com texto de
verdade, que o DOM nunca expunha (nem via accessibility tree - a suposicao
antiga de que precisaria de OCR deixou de valer).

CHAVE = (NOTA FISCAL, MARCA) - migrado de so-NF em 24/08/2026: confirmado com
print real da tela do Titan que a MESMA NF aparece em mais de uma linha, uma
por marca, cada uma com Romaneio DIFERENTE (ex: NF 940380 apareceu pra
Kokeshi com romaneio 169114 E pra Rituaria com romaneio 173827 - o risco que
parecia raro aconteceu de verdade). A marca de cada linha vem de "Nome
Projeto" (ex: "RITUARIA"), normalizada pra minuscula - linha sem "Nome
Projeto" visivel e ignorada (sem isso, nao da pra saber se colide com outra
marca da mesma NF). O "Numero do Pedido" continua nunca usado como CHAVE -
mas, ao contrario do que a gente achava antes, ele TEM correspondencia com o
numero de e-commerce da Torre pras marcas Gobeaute exceto apice (corrigido
04/09/2026 - ver registro_para_supabase/scraper.extrair_numero_pedido_torre).
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

import titan_bi_scraper as scraper
import titan_watcher  # reusa processar_pedido/marcar_erro do recheck por-NF (28/08/2026, ver rechecar_situacoes_presas)

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"
# Destino de tudo que NAO consegue entrar em infos_titan (marca nao
# identificavel, ou upsert que falhou mesmo depois das tentativas - ver
# _registrar_falhas) - nada mais e so descartado/logado no stderr da Action,
# fica gravado e consultavel aqui (achado real, 11/09/2026: NF 30280 e outras
# desapareciam sem NENHUM sinal em lugar nenhum que alguem realmente olhe).
TABELA_FALHAS = "infos_titan_falhas_backfill"
TAMANHO_LOTE = 200  # registros por chamada ao Supabase - evita 1 request por pedido
PASTA_EXPORTS = Path(__file__).parent / "titan_exports"  # so um local de trabalho - o arquivo e apagado apos o upload

# Metabase (16/09/2026, ver ITENS VIA METABASE no topo do arquivo) - mesmo
# banco "Data Mart" ja usado no backfill manual de itens (14-15/09/2026).
# METABASE_API_KEY vem de um Secret do GitHub (a conta da Maria no Metabase
# nao e admin, entao a chave foi gerada por quem tem acesso de admin e so
# cadastrada aqui - nunca commitada).
METABASE_URL = os.environ.get("METABASE_URL", "https://metabase.gobeaute.com.br")
METABASE_API_KEY = os.environ.get("METABASE_API_KEY")
METABASE_DATABASE_ID = 43  # "Data Mart"

# "marca" (usado no infos_titan/na Torre) so difere do "brand" do Metabase
# pra by samia - marca tem espaco, brand tem underscore (mesma pegadinha ja
# documentada em outros lugares deste projeto). Toda outra marca bate igual
# (rituaria, barbours, kokeshi, apice, lescent, aua, yenzah).
MARCA_PARA_BRAND_SHOPIFY = {"by samia": "by_samia"}

# RECHECK DE SITUACAO PRESA (28/08/2026, achado real pela Ivna): a janela do
# backfill acima e sempre "ultimos 5 dias corridos" - um pedido importado ha
# mais de 5 dias que AINDA nao chegou em situacao final (EMBARCADO/
# CANCELADO) cai fora dessa janela e nunca mais seria revisitado. Simetrico
# ao recheck que ja existe no titan_cf_worker (Cloudflare) pro mesmo
# problema, so que aqui roda com Playwright de verdade (sem o bloqueio de
# renderizacao que o Browser Rendering do Cloudflare tem pra esse dashboard
# Power BI - ver conversa de 28/08/2026).
STATUS_FINAIS = ["EMBARCADO", "CANCELADO"]
RECHECK_INTERVALO_HORAS = 2
RECHECK_ORCAMENTO_SEGUNDOS = 600  # 10min - deixa margem dentro do timeout do job (ver titan_backfill.yml)

# Achado real (01/09/2026, reportado pela Ivna - pedido SH1197313KS/NF
# 1197313/marca kokeshi sem tabela Eventos na Unilog CD apesar de ja estar
# EMBARCADO): pedido descoberto so pelo backfill (que de proposito nao
# coleta Eventos - ver ITENS VIA METABASE, EVENTOS SAIU DO ESCOPO no topo
# do arquivo) fica com eventos=NULL pra sempre se a situacao ja for final -
# buscar_situacao_presa acima ignora de proposito EMBARCADO/CANCELADO (a
# situacao em si esta certa, so falta Eventos). O unico outro caminho
# (server.ts reenfileirar quando 'concluido' sem eventos, ver commit
# dad290a) so dispara se alguem repetir a solicitacao daquela NF pela
# Torre - nao roda sozinho. Confirmado >=1000 pedidos reais nesse estado
# via query direta no Supabase (bateu o teto de 1000 da API, o numero real
# pode ser maior) - eventos ficou fora do escopo do backfill de vez em
# 16/09/2026, esse backlog nao e mais alvo de nenhum mecanismo automatico
# aqui.
#
# Achado real (11/09/2026, pedido da Maria): ordenar a fila so por
# atualizado_em.asc (mais antigo primeiro) colocava um pedido RECEM-criado
# pelo backfill no fim de uma fila de ~1,2 milhao de linhas antigas - na
# pratica ele nunca chegava a vez dele, mesmo rodando titan_recheck_eventos.
# yml a cada 15min. Um pedido recente (que um agente pode estar atendendo
# agora) importa mais do que um pedido de semanas atras ja embarcado. A
# funcao reivindicar_recheck_eventos no banco chegou a priorizar isso
# (janela de 3 dias) dentro do proprio ORDER BY - REMOVIDO de la em
# 13/09/2026 (achado real na run #46 manual do --completar-eventos-worker,
# ver docstring de _completar_eventos_itens_worker): essa expressao usava
# now(), o que impedia o Postgres de usar o indice existente e forcava
# Seq Scan + sort em disco sobre 1,37 milhao de linhas (~6,3s, estourando o
# timeout de 3s do PostgREST). A RPC hoje so ordena por atualizado_em asc
# (mais antigo primeiro, sem excecao pra recentes) - mais rapida (~5ms via
# indice), mas SEM a priorizacao de recentes descrita acima. So afeta
# buscar_concluidos_sem_eventos/--so-recheck (titan_recheck_eventos.yml,
# ainda desativado) - o worker de eventos/itens do backfill diario nao usa
# mais esta RPC (ver _completar_eventos_itens_worker, 14/09/2026).


def _agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def buscar_situacao_presa(limite=500):
    """
    Simetrico ao recheck que ja existe no titan_cf_worker (Cloudflare) pro
    mesmo problema: pedidos status='concluido' com situacao ainda nao-final
    (ou nula), sem atualizacao ha mais de RECHECK_INTERVALO_HORAS. "or" cobre
    situacao NULL tambem - "not.in" sozinho nunca bate NULL (semantica de
    NULL do Postgres).
    """
    # order=atualizado_em.asc (08/09/2026, mesmo achado real ja corrigido em
    # titan_watcher.buscar_pendentes - NF 1295282 ficou 3 dias parada porque,
    # sem ordenacao explicita, o Postgres/PostgREST nao garante nenhuma ordem
    # nas linhas devolvidas: um subconjunto podia ficar "escondido" atras de
    # outro indefinidamente entre rodadas. Mais antigo primeiro garante que
    # cada rodada avanca a fila de verdade (a linha processada tem
    # atualizado_em bumped, indo pro fim), em vez de arriscar reprocessar
    # sempre o mesmo bloco.
    lista_finais = ",".join(STATUS_FINAIS)
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        - datetime.timedelta(hours=RECHECK_INTERVALO_HORAS)
    ).isoformat() + "Z"
    path = (
        f"{TABELA}?status=eq.concluido"
        f"&atualizado_em=lt.{urllib.parse.quote(cutoff)}"
        f"&or=(situacao.is.null,situacao.not.in.({lista_finais}))"
        f"&select=numero_nf,marca&order=atualizado_em.asc&limit={limite}"
    )
    return titan_watcher._supabase_request("GET", path) or []


def rechecar_situacoes_presas(page, orcamento_segundos=RECHECK_ORCAMENTO_SEGUNDOS):
    """
    Reconfere um por um (mesma logica ja validada em titan_watcher.
    processar_pedido - login com Playwright de verdade, sem o bloqueio de
    renderizacao do Browser Rendering do Cloudflare pra esse dashboard Power
    BI) os pedidos que ficaram presos numa situacao intermediaria fora da
    janela fixa de 5 dias do backfill acima. Orcamento de tempo (nao so
    contagem de itens) pra nao estourar o timeout do job independente de
    quantos pedidos estiverem presos.

    orcamento_segundos e parametro (nao so a constante direto) desde
    11/09/2026 - o modo --so-recheck de main() roda SO isto (sem a
    exportacao em massa antes), varias vezes por hora via
    titan_recheck_eventos.yml, e passa um orcamento proprio maior que o do
    job diario (que reparte tempo com a exportacao principal).
    """
    # Busca protegida por try/except (11/09/2026, achado real - erro visto em
    # producao): buscar_situacao_presa/buscar_concluidos_sem_eventos chamam o
    # Supabase direto via urllib, que joga excecao (nao devolve so um valor de
    # erro) em qualquer falha HTTP (ex: 500 transitorio do lado do Supabase).
    # Sem este try/except, isso derrubava o processo INTEIRO com
    # sys.exit(1) antes mesmo de reconferir um unico pedido - mesmo padrao
    # ja usado no loop abaixo pra cada item individual, so que faltava aqui
    # pra chamada inicial.
    try:
        presos = buscar_situacao_presa()
    except Exception as e:
        print(f"Nao consegui buscar pedidos presos em situacao intermediaria: {e} - "
              f"pulando este recheck, tenta de novo na proxima rodada.", file=sys.stderr)
        return
    if not presos:
        print("Nenhum pedido preso em situacao intermediaria fora da janela do backfill.")
        return
    print(f"{len(presos)} pedido(s) presos em situacao intermediaria - reconferindo (orcamento {orcamento_segundos}s)...")
    inicio = time.monotonic()
    processados = 0
    for item in presos:
        if time.monotonic() - inicio > orcamento_segundos:
            print(f"  orcamento de tempo esgotado - {processados}/{len(presos)} reconferido(s), resto fica pra proxima rodada.")
            break
        try:
            # permitir_limpar_dados=False (achado real, 11/09/2026): isto so
            # roda sobre pedidos ja 'concluido' com dado bom - um "nao
            # encontrado" transitorio (sessao/scraping, nao o pedido ter
            # sumido de verdade) nao pode apagar situacao/romaneio/eventos
            # ja gravados. Ver docstring de titan_watcher.processar_pedido.
            titan_watcher.processar_pedido(page, item, permitir_limpar_dados=False)
        except Exception as e:
            print(f"  [NF {item.get('numero_nf')} / marca {item.get('marca')}] erro no recheck: {e}", file=sys.stderr)
            if item.get("numero_nf") and item.get("marca"):
                titan_watcher.marcar_erro(item.get("numero_nf"), item.get("marca"), str(e))
        processados += 1
    print(f"Recheck de situacao concluido: {processados} pedido(s) processado(s).")


# REMOVIDAS (16/09/2026, ver ITENS VIA METABASE no topo do arquivo):
# buscar_concluidos_sem_eventos/rechecar_concluidos_sem_eventos - eventos
# saiu do escopo do backfill de vez, entao o recheck-por-clique dedicado a
# eventos (usado so pelo modo --so-recheck, hoje ja desativado via
# titan_recheck_eventos.yml) nao faz mais sentido existir aqui.


def _supabase_upsert_lote(registros):
    """
    Upsert em lote, on_conflict=numero_nf,marca. O Postgres/PostgREST so
    atualiza as colunas presentes no JSON enviado quando ha conflito
    (resolution=merge-duplicates) - quando numero_pedido/eventos/itens nao
    entram no dict (nao deu pra derivar, ou o pedido nao precisava de
    completar eventos/itens nesta rodada - ver registro_para_supabase/
    _completar_eventos_itens), um valor ja gravado antes (por uma consulta
    avulsa do titan_watcher.py ou por uma rodada anterior do backfill) NAO
    e apagado. So romaneio/situacao/datas/etc. sao sobrescritos sempre (o
    que e o esperado - o backfill sempre traz o dado mais recente do Titan
    pra esses campos).

    O PostgREST exige que TODOS os objetos de um mesmo array de upsert tenham
    exatamente as mesmas chaves - senao rejeita o POST inteiro com
    PGRST102 "All object keys must match" (achado real, 11/09/2026: como
    numero_pedido so entra no dict quando da pra derivar - ver
    registro_para_supabase -, um lote de 200 registros que misturasse
    pedidos com e sem essa chave derrubava o lote INTEIRO, silenciosamente -
    numa unica rodada isso descartou ~54% dos 150 mil registros exportados,
    sem nenhuma linha sequer chegar ao Supabase, sem erro nenhum gravado
    nelas: elas simplesmente nunca existiram na tabela). Agrupa por conjunto
    de chaves antes de mandar - um POST por grupo uniforme, em vez de um so
    pro lote inteiro, pra um formato de linha diferente nao derrubar as
    outras.
    """
    if not registros:
        return
    grupos = {}
    for r in registros:
        grupos.setdefault(frozenset(r.keys()), []).append(r)
    erros = []
    for grupo in grupos.values():
        try:
            _supabase_upsert_grupo_uniforme(grupo)
        except Exception as e:
            erros.append(str(e))
            # Antes disso o grupo so virava uma linha de log no stderr da
            # Action e sumia pra sempre (11/09/2026, mesmo achado da NF
            # 30280) - agora fica gravado e consultavel em TABELA_FALHAS,
            # mesmo que a rodada inteira ainda seja reportada como "com erro"
            # (ver lotes_com_erro em main()).
            _registrar_falhas("upsert_falhou", grupo, detalhe=str(e))
    if erros:
        raise RuntimeError("; ".join(erros))

def _supabase_upsert_grupo_uniforme(registros, tentativas=3):
    """POST de um unico grupo onde todo registro tem o mesmo conjunto de
    chaves (exigencia do PostgREST pra upsert em lote - ver
    _supabase_upsert_lote acima).

    Tenta ate `tentativas` vezes com backoff (2s, 4s, ...) pra erro
    transitorio (rede, timeout, 5xx do proprio Supabase) - achado real
    (11/09/2026): antes, qualquer excecao aqui - transitoria ou nao -
    derrubava o grupo na primeira tentativa, e uma instabilidade momentanea
    da rede/Supabase perdia pedidos do mesmo jeito que um erro de dado real.
    Erro 4xx (ex: PGRST102, coluna invalida) NAO tenta de novo - e um
    problema no formato dos dados, tentar de novo com o mesmo payload so
    atrasa sem mudar o resultado.
    """
    url = f"{SUPABASE_URL}/rest/v1/{TABELA}?on_conflict=numero_nf,marca"
    data = json.dumps(registros).encode("utf-8")
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            # Achado real (08/09/2026): um HTTPError sozinho so mostra "HTTP Error
            # 400: Bad Request", sem o motivo de verdade que o PostgREST/Postgres
            # devolve no corpo da resposta (ex: qual coluna/valor violou o que) -
            # sem ler esse corpo, um lote ruim vira um "Bad Request" mudo e a
            # unica pista sobra ser adivinhar. Le e inclui na excecao antes de
            # repropagar, pra quem chamar (o loop principal) conseguir logar o
            # motivo real e (com o fix ao lado) isolar so o lote problematico.
            corpo = e.read().decode("utf-8", errors="replace")[:1000]
            ultimo_erro = RuntimeError(f"Supabase respondeu {e.code} no upsert em lote: {corpo}")
            if e.code < 500:
                raise ultimo_erro from e
        except urllib.error.URLError as e:
            ultimo_erro = RuntimeError(f"Falha de rede no upsert em lote: {e}")
        if tentativa < tentativas:
            time.sleep(2 ** tentativa)
    raise ultimo_erro

def _registrar_falhas(motivo, itens, detalhe=None):
    """
    Grava em TABELA_FALHAS em vez de so descartar/logar. Usado tanto pra
    linha do Titan sem marca identificavel (motivo="sem_nf_ou_marca", `item`
    e a linha bruta da exportacao) quanto pro registro que um upsert em lote
    nao conseguiu gravar mesmo depois das tentativas (motivo="upsert_falhou",
    `item` e o dict ja processado por registro_para_supabase).

    marca vira "" (nao None) de proposito: o unique constraint da tabela e
    (numero_nf, marca, motivo), e o Postgres trata cada NULL como distinto
    de qualquer outro NULL - com None, duas falhas da MESMA nf+motivo sem
    marca virariam duas linhas em vez de uma so (upsert nunca colide).

    Nao deixa uma falha AQUI (ex: TABELA_FALHAS fora do ar tambem) derrubar
    o backfill - so avisa, mesmo padrao de titan_watcher.marcar_erro.

    DEDUPLICA por (numero_nf, marca, motivo) antes de enviar (11/09/2026,
    achado real - "HTTP Error 500" reproduzido em toda rodada com >1 linha
    "sem_nf_ou_marca" no mesmo lote): como marca vira "" de proposito (ver
    acima) pra colidir entre si, DUAS OU MAIS linhas sem NF/marca no mesmo
    lote geram a MESMA chave ("", "", motivo) - e um unico INSERT com
    ON CONFLICT nao aceita duas linhas com a mesma chave de conflito no
    mesmo comando (Postgres recusa com erro, que o PostgREST repassa como
    500). Sem isso, o lote inteiro falhava e nenhuma das falhas ficava
    registrada - exatamente o "sumico silencioso" que esta tabela existe
    pra evitar.
    """
    por_chave = {}
    for item in itens:
        nf = (item.get("numero_nf") or item.get("Nota Fiscal") or "").strip()
        marca = (item.get("marca") or item.get("Nome Projeto") or "").strip().lower()
        por_chave[(nf, marca)] = {
            "numero_nf": nf,
            "marca": marca,
            "motivo": motivo,
            "detalhe": (str(detalhe)[:500] if detalhe else None),
            "payload": item,
            "resolvido": False,
            "atualizado_em": _agora_iso(),
        }
    linhas = list(por_chave.values())
    if not linhas:
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/{TABELA_FALHAS}?on_conflict=numero_nf,marca,motivo"
        data = json.dumps(linhas).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        print(f"  (nao consegui registrar {len(linhas)} falha(s) de '{motivo}' em {TABELA_FALHAS}: {e})", file=sys.stderr)


def registro_para_supabase(r):
    """
    CHAVE = (numero_nf, marca) - migrado de so-NF em 24/08/2026 depois de
    confirmar (print real do Titan) que a mesma NF aparece em mais de uma
    linha, uma por marca, cada uma com Romaneio diferente. "marca" e derivada
    de "Nome Projeto" (ex: "RITUARIA"), normalizada pra minuscula pra bater
    com o id que a Torre usa (ex: "rituaria"). Sem "Nome Projeto" (celula
    vazia - acontece, ver print real onde uma linha nao tinha marca visivel),
    ignora a linha: nao ha como saber se ela colide com outra marca da mesma
    NF, e gravar sem marca arriscaria sobrescrever ou ser sobrescrita por
    engano depois.

    numero_pedido: deriva o numero de e-commerce da Torre a partir do
    "Numero do Pedido" do Titan pra marcas Gobeaute exceto apice (corrigido
    04/09/2026 - achado real da Ivna: ao contrario do que a gente achava
    antes, o "Numero do Pedido" do Titan E o numero da Torre + a NF colada no
    final, sem separador - ver scraper.extrair_numero_pedido_torre pro
    detalhe). Fica de fora quando nao der pra confirmar o sufixo.
    """
    nf = (r.get("Nota Fiscal") or "").strip()
    marca = (r.get("Nome Projeto") or "").strip().lower()
    if not marca:
        # Fallback so pra Apice (25/08/2026, confirmado pela Ivna e por
        # 28.671/28.674 casos reais numa exportacao real): quando "Nome
        # Projeto" vem em branco, o "Depositante" sempre e "APICE ES" ou
        # "APICE RJ" - usa isso so pra essa marca especifica em vez de
        # arriscar um fallback generico pra qualquer marca (Depositante NAO
        # e um identificador confiavel de marca em geral - valores como
        # "GOBEAUTY ES"/"BEAUTY HUB ES" cobrem varias marcas juntas).
        depositante = (r.get("Depositante") or "").strip().lower()
        if depositante.startswith("apice"):
            marca = "apice"
    if not nf or not marca:
        return None
    registro = {
        "numero_nf": nf,
        "marca": marca,
        "status": "concluido",
        "erro": None,
        "situacao": r.get("Situação"),
        "romaneio": r.get("Romaneio") or None,
        "valor_pedido": r.get("Valor Pedido"),
        "volume": r.get("Volume"),
        "observacao": r.get("Observação"),
        "nome_projeto": r.get("Nome Projeto"),
        "nome_projeto_antigo": r.get("Nome Projeto (Antigo)"),
        "data_importado": r.get("Data Importado"),
        "data_expedido": r.get("Data Expedido"),
        "data_conferido": r.get("Data Conferido"),
        # Novos (25/08/2026) - so a exportacao nativa expoe texto de verdade
        # pra essas duas colunas (ver comentario grande no topo do arquivo).
        "depositante": r.get("Depositante"),
        "cliente": r.get("Cliente"),
        "atualizado_em": _agora_iso(),
    }
    # NAO promove direto pra "numero_pedido" aqui (16/09/2026, pedido direto
    # da Maria: "somente para as marcas que nao tem nada") - fica guardado
    # numa chave privada (nunca mandada ao Supabase, ver o pop() em
    # exportar_e_gravar_periodo) ate o chamador conferir em lote se a linha
    # JA tem numero_pedido gravado (ver _buscar_numero_pedido_ja_preenchido)
    # - so promove quando ainda nao tem. Continua vazio quando o sufixo nao
    # confirma (ver docstring acima).
    registro["_numero_pedido_derivado"] = scraper.extrair_numero_pedido_torre(r)
    return registro


LINHA_TRUNCAMENTO = "Exported data exceeded the allowed volume"


def _validar_filtro_aplicado(filtro_aplicado, registros, data_inicial, data_final):
    """
    Confere se o periodo que o Titan REALMENTE aplicou bate com o periodo
    pedido, ANTES de confiar nos dados.

    DOIS sinais, porque nenhum sozinho e confiavel:
    1. O texto "Filtros aplicados: ..." (ver ler_export_xlsx) quando o
       Titan inclui ele - confere se cita o dia certo. So decide algo
       quando data_inicial == data_final (um unico dia da pra comparar
       contra um texto especifico); pra periodo de varios dias, ou sem
       essa linha, esse sinal fica de fora (fica so pro sinal 2).
    2. TRUNCAMENTO (ver LINHA_TRUNCAMENTO): se o Titan avisa que a
       exportacao passou do teto de volume, os dados vieram incompletos -
       vale pra QUALQUER tamanho de periodo (nao so 1 dia), entao mais
       seguro descartar e tentar de novo do que arriscar gravar so uma
       parte do periodo pedido.
    """
    if data_inicial == data_final and filtro_aplicado:
        dia = datetime.datetime.strptime(data_inicial, "%d/%m/%Y").date()
        dia_seguinte = dia + datetime.timedelta(days=1)
        if not (dia.strftime("%d/%m/%Y") in filtro_aplicado and dia_seguinte.strftime("%d/%m/%Y") in filtro_aplicado):
            return False
    if any(LINHA_TRUNCAMENTO in str(v) for r in registros for v in r.values()):
        return False
    return True


def _buscar_pendentes_itens(nfs):
    """
    Consulta o Supabase em lotes (por numero_nf, ate 200 por vez - mesmo
    tamanho de TAMANHO_LOTE) pra achar, dentro das NFs desta janela de
    backfill, quais pedidos ja existem na tabela com itens ainda NULO.

    RENOMEADA de _buscar_pendentes_eventos_itens (16/09/2026, ver ITENS VIA
    METABASE no topo do arquivo) - o "OU eventos.is.null" saiu da condicao
    porque eventos nao e mais responsabilidade deste script. NAO filtra por
    status (mesmo comportamento de antes, 14/09/2026 - pedido da Maria).

    Devolve numero_pedido junto (usado por _buscar_itens_via_metabase como
    chave de match no Shopify) - por isso devolve uma LISTA de dicts (um
    por pedido), nao um set de tuplas (numero_nf, marca).

    So cobre pedidos que JA EXISTEM na tabela (linha nova desta propria
    rodada, ainda nao gravada, so entra aqui na PROXIMA rodada do backfill,
    2x/dia - simplificacao aceita de proposito, evita ter que juntar dois
    numero_pedido diferentes - o gravado no banco e o derivado nesta mesma
    exportacao - so pra tentar cobrir o mesmo dia).

    Volume baixo por rodada: a maioria dos pedidos de uma janela de poucos
    dias ja foi vista e completada numa rodada anterior (a janela e
    deslizante e se repete a cada backfill).
    """
    pendentes = []
    nfs_unicas = sorted({str(nf) for nf in nfs if nf})
    for i in range(0, len(nfs_unicas), TAMANHO_LOTE):
        lote_nfs = nfs_unicas[i:i + TAMANHO_LOTE]
        lista = ",".join(urllib.parse.quote(nf) for nf in lote_nfs)
        path = (f"{TABELA}?numero_nf=in.({lista})"
                f"&itens=is.null"
                f"&select=numero_nf,marca,numero_pedido")
        linhas = titan_watcher._supabase_request("GET", path) or []
        pendentes.extend(linhas)
    return pendentes


def _buscar_numero_pedido_ja_preenchido(nfs):
    """
    Devolve o subconjunto de (numero_nf, marca), dentro das NFs desta
    janela, que JA TEM numero_pedido preenchido no Supabase - usado pra so
    incluir o numero_pedido derivado desta exportacao (ver
    registro_para_supabase/_numero_pedido_derivado) quando a linha AINDA
    nao tem nada (pedido direto da Maria, 16/09/2026: "numero_pedido
    somente para as marcas que nao tem nada"). Uma linha nova (ainda nem
    existe na tabela) nao aparece aqui - conta como "nao preenchido", e por
    isso recebe o valor derivado normalmente.
    """
    preenchidos = set()
    nfs_unicas = sorted({str(nf) for nf in nfs if nf})
    for i in range(0, len(nfs_unicas), TAMANHO_LOTE):
        lote_nfs = nfs_unicas[i:i + TAMANHO_LOTE]
        lista = ",".join(urllib.parse.quote(nf) for nf in lote_nfs)
        path = (f"{TABELA}?numero_nf=in.({lista})"
                f"&numero_pedido=not.is.null"
                f"&select=numero_nf,marca")
        linhas = titan_watcher._supabase_request("GET", path) or []
        for linha in linhas:
            preenchidos.add((linha["numero_nf"], linha["marca"]))
    return preenchidos


def _brand_shopify(marca):
    return MARCA_PARA_BRAND_SHOPIFY.get(marca, marca)


def _sql_lista_texto(valores):
    """Monta um IN (...) de literais texto pro Metabase, escapando aspas
    simples - os valores aqui sao numero_pedido ja validados por
    scraper.extrair_numero_pedido_torre (formato conhecido), mas escapa do
    mesmo jeito por seguranca (nunca confia em string concatenada direto em
    SQL sem escapar, mesmo quando a fonte parece controlada)."""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in valores)


def _metabase_query(sql):
    """
    Roda uma query SQL crua no Metabase (banco "Data Mart", ver
    METABASE_DATABASE_ID) via API, autenticada por API Key
    (METABASE_API_KEY - Secret do GitHub). Devolve lista de dicts (uma por
    linha) - a API crua do Metabase (/api/dataset) nao tem o teto de 500
    linhas que a ferramenta MCP interativa usada pra explorar o schema tem.
    """
    body = json.dumps({
        "database": METABASE_DATABASE_ID,
        "type": "native",
        "native": {"query": sql},
    }).encode("utf-8")
    req = urllib.request.Request(f"{METABASE_URL}/api/dataset", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", METABASE_API_KEY)
    with urllib.request.urlopen(req, timeout=60) as resp:
        resultado = json.loads(resp.read())
    colunas = [c["name"] for c in resultado["data"]["cols"]]
    return [dict(zip(colunas, linha)) for linha in resultado["data"]["rows"]]


def _buscar_itens_via_metabase(pendentes):
    """
    Busca "Itens do pedido" no Metabase (gold.shopify_order_items) em vez
    de clicar pedido por pedido no Titan (16/09/2026, pedido direto da
    Maria - ver ITENS VIA METABASE no topo do arquivo) - mesma fonte e
    mesma logica de match ja validadas no backfill manual de itens feito
    em 14-15/09/2026 pra rituaria/barbours/kokeshi/apice.

    Recebe a lista de pendentes desta janela (formato de
    _buscar_pendentes_itens - numero_nf/marca/numero_pedido) e devolve um
    dict {(numero_nf, marca): itens} pros que deu pra casar no Shopify.

    CHAVE DE MATCH: apice casa por order_number (bigint, sem "#") ==
    numero_pedido; as outras marcas casam por order_name (texto,
    "SH<id><SUFIXO>") == numero_pedido - o proprio numero_pedido gravado no
    Supabase JA E esse valor (ver scraper.extrair_numero_pedido_torre/
    registro_para_supabase), sem transformacao nenhuma aqui.

    FORMATO DO JSON: replica o MESMO schema que "Itens do pedido" tem
    quando vem direto do Titan (Ean/Tipo/Codigo/Quantidade/Descricao/Valor
    Total/Row Selection/Valor Unitario/Checkout Realizado) - "Row
    Selection"/"Checkout Realizado" sao so replicados sem significado real
    (artefato de UI do Titan, mesma convencao ja usada pro schema de
    eventos), "Ean" repete o SKU do Shopify (o Metabase nao tem EAN de
    verdade nesta tabela).
    """
    if not METABASE_API_KEY:
        print("METABASE_API_KEY nao configurada - pulando busca de itens via Metabase nesta rodada.", file=sys.stderr)
        return {}

    por_marca = {}
    for p in pendentes:
        marca = (p.get("marca") or "").strip()
        numero_pedido = (p.get("numero_pedido") or "").strip()
        if not marca or not numero_pedido:
            continue
        por_marca.setdefault(marca, []).append((p["numero_nf"], numero_pedido))

    resultado = {}
    for marca, pares in por_marca.items():
        brand = _brand_shopify(marca)
        pedidos_unicos = sorted({numero_pedido for _, numero_pedido in pares})
        linhas = []
        for i in range(0, len(pedidos_unicos), TAMANHO_LOTE):
            fatia = pedidos_unicos[i:i + TAMANHO_LOTE]
            if marca == "apice":
                ids_validos = [v for v in fatia if v.isdigit()]
                if not ids_validos:
                    continue
                condicao = f"order_number in ({', '.join(ids_validos)})"
                campo_chave = "order_number"
            else:
                condicao = f"order_name in ({_sql_lista_texto(fatia)})"
                campo_chave = "order_name"
            sql = (
                f"select {campo_chave} as pedido_chave, sku, title, quantity, price "
                f"from gold.shopify_order_items "
                f"where brand = '{brand}' and {condicao}"
            )
            try:
                linhas.extend(_metabase_query(sql))
            except Exception as e:
                print(f"  ERRO consultando itens no Metabase pra marca {marca}: {e}", file=sys.stderr)

        itens_por_pedido = {}
        for linha in linhas:
            chave = str(linha["pedido_chave"])
            preco = float(linha.get("price") or 0)
            quantidade = linha.get("quantity") or 0
            sku = linha.get("sku") or ""
            itens_por_pedido.setdefault(chave, []).append({
                "Ean": sku,
                "Tipo": "UN",
                "Código": sku,
                "Quantidade": str(quantidade),
                "Descrição": linha.get("title") or "",
                "Valor Total": f"{preco * float(quantidade):.2f}",
                "Row Selection": "Select Row",
                "Valor Unitário": f"{preco:.2f}",
                "Checkout Realizado": "1",
            })

        for numero_nf, numero_pedido in pares:
            itens = itens_por_pedido.get(numero_pedido)
            if itens:
                resultado[(numero_nf, marca)] = itens
    return resultado


def exportar_e_gravar_periodo(frame, data_inicial, data_final, tentativas=2):
    """
    Faz um export + upsert de 'Informação Pedido' pro periodo
    (data_inicial, data_final) inteiro. Devolve (gravados, ignorados,
    lotes_com_erro) - (0, 0, 0) se o filtro de periodo nunca bater mesmo
    apos `tentativas` (ver _validar_filtro_aplicado - melhor pular esta
    rodada e tentar de novo na proxima do que arriscar gravar dado de um
    periodo errado ou incompleto).

    Itens vem do Metabase (ver _buscar_itens_via_metabase), consultado
    aqui mesmo dentro deste job (16/09/2026) - nao precisa mais de um job
    paralelo separado nem de artifact pra passar lista entre jobs (ver
    ITENS VIA METABASE no topo do arquivo).
    """
    registros = []
    filtro_ok = False
    for tentativa in range(1, tentativas + 1):
        sufixo = f" (tentativa {tentativa}/{tentativas})" if tentativa > 1 else ""
        print(f"Definindo periodo {data_inicial} - {data_final}{sufixo}...")
        try:
            scraper.definir_periodo(frame, data_inicial, data_final)
            # Print SEMPRE (nao so quando falha) - achado real (11/09/2026): sem
            # nenhuma evidencia visual de como o filtro fica depois do
            # definir_periodo, nao da pra saber se o bug e na digitacao (campo
            # mostra data errada) ou em como o Power BI reage a ela (campo
            # mostra certo, visual nao filtra mesmo assim). Nome do arquivo
            # inclui o dia e a tentativa pra nao sobrescrever entre chamadas.
            scraper.salvar_diagnostico(frame, f"periodo_definido_{data_inicial.replace('/', '-')}_t{tentativa}")

            print("Exportando dados do painel 'Informação Pedido'...")
            caminho_export = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
            print(f"  baixado em {caminho_export}")
            filtro_aplicado, registros = scraper.ler_export_xlsx(caminho_export)
            try:
                caminho_export.unlink()
            except OSError:
                pass  # nao critico - so um arquivo de trabalho

            filtro_ok = _validar_filtro_aplicado(filtro_aplicado, registros, data_inicial, data_final)
            if filtro_ok:
                break
            print(f"  AVISO: o filtro de periodo que o Titan aplicou nao bate com o periodo pedido "
                  f"({data_inicial} a {data_final}) - texto do Titan: {filtro_aplicado!r}, "
                  f"{len(registros)} linha(s) recebidas.", file=sys.stderr)
        except Exception as e:
            # NOVO (11/09/2026, achado real - crash em producao, run #32):
            # scraper.definir_periodo as vezes estoura o timeout esperando a
            # 1a linha da tabela aparecer (Playwright TimeoutError) - sem
            # este try/except, essa excecao nunca era pega AQUI, so
            # propagava e derrubava o processo INTEIRO (sys.exit(1)) mesmo
            # com `tentativas` pra tentar de novo (o loop nunca chegava a
            # tentar a 2a vez). Trata como "filtro nao bateu" nesta
            # tentativa - tenta de novo (ou desiste e pula esta janela pra
            # proxima rodada, ver abaixo) em vez de derrubar o job inteiro.
            filtro_ok = False
            registros = []
            print(f"  ERRO tentando definir/exportar o periodo: {e}", file=sys.stderr)

    if not filtro_ok:
        print(f"  Desistindo de {data_inicial} apos {tentativas} tentativa(s) sem o filtro certo - "
              f"esse dia fica pra proxima rodada do backfill (nao grava nada agora, pra nao arriscar "
              f"gravar dado de um periodo errado). Ver titan_debug/periodo_definido_{data_inicial.replace('/', '-')}_t*"
              f" no artifact do job pra diagnosticar.", file=sys.stderr)
        return 0, 0, 0

    print(f"{len(registros)} linha(s) encontradas no periodo.")
    if not registros:
        print("Nenhum registro encontrado - confira se o periodo esta certo (ver print salvo, se houver).")
        return 0, 0, 0

    # O Power BI tem um limite de volume por exportacao - descoberto com uma
    # exportacao real (25/08/2026) que voltou com uma linha extra so com
    # este aviso no lugar de um pedido de verdade. Pra periodo de VARIOS
    # dias (nao e o caso do loop diario de main(), mas exportar_e_gravar_
    # periodo pode ser chamada com qualquer periodo) isso ainda pode
    # acontecer legitimamente (volume real grande, nao filtro quebrado) -
    # _validar_filtro_aplicado acima ja tratou o caso de 1 dia so (onde
    # truncamento e sinal de bug, nao de volume real). Aqui so filtra a
    # linha sintetica antes de processar, pro caso legitimo de periodo
    # maior mesmo.
    truncou = any(LINHA_TRUNCAMENTO in str(v) for r in registros for v in r.values())
    if truncou:
        print(f"\n  AVISO: o Titan/Power BI truncou esta exportacao (limite de volume excedido) - "
              f"o periodo {data_inicial} a {data_final} tem mais dados do que a exportacao trouxe de "
              f"uma vez so.", file=sys.stderr)
        # NAO so avisa - filtra a linha sintetica antes de processar
        # (08/09/2026, achado real: o backfill so CHECAVA essa mensagem na
        # coluna "Depositante", mas nunca excluia a linha de registros - ela
        # seguia pro loop normal como se fosse um pedido de verdade. Se o
        # Power BI colocar o aviso em outra coluna dessa vez, ela pode
        # passar pelas checagens de nf/marca de registro_para_supabase com
        # lixo em vez de um valor de verdade, envenenando o lote inteiro no
        # upsert). Confere QUALQUER coluna, nao so Depositante, pra nao
        # depender de onde o Power BI decidir colocar o aviso.
        antes = len(registros)
        registros = [r for r in registros if not any(LINHA_TRUNCAMENTO in str(v) for v in r.values())]
        print(f"  ({antes - len(registros)} linha(s) de aviso de truncamento removida(s) antes de gravar)", file=sys.stderr)

    payloads = []
    ignorados_lote = []
    ignorados = 0
    for r in registros:
        payload = registro_para_supabase(r)
        if payload is None:
            # Antes so incrementava esse contador e a linha sumia - agora
            # fica gravada em TABELA_FALHAS pra revisao manual (mesmo
            # achado da NF 30280: nada pode desaparecer sem deixar rastro
            # consultavel em algum lugar).
            ignorados += 1
            ignorados_lote.append(r)
            if len(ignorados_lote) >= TAMANHO_LOTE:
                _registrar_falhas("sem_nf_ou_marca", ignorados_lote)
                ignorados_lote = []
            continue
        payloads.append(payload)
    if ignorados_lote:
        _registrar_falhas("sem_nf_ou_marca", ignorados_lote)

    # numero_pedido fill-only-if-empty (16/09/2026, pedido direto da Maria -
    # ver ITENS VIA METABASE/registro_para_supabase): promove o valor
    # derivado (guardado em "_numero_pedido_derivado") pra "numero_pedido"
    # de verdade so nas linhas que AINDA nao tem nada gravado - o pop()
    # tira a chave privada de TODO payload antes de seguir, senao o
    # PostgREST rejeitaria o upsert com uma coluna que a tabela nao tem.
    ja_tem_numero_pedido = _buscar_numero_pedido_ja_preenchido([p["numero_nf"] for p in payloads])
    for payload in payloads:
        derivado = payload.pop("_numero_pedido_derivado", None)
        if derivado and (payload["numero_nf"], payload["marca"]) not in ja_tem_numero_pedido:
            payload["numero_pedido"] = derivado

    # Itens via Metabase (16/09/2026, ver ITENS VIA METABASE no topo do
    # arquivo) - substitui o clique-por-pedido no Titan que rodava aqui
    # antes (job completar-eventos separado, removido junto com esta
    # mudanca). So busca pra quem AINDA esta com itens nulo no Supabase
    # (_buscar_pendentes_itens so cobre pedidos que ja existem na tabela -
    # ver docstring dela).
    nfs_desta_janela = [p["numero_nf"] for p in payloads]
    pendentes_itens = _buscar_pendentes_itens(nfs_desta_janela)
    print(f"{len(pendentes_itens)} pedido(s) desta janela sem itens - buscando no Metabase...")
    itens_encontrados = _buscar_itens_via_metabase(pendentes_itens)
    print(f"  {len(itens_encontrados)} casado(s) no Metabase de {len(pendentes_itens)} pendente(s).")
    if itens_encontrados:
        # Reconfere bem antes de gravar (mesmo motivo do "ainda_vazio" usado
        # no backfill manual de itens/eventos - ver upsert_itens.py/
        # upsert_eventos.py) - evita sobrescrever um itens que
        # titan_watcher.py (consulta avulsa da Torre) tenha preenchido com
        # dado real do Titan nesse meio tempo.
        ainda_sem_itens = {(p["numero_nf"], p["marca"]) for p in _buscar_pendentes_itens(nfs_desta_janela)}
        for payload in payloads:
            chave = (payload["numero_nf"], payload["marca"])
            if chave in itens_encontrados and chave in ainda_sem_itens:
                payload["itens"] = itens_encontrados[chave]

    lote = []
    gravados = 0
    lotes_com_erro = 0
    for payload in payloads:
        lote.append(payload)
        if len(lote) >= TAMANHO_LOTE:
            try:
                _supabase_upsert_lote(lote)
                gravados += len(lote)
                print(f"  {gravados} gravados...")
            except Exception as e:
                # Isola o lote ruim (08/09/2026, achado real: um HTTP 400
                # num unico lote de 200 derrubava o script inteiro com
                # sys.exit(1), descartando todos os lotes restantes - lote
                # ruim vira log e o resto continua, em vez de tudo ou nada).
                lotes_com_erro += 1
                print(f"  ERRO no lote (linhas {gravados + 1}-{gravados + len(lote)}), pulando: {e}", file=sys.stderr)
            lote = []
    if lote:
        try:
            _supabase_upsert_lote(lote)
            gravados += len(lote)
        except Exception as e:
            lotes_com_erro += 1
            print(f"  ERRO no ultimo lote ({len(lote)} linha(s)), pulando: {e}", file=sys.stderr)

    return gravados, ignorados, lotes_com_erro


# REMOVIDAS (16/09/2026, ver ITENS VIA METABASE no topo do arquivo):
# _marcar_eventos_itens/_erro_de_frame_invalido/_fatia_do_worker/
# _completar_eventos_itens_worker - todo o mecanismo de clique-por-pedido
# via Playwright (job completar-eventos, matrix de 20 "workers") foi
# substituido pela consulta em lote ao Metabase (ver
# _buscar_itens_via_metabase, chamada direto dentro de
# exportar_e_gravar_periodo).


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-inicial", help="formato DD/MM/AAAA, ex: 01/06/2026 (obrigatorio, exceto com --so-recheck)")
    parser.add_argument("--data-final", help="formato DD/MM/AAAA, ex: 24/08/2026 (obrigatorio, exceto com --so-recheck)")
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    # --so-recheck: pula a exportacao em massa e roda so o recheck de
    # situacao presa (ver rechecar_situacoes_presas). Eventos SAIU do
    # escopo deste script (16/09/2026, ver ITENS VIA METABASE no topo do
    # arquivo) - --recheck-orcamento-eventos-segundos e a chamada de
    # rechecar_concluidos_sem_eventos foram removidos daqui junto com a
    # funcao em si.
    parser.add_argument("--so-recheck", action="store_true", default=False,
                         help="pula a exportacao em massa - so roda rechecar_situacoes_presas")
    parser.add_argument("--recheck-orcamento-situacao-segundos", type=int, default=RECHECK_ORCAMENTO_SEGUNDOS,
                         help=f"orcamento pro recheck de situacao presa (padrao {RECHECK_ORCAMENTO_SEGUNDOS}s)")
    args = parser.parse_args()
    if not args.so_recheck and (not args.data_inicial or not args.data_final):
        parser.error("--data-inicial e --data-final sao obrigatorios (a nao ser que use --so-recheck)")

    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar.", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            print("Entrando no Titan BI...")
            scraper.login(page, email, senha)

            if args.so_recheck:
                print("\n--so-recheck: pulando exportacao em massa, so reconferindo pedidos presos em situacao intermediaria...")
                rechecar_situacoes_presas(page, orcamento_segundos=args.recheck_orcamento_situacao_segundos)
                return

            frame = scraper.get_dashboard_frame(page)

            # Export UNICO pro periodo inteiro (voltou a ser assim em
            # 11/09/2026 - tinha virado um export por dia por engano: o
            # motivo real do teto de volume do Power BI estourar era o
            # filtro de data nao aplicar direito (ja corrigido em
            # scraper.definir_periodo), nao o tamanho do periodo em si.
            # Conferido direto no Supabase: o total REAL e distinto de uma
            # janela de poucos dias fica bem abaixo do teto de ~150 mil
            # linhas - exportar por dia so multiplicava o tempo do job sem
            # necessidade. _validar_filtro_aplicado ainda pega qualquer
            # truncamento de verdade (volume real crescendo no futuro),
            # devolvendo (0, 0, 0) sem gravar nada errado.
            print(f"Exportando periodo {args.data_inicial} - {args.data_final}...")
            gravados_total, ignorados_total, lotes_com_erro_total = exportar_e_gravar_periodo(
                frame, args.data_inicial, args.data_final
            )

            print(f"\nConcluido - {gravados_total} pedido(s) gravados no Supabase.")
            if lotes_com_erro_total:
                print(f"{lotes_com_erro_total} lote(s) falharam e foram pulados (ver ERRO acima pro motivo real, e "
                      f"{TABELA_FALHAS} pros registros exatos) - esses pedidos ficam pra proxima rodada do "
                      f"backfill.", file=sys.stderr)
            if ignorados_total:
                print(f"{ignorados_total} linha(s) ignoradas por falta de Nota Fiscal ou de \"Nome Projeto\" "
                      f"(marca) - sem os dois, nao da pra identificar com seguranca. Gravadas em "
                      f"{TABELA_FALHAS} pra revisao manual, em vez de so descartadas.")

        finally:
            browser.close()


if __name__ == "__main__":
    main()
