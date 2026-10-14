"""
titan_eventos_status.py

Alimenta a tabela NOVA eventos_titan (28/09/2026, pedido direto da Maria)
com o historico de eventos por pedido, direto da aba "Exportação - Status
por Pedido" do Titan BI - mesma aba, mesma navegacao e mesmo filtro de data
ja escritos em titan_preencher_lacunas.py (funcoes REUSADAS daqui via
import, nao duplicadas: _navegar_status_por_pedido, _checar_truncamento).

TABELA NOVA, SEPARADA de infos_titan - nao tenta casar com marca/
depositante nem escrever em infos_titan.eventos (aquele mecanismo saiu de
escopo em 16/09/2026, ver ITENS VIA METABASE em titan_backfill.py).
eventos_titan e chaveada por (nota_fiscal_saida_numero, numero) - MAIS FINA
que infos_titan (numero_nf, marca), entao NAO tem o problema de colisao
entre marcas que titan_preencher_lacunas.etapa2_processar_lote precisa
resolver via Depositante: cada (nota_fiscal_saida_numero, numero) e uma
chave unica sem ambiguidade, "marca" nem entra na conta aqui.

FORMATO DA COLUNA evento: mesmo padrao ja usado em infos_titan.eventos -
lista de dicts {"Situação": ..., "Horário da Situação": ...} (ver
scraper.extrair_eventos, que le a mesma informacao pedido-por-pedido no
Titan em vez de via exportacao em lote).

FILTRO DE DATA (corrigido 14/10/2026): o filtro "Data Inicial - Data Final"
desta aba e a data de IMPORTACAO do pedido, nao a data do evento, e a Data
Final e sempre hoje - cada exportacao traz o historico completo de eventos
dos pedidos importados de Data Inicial ate hoje (ver
_exportar_janela_por_depositante). A rodada usa Data Inicial = hoje - 2 dias
(--dias 2, padrao) e fatia por Depositante pra ficar abaixo do teto de 150 mil
linhas do Power BI. O workflow roda de hora em hora.

MESCLA EM VEZ DE SUBSTITUIR (pedido direto da Maria, 28/09/2026): a coluna
evento nunca e substituida - _buscar_eventos_existentes busca o evento[] ja
salvo pra cada pedido deste lote ANTES de gravar, e _mesclar_eventos combina
com o que vier da exportacao nova (sem duplicar por (Situação, Horário da
Situação)), preservando o historico completo pra sempre.

RODA DE HORA EM HORA (GitHub Actions, titan_eventos_status.yml).

⚠️ NAVEGACAO ATE A ABA "Exportação - Status por Pedido" HERDA O MESMO AVISO
de titan_preencher_lacunas.py: nunca validada ao vivo por mim (sem acesso
ao Titan) antes desta escrita - RECOMENDADO rodar `python
titan_eventos_status.py` na sua maquina, SEM --headless, antes de confiar
na primeira rodada agendada. Se travar, o diagnostico cai no artifact
titan_debug de sempre.

USO
    set TITAN_EMAIL=sacgobeauty@unilog.com
    set TITAN_SENHA=sua-senha-aqui
    python titan_eventos_status.py
    python titan_eventos_status.py --headless
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import titan_bi_scraper as scraper
import titan_preencher_lacunas as lacunas  # reusa _navegar_status_por_pedido/_checar_truncamento/_chunks
import titan_watcher  # reusa _supabase_request/SUPABASE_URL/SUPABASE_KEY

TABELA = "eventos_titan"
PASTA_EXPORTS = Path(__file__).parent / "titan_exports"


def _hoje_brt():
    """Dia de hoje em Brasilia (o runner do GitHub esta em UTC; Brasil nao
    tem horario de verao desde 2019, entao o offset -3h e fixo)."""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).date()


def _agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _definir_apenas_data_inicial(frame, data_inicial):
    """
    Variante de titan_preencher_lacunas._definir_periodo_evento que so
    digita a Data Inicial, deixando a Data Final como a propria tela ja
    mostra (nao digita nada nesse campo - pedido direto da Maria,
    28/09/2026: "Não precisa selecionar a data final"). Mesma sequencia de
    cliques/Tab/Enter e o mesmo cuidado com o popup do calendario (ver
    docstring grande de _definir_periodo_evento em titan_preencher_
    lacunas.py) - o Tab move o foco pro campo Data Final SEM apagar o que
    ja estava digitado la (Ctrl+A + digitar so acontece no campo Data
    Inicial aqui).
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
        frame.page.keyboard.press("Tab")  # move pro campo Data Final, sem digitar nada nele
        time.sleep(0.3)
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
        scraper.salvar_diagnostico(frame, "titan_eventos_status_filtro_data_inicial")
        raise


