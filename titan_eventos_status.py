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

FILTRO DE DATA (pedido direto da Maria, 28/09/2026): so Data Inicial =
ontem (mesmo "dia anterior" que titan_backfill.py usa) - Data Final fica
com o valor padrao que a propria tela do Titan ja mostra (hoje), sem
digitar nada nesse campo ("Não precisa selecionar a data final") - ver
_definir_apenas_data_inicial abaixo, variante de
titan_preencher_lacunas._definir_periodo_evento que pula a digitacao da
Data Final.

MESCLA EM VEZ DE SUBSTITUIR (pedido direto da Maria, 28/09/2026): como o
filtro e sempre "a partir de ontem", cada exportacao so traz eventos cuja
PROPRIA data cai nessa janela - um evento "IMPORTADO" de 5 dias atras nao
aparece mais numa exportacao filtrada so a partir de ontem. Se cada rodada
SUBSTITUISSE a coluna evento inteira, eventos antigos que ja tinham sido
capturados em rodadas passadas seriam apagados assim que saissem da janela.
Por isso _buscar_eventos_existentes busca o evento[] ja salvo pra cada
pedido deste lote ANTES de gravar, e _mesclar_eventos combina com o que
vier da exportacao nova (sem duplicar por (Situação, Horário da Situação)),
preservando o historico completo pra sempre.

RODA A CADA 30 MINUTOS (GitHub Actions, titan_eventos_status.yml) - mesma
cadencia do titan_watcher.py.

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


def _exportar_eventos(page, data_inicial):
    frame = scraper.get_dashboard_frame(page)
    try:
        lacunas._navegar_status_por_pedido(frame)
    except Exception:
        scraper.salvar_diagnostico(page, "titan_eventos_status_aba_status_por_pedido")
        raise
    data_str = data_inicial.strftime("%d/%m/%Y")
    _definir_apenas_data_inicial(frame, data_str)
    caminho = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
    # scraper.ler_export_xlsx devolve (filtro_aplicado, registros) neste
    # repo - filtro_aplicado nao e usado aqui de proposito, mesmo criterio
    # ja aplicado em titan_preencher_lacunas.etapa2_exportar_eventos_fatia.
    _filtro_aplicado, registros = scraper.ler_export_xlsx(caminho)
    try:
        caminho.unlink()
    except OSError:
        pass
    return lacunas._checar_truncamento(registros)


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


def _upsert_lote(registros):
    gravados = 0
    for lote in lacunas._chunks(registros, 200):
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
            gravados += len(lote)
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", errors="replace")[:500]
            print(f"ERRO no lote de upsert ({len(lote)} linha(s)): {corpo}", file=sys.stderr)
    return gravados


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    args = parser.parse_args()

    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar.", file=sys.stderr)
        sys.exit(1)

    ontem = datetime.date.today() - datetime.timedelta(days=1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            print("Entrando no Titan BI...")
            scraper.login(page, email, senha)
            print(f"Exportando 'Status por Pedido' a partir de {ontem.strftime('%d/%m/%Y')}...")
            registros = _exportar_eventos(page, ontem)
        finally:
            browser.close()

    print(f"{len(registros)} linha(s) de evento exportada(s).")
    por_pedido = _agrupar_por_pedido(registros)
    print(f"{len(por_pedido)} pedido(s) distinto(s) nesta exportacao.")
    if not por_pedido:
        print("Concluido - nada pra gravar.")
        return

    existentes = _buscar_eventos_existentes(list(por_pedido.keys()))
    payloads = _montar_payloads(por_pedido, existentes)
    gravados = _upsert_lote(payloads)
    print(f"Concluido - {gravados} pedido(s) gravado(s)/atualizado(s) em {TABELA}.")


if __name__ == "__main__":
    main()
