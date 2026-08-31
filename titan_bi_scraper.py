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


def marcar_item_da_lista(frame, campo_busca, valor):
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
    """
    campo_busca.press("Enter")
    time.sleep(1)  # deixa a lista filtrar
    opcao = elemento_visivel(frame.get_by_role("option", name=str(valor), exact=True))
    opcao.click()


def digitar_busca(campo, valor):
    """
    press_sequentially() digita tecla por tecla (precisa disparar
    keydown/keypress de verdade pro filtro do Power BI reagir - ver
    marcar_item_da_lista), mas ao contrario de fill() ele INSERE no cursor em
    vez de substituir o conteudo. Confirmado com print real: sem limpar antes,
    o valor anterior do campo ficou concatenado com o novo (busca virou
    "6486299980880" em vez de "9980880", e claro deu "Nenhum resultado
    encontrado"). Seleciona tudo e apaga antes de digitar.
    """
    campo.click()
    campo.press("Control+A")
    campo.press("Delete")
    campo.press_sequentially(str(valor), delay=60)


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


def filtrar(frame, nf=None, numero_pedido=None):
    """
    Abre o combobox do filtro, digita a busca de verdade (tecla por tecla),
    confirma com Enter e clica no item resultante pra de fato marcar o filtro.
    """
    try:
        if nf:
            campo = _abrir_dropdown_e_pegar_campo_busca(frame, "Nota Fiscal de Saída")
            digitar_busca(campo, nf)
            time.sleep(1)  # deixa a lista filtrar
            marcar_item_da_lista(frame, campo, nf)
            time.sleep(1)  # deixa o filtro assincrono aplicar na tabela
            frame.page.keyboard.press("Escape")
        if numero_pedido:
            campo = _abrir_dropdown_e_pegar_campo_busca(frame, "Número do pedido")
            digitar_busca(campo, numero_pedido)
            time.sleep(1)
            marcar_item_da_lista(frame, campo, numero_pedido)
            time.sleep(1)
            frame.page.keyboard.press("Escape")
    except PWTimeout:
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
    """
    return frame.locator(f'[role="group"][aria-label^="{titulo}"]').first


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
    Le um .xlsx baixado via exportar_dados_do_painel e devolve uma lista de
    dicts {cabecalho: valor}, um por linha. Le em modo streaming
    (read_only=True) pra nao pesar na memoria - exportacoes reais chegaram a
    150.003 linhas (25/08/2026). Datas viram string ISO (o openpyxl devolve
    datetime.datetime de verdade pras celulas de data - diferente da leitura
    por DOM/scroll, que sempre devolvia texto).

    Precisa de `pip install openpyxl` (import so aqui dentro, de proposito -
    quem so usa titan_watcher.py, que nunca chama isto, nao precisa instalar).
    """
    import datetime as _datetime
    from openpyxl import load_workbook

    wb = load_workbook(str(caminho), read_only=True, data_only=True)
    try:
        linhas = wb.worksheets[0].iter_rows(values_only=True)
        cabecalho = next(linhas)
        registros = []
        for linha in linhas:
            registro = {}
            for chave, valor in zip(cabecalho, linha):
                if isinstance(valor, _datetime.datetime):
                    valor = valor.isoformat()
                registro[chave] = valor
            registros.append(registro)
        return registros
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
    existindo certinha na tabela (lote real de NFs em 31/08/2026)."""
    if not marca_esperada:
        return True
    projeto = _normalizar_espacos(registro.get("Nome Projeto")).upper()
    esperado = _normalizar_espacos(marca_esperada).upper()
    if not projeto:
        return esperado == "APICE"
    return projeto == esperado


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

    candidatos = []  # lista de (registro, locator)
    for linha in todas:
        texto = linha.inner_text().strip()
        if not texto or texto == texto_cabecalho or numero_pedido not in texto:
            continue
        valores = _celulas_da_linha(linha)
        n = min(len(rotulos), len(valores))
        candidatos.append((dict(zip(rotulos[:n], valores[:n])), linha))

    if not candidatos:
        return None, None

    if marca_esperada:
        batem = [c for c in candidatos if _bate_marca(c[0], marca_esperada)]
        if batem:
            return batem[0]
        # Achou a NF, mas NENHUMA linha e da marca esperada - mais seguro
        # devolver "nao achado" do que arriscar o romaneio de outra marca.
        return None, None

    if len(candidatos) > 1:
        print(f"  (aviso: '{numero_pedido}' bateu em {len(candidatos)} linhas diferentes - sem marca pra "
              f"desambiguar, usando a primeira. Nomes de projeto encontrados: "
              f"{[c[0].get('Nome Projeto') for c in candidatos]})", file=sys.stderr)
    return candidatos[0]


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
