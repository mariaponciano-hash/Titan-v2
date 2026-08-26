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
marca da mesma NF). O "Numero do Pedido" continua sem correspondencia com o
numero de e-commerce da Torre - nunca usado como chave.
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import titan_bi_scraper as scraper

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"
TAMANHO_LOTE = 200  # registros por chamada ao Supabase - evita 1 request por pedido
PASTA_EXPORTS = Path(__file__).parent / "titan_exports"  # so um local de trabalho - o arquivo e apagado apos o upload


def _agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _supabase_upsert_lote(registros):
    """
    Upsert em lote, on_conflict=numero_nf. O Postgres/PostgREST so atualiza
    as colunas presentes no JSON enviado quando ha conflito (resolution=
    merge-duplicates) - como este script NAO envia numero_pedido/eventos/
    itens, um pedido que ja tenha esses campos preenchidos por uma consulta
    avulsa anterior (titan_watcher.py) NAO tem esses campos apagados por um
    backfill rodado depois. So romaneio/situacao/datas/etc. sao sobrescritos
    (o que e o esperado - o backfill sempre traz o dado mais recente do Titan
    pra esses campos).
    """
    if not registros:
        return
    url = f"{SUPABASE_URL}/rest/v1/{TABELA}?on_conflict=numero_nf,marca"
    data = json.dumps(registros).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def fechar_popup_calendario(frame, tentativas=5):
    """
    CONFIRMADO COM ERRO REAL (24/08/2026): um Escape sozinho as vezes NAO
    fecha o popup do calendario - ele e um overlay do Angular Material (CDK),
    e sobra um <div class="cdk-overlay-backdrop..."> TRANSPARENTE cobrindo a
    tela inteira, que intercepta qualquer clique/hover seguinte (erro real:
    "cdk-overlay-backdrop... subtree intercepts pointer events", travando
    coletar_todos_registros no primeiro hover). CDK overlays fecham ao
    clicar no proprio backdrop - entao clica nele (nao so aperta Escape) e
    confirma que sumiu antes de seguir.
    """
    for _ in range(tentativas):
        backdrop = frame.locator(".cdk-overlay-backdrop")
        if backdrop.count() == 0:
            return
        try:
            backdrop.first.click(timeout=1000, force=True)
        except Exception:
            frame.page.keyboard.press("Escape")
        time.sleep(0.3)
    if frame.locator(".cdk-overlay-backdrop").count() > 0:
        raise PWTimeout("cdk-overlay-backdrop nao fechou depois de varias tentativas")


