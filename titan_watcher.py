import datetime
import json
import os
import sys
import time
import traceback
import urllib.error
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


def buscar_pendentes():
    return _supabase_request(
        "GET", f"{TABELA}?status=eq.pendente&select=numero_pedido,numero_nf,marca"
    ) or []


def marcar_erro(numero_nf, marca, mensagem):
    try:
        _supabase_request("PATCH", f"{TABELA}?numero_nf=eq.{numero_nf}&marca=eq.{marca}", {
            "status": "erro",
            "erro": str(mensagem)[:500],
            "atualizado_em": _agora_iso(),
        })
    except Exception as e:
        print(f"  (nao consegui nem marcar erro no Supabase: {e})", file=sys.stderr)


def marcar_concluido(numero_nf, marca, pedido_data, eventos, itens):
    _supabase_request("PATCH", f"{TABELA}?numero_nf=eq.{numero_nf}&marca=eq.{marca}", {
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
    })


def processar_pedido(page, item):
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
    frame = scraper.get_dashboard_frame(page)  # recarrega o relatorio do zero - reseta os filtros da rodada anterior
    scraper.filtrar(frame, nf=numero_nf)
    scraper.rolar_tabela_ate_o_fim(frame)

    # marca_esperada desambigua quando a mesma NF aparece pra mais de uma
    # marca (confirmado que acontece de verdade - ver aviso grande no topo
    # de titan_bi_scraper.py) - "marca" aqui e o id da Torre (ex: "rituaria"),
    # comparado sem diferenciar caixa contra "Nome Projeto" do Titan.
    pedido_data = scraper.extrair_linha_por_pedido(frame, numero_nf, marca_esperada=marca)
    if pedido_data is None:
        print(f"[NF {numero_nf} / marca {marca}] nao encontrada no Titan (ou a NF existe mas nao pra essa marca).")
        marcar_erro(numero_nf, marca, f"NF {numero_nf} nao encontrada no Titan BI para a marca {marca}.")
        return

    scraper.clicar_na_linha(frame, numero_nf, marca_esperada=marca)
    eventos = scraper.extrair_eventos(frame)
    itens = scraper.extrair_itens_pedido(frame)
    marcar_concluido(numero_nf, marca, pedido_data, eventos, itens)
    print(f"[NF {numero_nf}] ok - Romaneio: {pedido_data.get('Romaneio') or '(nao veio)'} | {len(itens)} item(ns)")


def main():
    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar.", file=sys.stderr)
        print("Ex (PowerShell): $env:TITAN_EMAIL='...'; $env:TITAN_SENHA='...'", file=sys.stderr)
        sys.exit(1)

    print("Entrando no Titan BI...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            scraper.login(page, email, senha)
        except Exception:
            print("Falha no login - confira TITAN_EMAIL/TITAN_SENHA e os prints em titan_debug/.", file=sys.stderr)
            browser.close()
            raise

        print(f"Login ok. Verificando a fila a cada {INTERVALO_POLL_SEGUNDOS}s - deixe esta janela aberta. Ctrl+C pra parar.")
        try:
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
