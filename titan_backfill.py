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

LIMITACAO DE PROPOSITO: por velocidade, o backfill so pega os campos da
tabela "Informacao Pedido" (Romaneio, Situacao, datas etc.) - nao clica
pedido por pedido pra puxar Eventos/Itens tambem (isso levaria uma consulta
inteira por pedido, inviavel pra um periodo grande). Eventos/Itens de um
pedido especifico continuam vindo do jeito de sempre: titan_watcher.py, sob
demanda, so quando a Torre realmente precisar montar o ticket completo
daquele pedido.

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

# RECHECK DE SITUACAO PRESA (28/08/2026, achado real pela Ivna): a janela do
# backfill acima e sempre "ultimos 10 dias corridos" - um pedido importado ha
# mais de 10 dias que AINDA nao chegou em situacao final (EMBARCADO/
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
# coleta Eventos/Itens - ver LIMITACAO no topo do arquivo) fica com
# eventos=NULL pra sempre se a situacao ja for final - buscar_situacao_presa
# acima ignora de proposito EMBARCADO/CANCELADO (a situacao em si esta
# certa, so falta Eventos/Itens). O unico outro caminho (server.ts
# reenfileirar quando 'concluido' sem eventos, ver commit dad290a) so
# dispara se alguem repetir a solicitacao daquela NF pela Torre - nao roda
# sozinho. Confirmado >=1000 pedidos reais nesse estado via query direta no
# Supabase (bateu o teto de 1000 da API, o numero real pode ser maior).
#
# ORCAMENTO AUMENTADO de 600 pra 1800 (08/09/2026, pedido real da Ivna -
# "muitos pedidos ja embarcados sem eventos"): com 10min (a ~15-20s por
# pedido, por causa da navegacao real via Playwright) so dava pra reconferir
# ~30-40 pedidos por rodada, 2x/dia - contra um backlog de 1000+, levaria
# semanas pra zerar. 30min da ~3x mais throughput por rodada. Aumentado
# junto com timeout-minutes do job em titan_backfill.yml (50 -> 90) pra
# sobrar margem real (exportacao principal + recheck de situacao presa +
# este recheck, todos dentro do mesmo job).
EVENTOS_NULOS_RECHECK_ORCAMENTO_SEGUNDOS = 1800  # 30min

