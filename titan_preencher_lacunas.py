"""
titan_preencher_lacunas.py

PORTADO do repo central-tickets (17/09/2026, a pedido direto da Maria -
"você precisa pegar o fluxo titan_preencher_lacunas (diario) do projeto
central-tickets e levar para o meu titan v2") - script original de
15/09/2026, a pedido da Ivna la naquele repo. Cobre pedidos RECENTES que
titan_backfill.py/titan_watcher.py ainda nao trouxeram pro infos_titan.
Script SEPARADO de proposito (mesmo pedido original da Ivna - "em outro
action, sem ser pelo backfill") - nao mexe em titan_backfill.py.

TRES AJUSTES feitos nesta portagem, porque titan_bi_scraper.py deste repo
(Titan-v2) ja evoluiu depois que central-tickets tirou sua copia (os dois
repos divergiram):

1. marca "by_samia" -> "by samia" na Etapa 1 (BUG REAL da versao original,
   nunca corrigido la): o brand_code que vem do Metabase pra essa marca e
   "by_samia" (underscore), mas infos_titan.marca grava essa marca com
   ESPACO ("by samia") em todo o resto do projeto (mesma pegadinha ja
   documentada em titan_backfill.py) - confirmado consultando o Metabase
   antes de portar (gold.intelipost_orders.brand_code = 'by_samia' de
   verdade). Sem esse fix, a Etapa 1 teria criado uma marca "by_samia"
   paralela, nunca reconhecida pelo resto do sistema.
2. _definir_periodo_evento agora usa scraper._formatar_data_para_titan()
   antes de digitar as datas - a versao original (escrita antes desse fix
   existir em titan_bi_scraper.py) digitava dd/mm/aaaa direto no campo, que
   exige M/d/aaaa (sem zero a esquerda, mes antes de dia) - EXATAMENTE a
   causa raiz ja documentada na docstring de _formatar_data_para_titan (o
   "150003 linha(s) recebidas" identico todo dia). Sem este fix, a Etapa 2
   teria o MESMO bug de filtro de data que ja foi corrigido uma vez neste
   projeto.
3. scraper.ler_export_xlsx() deste repo devolve (filtro_aplicado,
   registros) - uma tupla - desde 11/09/2026; a versao de central-tickets
   ainda devolve so `registros`. Ajustado o unpacking em
   etapa2_exportar_eventos_fatia.

Alem disso, os nomes das variaveis de ambiente do Metabase foram alinhados
com o padrao ja usado em titan_backfill.py (METABASE_URL/METABASE_API_KEY,
com o mesmo valor ja configurado como Secret do GitHub pro backfill - nao
precisa cadastrar nada novo) em vez do METABASE_KEY_GOBEAUTE original.

DUAS ETAPAS INDEPENDENTES:

ETAPA 1 (so Metabase + Supabase, sem Titan/Playwright): busca na Data Mart
(banco 43, gold.intelipost_orders) os pedidos faturados nos ultimos N dias,
e CADASTRA (INSERT, nunca upsert/sobrescreve) no infos_titan qualquer
(numero_nf, marca) que ainda nao exista la - com numero_pedido/cnpj ja
preenchidos, mas situacao/eventos ainda vazios ("status": "pendente_titan",
um status novo so pra essas linhas, pra nao confundir com o "pendente" que
o titan_watcher.py ja usa com outro sentido - ver marcar_erro/scraper.py).

ETAPA 2 (Titan BI via Playwright, como os outros scripts): exporta a aba
"Exportação - Status por Pedido" do Titan (tabela ja achatada por evento,
com Depositante, Nota Fiscal, Data_Evento e Evento - nao precisa clicar
pedido por pedido feito titan_watcher.py faz). Filtra pelos ultimos N dias,
agrupa por NF em "eventos" (mesmo formato {"Situação":..., "Horário da
Situação":...} que o resto do projeto ja usa) e atualiza as linhas do
infos_titan que baterem.

⚠️ ETAPA 2 CONTINUA A PARTE MENOS TESTADA DESTE SCRIPT - a navegacao ate a
aba "Exportação - Status por Pedido" foi escrita a partir de um PRINT (nunca
testada ao vivo por mim, sem acesso ao Titan), so o fix #2 acima foi
aplicado por analogia com um bug ja confirmado em outra aba do mesmo
dashboard. RECOMENDADO rodar `python titan_preencher_lacunas.py --dias 1
--so-etapa2` na sua maquina, SEM --headless, antes de confiar na rodada
agendada (GitHub Actions so roda headless - nao da pra fazer essa validacao
visual la; se travar, o screenshot/HTML cai em titan_debug/ do jeito de
sempre e da pra ajustar o seletor).

DEPOSITANTE NAO IDENTIFICA MARCA SOZINHO (confirmado consultando o
infos_titan de verdade antes de escrever o script original): "GOBEAUTY ES"
cobre kokeshi/lescent/aua/by samia JUNTOS, "BEAUTY HUB ES" cobre
rituaria/yenzah JUNTOS - so "APICE ES" (apice) e "BEAUTY HUB RJ" (barbours)
sao 1-pra-1. Por isso a Etapa 2 NUNCA deriva marca do Depositante: so usa
Depositante pra decidir ONDE ANEXAR um evento quando a mesma NF ja tem mais
de uma linha no infos_titan (colisao entre marcas, ja documentada em
titan_backfill.py) - se a NF so tem UMA linha, o evento vai pra ela direto,
marca nem entra na conta. Se tiver mais de uma linha E o Depositante nao
decidir sozinho (2+ marcas do mesmo grupo colidindo), a linha fica pulada e
reportada no final, nunca chuta.

APICE: numero_pedido do Titan NAO bate com o da Torre pra essa marca (ja
documentado em titan_backfill.py) - a Etapa 1 usa SOMENTE o que vem da
Intelipost (shopify_order_number, com fallback pro ecommerce_number quando
so-digitos) pra essa marca, nunca o que o Titan mostra.

USO
    set TITAN_EMAIL=sacgobeauty@unilog.com
    set TITAN_SENHA=sua-senha-aqui
    set METABASE_API_KEY=sua-chave-do-metabase-gobeaute
    python titan_preencher_lacunas.py --dias 30
    python titan_preencher_lacunas.py --dias 30 --so-etapa1  (nao abre o Titan)
    python titan_preencher_lacunas.py --dias 30 --so-etapa2  (nao usa o Metabase)
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import titan_bi_scraper as scraper
import titan_watcher  # reusa _supabase_request - mesmo projeto/chave, so uma tabela

METABASE_URL = os.environ.get("METABASE_URL", "https://metabase.gobeaute.com.br")
METABASE_DB_DATAMART = 43
TABELA = "infos_titan"
PASTA_EXPORTS = Path(__file__).parent / "titan_exports"

# Status novo (nao usar "pendente" - titan_watcher.buscar_pendentes ja filtra
# por esse valor com outro sentido: "aguardando titan_watcher.py processar
# via Titan direto". "pendente_titan" marca "ja sabemos NF/marca/pedido pela
# Intelipost, falta so completar com o Titan" - nunca entra na fila do
# titan_watcher.py, so na da Etapa 2 deste script ou de um recheck futuro).
STATUS_BASICO = "pendente_titan"

# Empirico (consultado direto no infos_titan antes de escrever o script
# original) - NAO e um mapa 1-pra-1, alguns depositantes cobrem mais de uma
# marca.
DEPOSITANTE_MARCAS = {
    "apice es": {"apice"},
    "apice rj": {"apice"},
    "beauty hub rj": {"barbours"},
    "beauty hub es": {"rituaria", "yenzah"},
    "gobeauty es": {"kokeshi", "lescent", "aua", "by samia"},
}

# "marca" (usado no infos_titan/na Torre) so difere do "brand_code" do
# Metabase pra by samia - marca tem espaco, brand_code tem underscore
# (confirmado direto no Metabase ao portar este script pro Titan-v2,
# 17/09/2026 - mesma pegadinha ja documentada em titan_backfill.py). Toda
# outra marca bate igual (apice, barbours, rituaria, kokeshi, lescent, aua,
# yenzah).
BRAND_CODE_PARA_MARCA = {"by_samia": "by samia"}


def _agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def metabase_query(sql, database=METABASE_DB_DATAMART):
    key = os.environ.get("METABASE_API_KEY")
    if not key:
        raise RuntimeError("Defina METABASE_API_KEY (mesma chave ja usada pelo titan_backfill.py).")
    # "constraints" e obrigatorio pra passar do teto silencioso de ~2000
    # linhas que o Metabase aplica em toda query nativa ad-hoc. Um LIMIT no
    # proprio SQL NAO basta, so esse campo top-level resolve. 50000 cobre
    # qualquer janela de dias razoavel pro volume desta query.
    body = json.dumps({
        "type": "native",
        "native": {"query": sql},
        "database": database,
        "constraints": {"max-results": 50000, "max-results-bare-rows": 50000},
    }).encode("utf-8")
    req = urllib.request.Request(f"{METABASE_URL}/api/dataset", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", key)
    # Retry simples (achado ao vivo no script original: dia com ~11 mil
    # linhas deu timeout de leitura na 8a chamada seguida de um --dias 30) -
    # 1 nova tentativa antes de desistir, timeout mais folgado (60 -> 120s).
    ultimo_erro = None
    for tentativa in range(2):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                j = json.loads(resp.read())
            if j.get("error"):
                raise RuntimeError(f"Metabase devolveu erro: {j['error']}")
            cols = [c["name"] for c in j["data"]["cols"]]
            return [dict(zip(cols, row)) for row in j["data"]["rows"]]
        except (TimeoutError, urllib.error.URLError) as e:
            ultimo_erro = e
            print(f"  (timeout/erro de rede no Metabase, tentativa {tentativa + 1}/2: {e})", file=sys.stderr)
    raise RuntimeError(f"Metabase falhou apos 2 tentativas: {ultimo_erro}")


def _chunks(lista, tamanho):
    for i in range(0, len(lista), tamanho):
        yield lista[i:i + tamanho]


# ---------------------------------------------------------------------------
# ETAPA 1
# ---------------------------------------------------------------------------

def etapa1_candidatos_intelipost_dia(data):
    """
    UM dia so (data = date) - achado ao vivo no script original: mesmo com
    o fix de "constraints" pro teto de 2000 do Metabase, um --dias 30
    inteiro numa query so bateu no proximo teto que o proprio script
    passava (50000) - o volume real da Intelipost Orders e maior do que
    parecia (a tabela parece ter mais de uma linha por invoice_number, nao
    1-pra-1). Rodar dia por dia mantem cada query pequena o bastante pra
    nunca preocupar com teto nenhum, e cada dia processado ja fica salvo no
    Supabase antes do proximo comecar - interromper no meio nao perde nada
    (etapa1_rodar so cadastra o que ainda nao existe, entao repetir um dia
    ja processado e seguro e rapido).
    """
    sql = f"""
        SELECT invoice_number, brand_code, origin_federal_tax_payer_id,
               shopify_order_number,
               external_order_numbers->>'ecommerce_number' AS ecommerce_number,
               external_order_numbers->>'sales' AS sales_number
        FROM gold.intelipost_orders
        WHERE invoice_date = DATE '{data.isoformat()}'
          AND invoice_number IS NOT NULL
          AND brand_code IS NOT NULL
          AND brand_code <> 'gocase'
    """
    return metabase_query(sql)


def _numero_pedido_torre(row, marca):
    # Apice diverge (ver docstring grande no topo) - so Intelipost, nunca Titan.
    if marca == "apice":
        pedido = (row.get("shopify_order_number") or "").strip()
        if pedido:
            return pedido
        ec = (row.get("ecommerce_number") or "").strip()
        # O fallback aceita SO digitos puros, ou o formato novo SH<numero>AP
        # (16/09/2026, a pedido da Ivna: "essa semana o numero do pedido da
        # apice mudou e segue o mesmo padrao das outras SH1629891AP") -
        # formato antigo continua aceito pra pedidos anteriores a mudanca.
        if re.match(r"^[0-9]{1,8}$", ec) or re.match(r"^SH\d+AP$", ec, re.IGNORECASE):
            return ec
        return None
    return (row.get("sales_number") or "").strip() or None


def _processar_linhas_intelipost(linhas):
    """Recebe linhas cruas da Intelipost Orders (de um dia, ou de um periodo
    inteiro) e cadastra no infos_titan so o que ainda nao existe. Devolve
    (candidatos, gravados) pra quem chama poder logar progresso."""
    candidatos = {}  # (nf, marca) -> registro
    nfs = set()
    for r in linhas:
        nf = (r.get("invoice_number") or "").strip()
        brand_code = (r.get("brand_code") or "").strip().lower()
        marca = BRAND_CODE_PARA_MARCA.get(brand_code, brand_code)
        if not nf or not marca:
            continue
        candidatos[(nf, marca)] = {
            "numero_nf": nf,
            "marca": marca,
            "numero_pedido": _numero_pedido_torre(r, marca),
            "cnpj": (r.get("origin_federal_tax_payer_id") or "").strip() or None,
        }
        nfs.add(nf)

    if not candidatos:
        return 0, 0

    # So insere o que realmente falta - nunca sobrescreve (pedido original
    # da Ivna: "cadastra-lo" so pro que ainda nao existe, nao um upsert
    # geral, que arriscaria resetar status/eventos ja preenchidos por outra
    # fonte).
    existentes = set()
    for lote in _chunks(list(nfs), 200):
        filtro = ",".join(lote)
        path = f"{TABELA}?numero_nf=in.({urllib.parse.quote(filtro)})&select=numero_nf,marca"
        for row in (titan_watcher._supabase_request("GET", path) or []):
            existentes.add((row["numero_nf"], row["marca"]))

    faltando = [v for k, v in candidatos.items() if k not in existentes]
    if not faltando:
        return len(candidatos), 0

    agora = _agora_iso()
    registros = [{
        "numero_nf": c["numero_nf"],
        "marca": c["marca"],
        "numero_pedido": c["numero_pedido"],
        "status": STATUS_BASICO,
        "erro": None,
        "solicitado_em": agora,
        "atualizado_em": agora,
        # cnpj nao tem coluna propria em infos_titan hoje - vai em observacao
        # so como referencia legivel, sem inventar coluna nova sem confirmar
        # com a Maria se algo mais ja depende do schema atual.
        "observacao": f"CNPJ (Intelipost): {c['cnpj']}" if c["cnpj"] else None,
    } for c in faltando]

    gravados = 0
    for lote in _chunks(registros, 200):
        try:
            titan_watcher._supabase_request("POST", f"{TABELA}", lote)
            gravados += len(lote)
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[Etapa 1] ERRO no lote ({len(lote)} linha(s)), pulando: {corpo}", file=sys.stderr)
    return len(candidatos), gravados


def etapa1_rodar(dias):
    """
    Dia por dia, do mais antigo pro mais recente (achado ao vivo no script
    original: uma query so pro periodo inteiro batia em qualquer teto de
    linhas que se configurasse - o volume real e maior do que "pedidos"
    sugere, parece ter mais de uma linha por NF na tabela). Cada dia e uma
    query pequena (nunca perto de nenhum teto) e ja fica salvo antes do
    proximo comecar - interromper no meio (Ctrl+C, queda de conexao) nao
    perde nada: rodar de novo so re-varre os mesmos dias e nao recria o que
    ja gravou (_processar_linhas_intelipost so insere o que falta).
    """
    hoje = datetime.date.today()
    total_candidatos = 0
    total_gravados = 0
    dias_com_erro = []
    for i in range(dias, 0, -1):
        dia = hoje - datetime.timedelta(days=i)
        try:
            linhas = etapa1_candidatos_intelipost_dia(dia)
        except Exception as e:
            # Isola o dia ruim (mesmo espirito do titan_backfill.py com
            # lotes ruins) - um dia com problema de rede nao pode derrubar
            # os outros 29. Fica registrado no resumo final; rodar de novo
            # com os mesmos --dias reprocessa so o que falta (idempotente).
            print(f"[Etapa 1] {dia.strftime('%d/%m/%Y')}: ERRO, pulando este dia - {e}", file=sys.stderr)
            dias_com_erro.append(dia)
            continue
        candidatos, gravados = _processar_linhas_intelipost(linhas)
        total_candidatos += candidatos
        total_gravados += gravados
        print(f"[Etapa 1] {dia.strftime('%d/%m/%Y')}: {len(linhas)} linha(s), {candidatos} candidato(s), {gravados} novo(s) gravado(s). (acumulado: {total_gravados})")
    print(f"[Etapa 1] Concluido - {total_gravados} pedido(s) novo(s) cadastrado(s) com status='{STATUS_BASICO}' em {dias} dia(s).")
    if dias_com_erro:
        listados = ", ".join(d.strftime("%d/%m/%Y") for d in dias_com_erro)
        print(f"[Etapa 1] {len(dias_com_erro)} dia(s) falharam e foram pulados: {listados} - rode de novo pra tentar so esses (os outros ja estao gravados).", file=sys.stderr)


# ---------------------------------------------------------------------------
# ETAPA 2
# ---------------------------------------------------------------------------

def _navegar_status_por_pedido(frame):
    """
    ⚠️ NAO TESTADO AO VIVO (ver aviso grande no topo do arquivo) - escrito a
    partir de um print mostrando 3 abas no rodape do relatorio:
    "Exportação", "Exportação - Sem Saldo", "Exportação - Status por
    Pedido". Mesmo cuidado PT/EN ja usado no resto do scraper (login/menu ja
    trocaram de idioma sem aviso antes).
    """
    aba = scraper.elemento_visivel(
        frame.get_by_text(re.compile(r"^Exporta[cç][aã]o - Status por Pedido$", re.IGNORECASE)),
        timeout_ms=15000,
    )
    aba.click()


LINHA_TRUNCAMENTO = "Exported data exceeded the allowed volume"


def _checar_truncamento(registros):
    """Mesmo aviso sintetico ja documentado em titan_backfill.py - o Power BI
    insere uma linha-aviso (nao um pedido de verdade) quando o volume
    excede o limite de exportacao. Filtra a linha e avisa alto, em vez de
    deixar passar como se fosse um evento real de algum pedido."""
    truncou = any(LINHA_TRUNCAMENTO in str(v) for r in registros for v in r.values())
    if not truncou:
        return registros
    print(f"  AVISO: Power BI truncou esta exportacao (limite de volume excedido) - "
          f"este dia sozinho tem mais linhas do que a exportacao trouxe de uma vez. "
          f"Alguns eventos deste dia podem estar faltando.", file=sys.stderr)
    return [r for r in registros if not any(LINHA_TRUNCAMENTO in str(v) for v in r.values())]


def _definir_periodo_evento(frame, data_inicial, data_final):
    """
    Igual a scraper.definir_periodo, so que com um passo a mais: clicar no
    painel "Informação Pedido" logo depois de digitar as datas, ANTES de
    tentar fechar o popup do calendario. Achado ao vivo no script original
    (print real salvo em titan_debug/set_filtro_data_nao_encontrado.png):
    nesta aba especifica ("Exportação - Status por Pedido"), o popup do
    calendario fica aberto depois do Enter e scraper.fechar_popup_calendario
    (clicar no cdk-overlay-backdrop) nao consegue fecha-lo sozinho, sempre
    no mesmo estado - clicar num elemento estavel da propria tela (o
    painel, que ja temos locator dele) tira o foco do calendario primeiro,
    sem depender so do backdrop.

    CORRIGIDO NA PORTAGEM PRO TITAN-V2 (17/09/2026): a versao original
    digitava data_inicial/data_final DIRETO (formato dd/mm/aaaa), sem
    converter - o campo do Titan exige M/d/aaaa (sem zero a esquerda, mes
    antes de dia), EXATAMENTE a causa raiz ja documentada na docstring de
    scraper._formatar_data_para_titan (o "150003 linha(s) recebidas"
    identico em todo dia testado, la em titan_bi_scraper.py). Essa aba
    "Status por Pedido" nunca tinha sido testada ao vivo com esse fix
    aplicado - se o campo de data aqui se comportar diferente do de
    "Informação Pedido", pode precisar de ajuste depois de um teste real.
    """
    try:
        rotulo = scraper.elemento_visivel(frame.get_by_text("Data Inicial - Data Final", exact=False), timeout_ms=60000)
        caixa = rotulo.bounding_box()
        if not caixa:
            raise PWTimeout("rotulo 'Data Inicial - Data Final' visivel mas sem bounding_box (layout inesperado)")
        x = caixa["x"] + caixa["width"] / 2
        y = caixa["y"] + caixa["height"] + 15
        frame.page.mouse.click(x, y)
        time.sleep(0.5)
        frame.page.keyboard.press("Control+A")
        frame.page.keyboard.type(scraper._formatar_data_para_titan(data_inicial), delay=60)
        frame.page.keyboard.press("Tab")
        time.sleep(0.3)
        frame.page.keyboard.press("Control+A")
        frame.page.keyboard.type(scraper._formatar_data_para_titan(data_final), delay=60)
        frame.page.keyboard.press("Enter")
        time.sleep(0.5)

        painel = scraper.localizar_painel(frame, "Informação Pedido")
        try:
            painel.click(timeout=3000, force=True)
        except Exception:
            pass
        time.sleep(0.3)
        scraper.fechar_popup_calendario(frame)

        painel.locator("xpath=.//*[self::tr or @role='row']").first.wait_for(timeout=30000)
        time.sleep(1.5)
    except PWTimeout:
        scraper.salvar_diagnostico(frame, "titan_preencher_lacunas_filtro_data_evento")
        raise


def _marca_por_depositante(depositante):
    return DEPOSITANTE_MARCAS.get((depositante or "").strip().lower())


def _agrupar_eventos_por_nf(registros):
    # Agrupa por NF, guardando (depositante, data_evento, evento) - a mesma
    # NF pode ter linhas de marcas diferentes misturadas (colisao), por isso
    # nao agrupa direto por (nf, marca): so decide a marca na hora de casar
    # com o infos_titan (ver _marca_por_depositante e etapa2_processar_lote).
    por_nf = {}
    for r in registros:
        nf = str(r.get("nota_fiscal_saida_numero") or r.get("Nota Fiscal de Saída") or "").strip()
        evento = str(r.get("Evento") or "").strip()
        data_evento = r.get("Data_Evento") or r.get("Data Evento")
        depositante = str(r.get("Depositante") or "").strip()
        if not nf or not evento:
            continue
        por_nf.setdefault(nf, []).append({
            "depositante": depositante,
            "situacao": evento,
            "horario": str(data_evento) if data_evento is not None else "",
        })
    return por_nf


def etapa2_processar_lote(por_nf):
    """
    Casa os eventos agrupados por NF com as linhas ja existentes no
    infos_titan e devolve os registros prontos pra upsert em lote (nunca
    escreve aqui - so monta a lista, ver _supabase_upsert_eventos_lote).
    """
    nfs = list(por_nf.keys())
    linhas_existentes = {}  # nf -> [ {numero_nf, marca, status} ]
    for lote in _chunks(nfs, 200):
        filtro = ",".join(lote)
        path = f"{TABELA}?numero_nf=in.({urllib.parse.quote(filtro)})&select=numero_nf,marca,status"
        for row in (titan_watcher._supabase_request("GET", path) or []):
            linhas_existentes.setdefault(row["numero_nf"], []).append(row)

    registros_upsert = []
    pulados_sem_linha = 0
    pulados_ambiguos = 0
    for nf, eventos_brutos in por_nf.items():
        candidatas = linhas_existentes.get(nf, [])
        if not candidatas:
            # Nenhuma linha pra essa NF ainda - a Etapa 1 (ou o backfill/
            # watcher normal) que cadastra; este script so ATUALIZA o que ja
            # existe, nunca cria uma linha so com eventos e sem marca confirmada.
            pulados_sem_linha += 1
            continue
        if len(candidatas) == 1:
            alvo = candidatas[0]
        else:
            # Colisao real (mesma NF, mais de uma marca ja cadastrada) - usa
            # Depositante pra tentar restringir a UMA candidata so.
            marcas_por_depositante = set()
            for e in eventos_brutos:
                grupo = _marca_por_depositante(e["depositante"])
                if grupo:
                    marcas_por_depositante |= grupo
            bateram = [c for c in candidatas if c["marca"] in marcas_por_depositante]
            if len(bateram) != 1:
                pulados_ambiguos += 1
                continue
            alvo = bateram[0]

        eventos_ordenados = sorted(eventos_brutos, key=lambda e: e["horario"])
        eventos_payload = [{"Situação": e["situacao"], "Horário da Situação": e["horario"]} for e in eventos_ordenados]
        situacao_atual = eventos_ordenados[-1]["situacao"] if eventos_ordenados else None

        # PostgREST exige que TODO objeto do lote tenha exatamente as mesmas
        # chaves (upsert em lote vira um INSERT so, colunas fixas pela 1a
        # linha) - por isso "situacao"/"status" sempre entram, nunca
        # condicionalmente - so o VALOR muda por linha, nunca a presenca da
        # chave.
        registro = {
            "numero_nf": nf,
            "marca": alvo["marca"],
            "eventos": eventos_payload,
            "situacao": situacao_atual,
            # So promove pra 'concluido' se ainda nao estava - nunca regride
            # um status que outra fonte ja tenha avancado (ex: 'erro' fica
            # 'erro', alguem mais especializado decide se reprocessa). Quando
            # nao promove, reenvia o mesmo valor que ja tinha (no-op real).
            "status": "concluido" if alvo.get("status") == STATUS_BASICO else alvo.get("status"),
            "atualizado_em": _agora_iso(),
        }
        registros_upsert.append(registro)

    return registros_upsert, pulados_sem_linha, pulados_ambiguos


def _supabase_upsert_eventos_lote(registros):
    """Upsert em lote (on_conflict=numero_nf,marca, merge-duplicates) - so
    toca as colunas presentes em cada registro (mesmo mecanismo ja usado em
    titan_backfill.py/_supabase_upsert_lote), bem mais rapido que 1 PATCH por
    NF e da pra fazer em lotes de 200 igual a Etapa 1."""
    # Nao reusa titan_watcher._supabase_request pro POST aqui de proposito -
    # ele so manda "Prefer: return=minimal" no PATCH, nunca
    # "resolution=merge-duplicates" no POST (correto pra ele, que so faz
    # INSERT de linha nova - ver etapa1). Aqui o alvo e sempre uma linha JA
    # EXISTENTE (Etapa 1 quem cria) - sem esse Prefer, o POST bateria em
    # conflito de chave unica (numero_nf, marca) e falharia sempre. Mesmo
    # padrao ja usado em titan_backfill.py/_supabase_upsert_lote.
    gravados = 0
    for lote in _chunks(registros, 200):
        url = f"{titan_watcher.SUPABASE_URL}/rest/v1/{TABELA}?on_conflict=numero_nf,marca"
        data = json.dumps(lote).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("apikey", titan_watcher.SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {titan_watcher.SUPABASE_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            gravados += len(lote)
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[Etapa 2] ERRO no lote de upsert ({len(lote)} linha(s)): {corpo}", file=sys.stderr)
    return gravados


# Depositantes reais (consultados direto no infos_titan antes de escrever o
# script original, mesma fonte do DEPOSITANTE_MARCAS acima). Titan nao tem
# filtro de "marca" - Depositante e o mais fino que a tela oferece. Fatiar
# por isso (alem de por dia) reduz o volume por exportacao em ~5x.
DEPOSITANTES_PARA_FATIAR = ["APICE ES", "APICE RJ", "BEAUTY HUB RJ", "BEAUTY HUB ES", "GOBEAUTY ES"]


def _abrir_aba_e_frame(page):
    frame = scraper.get_dashboard_frame(page)
    try:
        _navegar_status_por_pedido(frame)
    except Exception:
        scraper.salvar_diagnostico(page, "titan_preencher_lacunas_aba_status_por_pedido")
        raise
    return frame


def _filtrar_depositante(frame, valor):
    """
    Mesmo padrao ja usado em scraper.filtrar() pra "Nota Fiscal de Saída"/
    "Número do pedido" - so que pro campo "Depositante", que nao tinha
    nenhum uso ainda neste projeto. "Depositante" nao esta no mapa de
    aria-label tecnico (ARIA_LABEL_POR_ROTULO), mas abrir_dropdown_filtro ja
    tem fallback por coordenada pra rotulos desconhecidos - mesmo mecanismo
    que definir_periodo ja usa com sucesso pro campo de data.
    """
    campo = scraper._abrir_dropdown_e_pegar_campo_busca(frame, "Depositante")
    scraper.digitar_busca(campo, valor)
    time.sleep(1)
    scraper.marcar_item_da_lista(frame, campo, valor)
    time.sleep(1)
    frame.page.keyboard.press("Escape")
    scraper._esperar_tabela_refletir_filtro(frame, valor)


def etapa2_exportar_eventos_fatia(page, dia, depositante):
    """
    Uma fatia (dia, depositante) = 1 pagina recarregada do zero. NAO
    reaproveita o frame entre fatias de proposito - reusar o mesmo frame e so
    trocar o filtro de Depositante arrisca acumular selecoes anteriores nos
    checkboxes do slicer (Power BI), o mesmo motivo pelo qual
    titan_watcher.py ja recarrega o dashboard inteiro entre pedidos em vez de
    so trocar o filtro de NF. Mais lento, mas correto - e o volume por fatia
    ja cabe tranquilo num timeout normal.
    """
    frame = _abrir_aba_e_frame(page)
    data_str = dia.strftime("%d/%m/%Y")
    _definir_periodo_evento(frame, data_str, data_str)
    _filtrar_depositante(frame, depositante)
    caminho = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
    # scraper.ler_export_xlsx devolve (filtro_aplicado, registros) neste
    # repo (Titan-v2) - filtro_aplicado nao e usado aqui de proposito, essa
    # validacao ja e feita a outro nivel (titan_backfill.py) e portar ela
    # tambem pra ca fugiria do escopo desta portagem.
    _filtro_aplicado, registros = scraper.ler_export_xlsx(caminho)
    try:
        caminho.unlink()
    except OSError:
        pass
    return _checar_truncamento(registros)


def etapa2_rodar(page, dias):
    hoje = datetime.date.today()
    total_atualizados = 0
    total_pulados_sem_linha = 0
    total_pulados_ambiguos = 0
    for i in range(dias, 0, -1):
        dia = hoje - datetime.timedelta(days=i)
        registros_do_dia = []
        for depositante in DEPOSITANTES_PARA_FATIAR:
            try:
                registros = etapa2_exportar_eventos_fatia(page, dia, depositante)
            except Exception as e:
                print(f"[Etapa 2] {dia.strftime('%d/%m/%Y')} / {depositante}: ERRO, pulando esta fatia - {e}", file=sys.stderr)
                continue
            registros_do_dia.extend(registros)
            print(f"[Etapa 2] {dia.strftime('%d/%m/%Y')} / {depositante}: {len(registros)} linha(s) de evento.")

        por_nf = _agrupar_eventos_por_nf(registros_do_dia)
        upsert, sem_linha, ambiguos = etapa2_processar_lote(por_nf)
        gravados = _supabase_upsert_eventos_lote(upsert)
        total_atualizados += gravados
        total_pulados_sem_linha += sem_linha
        total_pulados_ambiguos += ambiguos
        print(f"[Etapa 2] {dia.strftime('%d/%m/%Y')}: {len(registros_do_dia)} linha(s) de evento no total, "
              f"{len(por_nf)} NF(s), {gravados} atualizada(s). (acumulado: {total_atualizados})")

    print(f"[Etapa 2] Concluido - {total_atualizados} linha(s) atualizada(s) com eventos em {dias} dia(s).")
    if total_pulados_sem_linha:
        print(f"[Etapa 2] {total_pulados_sem_linha} NF(s) sem nenhuma linha correspondente no infos_titan (rode a Etapa 1 pro mesmo periodo antes).")
    if total_pulados_ambiguos:
        print(f"[Etapa 2] {total_pulados_ambiguos} NF(s) pulada(s) por colisao entre marcas que o Depositante nao resolveu sozinho.")


# Achado ao vivo no script original: mesmo com o fatiamento por dia+
# depositante, algum pedido pode nunca aparecer no export do Titan (foi
# EXCLUIDO de la, um erro de digitacao na NF, etc.) e ficar preso pra sempre
# sem situacao. Pedidos assim, se ja passaram tempo suficiente e NAO estao
# marcados como CANCELADO, sao quase sempre EMBARCADO na pratica (o CD nao
# costuma deixar pedido parado indefinidamente sem cancelar) - assumir isso
# destrava o pedido em vez de deixa-lo "pendente_titan" pra sempre. ISSO E
# UMA INFERENCIA, NAO UM DADO CONFIRMADO - nao ha coluna hoje em infos_titan
# pra distinguir "EMBARCADO confirmado pelo Titan" de "EMBARCADO assumido por
# este fallback" (nao inventar uma sem confirmar com a Maria se algo mais
# depende do schema atual). Se isso importar no futuro, revisitar.
DIAS_MINIMO_PARA_ASSUMIR_EMBARCADO = 5


def aplicar_fallback_embarcado(dias_minimo=DIAS_MINIMO_PARA_ASSUMIR_EMBARCADO):
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        - datetime.timedelta(days=dias_minimo)
    ).isoformat() + "Z"
    # So pedidos que a Etapa 1 cadastrou (status ainda no basico) e que
    # continuam sem "situacao" nenhuma - nunca mexe em 'erro' (precisa
    # investigacao, nao suposicao) nem em 'pendente' (fila normal do
    # titan_watcher.py, processo diferente).
    #
    # atualizado_em, nao solicitado_em (achado ao vivo no script original):
    # o filtro combinado com solicitado_em deu timeout de query numa tabela
    # deste tamanho (190 mil+ linhas) - atualizado_em e o mesmo campo que
    # titan_backfill.buscar_situacao_presa ja usa com sucesso num filtro
    # parecido, e respondeu rapido no teste real.
    path = (
        f"{TABELA}?status=eq.{STATUS_BASICO}&situacao=is.null"
        f"&atualizado_em=lt.{urllib.parse.quote(cutoff)}"
        f"&select=numero_nf,marca&limit=5000"
    )
    candidatos = titan_watcher._supabase_request("GET", path) or []
    print(f"[Fallback] {len(candidatos)} pedido(s) sem situacao ha mais de {dias_minimo} dia(s) - assumindo EMBARCADO.")
    if not candidatos:
        return 0
    agora = _agora_iso()
    registros = [{
        "numero_nf": c["numero_nf"],
        "marca": c["marca"],
        "situacao": "EMBARCADO",
        "status": "concluido",
        "atualizado_em": agora,
    } for c in candidatos]
    gravados = _supabase_upsert_eventos_lote(registros)
    print(f"[Fallback] {gravados} pedido(s) marcado(s) como EMBARCADO (assumido, nao confirmado pelo Titan).")
    return gravados


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dias", type=int, default=30, help="janela de dias corridos pra tras (padrao: 30)")
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    parser.add_argument("--so-etapa1", action="store_true", default=False, help="so cadastra o basico via Intelipost, nao abre o Titan")
    parser.add_argument("--so-etapa2", action="store_true", default=False, help="so completa eventos via Titan, nao usa o Metabase")
    parser.add_argument("--sem-fallback", action="store_true", default=False, help="nao roda o fallback que assume EMBARCADO pra pedidos antigos sem situacao (ver aplicar_fallback_embarcado)")
    parser.add_argument("--fallback-dias-minimo", type=int, default=DIAS_MINIMO_PARA_ASSUMIR_EMBARCADO, help=f"so assume EMBARCADO pra pedidos sem situacao ha pelo menos N dias (padrao: {DIAS_MINIMO_PARA_ASSUMIR_EMBARCADO})")
    args = parser.parse_args()

    if not args.so_etapa2:
        etapa1_rodar(args.dias)

    if args.so_etapa1:
        return

    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar a Etapa 2.", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            scraper.login(page, email, senha)
            etapa2_rodar(page, args.dias)
        finally:
            browser.close()

    if not args.sem_fallback:
        aplicar_fallback_embarcado(args.fallback_dias_minimo)


if __name__ == "__main__":
    main()
