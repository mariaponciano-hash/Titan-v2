import argparse
import datetime
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

import titan_bi_scraper as scraper

# Mesmo projeto Supabase que a Torre usa (ver TITAN_SB_URL em src/server.ts) -
# chave "publishable" separada, gerada so pra esta tabela. Nao e credencial de
# login de ninguem, e uma chave de API (mesma categoria das chaves da
# Intelipost/Metabase ja usadas no projeto).
SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"
INTERVALO_POLL_SEGUNDOS = 15
ORCAMENTO_UMA_VEZ_SEGUNDOS_PADRAO = 240  # 4min - ver rodar_fila_uma_vez/--uma-vez


def _agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _supabase_request(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    if method == "PATCH":
        req.add_header("Prefer", "return=minimal")
    with urllib.request.urlopen(req, timeout=15) as resp:
        corpo = resp.read()
        return json.loads(corpo) if corpo else None


PENDENTES_MAX_POR_BUSCA = 500  # so um teto de payload/memoria, nao um limite real de processamento (ver rodar_fila_uma_vez)


def buscar_pendentes():
    # order=solicitado_em.asc (28/08/2026, achado real - NF 1295282 ficou 3
    # dias parada como 'pendente' porque, sem ordenacao explicita, o Postgres/
    # PostgREST nao garante nenhuma ordem - um pedido antigo podia ficar
    # "escondido" atras de pedidos mais novos indefinidamente. Mais antigo
    # primeiro = justo (FIFO), mesmo criterio ja usado no recheck do
    # titan_cf_worker.
    return _supabase_request(
        "GET",
        f"{TABELA}?status=eq.pendente&select=numero_pedido,numero_nf,marca"
        f"&order=solicitado_em.asc&limit={PENDENTES_MAX_POR_BUSCA}",
    ) or []


def marcar_erro(numero_nf, marca, mensagem):
    try:
        # urllib.parse.quote() na marca: achado real (04/09/2026, NF 888537/
        # by samia) - marca com espaco no nome ("by samia") vira URL invalida
        # sem encoding ("URL can't contain control characters"), o
        # urllib.request.urlopen JOGA EXCECAO na hora de montar a request -
        # cai direto no except abaixo, que so imprime e engole. Resultado:
        # a linha nunca sai de 'pendente', reprocessada a cada 30min pra
        # sempre falhar do mesmo jeito, silenciosamente. titan_romaneio_links.
        # py ja fazia esse quote corretamente - so faltava aqui.
        _supabase_request("PATCH", f"{TABELA}?numero_nf=eq.{urllib.parse.quote(numero_nf)}&marca=eq.{urllib.parse.quote(marca)}", {
            "status": "erro",
            "erro": str(mensagem)[:500],
            "atualizado_em": _agora_iso(),
        })
    except Exception as e:
        print(f"  (nao consegui nem marcar erro no Supabase: {e})", file=sys.stderr)


def marcar_nao_encontrado(numero_nf, marca, mensagem):
    """
    Igual a marcar_erro, mas ALEM disso limpa os campos operacionais
    (romaneio, situacao, eventos etc.) - achado real (03/09/2026, NF 888537):
    um match falso-positivo antigo (ja corrigido em titan_bi_scraper.
    _achar_linha_pedido - comparava contra o texto da linha inteira, nao a
    coluna certa) tinha gravado o romaneio de um pedido ERRADO pra marca
    apice nessa NF. Depois do fix, o recheck passou a corretamente marcar
    'erro' - mas marcar_erro sozinho so troca status/erro, sem limpar os
    dados velhos: a linha ficava com status='erro' e AINDA COM o romaneio/
    situacao/romaneio_link errados de antes, prontos pra enganar quem lesse
    direto esses campos sem checar o status. So usado no caminho de "pedido
    genuinamente nao encontrado" (nunca em excecao generica/transitoria,
    onde os dados anteriores podem continuar validos e nao devem ser
    apagados).
    """
    try:
        _supabase_request("PATCH", f"{TABELA}?numero_nf=eq.{urllib.parse.quote(numero_nf)}&marca=eq.{urllib.parse.quote(marca)}", {
            "status": "erro",
            "erro": str(mensagem)[:500],
            "situacao": None,
            "romaneio": None,
            "romaneio_link": None,
            "valor_pedido": None,
            "volume": None,
            "observacao": None,
            "nome_projeto": None,
            "nome_projeto_antigo": None,
            "data_importado": None,
            "data_expedido": None,
            "data_conferido": None,
            "depositante": None,
            "cliente": None,
            "eventos": None,
            "itens": None,
            "atualizado_em": _agora_iso(),
        })
    except Exception as e:
        print(f"  (nao consegui nem marcar erro no Supabase: {e})", file=sys.stderr)


def marcar_concluido(numero_nf, marca, pedido_data, eventos, itens):
    corpo = {
        "status": "concluido",
        "erro": None,
        # Depositante/Cliente ficam de fora de proposito - confirmado (JSON
        # real, 24/08/2026) que o Titan renderiza essas duas colunas sem
        # texto acessivel no DOM, mesmo aparecendo na tela. Precisaria de OCR.
        "situacao": pedido_data.get("Situação"),
        "romaneio": pedido_data.get("Romaneio") or None,
        "valor_pedido": pedido_data.get("Valor Pedido"),
        "volume": pedido_data.get("Volume"),
        "observacao": pedido_data.get("Observação"),
        "nome_projeto": pedido_data.get("Nome Projeto"),
        "nome_projeto_antigo": pedido_data.get("Nome Projeto (Antigo)"),
        "data_importado": pedido_data.get("Data Importado"),
        "data_expedido": pedido_data.get("Data Expedido"),
        "data_conferido": pedido_data.get("Data Conferido"),
        "eventos": eventos or [],
        "itens": itens or [],
        "atualizado_em": _agora_iso(),
    }
    # numero_pedido: so pra Gobeaute exceto apice (confirmado pela Ivna,
    # 04/09/2026 - ver docstring de scraper.extrair_numero_pedido_torre). So
    # inclui a chave quando conseguimos derivar - omitir (em vez de gravar
    # None) preserva um numero_pedido ja existente (ex: vindo de uma
    # solicitacao real pela Torre) se por algum motivo nao der pra derivar
    # dessa vez.
    numero_pedido_derivado = scraper.extrair_numero_pedido_torre(pedido_data)
    if numero_pedido_derivado:
        corpo["numero_pedido"] = numero_pedido_derivado
    _supabase_request("PATCH", f"{TABELA}?numero_nf=eq.{urllib.parse.quote(numero_nf)}&marca=eq.{urllib.parse.quote(marca)}", corpo)


def processar_pedido(page, item, permitir_limpar_dados=True):
    """
    `permitir_limpar_dados=False` (achado real, 11/09/2026, reportado pela
    Maria): titan_backfill.rechecar_situacoes_presas/rechecar_concluidos_
    sem_eventos chamam esta funcao em massa, sem supervisao, SO pra pedidos
    que ja tem status='concluido' com dado bom (romaneio/situacao/eventos/
    itens reais). Um "nao encontrado" nesse contexto e MUITO mais provavel
    de ser um problema transitorio do proprio scraping (sessao caiu no meio
    da rodada, frame nao carregou, Titan mudou de layout etc.) do que o
    pedido ter genuinamente sumido do Titan (pedido de WMS nao se apaga
    sozinho) - mas marcar_nao_encontrado APAGA os campos como se fosse
    definitivo. Resultado real observado: uma rodada do backfill (11/09/2026,
    run #22) passou por centenas de pedidos ja concluidos e zerou
    situacao/romaneio/valor_pedido/eventos/itens/etc. de varios deles, ao
    que tudo indica por uma falha ampla e transitoria (nao pedido por
    pedido) que fez toda uma sequencia dar "nao encontrado" em fila. Com
    False, um "nao encontrado" vira so um marcar_erro (preserva os campos
    ja gravados, so seta status/erro) - o dado antigo fica visivel e intacto
    ate alguem confirmar de verdade que o pedido sumiu. True (padrao) mantem
    o comportamento original pra quem processa a fila 'pendente' via
    rodar_fila_uma_vez/main() - ali o pedido nunca teve dado bom pra perder
    (ou, se foi reenfileirado, "nao encontrado agora" e um sinal mais direto
    porque veio de uma consulta avulsa, nao de uma varredura em massa).
    """
    # (numero_nf, marca) e a CHAVE (ver comentario grande no topo do arquivo
    # e no endpoint /api/logistica/titan-solicitar em server.ts) -
    # numero_pedido so fica de referencia, nunca usado pra buscar/atualizar
    # no Supabase nem pra desambiguar no Titan.
    numero_nf = (item.get("numero_nf") or "").strip()
    marca = (item.get("marca") or "").strip()
    numero_pedido = item.get("numero_pedido") or "(sem numero_pedido)"
    if not numero_nf:
        print(f"[{numero_pedido}] linha na fila sem NF - nao deveria ter chegado aqui (server.ts ja bloqueia isso).")
        return
    if not marca:
        print(f"[{numero_pedido}] linha na fila sem marca - nao deveria ter chegado aqui (server.ts ja bloqueia isso).")
        return

    print(f"[NF {numero_nf} / marca {marca} / pedido {numero_pedido}] consultando no Titan BI...")
    frame = scraper.get_dashboard_frame(page)  # recarrega o relatorio do zero
    # Achado real (01/09/2026, print salvo em titan_debug/filtro_nao_encontrado.png):
    # um reload NAO volta pro "sem filtro nenhum" - o Titan carrega o
    # dashboard com um filtro de periodo estreito ja aplicado por padrao
    # (visto "6/1/2026" numa captura e "7/1/2026" no dia seguinte - parece
    # relativo a data de hoje). Sem abrir bem essa janela antes, uma busca
    # por NF de outro periodo dava "No results found" no proprio Power BI -
    # nao era bug de seletor. Ver scraper.definir_periodo/PERIODO_AMPLO_INICIAL.
    hoje = datetime.datetime.now().strftime("%d/%m/%Y")
    scraper.definir_periodo(frame, scraper.PERIODO_AMPLO_INICIAL, hoje)
    scraper.filtrar(frame, nf=numero_nf)
    scraper.rolar_tabela_ate_o_fim(frame)

    # marca_esperada desambigua quando a mesma NF aparece pra mais de uma
    # marca (confirmado que acontece de verdade - ver aviso grande no topo
    # de titan_bi_scraper.py) - "marca" aqui e o id da Torre (ex: "rituaria"),
    # comparado sem diferenciar caixa contra "Nome Projeto" do Titan.
    pedido_data = scraper.extrair_linha_por_pedido(frame, numero_nf, marca_esperada=marca)
    if pedido_data is None:
        print(f"[NF {numero_nf} / marca {marca}] nao encontrada no Titan (ou a NF existe mas nao pra essa marca).")
        mensagem = f"NF {numero_nf} nao encontrada no Titan BI para a marca {marca}."
        if permitir_limpar_dados:
            marcar_nao_encontrado(numero_nf, marca, mensagem)
        else:
            # Nao apaga dado bom com base num "nao encontrado" vindo de
            # varredura em massa sem supervisao - ver docstring da funcao.
            marcar_erro(numero_nf, marca, mensagem)
        return

    scraper.clicar_na_linha(frame, numero_nf, marca_esperada=marca)
    eventos = scraper.extrair_eventos(frame)
    itens = scraper.extrair_itens_pedido(frame)
    marcar_concluido(numero_nf, marca, pedido_data, eventos, itens)
    print(f"[NF {numero_nf}] ok - Romaneio: {pedido_data.get('Romaneio') or '(nao veio)'} | {len(itens)} item(ns)")


def rodar_fila_uma_vez(page, orcamento_segundos=ORCAMENTO_UMA_VEZ_SEGUNDOS_PADRAO):
    """
    Passada UNICA (nao-loop) pela fila 'pendente' - pra rodar via GitHub
    Actions (job que roda, processa o que tiver dentro do orcamento de tempo,
    e termina), em vez do loop infinito "while True: sleep(15)" de main()
    (pensado pra ficar aberto no PC, ver --uma-vez). Orcamento de TEMPO (nao
    contagem fixa de itens) pra sempre terminar dentro do timeout do job,
    seja qual for o tamanho da fila (28/08/2026, mesmo padrao ja usado em
    titan_backfill.rechecar_situacoes_presas).
    """
    inicio = time.monotonic()
    processados = 0
    erros = 0
    try:
        pendentes = buscar_pendentes()
    except (urllib.error.URLError, Exception) as e:
        print(f"Erro consultando a fila no Supabase: {e}", file=sys.stderr)
        return processados, erros

    for item in pendentes:
        if time.monotonic() - inicio > orcamento_segundos:
            print(f"  orcamento de tempo esgotado - {processados} processado(s), resto fica pra proxima rodada.")
            break
        try:
            processar_pedido(page, item)
            processados += 1
        except Exception as e:
            erros += 1
            print(f"[NF {item.get('numero_nf')} / marca {item.get('marca')}] erro inesperado: {e}", file=sys.stderr)
            traceback.print_exc()
            if item.get("numero_nf") and item.get("marca"):
                marcar_erro(item.get("numero_nf"), item.get("marca"), str(e))
    return processados, erros


def main():
    parser = argparse.ArgumentParser(description="Watcher da fila 'pendente' de infos_titan.")
    parser.add_argument("--uma-vez", action="store_true",
                         help="roda uma passada so pela fila (pra GitHub Actions) em vez do loop infinito (uso local no PC)")
    parser.add_argument("--headless", action="store_true", default=False,
                         help="roda sem abrir janela - o workflow do GitHub Actions sempre usa isso")
    parser.add_argument("--orcamento-segundos", type=int, default=ORCAMENTO_UMA_VEZ_SEGUNDOS_PADRAO,
                         help=f"so com --uma-vez: tempo maximo processando a fila (padrao {ORCAMENTO_UMA_VEZ_SEGUNDOS_PADRAO}s)")
    args = parser.parse_args()

    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar.", file=sys.stderr)
        print("Ex (PowerShell): $env:TITAN_EMAIL='...'; $env:TITAN_SENHA='...'", file=sys.stderr)
        sys.exit(1)

    if args.uma_vez:
        # Checa a fila ANTES de abrir navegador/logar - so uma chamada HTTP
        # leve (sem Playwright). Fila vazia (esperado na maior parte das
        # rodadas depois que o backlog atual baixar) termina em segundos, sem
        # gastar tempo nenhum de navegador/login - importante pro orcamento
        # de minutos do GitHub Actions rodando com frequencia (28/08/2026).
        try:
            pendentes_preview = buscar_pendentes()
        except (urllib.error.URLError, Exception) as e:
            print(f"Erro consultando a fila no Supabase: {e}", file=sys.stderr)
            pendentes_preview = []
        if not pendentes_preview:
            print("Fila 'pendente' vazia - nada a fazer, encerrando sem abrir o Titan.")
            return

    print("Entrando no Titan BI...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            scraper.login(page, email, senha)
        except Exception:
            print("Falha no login - confira TITAN_EMAIL/TITAN_SENHA e os prints em titan_debug/.", file=sys.stderr)
            browser.close()
            raise

        try:
            if args.uma_vez:
                print(f"Login ok. Rodando UMA passada pela fila (orcamento {args.orcamento_segundos}s)...")
                processados, erros = rodar_fila_uma_vez(page, args.orcamento_segundos)
                print(f"\nConcluido: {processados} pedido(s) processado(s), {erros} erro(s).")
                return

            print(f"Login ok. Verificando a fila a cada {INTERVALO_POLL_SEGUNDOS}s - deixe esta janela aberta. Ctrl+C pra parar.")
            while True:
                try:
                    pendentes = buscar_pendentes()
                except (urllib.error.URLError, Exception) as e:
                    print(f"Erro consultando a fila no Supabase: {e}", file=sys.stderr)
                    pendentes = []

                for item in pendentes:
                    try:
                        processar_pedido(page, item)
                    except Exception as e:
                        print(f"[NF {item.get('numero_nf')} / marca {item.get('marca')}] erro inesperado: {e}", file=sys.stderr)
                        traceback.print_exc()
                        if item.get("numero_nf") and item.get("marca"):
                            marcar_erro(item.get("numero_nf"), item.get("marca"), str(e))

                time.sleep(INTERVALO_POLL_SEGUNDOS)
        except KeyboardInterrupt:
            print("\nParando.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
