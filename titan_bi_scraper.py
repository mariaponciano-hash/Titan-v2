"""
titan_bi_scraper.py

Coleta dados do Titan BI (visao Unilog/allPICK, dashboard "Detalhamento de
Pedidos") para um pedido/NF especifico: os campos da tabela "Informacao
Pedido" (incluindo Romaneio, Observacao, Nome Projeto (Antigo)), a timeline
de eventos ("Horario da Situacao" / "Situacao") e os itens do pedido
(codigo/descricao/EAN/quantidade/valores).

QUEM RODA ISSO: voce, na sua maquina ou num servidor que voces controlem -
nunca eu. Sua senha fica so na variavel de ambiente TITAN_SENHA, que eu nunca
vejo nem vou ver em execucao.

USO
    set TITAN_EMAIL=sacgobeauty@unilog.com
    set TITAN_SENHA=sua-senha-aqui
    python titan_bi_scraper.py --nf 940380
    python titan_bi_scraper.py --nf 940380 --pedido SH1099815RT --out saida.json

REQUISITOS
    pip install playwright
    playwright install chromium

FRAGILIDADE CONHECIDA (leia antes de agendar isso como bot recorrente)
    O Titan BI e uma dashboard SPA proprietaria sem API documentada, renderizada
    dentro de um iframe. Os seletores abaixo usam TEXTO VISIVEL (rotulos,
    cabecalhos de coluna) em vez de classes CSS - e a opcao mais robusta
    disponivel sem acesso ao HTML real, mas ainda assim QUALQUER mudanca de
    layout/wording no Titan pode quebrar isso. Recomendado:
    - Rodar com --headed (padrao) da primeira vez, pra ver visualmente se esta
      navegando certo.
    - Se o filtro de Nota Fiscal achar MAIS de um pedido (numeros de NF se
      repetem entre marcas - ja vimos isso acontecer), o script pega o que
      bate tambem com --pedido (numero do pedido); combine os dois filtros
      sempre que possivel pra evitar ambiguidade.
    - Considere perguntar ao suporte do Titan/Unilog se existe uma API oficial
      antes de depender disso pra producao - scraping deve ser o plano B.

VERSAO "SEMPRE RODANDO" (24/08/2026): pra nao precisar chamar este script na
mao toda vez, ver titan_watcher.py (mesma pasta) - ele fica de olho numa fila
no Supabase que a Torre de Controle preenche sozinha quando o agente pesquisa
um pedido, processa automaticamente reaproveitando as funcoes deste arquivo, e
grava o resultado de volta pra Torre mostrar. Continua sendo voce quem roda,
com seu login - so passa a ficar rodando em segundo plano em vez de um
comando por vez.

BACKFILL EM MASSA (24/08/2026): ver titan_backfill.py (mesma pasta) - carrega
de uma vez um periodo inteiro de datas no Supabase, pra a maioria das buscas
da Torre ja achar tudo pronto em vez de esperar uma consulta avulsa. Chave =
Nota Fiscal (nao o "Numero do Pedido" do Titan, que e interno do armazem).
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

TITAN_URL = "https://www.titanbi.com.br/"
DASHBOARD_URL = "https://www.titanbi.com.br/embed/a1609652-2312-40a7-9cc8-124e25d6fcc3?page=0d62dad7c41ce11d7a3a"

PASTA_DIAGNOSTICO = Path(__file__).parent / "titan_debug"


def salvar_diagnostico(alvo, etapa):
    """
    Chamado quando um passo falha. Salva print + HTML da pagina/frame que
    falhou, pra mandar pra mim em vez de descrever de memoria o que apareceu
    na tela - isso substitui o "abrir DevTools e copiar seletor" manual.
    NUNCA salva e-mail/senha (so o que esta renderizado na tela).
    """
    PASTA_DIAGNOSTICO.mkdir(exist_ok=True)
    pagina = alvo.page if hasattr(alvo, "page") else alvo  # frame ou page
    try:
        pagina.screenshot(path=str(PASTA_DIAGNOSTICO / f"{etapa}.png"), full_page=True)
    except Exception as e:
        print(f"  (nao consegui tirar print: {e})", file=sys.stderr)
    try:
        html = alvo.content()
        (PASTA_DIAGNOSTICO / f"{etapa}.html").write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"  (nao consegui salvar HTML: {e})", file=sys.stderr)
    print(f"Falhou em '{etapa}' - print e HTML salvos em {PASTA_DIAGNOSTICO}\\{etapa}.png / .html", file=sys.stderr)
    print("Manda esses dois arquivos que eu ajusto o seletor.", file=sys.stderr)


def registrar_passo(alvo, pasta, indice, nome_passo):
    """
    Grava so o PRINT (sem HTML, pra nao pesar) de UM passo qualquer do
    fluxo - nao so quando falha (isso ja e salvar_diagnostico). Pedido
    direto da Maria (12/09/2026, depois do achado do "pbi-overlay-caret"):
    ela quer ver o passo a passo visual completo de cada acao (abrir
    dropdown, digitar NF, marcar item, ler eventos/itens, limpar filtro)
    pra acompanhar exatamente o que o robo esta fazendo, nao so o
    diagnostico do momento da falha.

    So chamado com uma `pasta` de verdade pelos primeiros N pedidos de uma
    rodada (ver REGISTRO_PASSO_A_PASSO_MAX_PEDIDOS em titan_backfill.py) -
    gravar isso pra TODOS os pedidos de uma janela com dezenas de milhares
    geraria centenas de milhares de imagens e deixaria o job bem mais
    lento.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    pagina = alvo.page if hasattr(alvo, "page") else alvo  # frame ou page
    nome = f"{indice:02d}_{nome_passo}.png"
    try:
        pagina.screenshot(path=str(pasta / nome))
    except Exception as e:
        print(f"  (nao consegui gravar o passo '{nome_passo}': {e})", file=sys.stderr)


def elemento_visivel(locator_multi, timeout_ms=30000, intervalo=0.3):
    """
    Confirmado (18/08/2026, com print real): o Titan BI e um embed do
    Microsoft Power BI (achamos classes tipo 'visualsEnterHint' e atributos
    Angular _ngcontent-*). O Power BI cria um <p aria-hidden="true"
    class="visualsEnterHint"> com o MESMO texto do titulo de cada visual, pra
    servir de tooltip - entao qualquer get_by_text(...) acha PELO MENOS 2
    elementos com o texto certo, um deles escondido. Pegar so o primeiro (que
    e o que get_by_text/wait_for_selector fazem por padrao) da errado direto
    na cara em ~50% das vezes, dependendo da ordem no DOM.
    Esta funcao ignora os escondidos e devolve o elemento de verdade.
    """
    limite = time.time() + timeout_ms / 1000
    while time.time() < limite:
        for el in locator_multi.all():
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        time.sleep(intervalo)
    raise PWTimeout(f"Nenhum elemento visivel encontrado dentro de {timeout_ms}ms (so achou versoes escondidas/tooltip)")


def primeira_opcao_real(frame, ignorar=("Select all", "Selecionar tudo"), timeout_ms=10000, intervalo=0.3):
    """
    Primeira opcao role="option" visivel de um dropdown ja aberto, pulando
    "Select all"/"Selecionar tudo" (12/09/2026, mesmo HTML real de
    titan_debug/filtro_nao_encontrado.html que motivou o exact=False em
    marcar_item_da_lista, enviado pela Maria - o dropdown "Numero do
    pedido" tinha exatamente 2 role="option": "Select all" no
    aria-posinset=1 e o pedido de verdade no aria-posinset=2). Usado por
    titan_backfill._completar_eventos_itens, que abre esse dropdown SEM
    digitar nada e antes so pegava a 1a opcao visivel - isso clicava em
    "Select all" (selecionando TODOS os pedidos cross-filtrados por essa NF
    em vez do pedido certo) sempre que ele aparecesse antes na lista.
    """
    limite = time.time() + timeout_ms / 1000
    while time.time() < limite:
        for el in frame.get_by_role("option").all():
            try:
                if not el.is_visible():
                    continue
                nome = (el.get_attribute("title") or el.inner_text() or "").strip()
                if nome in ignorar:
                    continue
                return el
            except Exception:
                continue
        time.sleep(intervalo)
    raise PWTimeout(f"Nenhuma opcao real encontrada dentro de {timeout_ms}ms (so achou 'Select all'/vazio)")


def preencher_primeiro_que_existir(page, estrategias, valor, timeout_cada=4000):
    """
    Tenta cada estrategia de localizador em ordem (get_by_label so funciona se
    existir um <label> de verdade associado ao campo - muitos formularios so
    tem placeholder, sem label real, e por isso get_by_label falha mesmo com
    o campo visivel na tela). Usa a primeira que aparecer na tela.
    """
    ultimo_erro = None
    for descricao, locator_fn in estrategias:
        try:
            loc = locator_fn(page)
            loc.wait_for(state="visible", timeout=timeout_cada)
            loc.fill(valor)
            return descricao
        except PWTimeout as e:
            ultimo_erro = e
            continue
    raise PWTimeout(
        f"Nenhuma das {len(estrategias)} estrategias de campo funcionou: "
        f"{[d for d, _ in estrategias]}"
    ) from ultimo_erro