# Achado real (11/09/2026, pedido da Maria): ordenar a fila so por
# atualizado_em.asc (mais antigo primeiro) colocava um pedido RECEM-criado
# pelo backfill no fim de uma fila de ~1,2 milhao de linhas antigas - na
# pratica ele nunca chegava a vez dele, mesmo rodando titan_recheck_eventos.
# yml a cada 15min. Um pedido recente (que um agente pode estar atendendo
# agora) importa mais do que um pedido de semanas atras ja embarcado. A
# janela em si (3 dias) mora dentro da funcao reivindicar_recheck_eventos
# no banco (nao aqui) desde que o paralelismo real (ver
# buscar_concluidos_sem_eventos) precisou de reserva atomica via SQL -
# no Postgres, nao dava pra fazer 2 chamadas HTTP separadas (recentes,
# depois antigos) de forma atomica sem risco de duas instancias
# reivindicarem o mesmo pedido entre uma chamada e outra.


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
    janela fixa de 10 dias do backfill acima. Orcamento de tempo (nao so
    contagem de itens) pra nao estourar o timeout do job independente de
    quantos pedidos estiverem presos.

    orcamento_segundos e parametro (nao so a constante direto) desde
    11/09/2026 - o modo --so-recheck de main() roda SO isto (sem a
    exportacao em massa antes), varias vezes por hora via
    titan_recheck_eventos.yml, e passa um orcamento proprio maior que o do
    job diario (que reparte tempo com a exportacao principal).
    """
    presos = buscar_situacao_presa()
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


def buscar_concluidos_sem_eventos(limite=500, lease_minutos=20):
    """
    REIVINDICA (nao so le) ate `limite` pedidos status='concluido' com
    eventos ainda NULL, chamando a funcao reivindicar_recheck_eventos no
    Supabase (SELECT ... FOR UPDATE SKIP LOCKED por baixo, ver migracao
    reivindicar_recheck_eventos).

    Achado real (11/09/2026, pedido da Maria pra acelerar o backlog de
    ~1,2 milhao de linhas de ~400 dias pra semanas): titan_recheck_eventos.
    yml agora roda VARIAS instancias em paralelo (ver strategy.matrix no
    workflow). Com um SELECT simples (sem reserva), duas instancias rodando
    ao mesmo tempo pegariam OS MESMOS pedidos - duplicando trabalho (o
    dobro de logins/navegacao no Titan pros MESMOS pedidos, cobrindo
    METADE dos pedidos diferentes que deveria num mesmo intervalo de
    tempo - o paralelismo inteiro seria desperdicado). A funcao no banco
    marca cada linha reivindicada com recheck_reservado_ate (lease de
    `lease_minutos`, default folgado o bastante pra cobrir uma rodada
    inteira) - outra instancia so pode reivindicar essa linha de novo
    depois que o lease expirar, o que tambem AUTO-RECUPERA pedidos cuja
    instancia caiu/travou no meio do processamento, sem precisar de
    limpeza manual nenhuma.

    A prioridade "recentes primeiro" (achado anterior - pedido novo nao
    pode ficar preso atras do backlog antigo) ja fica dentro da funcao no
    banco (janela de 3 dias), nao precisa mais de duas chamadas separadas
    aqui.
    """
    return titan_watcher._supabase_request(
        "POST", "rpc/reivindicar_recheck_eventos", {"qtd": limite, "lease_minutos": lease_minutos}
    ) or []


def rechecar_concluidos_sem_eventos(page, orcamento_segundos=EVENTOS_NULOS_RECHECK_ORCAMENTO_SEGUNDOS):
    """
    Reconfere um por um (mesma logica do rechecar_situacoes_presas acima -
    processar_pedido sempre clica na linha e extrai Eventos/Itens) os
    pedidos que o backfill deixou concluidos mas sem essa informacao.
    Orcamento de tempo proprio, separado do recheck de situacao presa.

    Achado real (11/09/2026, reportado pela Maria): confirmado por
    exportacao em massa dos paineis "Eventos"/"Itens do pedido" que NAO da
    pra trazer isso em lote - sem uma linha de "Informacao Pedido"
    selecionada, o Power BI devolve os dois paineis AGREGADOS (Eventos vira
    so Situacao+Horario sem NF nenhuma pra identificar o pedido; Itens vira
    quantidade somada por SKU no periodo inteiro) - impossivel desagregar de
    volta pro pedido certo. O clique-por-pedido continua sendo o UNICO jeito
    (backlog real medido nesse dia: ~1,2 milhao de linhas com eventos=NULL).
    Como paliativo (nao resolve a raiz, so aumenta o ritmo - ver conversa),
    orcamento_segundos virou parametro pra titan_recheck_eventos.yml poder
    rodar SO isto varias vezes por hora com um orcamento proprio, em vez de
    competir por tempo com a exportacao principal 2x/dia.
    """
    pendentes = buscar_concluidos_sem_eventos()
    if not pendentes:
        print("Nenhum pedido concluido sem Eventos/Itens.")
        return
    print(f"{len(pendentes)} pedido(s) concluidos sem Eventos/Itens - completando "
          f"(orcamento {orcamento_segundos}s)...")
    inicio = time.monotonic()
    processados = 0
    for item in pendentes:
        if time.monotonic() - inicio > orcamento_segundos:
            print(f"  orcamento de tempo esgotado - {processados}/{len(pendentes)} completado(s), resto fica pra proxima rodada.")
            break
        try:
            # permitir_limpar_dados=False - mesmo motivo de rechecar_situacoes_
            # presas acima: so roda sobre pedidos ja 'concluido'.
            titan_watcher.processar_pedido(page, item, permitir_limpar_dados=False)
        except Exception as e:
            print(f"  [NF {item.get('numero_nf')} / marca {item.get('marca')}] erro completando eventos/itens: {e}", file=sys.stderr)
            if item.get("numero_nf") and item.get("marca"):
                titan_watcher.marcar_erro(item.get("numero_nf"), item.get("marca"), str(e))
        processados += 1
    print(f"Recheck de eventos/itens concluido: {processados} pedido(s) processado(s).")


def _supabase_upsert_lote(registros):
    """
    Upsert em lote, on_conflict=numero_nf,marca. O Postgres/PostgREST so
    atualiza as colunas presentes no JSON enviado quando ha conflito
    (resolution=merge-duplicates) - como este script NAO envia numero_pedido/
    eventos/itens, um pedido que ja tenha esses campos preenchidos por uma
    consulta avulsa anterior (titan_watcher.py) NAO tem esses campos apagados
    por um backfill rodado depois. So romaneio/situacao/datas/etc. sao
    sobrescritos (o que e o esperado - o backfill sempre traz o dado mais
    recente do Titan pra esses campos).

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
    """
    linhas = []
    for item in itens:
        nf = (item.get("numero_nf") or item.get("Nota Fiscal") or "").strip()
        marca = (item.get("marca") or item.get("Nome Projeto") or "").strip().lower()
        linhas.append({
            "numero_nf": nf,
            "marca": marca,
            "motivo": motivo,
            "detalhe": (str(detalhe)[:500] if detalhe else None),
            "payload": item,
            "resolvido": False,
            "atualizado_em": _agora_iso(),
        })
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
    # So inclui a chave quando conseguimos derivar (ver docstring acima) -
    # omitir em vez de gravar None preserva, no upsert com merge-duplicates,
    # um numero_pedido ja existente (ex: vindo de uma solicitacao real pela
    # Torre) quando essa linha em particular nao permite confirmar o sufixo.
    numero_pedido_derivado = scraper.extrair_numero_pedido_torre(r)
    if numero_pedido_derivado:
        registro["numero_pedido"] = numero_pedido_derivado
    return registro


def _gerar_intervalos_diarios(data_inicial, data_final):
    """
    Quebra (data_inicial, data_final) em blocos de 1 dia (formato DD/MM/AAAA,
    igual o resto do script). Achado real (11/09/2026, reportado pela Maria -
    NF 10034363/apice, situacao AGUARDANDO_PRODUCAO, sumida do Supabase):
    confirmado que ela aparece no Titan filtrando so por essa NF, mas nao
    esta em infos_titan NEM em TABELA_FALHAS - ou seja, a linha nunca chegou
    a ser LIDA pelo backfill. O periodo de 10 dias inteiro ja bate perto do
    teto de exportacao do Power BI por conta propria (confirmado: um export
    real de 10 dias veio com exatamente 150.003 linhas - o aviso de
    truncamento SEMPRE dispara pra essa janela). O codigo so avisava
    (LINHA_TRUNCAMENTO em exportar_e_gravar_periodo) mas nunca quebrava o
    periodo sozinho - dependia de alguem notar o aviso no log e rodar nao
    de novo manualmente com um periodo menor, o que nunca acontecia na
    pratica. 1 dia por vez fica bem abaixo do teto (150mil/10 = ~15mil
    linhas/dia).
    """
    inicio = datetime.datetime.strptime(data_inicial, "%d/%m/%Y").date()
    fim = datetime.datetime.strptime(data_final, "%d/%m/%Y").date()
    dia = inicio
    while dia <= fim:
        texto = dia.strftime("%d/%m/%Y")
        yield texto, texto
        dia += datetime.timedelta(days=1)


def _validar_filtro_aplicado(filtro_aplicado, data_inicial, data_final):
    """
    Confere se o texto "Filtros aplicados: ..." que vem na propria
    exportacao (ver scraper.ler_export_xlsx) bate de verdade com o periodo
    pedido, ANTES de confiar nos dados. Achado real (11/09/2026, Maria): um
    periodo de 1 dia so (data_inicial == data_final, o unico caso que o
    loop diario de main() realmente usa) as vezes nao era aplicado de
    verdade pelo scraper.definir_periodo - a exportacao voltava com um
    periodo bem mais largo, e o mesmo teto de 150.000 linhas do Power BI
    disparava de novo (confirmado: uma exportacao pra "01/09/2026 -
    01/09/2026" voltou com exatamente 150.003 linhas - o MESMO numero que
    uma exportacao anterior de 10 dias inteiros - coincidencia demais pra
    ser real; um export manual do mesmo painel/dia trouxe so 14.806 linhas
    de verdade).

    So valida quando data_inicial == data_final (unico caso real usado por
    main()) - pra periodo com datas diferentes nao da pra prever com
    seguranca o texto exato que o Titan gera, entao deixa passar sem checar
    em vez de arriscar falso-negativo. Sem filtro_aplicado nenhum (Titan as
    vezes nao inclui essa linha - ver ler_export_xlsx), tambem deixa passar,
    ja que nao ha como validar.
    """
    if not filtro_aplicado or data_inicial != data_final:
        return True
    dia = datetime.datetime.strptime(data_inicial, "%d/%m/%Y").date()
    dia_seguinte = dia + datetime.timedelta(days=1)
    return dia.strftime("%d/%m/%Y") in filtro_aplicado and dia_seguinte.strftime("%d/%m/%Y") in filtro_aplicado


def exportar_e_gravar_periodo(frame, data_inicial, data_final, tentativas=2):
    """
    Faz UM export + upsert de 'Informação Pedido' pro periodo
    (data_inicial, data_final) - extraido de main() (11/09/2026) pra poder
    ser chamado uma vez por dia (ver _gerar_intervalos_diarios) em vez de
    uma vez so pro periodo inteiro, que estoura o teto de volume do Power
    BI pra janelas de varios dias. Devolve (gravados, ignorados,
    lotes_com_erro) desse periodo especifico - (0, 0, 0) se o filtro de
    periodo nunca bater mesmo apos `tentativas` (ver
    _validar_filtro_aplicado - melhor pular o dia e tentar de novo na
    proxima rodada do que arriscar gravar dado de um periodo errado).
    """
    registros = []
    filtro_ok = False
    for tentativa in range(1, tentativas + 1):
        sufixo = f" (tentativa {tentativa}/{tentativas})" if tentativa > 1 else ""
        print(f"Definindo periodo {data_inicial} - {data_final}{sufixo}...")
        scraper.definir_periodo(frame, data_inicial, data_final)

        print("Exportando dados do painel 'Informação Pedido'...")
        caminho_export = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
        print(f"  baixado em {caminho_export}")
        filtro_aplicado, registros = scraper.ler_export_xlsx(caminho_export)
        try:
            caminho_export.unlink()
        except OSError:
            pass  # nao critico - so um arquivo de trabalho

        filtro_ok = _validar_filtro_aplicado(filtro_aplicado, data_inicial, data_final)
        if filtro_ok:
            break
        print(f"  AVISO: o filtro de periodo que o Titan aplicou nao bate com o periodo pedido "
              f"({data_inicial} a {data_final}) - texto real do Titan: {filtro_aplicado!r}.", file=sys.stderr)

    if not filtro_ok:
        print(f"  Desistindo de {data_inicial} apos {tentativas} tentativa(s) sem o filtro certo - "
              f"esse dia fica pra proxima rodada do backfill (nao grava nada agora, pra nao arriscar "
              f"gravar dado de um periodo errado).", file=sys.stderr)
        return 0, 0, 0

    print(f"{len(registros)} linha(s) encontradas no periodo.")
    if not registros:
        print("Nenhum registro encontrado - confira se o periodo esta certo (ver print salvo, se houver).")
        return 0, 0, 0

    # O Power BI tem um limite de volume por exportacao - descoberto com uma
    # exportacao real (25/08/2026) que voltou com uma linha extra so com
    # este aviso no lugar de um pedido de verdade. Quebrar em blocos de 1
    # dia (ver _gerar_intervalos_diarios) deveria evitar isso na pratica,
    # mas a checagem continua aqui como rede de seguranca pra um dia
    # especifico com volume fora do normal.
    LINHA_TRUNCAMENTO = "Exported data exceeded the allowed volume"
    truncou = any(LINHA_TRUNCAMENTO in str(v) for r in registros for v in r.values())
    if truncou:
        print(f"\n  AVISO: o Titan/Power BI truncou esta exportacao (limite de volume excedido) mesmo pra "
              f"um periodo de 1 dia so ({data_inicial}) - esse dia especifico tem volume incomum. Pedidos "
              f"desse dia podem estar faltando - considere rodar so ele manualmente com um periodo ainda "
              f"menor.", file=sys.stderr)
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

    lote = []
    ignorados_lote = []
    gravados = 0
    ignorados = 0
    lotes_com_erro = 0
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
    if ignorados_lote:
        _registrar_falhas("sem_nf_ou_marca", ignorados_lote)

    return gravados, ignorados, lotes_com_erro


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-inicial", help="formato DD/MM/AAAA, ex: 01/06/2026 (obrigatorio, exceto com --so-recheck)")
    parser.add_argument("--data-final", help="formato DD/MM/AAAA, ex: 24/08/2026 (obrigatorio, exceto com --so-recheck)")
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    # --so-recheck (11/09/2026, achado real: backlog de ~1,2 milhao de
    # pedidos com eventos=NULL - ver docstring de rechecar_concluidos_sem_
    # eventos pro porque o clique-por-pedido e o unico jeito) - pula a
    # exportacao em massa (que so roda 2x/dia e tem que repartir tempo com
    # os dois rechecks) e roda SO os rechecks, com orcamento proprio maior.
    # Pensado pra rodar isolado, varias vezes por hora, via
    # titan_recheck_eventos.yml - throughput bem maior que os 2x/dia atuais
    # sem competir pelo tempo do job de exportacao.
    parser.add_argument("--so-recheck", action="store_true", default=False,
                         help="pula a exportacao em massa - so roda rechecar_situacoes_presas + rechecar_concluidos_sem_eventos")
    parser.add_argument("--recheck-orcamento-situacao-segundos", type=int, default=RECHECK_ORCAMENTO_SEGUNDOS,
                         help=f"orcamento pro recheck de situacao presa (padrao {RECHECK_ORCAMENTO_SEGUNDOS}s)")
    parser.add_argument("--recheck-orcamento-eventos-segundos", type=int, default=EVENTOS_NULOS_RECHECK_ORCAMENTO_SEGUNDOS,
                         help=f"orcamento pro recheck de eventos/itens faltando (padrao {EVENTOS_NULOS_RECHECK_ORCAMENTO_SEGUNDOS}s)")
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
                print("\n--so-recheck: pulando exportacao em massa, so reconferindo pedidos presos e sem eventos...")
                rechecar_situacoes_presas(page, orcamento_segundos=args.recheck_orcamento_situacao_segundos)
                rechecar_concluidos_sem_eventos(page, orcamento_segundos=args.recheck_orcamento_eventos_segundos)
                return

            frame = scraper.get_dashboard_frame(page)

            # Um export por DIA em vez de um export so pro periodo inteiro
            # (11/09/2026, achado real - ver docstring de
            # _gerar_intervalos_diarios: NF 10034363/apice existia no Titan
            # dentro da janela normal do backfill mas nunca chegou nem a ser
            # lida, porque 10 dias inteiros de 'Informacao Pedido' ja batem
            # perto do teto de exportacao do Power BI por conta propria).
            intervalos = list(_gerar_intervalos_diarios(args.data_inicial, args.data_final))
            print(f"Periodo {args.data_inicial} - {args.data_final} quebrado em {len(intervalos)} dia(s) "
                  f"(um export por dia, evita estourar o teto de volume do Power BI).")
            gravados_total = 0
            ignorados_total = 0
            lotes_com_erro_total = 0
            for i, (dia_inicial, dia_final) in enumerate(intervalos, start=1):
                print(f"\n[{i}/{len(intervalos)}] --- {dia_inicial} ---")
                gravados, ignorados, lotes_com_erro = exportar_e_gravar_periodo(frame, dia_inicial, dia_final)
                gravados_total += gravados
                ignorados_total += ignorados
                lotes_com_erro_total += lotes_com_erro

            print(f"\nConcluido - {gravados_total} pedido(s) gravados no Supabase.")
            if lotes_com_erro_total:
                print(f"{lotes_com_erro_total} lote(s) falharam e foram pulados (ver ERRO acima pro motivo real, e "
                      f"{TABELA_FALHAS} pros registros exatos) - esses pedidos ficam pra proxima rodada do "
                      f"backfill.", file=sys.stderr)
            if ignorados_total:
                print(f"{ignorados_total} linha(s) ignoradas por falta de Nota Fiscal ou de \"Nome Projeto\" "
                      f"(marca) - sem os dois, nao da pra identificar com seguranca. Gravadas em "
                      f"{TABELA_FALHAS} pra revisao manual, em vez de so descartadas.")
            print("Eventos/Itens NAO foram trazidos por este backfill (ver LIMITACAO no topo do arquivo) - "
                  "continuam vindo via titan_watcher.py quando a Torre precisar de um pedido especifico.")

            print("\nReconferindo pedidos presos em situacao intermediaria fora da janela do backfill...")
            rechecar_situacoes_presas(page)

            print("\nCompletando Eventos/Itens de pedidos que o backfill deixou concluidos sem essa informacao...")
            rechecar_concluidos_sem_eventos(page)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