def _abrir_aba_status(page):
    frame = scraper.get_dashboard_frame(page)
    try:
        lacunas._navegar_status_por_pedido(frame)
    except Exception:
        scraper.salvar_diagnostico(page, "titan_eventos_status_aba_status_por_pedido")
        raise
    return frame


def _exportar_uma(frame, dia, rotulo_arquivo, depositante=None):
    """Define o periodo (e, se pedido, o Depositante), exporta e le UM xlsx."""
    data_str = dia.strftime("%d/%m/%Y")
    lacunas._definir_periodo_evento(frame, data_str, data_str)
    if depositante:
        lacunas._filtrar_depositante(frame, depositante)
    caminho = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
    # scraper.ler_export_xlsx devolve (filtro_aplicado, registros) neste
    # repo - filtro_aplicado nao e usado aqui de proposito, mesmo criterio
    # ja aplicado em titan_preencher_lacunas.etapa2_exportar_eventos_fatia.
    _filtro_aplicado, registros = scraper.ler_export_xlsx(caminho)
    truncou = scraper.ultimo_aviso_exportacao is not None
    # O xlsx fica em titan_exports/ (sobe como artifact no workflow) pra
    # conferir a exportacao - 14/10/2026, pedido direto da Maria.
    try:
        caminho.rename(PASTA_EXPORTS / f"eventos_{rotulo_arquivo}.xlsx")
    except OSError:
        pass
    return lacunas._checar_truncamento(registros), truncou


def _exportar_janela_por_depositante(page, data_inicial, hoje):
    """
    Exporta 'Informação Pedido' da aba Status por Pedido, uma vez por
    Depositante, com Data Inicial = data_inicial (a Data Final e sempre hoje).

    ACHADOS REAIS (14/10/2026, planilhas da run #78):
    - O filtro de data desta aba e a data de IMPORTACAO do pedido, nao a do
      evento: a exportacao traz o historico completo de eventos dos pedidos
      importados de data_inicial ate hoje. Por isso "a partir de hoje" nunca
      atualizava o evento de um pedido importado antes (ficava preso no
      ultimo evento que ele tinha na epoca).
    - A "Data Final" nao e configuravel (sempre hoje), entao nao da pra
      exportar um dia isolado: cada exportacao e "de D ate hoje".
    - O teto de 150 mil linhas do Power BI corta a exportacao; as linhas
      saem em ordem de NF crescente, entao o corte derruba as NFs mais NOVAS.
      3 dias de todos os depositantes juntos passam do teto (o arquivo de
      D = hoje-2 veio com exatamente 150.000 linhas). Por isso fatia por
      Depositante (DEPOSITANTES_PARA_FATIAR, mesmo criterio de
      titan_preencher_lacunas.etapa2_exportar_eventos_fatia) - cada fatia
      reabre o dashboard do zero (get_dashboard_frame faz page.goto) pra
      nenhum filtro vazar pra proxima.

    Devolve [(depositante, registros)] das fatias que deram certo.
    """
    nome = data_inicial.strftime("%d-%m-%Y")
    fatias = []
    for dep in lacunas.DEPOSITANTES_PARA_FATIAR:
        try:
            frame = _abrir_aba_status(page)
            lacunas._definir_periodo_evento(frame, data_inicial.strftime("%d/%m/%Y"), hoje.strftime("%d/%m/%Y"))
            lacunas._filtrar_depositante(frame, dep)
            caminho = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
            # scraper.ler_export_xlsx devolve (filtro_aplicado, registros) neste
            # repo - filtro_aplicado nao e usado aqui de proposito.
            _filtro_aplicado, registros = scraper.ler_export_xlsx(caminho)
            truncou = scraper.ultimo_aviso_exportacao is not None
            # O xlsx fica em titan_exports/ (sobe como artifact no workflow)
            # pra conferir a exportacao - pedido direto da Maria, 14/10/2026.
            try:
                caminho.rename(PASTA_EXPORTS / f"eventos_{nome}_{dep.replace(' ', '_')}.xlsx")
            except OSError:
                pass
            registros = lacunas._checar_truncamento(registros)
        except Exception as e:
            print(f"ERRO na fatia {dep}, pulando - {e}", file=sys.stderr)
            continue
        print(f"  {dep}: {len(registros)} linha(s){' - TRUNCOU (as NFs mais novas desta fatia ficaram de fora)' if truncou else ''}.")
        if truncou:
            print(f"AVISO: fatia {dep} truncada pelo Power BI - reduza --dias.", file=sys.stderr)
        fatias.append((dep, registros))
    return fatias