def login(page, email, senha):
    page.goto(TITAN_URL)
    try:
        campo_usado = preencher_primeiro_que_existir(
            page,
            [
                ("label E-mail", lambda p: p.get_by_label("E-mail", exact=False)),
                ("placeholder E-mail", lambda p: p.get_by_placeholder("E-mail", exact=False)),
                ("input type=email", lambda p: p.locator("input[type='email']")),
                ("primeiro input de texto", lambda p: p.locator("input[type='text'], input:not([type])").first),
            ],
            email,
        )
    except PWTimeout:
        salvar_diagnostico(page, "login_campo_email_nao_encontrado")
        raise
    try:
        preencher_primeiro_que_existir(
            page,
            [
                ("label Palavra-passe", lambda p: p.get_by_label("Palavra-passe", exact=False)),
                ("placeholder Palavra-passe", lambda p: p.get_by_placeholder("Palavra-passe", exact=False)),
                ("input type=password", lambda p: p.locator("input[type='password']")),
            ],
            senha,
        )
    except PWTimeout:
        salvar_diagnostico(page, "login_campo_senha_nao_encontrado")
        raise

    try:
        # NAO depende mais do texto/idioma do botao (bug real, 27/08/2026,
        # achado via titan_debug artifact no GitHub Actions: o Titan trocou
        # o rotulo de "Entrar" pra "Login" em algum momento, quebrando o
        # seletor por texto exato). button[type='submit'] e estavel mesmo se
        # o idioma mudar nao importa mais o texto, e nao colide com o botao
        # separado "Login with Microsoft" (esse e type='button', nao
        # 'submit').
        page.locator("button[type='submit']").first.click()
    except PWTimeout:
        # Unico ponto do login que nao salvava diagnostico nenhum na falha
        # (achado 27/08/2026 rodando no GitHub Actions - sem isso, nao dava
        # pra saber se o botao simplesmente nao apareceu, se um CAPTCHA/
        # bloqueio por IP de datacenter surgiu, ou outra coisa).
        salvar_diagnostico(page, "login_botao_entrar_nao_encontrado")
        raise

    # Depois do clique, o botao mostra "Autenticando no TitanBI..." com um
    # spinner por um tempo - networkidle sozinho dispara ANTES desse processo
    # terminar (confirmado com print real: campos preenchidos certinho, so
    # que ainda "Autenticando" quando o script ja tinha checado e desistido).
    # Espera esse texto sumir antes de decidir se deu certo ou nao.
    try:
        page.wait_for_selector("text=Autenticando", state="hidden", timeout=20000)
    except PWTimeout:
        pass  # nao apareceu esse texto especifico - segue pro proximo cheque
    page.wait_for_load_state("networkidle", timeout=20000)

    # Mesmo motivo do clique acima: nao depende mais do rotulo do botao
    # (ja quebrou uma vez, 27/08/2026) - o campo de senha continuar visivel
    # e um sinal de "ainda no login" independente de idioma/rotulo.
    if page.locator("input[type='password']").first.is_visible():
        salvar_diagnostico(page, "login_nao_saiu_da_tela")
        raise RuntimeError(
            "Ainda na tela de login depois de tentar entrar - confira "
            "TITAN_EMAIL/TITAN_SENHA, ou pode ter aparecido um CAPTCHA/MFA "
            "que este script nao trata."
        )

    # Depois do login, o proprio site redireciona sozinho pra /home - se a
    # gente tentar navegar pro dashboard ANTES desse redirecionamento
    # terminar, as duas navegacoes competem e o Playwright cancela a nossa
    # (confirmado com erro real: "interrupted by another navigation to
    # .../home"). Espera a URL estabilizar em /home antes de sair daqui.
    try:
        page.wait_for_url("**/home*", timeout=15000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass  # pode ja ter caido direto numa URL diferente de /home - segue


def _ir_pro_dashboard(page):
    """page.goto com retry - a navegacao pos-login pode ainda estar em
    andamento (confirmado com erro real: "interrupted by another navigation
    to .../home")."""
    for tentativa in range(3):
        try:
            page.goto(DASHBOARD_URL)
            return
        except Exception as e:
            if "interrupted by another navigation" in str(e) and tentativa < 2:
                time.sleep(1.5)  # deixa a navegacao concorrente (ex: redirect pos-login) terminar
                continue
            raise


def get_dashboard_frame(page):
    """
    O conteudo real (tabela, filtros) fica dentro de um <iframe> - a pagina
    "de fora" so tem a barra lateral e o seletor de abas (Consulta/Exportacao).
    Se o Titan tiver mais de um iframe na pagina, ajuste o seletor abaixo (ex:
    'iframe[src*="embed"]') depois de checar com o DevTools (F12 -> Elements).

    CORRIGIDO (31/08/2026, HTML+print reais salvos em titan_debug/
    dashboard_iframe_nao_encontrado): o print mostrava a mensagem de erro do
    proprio Titan "Request timed out: GET https://api.titanbi.com.br/api/
    dashboard/.../token" - o backend dele falhou ao gerar o token de embed do
    Power BI, e nesse caso o <iframe> nunca chega a existir no DOM (confirmado:
    zero ocorrencias no HTML salvo). Esperar mais nao ajuda, ja que o elemento
    simplesmente nao vai aparecer. E uma falha transitoria do lado do Titan,
    nao do nosso seletor/timing - um reload completo (page.goto de novo)
    resolve na pratica. Tenta ate 3 vezes antes de desistir.
    """
    ultimo_erro = None
    for tentativa in range(3):
        _ir_pro_dashboard(page)
        try:
            frame_el = page.wait_for_selector("iframe", timeout=30000)
            frame = frame_el.content_frame()
            elemento_visivel(frame.get_by_text("Informação Pedido", exact=False), timeout_ms=30000)
            return frame
        except PWTimeout as e:
            ultimo_erro = e
            time.sleep(3)
    salvar_diagnostico(page, "dashboard_iframe_nao_encontrado")
    raise ultimo_erro


# Mapa do rotulo visivel (h3 do slicer) pro aria-label TECNICO real do
# combobox (nome do campo Power BI por baixo) - confirmado no HTML real
# salvo em titan_debug/filtro_nao_encontrado.html (31/08/2026): o elemento
# que abre o popup e <div role="combobox" data-testid="slicer-dropdown"
# aria-label="nota_fiscal_saida_numero">, um irmao do <h3> do rotulo, nao um
# filho - por isso clicar por coordenada relativa ao rotulo era fragil.
ARIA_LABEL_POR_ROTULO = {
    "Nota Fiscal de Saída": "nota_fiscal_saida_numero",
    "Número do pedido": "numero",
}


def abrir_dropdown_filtro(frame, rotulo):
    """
    Abre o combobox do slicer. Preferencia: seletor real por aria-label
    tecnico (ARIA_LABEL_POR_ROTULO) - so existe pros rotulos ja confirmados
    no HTML. Fallback (rotulo desconhecido): clicar numa posicao relativa ao
    rotulo, ~15-20px abaixo da sua caixa delimitadora (abordagem antiga,
    usada quando ainda nao tinhamos o HTML real do slicer pra inspecionar -
    confirmado, com print, que clicar direto no TEXTO do rotulo nao abre
    nada; e essa mesma coordenada, por ser sensivel a diferencas finas de
    fonte/DPI entre o PC da Ivna e o runner do GitHub Actions, foi a causa
    real de falhas la mesmo com a pagina 100% carregada).
    """
    aria_label = ARIA_LABEL_POR_ROTULO.get(rotulo)
    if aria_label:
        combobox = frame.locator(
            f'div[role="combobox"][data-testid="slicer-dropdown"][aria-label="{aria_label}"]'
        )
        if combobox.count() > 0:
            combobox.first.click(timeout=10000)
            return

    titulo = elemento_visivel(frame.get_by_text(rotulo, exact=False))
    caixa = titulo.bounding_box()
    if not caixa:
        raise PWTimeout(f"Rotulo '{rotulo}' visivel mas sem bounding_box (layout inesperado)")
    x = caixa["x"] + caixa["width"] / 2
    y = caixa["y"] + caixa["height"] + 15  # a caixa "Todos" fica ~15-20px abaixo do rotulo
    frame.page.mouse.click(x, y)


def marcar_item_da_lista(frame, campo_busca, valor, tentativas=3, exact=True):
    """
    CORRIGIDO (24/08/2026): a causa raiz do filtro nao aplicar era o
    campo.fill() usado antes - ele seta o valor do input direto via JS, sem
    disparar os eventos de tecla (keydown/keypress) que o listener de busca
    do Power BI escuta pra filtrar a lista. Resultado: o texto aparecia
    certo no campo, mas a lista de opcoes nunca mudava (confirmado: usuario
    testou manualmente digitando de verdade e a NF apareceu certinho).
    Fluxo confirmado manualmente pelo usuario: digita a busca -> Enter (aplica
    o filtro na lista) -> clica na caixa de selecao do item. Os itens da
    lista sao canvas (nao aparecem no HTML/innerText), mas tem role="option"
    com o texto certo na arvore de acessibilidade - por isso da pra usar
    get_by_role em vez de coordenada de pixel.

    Em vez de desistir na primeira, repete a busca (Enter de novo, com mais
    folga a cada tentativa) ate 'tentativas' vezes antes de propagar o erro
    de verdade - o Titan/Power BI as vezes demora um instante a mais pra
    popular a lista de opcoes depois do Enter.

    exact=False (12/09/2026, achado real - HTML de titan_debug/
    filtro_nao_encontrado.html enviado pela Maria): usado pra filtrar()
    (nf=..., numero_pedido=...). O "numero_pedido" gravado no Supabase e o
    ID BRUTO da Torre (ex: "1254848"), mas o slicer "Numero do pedido" no
    Titan sempre mostra o texto completo original ("SH1254848RT1147557" -
    prefixo SH + o id da Torre + RT + a NF colada no final, ver
    scraper.extrair_numero_pedido_torre). exact=True nunca bate ("1254848"
    != "SH1254848RT1147557"), mesmo com a opcao certa (e unica) ja filtrada
    e visivel na tela - o filtro ficava preso pra sempre em
    "filtro_nao_encontrado" e o pedido nunca tinha Eventos/Itens
    exportados. exact=False usa contains (comportamento padrao do
    Playwright pra `name=` sem exact) - a busca por texto ja estreita a
    lista pra so a opcao certa mesmo quando duas NFs iguais tem numero_
    pedido diferente, so o MATCH final que precisava parar de exigir
    igualdade estrita.
    """
    ultimo_erro = None
    for tentativa in range(tentativas):
        campo_busca.press("Enter")
        time.sleep(1 + tentativa * 1.5)  # da mais folga a cada nova tentativa
        try:
            opcao = elemento_visivel(frame.get_by_role("option", name=str(valor), exact=exact), timeout_ms=8000)
            opcao.click()
            return
        except PWTimeout as e:
            ultimo_erro = e
    raise ultimo_erro


def digitar_busca(campo, valor):
    """
    press_sequentially() digita tecla por tecla (precisa disparar
    keydown/keypress de verdade pro filtro do Power BI reagir - ver
    marcar_item_da_lista), mas ao contrario de fill() ele INSERE no cursor em
    vez de substituir o conteudo. Confirmado com print real: sem limpar antes,
    o valor anterior do campo ficou concatenado com o novo (busca virou
    "6486299980880" em vez de "9980880", e claro deu "Nenhum resultado
    encontrado"). Seleciona tudo e apaga antes de digitar.

    CORRIGIDO (11/09/2026, achado real da Maria testando manualmente, HTML
    real de titan_debug/filtro_nao_encontrado.html confirmando): digitar
    tecla por tecla (mesmo com delay baixo) faz o Power BI rodar uma busca
    PARA CADA CARACTERE digitado - a lista de opcoes visivel no meio desse
    processo reflete um prefixo PARCIAL do valor (ex: buscando "1147557",
    a tela chegou a mostrar "1147568"/"1147569" junto - numeros que so
    combinam com um prefixo mais curto ainda em digitacao, nao com o valor
    final). insert_text() poe o valor INTEIRO de uma vez so (um unico
    evento "input", que o Angular do Power BI escuta pro two-way binding -
    ver _ngcontent-ng-c* nos elementos do slicer) - a busca roda uma vez
    so, ja com o valor completo, sem estados intermediarios incorretos no
    meio.
    """
    campo.click()
    campo.press("Control+A")
    campo.press("Delete")
    campo.page.keyboard.insert_text(str(valor))


def _abrir_dropdown_e_pegar_campo_busca(frame, rotulo, tentativas=3):
    """
    CORRIGIDO (24/08/2026, erro real reportado pela Ivna): abrir_dropdown_filtro
    as vezes clica certo (a setinha do rotulo vira pra cima, print confirmou)
    mas o popup abre VAZIO por um instante - nem o campo de busca aparece
    a tempo, sem nenhum erro visivel na tela (so um retangulo em branco).
    Parece lentidao pontual do proprio Titan, nao um clique errado. Em vez de
    desistir na primeira, fecha (Escape) e tenta abrir de novo ate
    'tentativas' vezes antes de propagar o erro de verdade.

    CORRIGIDO (31/08/2026, HTML real salvo em titan_debug/filtro_nao_encontrado.html):
    o placeholder desse campo nao e fixo em portugues - e uma string de UI do
    proprio Power BI, que segue o locale do navegador. No PC da Ivna renderiza
    "Pesquisar"; no runner do GitHub Actions (locale em ingles) renderiza
    "Search". Aceita os dois.
    """
    ultimo_erro = None
    for tentativa in range(tentativas):
        abrir_dropdown_filtro(frame, rotulo)
        time.sleep(0.8 + tentativa * 0.5)  # da mais folga a cada nova tentativa
        try:
            return elemento_visivel(frame.get_by_placeholder(re.compile("Pesquisar|Search")), timeout_ms=10000)
        except PWTimeout as e:
            ultimo_erro = e
            frame.page.keyboard.press("Escape")
            time.sleep(0.5)
    raise ultimo_erro


def _esperar_tabela_refletir_filtro(frame, valor, timeout_ms=15000):
    """
    Espera o painel "Informacao Pedido" realmente mostrar o valor filtrado,
    em vez de confiar num sleep fixo. CORRIGIDO (31/08/2026, HTML real salvo
    em titan_debug/tabela_linha_nao_encontrada.html): o slicer confirmava a
    selecao certinha (slicer-restatement e o checkbox do item mostravam
    "1285822" marcado, aria-selected="true"), mas a TABELA ainda mostrava os
    dados antigos/default por mais tempo - um lag assincrono entre o slicer
    comitar a selecao e o visual re-renderizar, mais lento no runner do
    GitHub Actions do que no PC (mesma classe de lentidao ja vista no slicer
    de periodo e no token do dashboard). Sem essa espera,
    _achar_linha_pedido rodava cedo demais contra uma tabela desatualizada.

    Se o valor nunca aparecer (NF que genuinamente nao existe pra essa
    marca), so retorna sem erro - _achar_linha_pedido/extrair_linha_por_pedido
    decidem "nao encontrado" do jeito de sempre.
    """
    painel = localizar_painel(frame, "Informação Pedido")
    limite = time.time() + timeout_ms / 1000
    while time.time() < limite:
        try:
            if str(valor) in painel.inner_text():
                return
        except Exception:
            pass
        time.sleep(0.5)


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


def _formatar_data_para_titan(data_str):
    """
    Converte 'dd/mm/yyyy' (formato usado no resto deste projeto) pro
    'M/d/yyyy' que o campo de data do Titan exige de verdade - SEM zero a
    esquerda em mes/dia, com MES antes de DIA (ao contrario do formato
    brasileiro). CAUSA RAIZ real do "150003 linha(s) recebidas" identica
    em TODO dia testado (confirmado com HTML real salvo por
    salvar_diagnostico em titan_debug/periodo_definido_*.html, 11/09/2026):
    os dois <input class="date-slicer-datepicker"> do slicer tem
    aria-description="Enter date in M/d/yyyy format" - digitar
    "01/09/2026" (querendo 1 de SETEMBRO) fazia o campo "Start date"
    aplicar mes=01/dia=09, ou seja 9 de JANEIRO. Pior: o campo "End date"
    nunca recebia valor nenhum (aria-label sempre confirmava
    "selected: 9/11/2026", a data de HOJE, em toda tentativa de todo dia -
    prova de que o Tab entre os dois campos, no codigo antigo, nunca
    chegava a focar o campo de verdade). Resultado real: o periodo
    aplicado nunca era "1 dia", e sim "[data errada, la atras] ate hoje" -
    uma janela enorme, sempre estourando o teto de volume do Power BI do
    mesmo jeito, disfarcado de "150003 linhas" indiferente ao dia pedido.
    """
    dia, mes, ano = data_str.split("/")
    return f"{int(mes)}/{int(dia)}/{ano}"


# Abreviacoes de mes do "seletor de mes" do Titan. CORRIGIDO (17/09/2026,
# run manual #60 - HTML/print reais: titan_debug/
# set_filtro_data_nao_encontrado.*): a 1a versao desta lista usava
# abreviacoes em PORTUGUES ("set" pra setembro) por suposicao - o HTML real
# confirmou que o seletor mostra "Jan Feb Mar Apr / May Jun Jul Aug / Sep
# Oct Nov Dec", em INGLES, mesmo com o resto do relatorio em portugues -
# mesmo padrao ja confirmado varias vezes neste arquivo (aria-label de
# "Start date"/"End date"/"Select all"/"Previous month" etc.). Indice 0 =
# janeiro.
MESES_ABREV_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _clicar_seta_calendario(frame, anterior, timeout_ms=5000):
    """
    As setas de navegacao do calendario (ano anterior/seguinte no seletor de
    mes, mes anterior/seguinte na grade de dias). CORRIGIDO (17/09/2026,
    HTML real salvo em titan_debug/set_filtro_data_nao_encontrado.html apos
    a 1a tentativa desta funcao falhar): o calendario NAO e Angular
    Material (a 1a versao desta funcao supunha isso por causa de outros
    popups CDK do Titan, mas era suposicao errada) - e um componente
    PROPRIO do Power BI (classe "pbi-date-picker"). O botao real e
    <button class="navigate-button" aria-label="Previous month"> (ou
    "Next month" no lado direito) - confirmado direto no HTML. Usa regex
    "^Previous"/"^Next" (aria-label sempre em INGLES, mesmo com o resto do
    relatorio em portugues - mesmo padrao ja confirmado em "Start date"/
    "End date"/"Select all" no resto deste arquivo) - pelo padrao visto no
    mes (aria-label="Previous month"/"Next month"), o ano deve seguir a
    mesma convencao ("Previous year"/"Next year") quando o seletor esta na
    grade de meses - NAO confirmado ainda com HTML real desse estado
    especifico (a 1a falha aconteceu antes de chegar la).
    """
    padrao = re.compile("^Previous", re.IGNORECASE) if anterior else re.compile("^Next", re.IGNORECASE)
    elemento_visivel(frame.get_by_role("button", name=padrao), timeout_ms=timeout_ms).click()
    time.sleep(0.4)


def _clicar_celula_calendario(frame, texto_exato, timeout_ms=8000):
    """
    Clica numa celula do calendario (mes ou dia) pelo texto exato.
    CORRIGIDO (17/09/2026, mesmo HTML real citado acima): a grade de dias
    usa <button class="date-cell" aria-description="Thursday, July 16,
    2026" [disabled]> 16 </button> dentro de ".calendar-table-container"
    - dias fora do mes atual (preenchimento) E dias fora do intervalo
    permitido vem com o atributo "disabled" de verdade (confirmado: 28-30
    de junho e 1 de agosto, os dias de preenchimento ao redor de julho,
    tinham "disabled"; 1-11 de julho TAMBEM vinham disabled nesse teste -
    o Titan parece limitar quanto passado da pra escolher a partir da Data
    Final atual, nao so mes vizinho). Exclui SEMPRE celulas disabled, senao
    um dia de preenchimento com o mesmo numero do dia certo (ex: dia "1")
    podia bater e clicar na celula errada.

    Escopo generico (".calendar-table-container button", nao especifico
    ".date-cell") de proposito: o HTML real confirmado e da grade de DIAS -
    a grade de MESES (dentro do "seletor de mes") ainda nao foi vista ao
    vivo, pode usar uma classe de botao diferente pro mes em si. Casar pelo
    texto exato dentro do mesmo container estavel cobre os dois casos sem
    depender de uma classe nao confirmada.
    """
    botoes = frame.locator(".calendar-table-container button:not([disabled])")
    alvo = botoes.filter(has_text=re.compile(rf"^\s*{re.escape(texto_exato)}\s*$", re.IGNORECASE))
    elemento_visivel(alvo, timeout_ms=timeout_ms).click()
    time.sleep(0.4)


def _selecionar_mes_ano_calendario(frame, mes, ano):
    """
    Abre o "seletor de mes" (clique no botao de periodo, que mostra
    "<mes> <ano>" na grade de dias) e navega ate o ANO certo antes de clicar
    no mes - passo a passo confirmado por print real da Maria (17/09/2026).

    CORRIGIDO (17/09/2026, HTML real - ver _clicar_seta_calendario/
    _clicar_celula_calendario acima pro mesmo achado): o botao de periodo
    e <button class="month-year" aria-label="Month Picker"
    aria-description="July 2026, change month"> July 2026 </button> -
    aria-label e SEMPRE "Month Picker" (nao muda com o mes/ano mostrado),
    entao localiza pela classe estavel ".month-year" (confirmada no HTML),
    nao pelo texto (que muda a cada mes).
    """
    botao_periodo = elemento_visivel(frame.locator("button.month-year"), timeout_ms=15000)
    botao_periodo.click()
    time.sleep(0.4)

    for _ in range(24):  # teto generoso (2 anos pra qualquer lado) - nunca deveria precisar de tanto
        texto = elemento_visivel(frame.locator("button.month-year"), timeout_ms=5000).inner_text().strip()
        m = re.search(r"\d{4}", texto)
        if not m:
            raise PWTimeout(f"Seletor de mes/ano com texto inesperado (sem ano): {texto!r}")
        ano_mostrado = int(m.group())
        if ano_mostrado == ano:
            break
        _clicar_seta_calendario(frame, anterior=ano < ano_mostrado)
    else:
        raise PWTimeout(f"Nao consegui navegar ate o ano {ano} no seletor de mes/ano")

    _clicar_celula_calendario(frame, MESES_ABREV_EN[mes - 1])


def definir_periodo(frame, data_inicial, data_final):
    """
    REESCRITO (17/09/2026, passo a passo exato + prints reais mandados pela
    Maria): o campo "Data Inicial - Data Final" deixou de ser dois <input>
    de texto no formato M/d/aaaa (ver historico desta funcao abaixo) - virou
    um UNICO seletor com icone de calendario, que abre um popup ao clicar.
    A "Data Final" NAO e mais configuravel por aqui - a Maria confirmou que
    ela agora SEMPRE equivale ao dia de hoje automaticamente, entao esta
    funcao so define a "Data Inicial" (`data_final` e recebido mas
    IGNORADO de proposito, so pra nao quebrar quem ja chama esta funcao com
    os dois argumentos - ver aviso abaixo se isso deixar de ser verdade).

    Passo a passo confirmado pela Maria (prints reais, 17/09/2026):
    1. Clica no botao de icone de calendario, ao lado do campo de data.
    2. Abre um popup mostrando o mes atual em grade de dias, com um botao
       de periodo no topo (ex: "julho 2026") e duas setas (anterior/
       seguinte).
    3. Clica no botao de periodo (o texto "<mes> <ano>") pra trocar pra um
       "seletor de mes" - grade de abreviacoes de mes (jan..dez) pro ano
       mostrado.
    4. Navega ate o ANO certo com as setas, se o ano mostrado nao for o ano
       de data_inicial.
    5. Clica na abreviacao do mes certo (ex: "set" pra setembro) - volta
       pra grade de dias, agora do mes/ano escolhido.
    6. Clica no numero do dia certo (ex: "16").

    CORRIGIDO (17/09/2026, run manual #59 no GitHub Actions - HTML real
    salvo em titan_debug/set_filtro_data_nao_encontrado.html): a 1a versao
    desta funcao supunha um componente Angular Material (mesma familia CDK
    ja usada em outros popups do Titan) - suposicao ERRADA, confirmada
    testando ao vivo. O calendario e um componente PROPRIO do Power BI
    (classe "pbi-date-picker", ainda Angular por baixo mas NAO Material) -
    o botao de icone e <button class="calendar-button"
    aria-label="Start date, calendar button, selected: 7/12/2026, choose
    date">, existe um igual pro "End date" (por isso o filtro por
    aria-label^="Start date", pra nunca abrir o calendario errado). Todos
    os seletores abaixo (calendar-button/month-year/navigate-button/
    date-cell) vem desse HTML real, nao mais de suposicao - so a grade de
    MESES (dentro do "seletor de mes") ainda nao foi vista ao vivo (a 1a
    falha aconteceu ANTES de chegar la - ver _clicar_celula_calendario
    pro motivo do escopo generico usado ali).

    Se travar em qualquer passo (incluindo a grade de meses, ainda nao
    validada), o print/HTML cai em titan_debug/
    set_filtro_data_nao_encontrado.* do jeito de sempre - manda que eu
    ajusto o seletor certo.

    HISTORICO (formato antigo, 2 <input> de texto - deixou de valer em
    17/09/2026, ver acima): confirmado com teste real (24/08/2026, periodo
    01/06-23/08/2026) que o clique-e-digita acertava os dois campos
    internos do slicer ("Start date"/"End date", via aria-label) - depois
    corrigido (11/09/2026) pro formato M/d/aaaa que o campo exigia de
    verdade (ver _formatar_data_para_titan, ainda usada por
    titan_preencher_lacunas.py, que roda numa aba diferente e ainda nao foi
    confirmada com este novo fluxo de calendario).
    """
    try:
        elemento_visivel(frame.get_by_text("Data Inicial - Data Final", exact=False), timeout_ms=60000)

        botao_calendario = elemento_visivel(
            frame.locator('button.calendar-button[aria-label^="Start date"]'),
            timeout_ms=15000,
        )
        botao_calendario.click()
        time.sleep(0.5)

        dia, mes, ano = (int(p) for p in data_inicial.split("/"))
        _selecionar_mes_ano_calendario(frame, mes, ano)
        _clicar_celula_calendario(frame, str(dia))

        painel = localizar_painel(frame, "Informação Pedido")
        painel.locator("xpath=.//*[self::tr or @role='row']").first.wait_for(timeout=30000)
        time.sleep(1.5)  # da tempo da query terminar de popular as linhas visiveis, nao so a 1a
    except PWTimeout:
        salvar_diagnostico(frame, "set_filtro_data_nao_encontrado")
        raise


def filtrar(frame, nf=None, numero_pedido=None):
    """
    Abre o combobox do filtro, digita a busca de verdade (tecla por tecla),
    confirma com Enter e clica no item resultante pra de fato marcar o filtro.

    numero_pedido usa exact=False (ver docstring de marcar_item_da_lista) -
    o valor gravado no Supabase e so o ID da Torre, mas a opcao no Titan
    mostra o texto completo original ("SH<id>RT<nf>") - contains, nao
    igualdade estrita.

    CORRIGIDO (12/09/2026, causa raiz achada pela Maria a partir do HTML
    real de titan_debug/filtro_nao_encontrado.html): quando marcar_item_da_
    lista dava timeout (a NF/numero_pedido nao aparecia a tempo na lista -
    normalmente porque o filtro do pedido ANTERIOR nao tinha sido limpo
    ainda), a excecao pulava direto pro except, sem passar pelo Escape que
    fecha o dropdown - ele ficava aberto (aria-expanded="true"). O proximo
    passo (_limpar_filtro_slicer, em titan_backfill.py) tentava clicar em
    "Clear selections" desse MESMO slicer pra limpar o filtro travado, mas
    o Power BI esconde esse botao (style="display: none") enquanto o
    proprio popup do slicer esta aberto - confirmado correlacionando os
    offsets de bytes no HTML (o botao ficava a ~200 bytes ANTES do rotulo
    do painel, igual aos outros slicers, so que com display:none) - ai a
    limpeza tambem falhava, e o proximo pedido repetia o mesmo problema em
    cascata. Fecha o popup (Escape) ANTES de propagar o erro, pra o
    dropdown nunca ficar preso aberto so por causa de um timeout.
    """
    try:
        if nf:
            campo = _abrir_dropdown_e_pegar_campo_busca(frame, "Nota Fiscal de Saída")
            digitar_busca(campo, nf)
            time.sleep(1)  # deixa a lista filtrar
            marcar_item_da_lista(frame, campo, nf)
            time.sleep(1)  # deixa o filtro assincrono aplicar na tabela
            frame.page.keyboard.press("Escape")
            _esperar_tabela_refletir_filtro(frame, nf)
        if numero_pedido:
            campo = _abrir_dropdown_e_pegar_campo_busca(frame, "Número do pedido")
            digitar_busca(campo, numero_pedido)
            time.sleep(1)
            marcar_item_da_lista(frame, campo, numero_pedido, exact=False)
            time.sleep(1)
            frame.page.keyboard.press("Escape")
            _esperar_tabela_refletir_filtro(frame, numero_pedido)
    except PWTimeout:
        try:
            frame.page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception:
            pass  # mesmo se isto falhar, ainda queremos salvar o diagnostico e propagar o erro real
        salvar_diagnostico(frame, "filtro_nao_encontrado")
        raise
    time.sleep(1)


def localizar_painel(frame, titulo):
    """
    CORRIGIDO (24/08/2026, a partir do HTML real salvo em titan_debug/): o
    painel/visual do Power BI e um <div role="group" aria-label="<Titulo> ">
    (com um espaco sobrando no final do aria-label) que envolve TANTO o
    titulo quanto a grade de dados. O codigo anterior tentava achar o painel
    subindo 1 ancestral a partir do texto do titulo - mas o texto visivel do
    titulo fica bem mais fundo na arvore (dentro de varios wrappers) e o
    texto que o get_by_text acha primeiro costuma ser o tooltip
    (role="tooltip", aria-hidden) do canto, nao o titulo real - por isso
    "ancestor::*[1]" nunca pegava um container grande o suficiente pra conter
    as linhas da tabela (elas ficam em outra ramificacao da arvore). Usar o
    proprio role="group" com aria-label prefixado pelo titulo e a forma
    correta e estavel de achar o painel inteiro.

    NAO FUNCIONA PRA SLICERS (12/09/2026, achado real - run #39, HTML
    confirmado): esse padrao vale pros paineis de dados ("Informação
    Pedido", "Eventos", "Itens do pedido", etc.), cujo role="group" TEM
    aria-label direto. Os slicers ("Nota Fiscal de Saída", "Numero do
    pedido", ...) sao um tipo de visual DIFERENTE (aria-roledescription=
    "Slicer") cujo role="group" externo NAO tem aria-label nenhum - o
    titulo so existe num <h3 class="slicer-header-text" title="..."> bem
    mais fundo. Ou seja, localizar_painel(frame, "Nota Fiscal de Saída")
    NUNCA achou nada - `_limpar_filtro_slicer` usava esta funcao por
    engano; ver localizar_slicer_por_titulo pro jeito certo de achar um
    slicer especifico.
    """
    return frame.locator(f'[role="group"][aria-label^="{titulo}"]').first


def limpar_linha_selecionada(frame, titulo_painel):
    """
    Desmarca qualquer linha com selecao residual num painel de tabela
    (role="gridcell" com classe "cell-selected") - achado real da Maria
    (12/09/2026), via o print do proprio registro passo a passo: a linha
    "Rosana Bravin Klein" em "Informação Pedido" aparecia destacada mesmo
    ANTES de qualquer filtro de NF ser aplicado, o que ela identificou
    como um filtro/selecao real ja ativo no Power BI (confirmado no HTML:
    as celulas dessa linha tinham classe "cell-selected", as outras
    "cell-unselected" - a tabela e renderizada como "pivotTable", nao
    canvas puro, entao da pra achar isso de verdade via DOM).

    Clica na 1a celula selecionada que achar - Power BI DESSELECIONA ao
    clicar de novo no mesmo ponto ja selecionado (mesmo padrao de
    marcar_item_da_lista trocar a selecao de um slicer). Se nao houver
    nenhuma linha selecionada, nao faz nada (nada pra desmarcar).
    """
    painel = localizar_painel(frame, titulo_painel)
    celulas_selecionadas = painel.locator('[role="gridcell"].cell-selected')
    if celulas_selecionadas.count() > 0:
        celulas_selecionadas.first.click()
        time.sleep(0.5)


def localizar_slicer_por_titulo(frame, rotulo):
    """
    Acha o visual do slicer (role="group", aria-roledescription="Slicer")
    a partir do seu titulo visivel - diferente de localizar_painel (que so
    funciona pros paineis de dados, cujo role="group" tem aria-label
    direto - ver docstring la pro porque NAO da pra reusar aqui).

    Estrutura real confirmada em titan_debug/filtro_nao_encontrado.html
    (12/09/2026): o titulo mora num <h3 class="slicer-header-text"
    title="<Titulo>" aria-label="<Titulo>">, irmao do botao "Clear
    selections"/"Limpar selecoes" (<span class="slicer-header-clear
    enable-hover">), ambos dentro de um <div class="slicer-header-title">
    - que por sua vez fica dentro do role="group" do slicer inteiro. Subir
    ate esse role="group" (em vez de so pegar o <div class="slicer-header-
    title">) da margem pra escolher COMO achar o botao (por role, por
    classe) sem depender de mais uma camada de estrutura fragil.
    """
    titulo = frame.locator(f'h3.slicer-header-text[title="{rotulo}"]')
    return titulo.locator('xpath=ancestor::*[@role="group"][1]')


def esperar_slicer_limpo(frame, rotulo, timeout_ms=8000, intervalo=0.3):
    """
    Confirma DE VERDADE (nao so assume que o clique surtiu efeito) que o
    slicer `rotulo` voltou pra "Todos"/"All" - pedido direto da Maria
    (12/09/2026), depois do achado real do run #34: _limpar_filtro_slicer
    so clicava em "Clear selections" e seguia em frente sem checar nada,
    entao um clique que falhasse silenciosamente (ou nao surtisse efeito a
    tempo) deixava o filtro sujo pro proximo pedido sem nenhum aviso -
    causa raiz da cascata de 11 falhas em sequencia vista naquele log.

    O sinal visivel e o texto do <div class="slicer-restatement"> dentro do
    painel do slicer (confirmado em HTML real de titan_debug/filtro_nao_
    encontrado.html, 12/09/2026): mostra "All" quando limpo, ou o valor
    selecionado (ex: "1147558", "SH1254855RT1147558") quando filtrado -
    exatamente o mesmo elemento que provou visualmente, nos prints da
    Maria, que o filtro de "Numero do pedido" ficava preso depois de trocar
    de NF. So aceita "all"/"todos" (minusculo, sem acento) - texto de UI do
    proprio Power BI, mesmo padrao ja visto no placeholder de busca
    ("Pesquisar"/"Search") que segue o locale do navegador.

    CORRIGIDO (12/09/2026, mesmo achado real de localizar_slicer_por_
    titulo): usava localizar_painel, que NUNCA acha nada pra slicers (ver
    docstring de localizar_painel) - essa funcao so nunca chegava a rodar
    de verdade porque _limpar_filtro_slicer ja falhava um passo antes.
    """
    painel = localizar_slicer_por_titulo(frame, rotulo)
    limite = time.time() + timeout_ms / 1000
    while time.time() < limite:
        try:
            texto = painel.locator(".slicer-restatement").first.inner_text(timeout=1000).strip().lower()
            if texto in ("all", "todos"):
                return
        except Exception:
            pass
        time.sleep(intervalo)
    raise PWTimeout(f"Slicer '{rotulo}' nao voltou pra 'Todos'/'All' dentro de {timeout_ms}ms")


def rolar_tabela_ate_o_fim(frame):
    """A tabela 'Informacao Pedido' tem scroll horizontal proprio - sem rolar
    ate o fim, colunas como Romaneio ficam fora da tela (e Playwright as vezes
    ainda consegue ler o valor de uma celula fora da viewport, mas rolar deixa
    o comportamento mais previsivel e ajuda a debugar visualmente)."""
    try:
        painel = localizar_painel(frame, "Informação Pedido")
        painel.hover()
        for _ in range(10):
            frame.page.mouse.wheel(400, 0)
            time.sleep(0.05)
    except Exception:
        pass  # rolagem e so um reforco visual, nao e critica


def exportar_dados_do_painel(frame, titulo_painel, pasta_destino):
    """
    Automatiza "..." -> "Exportar dados" -> "Exportar" num painel do Power BI
    e devolve o caminho do arquivo baixado (.xlsx).

    Substitui rolar_tabela_ate_o_fim/extrair_registros_do_painel pra volumes
    grandes (25/08/2026, achado real pela Ivna): a tabela "Informação Pedido"
    e virtualizada, e mesmo com coletar_todos_registros (scroll + acumula) o
    titan_backfill.py so estava salvando ~90 pedidos por rodada quando a
    exportacao nativa do Power BI pra esse mesmo periodo trouxe 150.003
    linhas de verdade - o scroll estava perdendo a imensa maioria dos
    registros. Bonus: essa exportacao tambem traz "Depositante"/"Cliente" com
    texto de verdade, que o DOM nunca expos nem via accessibility tree (ver
    comentario antigo sobre isso precisar de OCR - deixa de ser necessario).

    Print real da caixa de dialogo (25/08/2026): sao 3 opcoes -
    "Dados com layout atual" (selecionada por padrao), "Dados resumidos" e
    "Dados subjacentes" - esta ultima aparece DESATIVADA ("O autor do
    relatorio desativou esta opcao"). Usamos a opcao padrao (ja vem marcada,
    nao precisa clicar em nada antes de "Exportar") - foi a que a Ivna
    confirmou trazer o dataset completo.
    """
    try:
        painel = localizar_painel(frame, titulo_painel)
        painel.hover()
        # Mesmo tipo de bug do login (27/08/2026): o Power BI trocou o
        # aria-label do botao "..." de "Mais opcoes" pra "More options",
        # quebrando o get_by_role por nome. Confirmado via titan_debug
        # artifact: o botao real tem data-testid="visual-more-options-btn"
        # (classe vcMenuBtn), estavel independente do idioma do aria-label.
        botao_opcoes = elemento_visivel(
            painel.locator('[data-testid="visual-more-options-btn"]'), timeout_ms=10000
        )
        botao_opcoes.click()

        # Mesmo idioma trocou aqui tambem (confirmado via titan_debug: o menu
        # do "..." agora mostra "Export data" em vez de "Exportar dados").
        # Aceita os dois pra nao quebrar de novo se o Power BI voltar pro
        # PT-BR em algum momento.
        item_exportar = elemento_visivel(
            frame.get_by_text(re.compile(r"^(Exportar dados|Export data)$")), timeout_ms=10000
        )
        item_exportar.click()

        botao_exportar_dialogo = elemento_visivel(
            frame.get_by_role("button", name=re.compile(r"^(Exportar|Export)$")), timeout_ms=15000
        )
        with frame.page.expect_download(timeout=180000) as download_info:
            botao_exportar_dialogo.click()
        download = download_info.value
    except PWTimeout:
        salvar_diagnostico(frame, f"exportar_dados_falhou_{titulo_painel}")
        raise

    pasta_destino.mkdir(parents=True, exist_ok=True)
    destino = pasta_destino / download.suggested_filename
    download.save_as(str(destino))
    return destino


def ler_export_xlsx(caminho):
    """
    Le um .xlsx baixado via exportar_dados_do_painel e devolve
    (filtro_aplicado, registros) - registros e uma lista de dicts
    {cabecalho: valor}, um por linha; filtro_aplicado e o texto da linha
    "Filtros aplicados: ..." quando o Titan inclui ela (ou None se nao
    incluir - ver abaixo). Datas viram string ISO (o openpyxl devolve
    datetime.datetime de verdade pras celulas de data - diferente da leitura
    por DOM/scroll, que sempre devolvia texto).

    NAO usa read_only=True (11/09/2026, achado real e bem mais grave que o
    de cima - descoberto testando um export manual real de "Informação
    Pedido"): o .xlsx que o Titan/Power BI gera vem com a tag interna
    <dimension ref="A1"/> (conferido abrindo o .xlsx como zip e olhando
    xl/worksheets/sheet1.xml) - ou seja, o proprio arquivo declara ERRADO
    que a planilha inteira e so a celula A1, mesmo tendo 14 mil+ linhas de
    dado de verdade. O modo read_only=True do openpyxl confia nessa tag pra
    otimizar a leitura em streaming, e para de iterar logo depois da
    "1 linha" declarada - um teste real confirmou que isso fazia
    ler_export_xlsx devolver ZERO registros pro arquivo inteiro, silencio-
    samente (nao um IndexError, nao um erro nenhum - so uma lista vazia,
    que main() interpreta como "nenhum registro encontrado" e segue em
    frente sem gravar nada). O modo padrao (sem read_only) ignora essa tag
    e le o XML de verdade - mais lento e mais memoria pra arquivos grandes,
    mas com o backfill agora quebrado em 1 dia por vez (ver
    _gerar_intervalos_diarios em titan_backfill.py) os arquivos ficam bem
    menores do que a mega-exportacao de 150 mil linhas de antes, entao o
    custo de memoria deixa de ser o problema critico que era.

    PULA linhas de preambulo antes do cabecalho de verdade (11/09/2026,
    achado real - confirmado com exports manuais de "Informação Pedido" e
    "Itens do pedido"): o Titan as vezes prefixa a exportacao com uma linha
    "Filtros aplicados:\n..." seguida de uma linha em branco, ANTES do
    cabecalho de verdade (Depositante/Cliente/Situacao/... pra "Informação
    Pedido"). Antes, este codigo assumia cegamente que a 1a linha do arquivo
    JA era o cabecalho - se essa linha de filtro estiver presente e nao for
    pulada, o "cabecalho" vira essa string gigante (colunas 2+ ficam None),
    e TODO registro subsequente perde o mapeamento de coluna de verdade
    (numero_nf/marca nunca sao encontrados, registro_para_supabase descarta
    tudo). Pula qualquer linha que comece com "Filtros aplicados" ou que
    esteja inteiramente vazia antes de aceitar a proxima como cabecalho -
    funciona tanto quando o preambulo existe quanto quando nao existe.

    Precisa de `pip install openpyxl` (import so aqui dentro, de proposito -
    quem so usa titan_watcher.py, que nunca chama isto, nao precisa instalar).
    """
    import datetime as _datetime
    from openpyxl import load_workbook

    wb = load_workbook(str(caminho), data_only=True)
    try:
        linhas = wb.worksheets[0].iter_rows(values_only=True)
        filtro_aplicado = None
        cabecalho = None
        for linha in linhas:
            primeira_celula = str(linha[0]).strip() if linha and linha[0] is not None else ""
            if not primeira_celula and all(v is None for v in linha):
                continue  # linha em branco entre o preambulo e o cabecalho
            if primeira_celula.startswith("Filtros aplicados"):
                filtro_aplicado = primeira_celula
                continue
            cabecalho = linha
            break
        if cabecalho is None:
            return filtro_aplicado, []

        registros = []
        for linha in linhas:
            registro = {}
            for chave, valor in zip(cabecalho, linha):
                if isinstance(valor, _datetime.datetime):
                    valor = valor.isoformat()
                registro[chave] = valor
            registros.append(registro)
        return filtro_aplicado, registros
    finally:
        wb.close()


def _celulas_da_linha(linha):
    """
    Devolve os valores de uma linha, um por celula. Preferencia pelos
    elementos de celula de verdade (preserva celula VAZIA como item proprio
    da lista, ex: "Nome Projeto" as vezes vem em branco); so cai no fallback
    de separar o innerText bruto se nao achar nenhum elemento de celula
    reconhecivel - esse fallback tem a LIMITACAO de nao conseguir marcar o
    lugar de uma celula vazia (nao ha tab/quebra sobrando pra indicar isso),
    entao pode desalinhar se a linha tiver campo em branco no meio.
    """
    cel = linha.locator("xpath=.//*[self::td or @role='cell' or @role='gridcell' or @role='columnheader']")
    valores = [c.inner_text().strip() for c in cel.all()]
    if valores:
        return valores
    bruto = linha.inner_text()
    return [p.strip() for p in bruto.replace("\t", "\n").split("\n") if p.strip()]


def extrair_registros_do_painel(painel, marcadores_cabecalho):
    """
    Le TODAS as linhas de um painel (tabela do Power BI) como uma lista de
    dicts {rotulo: valor} - o cabecalho e lido de verdade no momento da
    execucao (a UNICA linha que contem TODOS os textos em marcadores_cabecalho
    ao mesmo tempo - linhas de DADO tem o VALOR daquela coluna, nao o nome
    dela), e cada valor e emparelhado com o rotulo acima dele. Generaliza a
    abordagem "sem lista fixa de coluna" pra qualquer tabela do Titan (nao so
    'Informacao Pedido' - tambem 'Eventos' e 'Itens do pedido').
    Devolve [] se nao achar cabecalho ou nao achar nenhuma linha.
    """
    linhas = painel.locator("xpath=.//*[self::tr or @role='row']")
    try:
        linhas.first.wait_for(timeout=10000)
    except PWTimeout:
        return []
    todas = linhas.all()

    cabecalho = None
    texto_cabecalho = None
    for linha in todas:
        texto = linha.inner_text().strip()
        if texto and all(marcador in texto for marcador in marcadores_cabecalho):
            cabecalho, texto_cabecalho = linha, texto
            break
    if cabecalho is None:
        return []

    rotulos = _celulas_da_linha(cabecalho)
    if not rotulos:
        return []

    registros = []
    for linha in todas:
        texto = linha.inner_text().strip()
        if not texto or texto == texto_cabecalho:
            continue
        valores = _celulas_da_linha(linha)
        n = min(len(rotulos), len(valores))
        if n < len(rotulos):
            print(f"  (aviso: cabecalho tem {len(rotulos)} colunas mas uma linha so trouxe {len(valores)} valores - "
                  f"resultado pode estar desalinhado)", file=sys.stderr)
        registros.append(dict(zip(rotulos[:n], valores[:n])))
    return registros


def _normalizar_espacos(valor):
    """
    Colapsa qualquer sequencia de espaco (inclusive \\xa0, non-breaking space
    - confirmado no HTML real, 31/08/2026: a celula "Nome Projeto" da NF
    1283798 veio como 'BY\\xa0SAMIA', nao 'BY SAMIA') pra um unico espaco
    normal. .strip() sozinho nao resolve - o \\xa0 fica NO MEIO do texto, nao
    so nas pontas. str.split() sem argumento ja trata \\xa0 como espaco.
    """
    return " ".join(str(valor or "").split())


def _bate_marca(registro, marca_esperada):
    """Compara "Nome Projeto" (ex: "RITUARIA") com o id de marca da Torre
    (ex: "rituaria") - sem diferenciar caixa nem tipo de espaco. Sem
    marca_esperada, aceita qualquer linha (comportamento antigo).

    APICE (confirmado pela Ivna, 31/08/2026): e a UNICA marca cujo "Nome
    Projeto" vem vazio no Titan (todas as outras marcas sempre preenchem
    esse campo). Por isso celula vazia e tratada como sinal confiavel de
    "essa linha e da apice", nao como "nao bate com marca nenhuma" - sem
    isso, toda NF de apice era descartada como "nao encontrada" mesmo
    existindo certinha na tabela (lote real de NFs em 31/08/2026).

    Compara ignorando espaco por completo (nao so colapsando), nao so
    normalizando (04/09/2026, NF 1196008): o id canonico da Torre pra essa
    marca e "bysamia" (sem espaco - ver MARCAS em index.html/server.ts),
    mas o "Nome Projeto" do Titan pra essa mesma marca vem "BY SAMIA" (com
    espaco) - com normalizacao de espaco (colapsar, nao remover), as duas
    strings NUNCA batiam, e todo pedido novo de "bysamia" solicitado pela
    Torre virava "nao encontrado" pra sempre, mesmo existindo certinho no
    Titan. Sem risco de colisao entre marcas diferentes ao ignorar espaco -
    sao codigos curtos e distintos mesmo concatenados."""
    if not marca_esperada:
        return True
    projeto = _normalizar_espacos(registro.get("Nome Projeto")).upper().replace(" ", "")
    esperado = _normalizar_espacos(marca_esperada).upper().replace(" ", "")
    if not projeto:
        return esperado == "APICE"
    return projeto == esperado


def extrair_numero_pedido_torre(registro):
    """
    Deriva o numero de e-commerce da Torre (ex: "SH1099815RT") a partir do
    "Numero do Pedido" do Titan (confirmado pela Ivna, 04/09/2026): pra toda
    marca Gobeaute EXCETO apice, o "Numero do Pedido" do Titan e sempre o
    numero da Torre com a NF colada no final, sem separador (ex: NF 1196008
    -> Titan mostra "SH38497BS001196008"). Gocase fica de fora - o
    Titan/Unilog e exclusivo de Gobeaute (Gocase nem chega a ter linha aqui,
    ver bloqueio em torreSolicitarTitan/index.html - checagem so por
    seguranca).

    APICE (achado real, 11/09/2026 - print da Maria mostrando o painel
    "Informação Pedido" filtrado por AGUARDANDO_PRODUCAO): ao contrario do
    que o padrao "NF colada no numero" sugeria, o "Numero do Pedido" da
    apice no Titan e um ID PROPRIO e direto (ex: "1684668"), sem NF colada -
    confirmado que nao termina com a NF da mesma linha. Antes disto, "Nome
    Projeto" vazio (unico jeito de identificar apice - ver _bate_marca)
    fazia esta funcao devolver None sempre pra apice, descartando um valor
    que o Titan ja trazia certinho. Usa o valor bruto direto pra apice, sem
    o corte de sufixo que as outras marcas precisam.

    So corta o sufixo IGUAL a "Nota Fiscal" dessa mesma linha pras outras
    marcas - devolve None se o "Numero do Pedido" nao terminar exatamente
    com a NF (mais seguro que arriscar gravar um valor cortado errado).

    LOG DE DIAGNOSTICO (08/09/2026, investigando "maioria continua null" pos-
    fix): pra marca que DEVERIA dar pra derivar (nao apice/gocase), imprime
    os valores brutos quando mesmo assim devolve None - sem isso nao da pra
    saber se e um problema de dado real no Titan (campo "Numero do Pedido"
    veio vazio pra aquele pedido especifico) ou um problema de leitura do
    scraper (celula nao terminou de renderizar/rolar a tempo) - ver conversa
    com a Ivna, 08/09/2026."""
    projeto = _normalizar_espacos(registro.get("Nome Projeto")).upper().replace(" ", "")
    numero_pedido_titan = _normalizar_espacos(registro.get("Número do Pedido"))
    if not projeto:
        return numero_pedido_titan or None  # apice
    if projeto == "GOCASE":
        return None
    nf = _normalizar_espacos(registro.get("Nota Fiscal"))
    if not nf or not numero_pedido_titan or not numero_pedido_titan.endswith(nf):
        print(f"  (numero_pedido nao derivado pra marca {projeto}: "
              f"Nota Fiscal={nf!r} Numero do Pedido={numero_pedido_titan!r})", file=sys.stderr)
        return None
    resto = numero_pedido_titan[:-len(nf)]
    return resto or None


def _recompletar_se_numero_pedido_vazio(registro, linha, rotulos, tentativas=4, intervalo=0.4):
    """
    Investigando (08/09/2026, com a Ivna) por que "numero_pedido" continua
    None mesmo pra pedidos de marca elegivel (nao apice/gocase) que acabaram
    de ser processados com sucesso (romaneio/situacao vieram certinhos): uma
    hipotese ainda NAO confirmada com print real e a celula "Número do
    Pedido" (ou a linha inteira) nao ter terminado de re-renderizar no
    instante exato em que _celulas_da_linha le o texto (a tabela e
    virtualizada - ver docstring de exportar_dados_do_painel). Re-ler a
    MESMA linha de novo e uma tentativa barata e sem risco (nunca troca um
    valor ja lido por um pior - so tenta de novo enquanto vier vazio) antes
    de aceitar "vazio" como definitivo. Se ainda assim vier vazio apos as
    tentativas, extrair_numero_pedido_torre loga os valores brutos (ver seu
    docstring) pra confirmar se o campo e realmente vazio no Titan.
    """
    for _ in range(tentativas):
        if _normalizar_espacos(registro.get("Número do Pedido")):
            return registro
        time.sleep(intervalo)
        valores = _celulas_da_linha(linha)
        n = min(len(rotulos), len(valores))
        registro = dict(zip(rotulos[:n], valores[:n]))
    return registro


def _achar_linha_pedido(frame, numero_pedido, marca_esperada=None):
    """
    Acha, dentro de 'Informacao Pedido', a linha (registro + locator) cujo
    texto contem numero_pedido (na pratica, a NOTA FISCAL - ver aviso grande
    no topo do arquivo). Devolve (registro, locator) ou (None, None).

    CONFIRMADO COM PRINT REAL (24/08/2026): a mesma NF PODE aparecer em mais
    de uma linha, uma por marca diferente, cada uma com Romaneio DIFERENTE
    (ex: NF 940380 apareceu em 3 linhas: Kokeshi romaneio 169114, uma sem
    "Nome Projeto" visivel, e Rituaria romaneio 173827) - o risco que a Ivna
    achava raro aconteceu de verdade. Por isso, quando marca_esperada e
    passada, SO aceita a linha cuja "Nome Projeto" bate - nunca a primeira
    que so bater pela NF, que podia ser de outra marca com numero coincidente.
    Sem marca_esperada (chamada legada/CLI avulsa), usa a primeira e avisa no
    stderr se achou mais de uma, pra nao esconder a ambiguidade em silencio.

    CONHECIDO (confirmado num JSON real de execucao, 24/08/2026): as colunas
    "Depositante" e "Cliente" NAO aparecem no resultado mesmo quando visiveis
    na tela - o Power BI as renderiza de um jeito que nao expoe texto no DOM/
    acessibilidade (provavelmente canvas). Precisaria de OCR pra capturar -
    nao implementado ainda (avaliar se realmente e necessario antes).

    CORRIGIDO (03/09/2026, achado real reportado pela Ivna): o match antigo
    testava "numero_pedido in texto", onde texto era a linha INTEIRA
    concatenada (todas as colunas juntas) - um falso positivo real
    aconteceu com a NF 888537: o "Numero do Pedido" de QUALQUER linha
    incorpora a NF dela mesma por dentro (ex: "SH...0888537"), entao uma
    linha de OUTRO pedido/marca completamente diferente, cujo "Numero do
    Pedido" so por coincidencia continha os digitos "888537" em algum lugar
    (nao necessariamente no fim), batia como candidata - resultado real:
    romaneio de um pedido errado gravado pra marca "by samia" nessa NF, que
    nem existe pra essa marca no Titan. Agora exige bater EXATO (nao
    substring) contra a coluna "Nota Fiscal" OU "Numero do Pedido"
    especificamente - os dois continuam validos porque o uso avulso via CLI
    (--pedido sem --nf) busca pelo "Numero do Pedido" mesmo.
    """
    try:
        painel = localizar_painel(frame, "Informação Pedido")
        linhas = painel.locator("xpath=.//*[self::tr or @role='row']")
        linhas.first.wait_for(timeout=10000)
        todas = linhas.all()
    except PWTimeout:
        return None, None

    cabecalho = None
    texto_cabecalho = None
    for linha in todas:
        texto = linha.inner_text().strip()
        if texto and "Romaneio" in texto and "Número do Pedido" in texto:
            cabecalho, texto_cabecalho = linha, texto
            break
    if cabecalho is None:
        return None, None

    rotulos = _celulas_da_linha(cabecalho)
    if not rotulos:
        return None, None

    alvo = _normalizar_espacos(numero_pedido)
    candidatos = []  # lista de (registro, locator)
    for linha in todas:
        texto = linha.inner_text().strip()
        if not texto or texto == texto_cabecalho:
            continue
        valores = _celulas_da_linha(linha)
        n = min(len(rotulos), len(valores))
        registro = dict(zip(rotulos[:n], valores[:n]))
        if (_normalizar_espacos(registro.get("Nota Fiscal")) != alvo
                and _normalizar_espacos(registro.get("Número do Pedido")) != alvo):
            continue
        candidatos.append((registro, linha))

    if not candidatos:
        return None, None

    if marca_esperada:
        batem = [c for c in candidatos if _bate_marca(c[0], marca_esperada)]
        if batem:
            registro, linha = batem[0]
            registro = _recompletar_se_numero_pedido_vazio(registro, linha, rotulos)
            return registro, linha
        # Achou a NF, mas NENHUMA linha e da marca esperada - mais seguro
        # devolver "nao achado" do que arriscar o romaneio de outra marca.
        return None, None

    if len(candidatos) > 1:
        print(f"  (aviso: '{numero_pedido}' bateu em {len(candidatos)} linhas diferentes - sem marca pra "
              f"desambiguar, usando a primeira. Nomes de projeto encontrados: "
              f"{[c[0].get('Nome Projeto') for c in candidatos]})", file=sys.stderr)
    registro, linha = candidatos[0]
    registro = _recompletar_se_numero_pedido_vazio(registro, linha, rotulos)
    return registro, linha


def extrair_linha_por_pedido(frame, numero_pedido, marca_esperada=None):
    """
    Devolve o dict {coluna: valor} (incluindo "Romaneio", "Situação" etc.) da
    linha certa de 'Informacao Pedido' pra numero_pedido (NF, na pratica) -
    desambiguado por marca_esperada quando informada (ver _achar_linha_pedido
    pro porque isso importa de verdade). Devolve None se nao achar.
    """
    registro, _linha = _achar_linha_pedido(frame, numero_pedido, marca_esperada)
    if registro is None:
        salvar_diagnostico(frame, "tabela_linha_nao_encontrada")
    return registro


def clicar_na_linha(frame, numero_pedido, marca_esperada=None):
    """
    Clica na linha CERTA do pedido (mesma desambiguacao por marca de
    extrair_linha_por_pedido) pra filtrar os paineis 'Eventos' e 'Itens do
    pedido' so pra esse registro.

    CORRIGIDO (24/08/2026): antes clicava no primeiro elemento de texto igual
    em QUALQUER lugar da pagina (frame.get_by_text(...).first) - com NF
    repetida entre marcas (confirmado, ver _achar_linha_pedido), isso podia
    clicar na linha de OUTRA marca. Agora clica exatamente na linha
    localizada por _achar_linha_pedido; so cai no comportamento antigo (busca
    de texto generica) se essa linha nao for re-achada por algum motivo (ex:
    o DOM mudou entre a chamada de extrair_linha_por_pedido e esta).
    """
    _registro, linha = _achar_linha_pedido(frame, numero_pedido, marca_esperada)
    if linha is not None:
        linha.click()
    else:
        frame.get_by_text(numero_pedido, exact=False).first.click()
    time.sleep(1)


def extrair_eventos(frame):
    """
    Le a timeline do painel 'Eventos' - devolve uma lista de dicts
    {"Horário da Situação": ..., "Situação": ...} (rotulos exatos como
    aparecem na tela). Chame DEPOIS de clicar_na_linha, senao pode vir com
    eventos de todos os pedidos que bateram no filtro, nao so do que voce
    quer.
    """
    try:
        painel = localizar_painel(frame, "Eventos")
    except PWTimeout:
        salvar_diagnostico(frame, "painel_eventos_nao_encontrado")
        return []
    return extrair_registros_do_painel(painel, ["Situação"])


def extrair_itens_pedido(frame):
    """
    Le a tabela 'Itens do pedido' (Codigo/Descricao/Ean/Quantidade/Tipo/
    Checkout Realizado/Valor Total/Valor Unitario) - devolve uma lista de
    dicts, um por item, com os rotulos exatos como aparecem na tela (inclui
    "Ean"). Chame DEPOIS de clicar_na_linha, mesma ressalva de extrair_eventos.
    """
    try:
        painel = localizar_painel(frame, "Itens do pedido")
    except PWTimeout:
        salvar_diagnostico(frame, "painel_itens_nao_encontrado")
        return []
    return extrair_registros_do_painel(painel, ["Código", "Ean"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nf", help="Numero da Nota Fiscal de Saida")
    parser.add_argument("--pedido", help="Numero do pedido (ex: SH1099815RT) - use junto com --nf pra evitar ambiguidade quando a NF se repete entre marcas")
    parser.add_argument("--marca", help="Nome do projeto no Titan (ex: RITUARIA, KOKESHI) - CONFIRMADO que a mesma NF pode aparecer pra mais de uma marca com romaneio diferente; sem isso, usa a primeira linha que bater e avisa no terminal se havia mais de uma")
    parser.add_argument("--out", help="Arquivo de saida (JSON). Sem isso, imprime no terminal.")
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela do navegador - so use depois de validar visualmente sem esta flag")
    args = parser.parse_args()

    if not args.nf and not args.pedido:
        parser.error("informe --nf e/ou --pedido")

    email = os.environ.get("TITAN_EMAIL")
    senha = os.environ.get("TITAN_SENHA")
    if not email or not senha:
        print("Defina TITAN_EMAIL e TITAN_SENHA como variaveis de ambiente antes de rodar.", file=sys.stderr)
        print("Ex (PowerShell): $env:TITAN_EMAIL='...'; $env:TITAN_SENHA='...'", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            login(page, email, senha)
            frame = get_dashboard_frame(page)
            filtrar(frame, nf=args.nf, numero_pedido=args.pedido)
            rolar_tabela_ate_o_fim(frame)

            chave_busca = args.pedido or args.nf
            pedido_data = extrair_linha_por_pedido(frame, chave_busca, marca_esperada=args.marca)
            if pedido_data is None:
                print(f"Nenhuma linha encontrada para {chave_busca!r}" + (f" da marca {args.marca!r}" if args.marca else "") + ".", file=sys.stderr)
                sys.exit(2)

            clicar_na_linha(frame, chave_busca, marca_esperada=args.marca)
            eventos = extrair_eventos(frame)
            itens = extrair_itens_pedido(frame)

            resultado = {"pedido": pedido_data, "eventos": eventos, "itens": itens}
        finally:
            browser.close()

    saida = json.dumps(resultado, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(saida)
        print(f"Salvo em {args.out}")
    else:
        print(saida)


if __name__ == "__main__":
    main()
