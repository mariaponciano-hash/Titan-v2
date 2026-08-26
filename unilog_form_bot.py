"""
unilog_form_bot.py

Abre o formulario publico "Alo Uni - Canal de Ocorrencias" da Unilog
(https://unilog-formularios.bitrix24.site/alo_uni_ocorrencias/) e PRE-PREENCHE
com os dados de cada rascunho salvo pela Central de Tickets (Supabase,
tabela ocorrencias_unilog, status='rascunho' - ver /api/logistica/
unilog-ocorrencia em server.ts): CNPJ, nome da agente, telefone, e-mail,
tipo de ocorrencia, especificacao, endereco de entrega (quando o tipo for
Entrega), NF/pedido e a descricao.

NUNCA CLICA EM "ENVIAR" - decisao explicita da Ivna (25/08/2026, confirmada
quando perguntada diretamente: "preenche e para antes do Enviar" venceu
"preenche e envia sozinho"). O bot deixa a aba aberta, pronta, pra uma
pessoa revisar, anexar as evidencias (o bot NAO tem os arquivos - o rascunho
so guarda uma contagem, ver qtd_anexos) e so ela apertar Enviar de verdade.

QUEM RODA ISSO: voce, na sua maquina - nunca eu, nem outra sessao do Claude.
Precisa rodar num terminal de verdade porque o Chromium abre visivel e a
revisao final e manual - nao da pra "rodar por voce" de um jeito que nao
seja voce sentada na frente da tela.

USO
    python unilog_form_bot.py

De proposito, ISSO NAO PASSA PELO APP DA CENTRAL DE TICKETS NO GODEPLOY - vai
direto no Supabase (mesma chave "publishable" que titan_watcher.py/
titan_backfill.py ja usam) pra ler/atualizar ocorrencias_unilog. O app no
GoDeploy fica atras de SSO (visibilidade "authenticated") - um script
nao tem como fazer esse login por voce, entao rotear por ele so adicionaria
uma trava sem necessidade (mesmo motivo pelo qual o titan_watcher.py tambem
nunca fala com o app, so com o Supabase direto - ver comentario grande em
TITAN_SB_URL no server.ts).

CAMPOS EXTRAS DESCOBERTOS NO FORMULARIO REAL (25/08/2026), QUE NAO TEMOS
NENHUM DADO PRA PREENCHER: inspecionando a arvore de acessibilidade completa
da pagina (nao so o que aparece na tela por padrao) apareceram 4 campos que
nunca tinham surgido nos prints manuais da Ivna: "Quantidade / Lote /
Validade e SKU", "Selecione o BI", "SKU" e "Tipo de imagens" - devem
aparecer so pra combinacoes especificas de Tipo+Especificacao (ex:
provavelmente "Avaria"), ainda nao mapeado ao certo. O bot NAO inventa
valor pra eles - so avisa no terminal quando algum aparece visivel, pra
a pessoa preencher manualmente antes de enviar.

MENOS TESTADO DE TODO O PROJETO: a interacao com os comboboxes deste
formulario ("Tipo de ocorrência"/"Especifique sua solicitação") foi escrita
por inspecao da arvore de acessibilidade, sem conseguir confirmar visualmente
cada clique durante o desenvolvimento (a ferramenta de navegador disponivel
nao estava renderizando frames pra tirar print naquele momento). Por
seguranca, NUNCA aperta Enter dentro de um campo do formulario (risco real:
um Enter que "escape" do combobox pode disparar o submit do <form> com
campos ainda faltando) - se digitar o valor nao fizer uma opcao aparecer
clicavel, o bot para e salva diagnostico em vez de arriscar. Espere precisar
de 1-2 rodadas de ajuste depois do primeiro teste real, como todo o resto
deste projeto - manda os arquivos de titan_debug/ (pasta reaproveitada
daqui) se travar em algum passo.
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import titan_bi_scraper as scraper

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "ocorrencias_unilog"
FORM_URL = "https://unilog-formularios.bitrix24.site/alo_uni_ocorrencias/"

# Campos descobertos que so aparecem condicionalmente e pros quais nao temos
# dado nenhum - ver comentario grande no topo do arquivo.
CAMPOS_SEM_DADO = ["Quantidade / Lote / Validade e SKU", "Selecione o BI", "SKU", "Tipo de imagens"]


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


def buscar_rascunhos():
    return _supabase_request("GET", f"{TABELA}?status=eq.rascunho&select=*") or []


def marcar_preenchido(protocolo):
    try:
        _supabase_request("PATCH", f"{TABELA}?protocolo=eq.{protocolo}", {"status": "preenchido"})
    except Exception as e:
        print(f"  (nao consegui marcar como 'preenchido' no Supabase: {e})", file=sys.stderr)


def preencher_texto(page, rotulo, valor):
    if not valor:
        return
    try:
        campo = scraper.elemento_visivel(page.get_by_role("textbox", name=rotulo), timeout_ms=4000)
        campo.fill(str(valor))
    except PWTimeout:
        pass  # campo nao esta visivel nesta combinacao de tipo/especificacao - segue sem preencher


def selecionar_combobox(page, rotulo, valor):
    """
    O formulario tem VARIOS comboboxes escondidos com o MESMO rotulo ao mesmo
    tempo no DOM (confirmado: "Especifique sua solicitação" aparece 5 vezes,
    uma por Tipo de ocorrência - so uma fica visivel por vez, dependendo do
    que foi escolhido em "Tipo de ocorrência") - mesmo problema dos elementos
    escondidos duplicados do Titan BI (ver elemento_visivel em
    titan_bi_scraper.py), reaproveitado aqui.

    De proposito, NUNCA aperta Enter - so digita e procura uma opcao clicavel
    com o texto exato. Se nao achar, levanta erro em vez de arriscar um Enter
    que "escape" pro <form> e dispare o Enviar com o resto ainda vazio.
    """
    combo = scraper.elemento_visivel(page.get_by_role("combobox", name=rotulo), timeout_ms=8000)
    campo_texto = combo.get_by_role("textbox")
    campo_texto.click()
    campo_texto.press("Control+A")
    campo_texto.press("Delete")
    campo_texto.press_sequentially(str(valor), delay=60)
    time.sleep(0.8)  # da tempo da lista filtrar
    opcao = scraper.elemento_visivel(page.get_by_role("option", name=str(valor), exact=True), timeout_ms=5000)
    opcao.click()
    time.sleep(0.5)


def avisar_campos_sem_dado(page):
    for rotulo in CAMPOS_SEM_DADO:
        visivel = False
        for role in ("textbox", "combobox"):
            try:
                scraper.elemento_visivel(page.get_by_role(role, name=rotulo), timeout_ms=1000)
                visivel = True
                break
            except PWTimeout:
                continue
        if visivel:
            print(f"  ATENÇÃO: o campo \"{rotulo}\" apareceu neste formulário - preencha manualmente antes de enviar.")


def preencher_ocorrencia(page, item):
    protocolo = item.get("protocolo", "sem-protocolo")
    print(f"[{protocolo}] abrindo o formulário e preenchendo...")
    page.goto(FORM_URL)
    page.wait_for_load_state("domcontentloaded")

    try:
        preencher_texto(page, "CNPJ (apenas números)", item.get("cnpj"))
        preencher_texto(page, "Seu nome", item.get("nome_agente"))
        preencher_texto(page, "Telefone", item.get("telefone"))
        preencher_texto(page, "E-mail", item.get("email"))

        selecionar_combobox(page, "Tipo de ocorrência", item.get("tipo_ocorrencia"))
        selecionar_combobox(page, "Especifique sua solicitação", item.get("especificacao"))

        # So preenche se o campo realmente ficou visivel apos a escolha acima
        # (ex: "Endereço de Entrega" so aparece pro tipo "Entrega").
        preencher_texto(page, "Endereço de Entrega", item.get("endereco_entrega"))

        numero = item.get("numero_nf") or item.get("numero_pedido") or ""
        preencher_texto(page, "Nº da Nota Fiscal/Nº do Pedido", numero)
        preencher_texto(page, "Descreva a ocorrência", item.get("descricao"))

        avisar_campos_sem_dado(page)

        qtd = item.get("qtd_anexos") or 0
        if qtd:
            print(f"  Lembrete: {qtd} evidência(s) marcada(s) no rascunho - anexe manualmente (o bot não guarda o arquivo).")

        print(f"[{protocolo}] preenchido. REVISE, anexe as evidências, aceite os termos e clique Enviar você mesma.")
    except PWTimeout as e:
        scraper.salvar_diagnostico(page, f"unilog_form_falhou_{protocolo}")
        raise e


def main():
    rascunhos = buscar_rascunhos()
    if not rascunhos:
        print("Nenhum rascunho pendente (status='rascunho') em ocorrencias_unilog.")
        return
    print(f"{len(rascunhos)} rascunho(s) encontrado(s) - uma aba por rascunho, todas ficam abertas pra revisão.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        for item in rascunhos:
            page = browser.new_page()
            try:
                preencher_ocorrencia(page, item)
                marcar_preenchido(item.get("protocolo"))
            except Exception as e:
                print(f"[{item.get('protocolo')}] ERRO ao preencher: {e}", file=sys.stderr)

        print("\nTodas as abas foram deixadas abertas. Revise, anexe evidências, aceite os termos e "
              "clique Enviar em cada uma - o bot nunca faz isso por você.")
        input("Pressione Enter aqui SÓ depois de terminar de revisar/enviar todas as abas (fecha o navegador)... ")
        browser.close()


if __name__ == "__main__":
    main()