def _agrupar_por_pedido(registros):
    """
    Agrupa por (nota_fiscal_saida_numero, numero) - a chave desta tabela.
    Sem conceito de marca aqui, entao sem colisao pra resolver (diferente de
    titan_preencher_lacunas.etapa2_processar_lote, que precisa do
    Depositante pra desempatar quando a mesma NF aparece em mais de uma
    marca dentro de infos_titan).
    """
    por_pedido = {}
    for r in registros:
        nf = str(r.get("nota_fiscal_saida_numero") or r.get("Nota Fiscal de Saída") or "").strip()
        numero = str(r.get("numero") or r.get("Número") or "").strip()
        evento = str(r.get("Evento") or "").strip()
        data_evento = r.get("Data_Evento") or r.get("Data Evento")
        depositante = str(r.get("Depositante") or "").strip()
        if not nf or not numero or not evento:
            continue
        chave = (nf, numero)
        if chave not in por_pedido:
            por_pedido[chave] = {"depositante": depositante, "eventos": []}
        por_pedido[chave]["eventos"].append({
            "Situação": evento,
            "Horário da Situação": str(data_evento) if data_evento is not None else "",
        })
    return por_pedido


def _buscar_eventos_existentes(chaves):
    """
    Busca o evento[] ja salvo pra cada (nota_fiscal_saida_numero, numero)
    deste lote, pra mesclar com o que vier da exportacao nova - ver MESCLA
    EM VEZ DE SUBSTITUIR no topo do arquivo (decisao de 28/09/2026, pra
    nunca perder evento antigo que saia da janela "a partir de ontem").
    """
    existentes = {}
    nfs = sorted({nf for nf, _ in chaves})
    for lote in lacunas._chunks(nfs, 200):
        filtro = ",".join(urllib.parse.quote(nf) for nf in lote)
        path = f"{TABELA}?nota_fiscal_saida_numero=in.({filtro})&select=nota_fiscal_saida_numero,numero,evento"
        for row in (titan_watcher._supabase_request("GET", path) or []):
            existentes[(row["nota_fiscal_saida_numero"], row["numero"])] = row.get("evento") or []
    return existentes


def _mesclar_eventos(existentes, novos):
    """Combina o evento[] ja salvo com os capturados nesta rodada, sem
    duplicar por (Situação, Horário da Situação) repetido, ordenado por
    horario (string - mesmo formato "DD/MM/AAAA HH:MM:SS" que o Titan ja usa,
    ordena certo como texto dentro do mesmo ano)."""
    vistos = {(e.get("Situação"), e.get("Horário da Situação")) for e in existentes}
    combinados = list(existentes)
    for e in novos:
        chave = (e["Situação"], e["Horário da Situação"])
        if chave not in vistos:
            combinados.append(e)
            vistos.add(chave)
    combinados.sort(key=lambda e: e.get("Horário da Situação") or "")
    return combinados