def definir_periodo(frame, data_inicial, data_final):
    """
    CONFIRMADO COM TESTE REAL (24/08/2026, periodo 01/06-23/08/2026): o
    clique-e-digita abaixo ACERTA os dois campos internos do slicer ("Data de
    inicio"/"Data de termino", confirmados via aria-label) - o bug real do
    "0 encontrados" nao era o valor setado, era ler a tabela cedo demais.
    Sem fechar o popup (Escape) e sem esperar a query terminar, a tabela fica
    vazia por varios segundos depois de mudar o periodo. Corrigido esperando
    de verdade a 1a linha aparecer em vez de um sleep fixo.
    """
    try:
        rotulo = scraper.elemento_visivel(frame.get_by_text("Data Inicial - Data Final", exact=False))
        caixa = rotulo.bounding_box()
        if not caixa:
            raise PWTimeout("rotulo 'Data Inicial - Data Final' visivel mas sem bounding_box (layout inesperado)")
        x = caixa["x"] + caixa["width"] / 2
        y = caixa["y"] + caixa["height"] + 15
        frame.page.mouse.click(x, y)
        time.sleep(0.5)
        frame.page.keyboard.press("Control+A")
        frame.page.keyboard.type(data_inicial, delay=60)
        frame.page.keyboard.press("Tab")
        time.sleep(0.3)
        frame.page.keyboard.press("Control+A")
        frame.page.keyboard.type(data_final, delay=60)
        frame.page.keyboard.press("Enter")
        time.sleep(0.5)
        fechar_popup_calendario(frame)

        painel = scraper.localizar_painel(frame, "Informação Pedido")
        painel.locator("xpath=.//*[self::tr or @role='row']").first.wait_for(timeout=30000)
        time.sleep(1.5)  # da tempo da query terminar de popular as linhas visiveis, nao so a 1a
    except PWTimeout:
        scraper.salvar_diagnostico(frame, "set_filtro_data_nao_encontrado")
        raise


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

    NAO mapeia "Numero do Pedido" pra dentro da coluna numero_pedido de
    proposito: essa coluna e escrita por /api/logistica/titan-solicitar com o
    numero de e-commerce da Torre (ex: "SH1099815RT"), enquanto o "Numero do
    Pedido" do Titan e um numero interno do armazem sem correspondencia com
    aquele (ver comentario grande no topo do arquivo e em titan_watcher.py) -
    escrever aqui sobrescreveria o valor certo pelo errado.
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
    return {
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-inicial", required=True, help="formato DD/MM/AAAA, ex: 01/06/2026")
    parser.add_argument("--data-final", required=True, help="formato DD/MM/AAAA, ex: 24/08/2026")
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    args = parser.parse_args()

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
            frame = scraper.get_dashboard_frame(page)

            print(f"Definindo periodo {args.data_inicial} - {args.data_final}...")
            definir_periodo(frame, args.data_inicial, args.data_final)

            print("Exportando dados do painel 'Informação Pedido'...")
            caminho_export = scraper.exportar_dados_do_painel(frame, "Informação Pedido", PASTA_EXPORTS)
            print(f"  baixado em {caminho_export}")
            registros = scraper.ler_export_xlsx(caminho_export)
            try:
                caminho_export.unlink()
            except OSError:
                pass  # nao critico - so um arquivo de trabalho
            print(f"{len(registros)} linha(s) encontradas no periodo.")
            if not registros:
                print("Nenhum registro encontrado - confira se o periodo esta certo (ver print salvo, se houver).")
                return

            # O Power BI tem um limite de volume por exportacao - descoberto
            # com uma exportacao real (25/08/2026) que voltou com uma linha
            # extra so com este aviso no lugar de um pedido de verdade. Sem
            # essa checagem, um periodo grande demais perderia pedidos SEM
            # nenhum sinal de que algo ficou de fora.
            if any("Exported data exceeded the allowed volume" in str(r.get("Depositante") or "") for r in registros):
                print(f"\n  AVISO: o Titan/Power BI truncou esta exportacao (limite de volume excedido) - "
                      f"o periodo {args.data_inicial} a {args.data_final} tem mais dados do que a exportacao "
                      f"trouxe de uma vez so. Pedidos podem estar faltando. Rode de novo quebrando esse "
                      f"periodo em blocos menores (ex: mes a mes) pra cobrir tudo.", file=sys.stderr)

            lote = []
            gravados = 0
            ignorados = 0
            for r in registros:
                payload = registro_para_supabase(r)
                if payload is None:
                    ignorados += 1
                    continue
                lote.append(payload)
                if len(lote) >= TAMANHO_LOTE:
                    _supabase_upsert_lote(lote)
                    gravados += len(lote)
                    print(f"  {gravados} gravados...")
                    lote = []
            if lote:
                _supabase_upsert_lote(lote)
                gravados += len(lote)

            print(f"\nConcluido - {gravados} pedido(s) gravados no Supabase.")
            if ignorados:
                print(f"{ignorados} linha(s) ignoradas por falta de Nota Fiscal ou de \"Nome Projeto\" "
                      f"(marca) - sem os dois, nao da pra identificar com seguranca.")
            print("Eventos/Itens NAO foram trazidos por este backfill (ver LIMITACAO no topo do arquivo) - "
                  "continuam vindo via titan_watcher.py quando a Torre precisar de um pedido especifico.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