def _montar_payloads(por_pedido, existentes):
    agora = _agora_iso()
    payloads = []
    for (nf, numero), dados in por_pedido.items():
        evento_final = _mesclar_eventos(existentes.get((nf, numero), []), dados["eventos"])
        payloads.append({
            "nota_fiscal_saida_numero": nf,
            "numero": numero,
            "depositante": dados["depositante"],
            "evento": evento_final,
            "atualizado_em": agora,
        })
    return payloads


def _upsert_um_lote(lote):
    """POST de um lote; devolve (gravados, erro_texto|None)."""
    url = f"{titan_watcher.SUPABASE_URL}/rest/v1/{TABELA}?on_conflict=nota_fiscal_saida_numero,numero"
    data = json.dumps(lote).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("apikey", titan_watcher.SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {titan_watcher.SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return len(lote), None
    except urllib.error.HTTPError as e:
        return 0, e.read().decode("utf-8", errors="replace")[:500]
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, str(e)


def _upsert_lote_dividindo(lote):
    """
    Cada linha gravada aqui dispara triggers no Supabase (sync pra
    infos_titan, regra da situacao, espelho de volta) e o papel anon tem
    statement_timeout de 3s - um lote de 200 estourou (57014) varias vezes
    em 14/10/2026 e essas linhas se perdiam. Ao falhar, divide o lote ao
    meio e tenta de novo, ate chegar numa linha so.
    """
    gravados, erro = _upsert_um_lote(lote)
    if erro is None:
        return gravados
    if len(lote) == 1:
        print(f"ERRO gravando 1 linha ({lote[0].get('nota_fiscal_saida_numero')}/{lote[0].get('numero')}): {erro}", file=sys.stderr)
        return 0
    meio = len(lote) // 2
    return _upsert_lote_dividindo(lote[:meio]) + _upsert_lote_dividindo(lote[meio:])


def _upsert_lote(registros):
    gravados = 0
    for lote in lacunas._chunks(registros, 100):
        gravados += _upsert_lote_dividindo(lote)
    return gravados


def _gravar_registros(registros):
    por_pedido = _agrupar_por_pedido(registros)
    print(f"  {len(registros)} linha(s) de evento, {len(por_pedido)} pedido(s) distinto(s).")
    if not por_pedido:
        return 0
    existentes = _buscar_eventos_existentes(list(por_pedido.keys()))
    payloads = _montar_payloads(por_pedido, existentes)
    return _upsert_lote(payloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    parser.add_argument("--dias", type=int, default=2, help="Data Inicial = hoje - N dias (padrao 2 = hoje e os 2 dias anteriores; maximo 8). Mais dias = fatias que podem passar de 150 mil linhas")
    args = parser.parse_args()
    dias = max(0, min(args.dias, 8))

    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar.", file=sys.stderr)
        sys.exit(1)

    hoje = _hoje_brt()
    data_inicial = hoje - datetime.timedelta(days=dias)
    print(f"Janela de Data Importado: {data_inicial.strftime('%d/%m/%Y')} ate {hoje.strftime('%d/%m/%Y')} ({dias} dia(s) antes de hoje), fatiada por Depositante.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            print("Entrando no Titan BI...")
            scraper.login(page, email, senha)
            fatias = _exportar_janela_por_depositante(page, data_inicial, hoje)
        finally:
            browser.close()

    total_gravados = 0
    for dep, registros in fatias:
        # grava cada fatia separadamente (a mescla com o que ja existe e por pedido)
        print(f"Gravando {dep}...")
        total_gravados += _gravar_registros(registros)

    print(f"Concluido - {total_gravados} pedido(s) gravado(s)/atualizado(s) em {TABELA} "
          f"({len(fatias)}/{len(lacunas.DEPOSITANTES_PARA_FATIAR)} fatia(s) exportada(s)).")
    if not fatias:
        sys.exit(1)


if __name__ == "__main__":
    main()
