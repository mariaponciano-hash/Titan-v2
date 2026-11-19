"""
titan_romaneio_links.py

Roda 2x por dia no GitHub Actions (ver .github/workflows/titan_romaneio_links.yml)
e preenche infos_titan.romaneio_link dos pedidos EMBARCADO, casando o
romaneio do pedido com o link do PDF na planilha "Registro dos arquivos
baixados", aba "Controle do Romaneio" (PLANILHA_ROMANEIOS_ID abaixo).

FONTE DOS LINKS: PLANILHA, NAO MAIS A PASTA DO DRIVE (16/11/2026, pedido da
Ivna). Ate aqui o script procurava o PDF por nome nas subpastas de
transportadora da pasta "Romaneios" do Drive. A pasta parou de ser a fonte:
os PDFs passaram a ser registrados na planilha, uma linha por romaneio
(Romaneio Numero, Depositante, Transportadora, Data, Hora, UF, Link), de
03/11/2025 em diante. As runs agendadas de 13 a 15/11 gastavam os 25 min
inteiros procurando no Drive. Agora o script baixa a planilha inteira uma vez
(CSV, ~16 mil linhas em 16/11) e casa em memoria, sem uma busca por romaneio.

ACESSO A PLANILHA (17/11/2026): ela passou a abrir so para a Gocase
("Qualquer pessoa neste grupo com o link"), e o export publico deu 401. Com
os secrets GOOGLE_OAUTH_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN (os mesmos
da versao do Drive) o script baixa o CSV pela API do Drive (files.export, que
exporta a primeira aba) em nome dessa conta. Sem os secrets, tenta o export
publico. Em qualquer falha de acesso para com erro claro e nao grava nada.

CHAVE: ROMANEIO + DEPOSITANTE. Desde 31/10/2026 a planilha traz numeros
pequenos (343, 399...) para o BEAUTY HUB ES, outra numeracao que pode repetir
em outro CD. Casar so pelo numero poderia gravar o PDF de outro deposito.
Conferido em 16/11 com amostra real: onde o numero bate, o depositante da
planilha e o da infos_titan sao iguais em 100% dos pedidos.

COBERTURA (amostra de 16/11/2026): a planilha nao tem todos os romaneios.
Pedidos de agosto e comeco de setembro acharam ~60% dos romaneios; de 25/09
a 05/11 quase nenhum (ex.: 183084-183372 do BEAUTY HUB ES, expedidos em
19/10, nao estao la). O que nao bate fica com romaneio_link nulo e e
tentado de novo em toda volta, entao se cura quando a linha aparecer.

GRAVACAO: um PATCH por (romaneio, depositante), filtrado por
romaneio_link=is.null - preenche de uma vez todos os pedidos daquele
romaneio, inclusive os que ainda nao entraram na pagina (usa o indice
infos_titan_romaneio_idx). Cada par e gravado uma vez por execucao.

PAGINACAO E CURSOR (de 27 e 31/08/2026, mantidos): pagina por atualizado_em
com cursor salvo em titan_romaneio_links_cursor (ver
infos_titan_romaneio_links_cursor.sql), avancado linha a linha dentro do
orcamento de tempo; quando a janela acaba, o cursor e apagado e a proxima
execucao recomeca do inicio, dando nova chance a quem nao casou. A consulta
usa o indice infos_titan_romaneio_link_pendente_idx.

SIMULACAO: com --simular (ou SIMULAR=1), so conta o que casaria, sem gravar
link nem mexer no cursor.

NAO MEXE em nada alem de romaneio_link - situacao e romaneio continuam
vindo so do titan_backfill.py.
"""
import csv
import io
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"
CURSOR_TABELA = "titan_romaneio_links_cursor"

PLANILHA_ROMANEIOS_ID = "10mwa4MpkZmZVfc5GYv2AR5wiHoJ0mpxKAXEuB5mI2h0"
PLANILHA_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{PLANILHA_ROMANEIOS_ID}/export?format=csv&gid=0"
)
# Desde 17/11/2026 a planilha so abre para quem esta logado na Gocase
# ("Qualquer pessoa neste grupo com o link"), e o export publico acima passou
# a dar 401. Com os secrets GOOGLE_OAUTH_* (o mesmo OAuth Client e refresh
# token que a versao do Drive usava desde 27/08/2026) o script baixa o CSV
# pela API do Drive em nome dessa conta. Sem eles, tenta o export publico.
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_EXPORT_URL = (
    f"https://www.googleapis.com/drive/v3/files/{PLANILHA_ROMANEIOS_ID}/export?mimeType=text/csv"
)

# 25min - folga sob o timeout de 30min do job (titan_romaneio_links.yml).
ORCAMENTO_SEGUNDOS = 25 * 60
PAGINA = 1000
# A planilha comeca em 03/11/2025. 120 dias pega os pedidos EMBARCADO desde
# meados de julho/2026, que a janela antiga de 45 dias ja tinha deixado para
# tras sem PDF. So vale quando nao ha cursor salvo.
JANELA_DIAS = 120


def _http_json(method, url, headers=None, data=None, tentativas=3):
    """3 tentativas com backoff curto - o runner do GitHub Actions ja mostrou
    500 pontual (27/08/2026). Na ultima falha, imprime o corpo do erro."""
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    for tentativa in range(tentativas):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                corpo = resp.read()
                return json.loads(corpo) if corpo else None
        except urllib.error.HTTPError as e:
            corpo_erro = e.read().decode("utf-8", errors="replace")
            if e.code < 500 or tentativa == tentativas - 1:
                print(f"  HTTP {e.code} em {method} {url}: {corpo_erro[:500]}", file=sys.stderr)
                raise
            time.sleep(0.5 * (tentativa + 1) * 3)
        except (urllib.error.URLError, TimeoutError) as e:
            if tentativa == tentativas - 1:
                print(f"  falha de rede em {method} {url}: {e}", file=sys.stderr)
                raise
            time.sleep(0.5 * (tentativa + 1) * 3)


def _sem_acento(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(texto or "")) if unicodedata.category(c) != "Mn"
    )


def normalizar_depositante(texto):
    return " ".join(_sem_acento(texto).upper().split())


def normalizar_romaneio(texto):
    valor = str(texto if texto is not None else "").strip()
    if valor.endswith(".0"):
        valor = valor[:-2]
    return valor


def obter_access_token(env=None):
    """Troca o refresh token por um access token. Devolve None quando os
    secrets GOOGLE_OAUTH_* nao estao definidos (ai o script usa o export
    publico). Se estao definidos e o Google recusa, derruba com o motivo:
    melhor o job ficar vermelho do que cair calado no export publico."""
    env = os.environ if env is None else env
    client_id = env.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = env.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        return None
    corpo = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=corpo, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as e:
        motivo = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(
            f"O Google recusou o refresh token dos secrets GOOGLE_OAUTH_* (HTTP {e.code}: "
            f"{motivo}). Gere um novo e atualize o secret GOOGLE_OAUTH_REFRESH_TOKEN. "
            "Nada foi gravado."
        ) from e


def baixar_planilha(env=None):
    token = obter_access_token(env)
    if token:
        url, origem = DRIVE_EXPORT_URL, "API do Drive (GOOGLE_OAUTH_*)"
        headers = {"Authorization": f"Bearer {token}"}
    else:
        url, origem = PLANILHA_CSV_URL, "export publico"
        headers = {}
    headers["User-Agent"] = "titan-romaneio-links"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            tipo = resp.headers.get("Content-Type", "")
            texto = resp.read().decode("utf-8-sig", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            dica = ("a conta dos secrets GOOGLE_OAUTH_* precisa ter acesso de leitura a planilha"
                    if token else
                    "a planilha nao e mais publica por link; defina os secrets GOOGLE_OAUTH_*")
            raise RuntimeError(
                f"Sem acesso a planilha de romaneios via {origem} (HTTP {e.code}): {dica}. "
                "Nada foi gravado."
            ) from e
        raise
    print(f"Planilha baixada via {origem}.")
    return validar_csv(texto, tipo)


def validar_csv(texto, tipo=""):
    if "text/html" in tipo or texto.lstrip().lower().startswith(("<!doctype", "<html")):
        raise RuntimeError(
            "A planilha de romaneios devolveu HTML em vez de CSV - sem acesso a ela "
            "(confira os secrets GOOGLE_OAUTH_* e o compartilhamento). Nada foi gravado."
        )
    return texto


def carregar_planilha(texto_csv):
    """Devolve {(romaneio, DEPOSITANTE): link}. Acha as colunas pelo nome
    (sem acento e sem diferenca de maiuscula), entao sobrevive a coluna nova
    ou trocada de lugar. Linha sem numero, depositante ou link e ignorada; se
    o mesmo par aparecer duas vezes, vale a linha mais abaixo (a mais nova)."""
    leitor = csv.reader(io.StringIO(texto_csv))
    cabecalho = next(leitor, None)
    if not cabecalho:
        raise RuntimeError("Planilha de romaneios vazia.")
    nomes = [normalizar_depositante(c) for c in cabecalho]

    def coluna(*opcoes):
        for opcao in opcoes:
            if opcao in nomes:
                return nomes.index(opcao)
        raise RuntimeError(f"Coluna {opcoes[0]!r} nao achada na planilha. Cabecalho: {cabecalho}")

    i_rom = coluna("ROMANEIO NUMERO", "ROMANEIO")
    i_dep = coluna("DEPOSITANTE")
    i_link = coluna("LINK")
    mapa = {}
    for linha in leitor:
        if len(linha) <= max(i_rom, i_dep, i_link):
            continue
        romaneio = normalizar_romaneio(linha[i_rom])
        depositante = normalizar_depositante(linha[i_dep])
        link = linha[i_link].strip()
        if romaneio and depositante and link.startswith("http"):
            mapa[(romaneio, depositante)] = link
    return mapa


def _supabase_request(method, path, body=None, prefer=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    if prefer:
        headers["Prefer"] = prefer
    elif method == "PATCH":
        headers["Prefer"] = "return=minimal"
    return _http_json(method, url, headers=headers, data=body)


def ler_cursor():
    resp = _supabase_request("GET", f"{CURSOR_TABELA}?select=ultimo_atualizado_em&id=eq.true")
    if resp and resp[0].get("ultimo_atualizado_em"):
        return resp[0]["ultimo_atualizado_em"]
    return None


def gravar_cursor(valor):
    _supabase_request(
        "POST",
        f"{CURSOR_TABELA}?on_conflict=id",
        {"id": True, "ultimo_atualizado_em": valor},
        prefer="resolution=merge-duplicates,return=minimal",
    )


def resetar_cursor():
    gravar_cursor(None)


def buscar_pagina(cutoff, operador):
    q = (
        f"{TABELA}?situacao=eq.EMBARCADO&romaneio=not.is.null"
        f"&romaneio_link=is.null&atualizado_em={operador}.{urllib.parse.quote(cutoff)}"
        f"&select=numero_nf,marca,romaneio,depositante,atualizado_em"
        f"&order=atualizado_em.asc&limit={PAGINA}"
    )
    return _supabase_request("GET", q) or []


def gravar_link_romaneio(romaneio, depositante_banco, link):
    """Preenche o link em todos os pedidos do romaneio naquele depositante que
    ainda estao sem link. Filtra pelo romaneio e pelo depositante como estao
    gravados na infos_titan (nao os normalizados), para o eq do PostgREST bater."""
    path = (
        f"{TABELA}?romaneio=eq.{urllib.parse.quote(romaneio)}"
        f"&depositante=eq.{urllib.parse.quote(depositante_banco)}"
        f"&romaneio_link=is.null"
    )
    _supabase_request("PATCH", path, {"romaneio_link": link})


def processar_pagina(pagina, mapa, inicio, orcamento_segundos, gravar, ja_gravados, agora=time.monotonic):
    """Percorre a pagina na ordem (atualizado_em.asc). Para cada pedido cujo
    (romaneio, depositante) esta na planilha, grava o link do romaneio uma vez
    por execucao (ja_gravados guarda os pares feitos). Pode parar em qualquer
    linha quando o orcamento acaba.

    Retorna (cursor_seguro, completou, pedidos_casados, romaneios_gravados,
    romaneios_fora_da_planilha). cursor_seguro e None so se nenhuma linha
    foi vista."""
    cursor_seguro = None
    pedidos_casados = 0
    romaneios_gravados = 0
    fora = set()
    for item in pagina:
        if agora() - inicio > orcamento_segundos:
            return cursor_seguro, False, pedidos_casados, romaneios_gravados, len(fora)
        # Dois pares por pedido (16/11/2026, revisao do PR #15): o normalizado
        # so serve para achar o link na planilha; o PATCH filtra com o valor
        # exatamente como esta na infos_titan, e o "ja gravado" usa esse mesmo
        # valor. Assim "Barbours RJ" e "BARBOURS RJ" no mesmo romaneio viram
        # dois PATCH, e um depositante com espaco sobrando nao e dado como
        # gravado sem o eq do PostgREST ter batido.
        romaneio_banco = "" if item.get("romaneio") is None else str(item.get("romaneio"))
        depositante_banco = str(item.get("depositante") or "")
        chave_planilha = (normalizar_romaneio(romaneio_banco), normalizar_depositante(depositante_banco))
        chave_gravacao = (romaneio_banco, depositante_banco)
        if chave_planilha[0] and chave_planilha[1]:
            link = mapa.get(chave_planilha)
            if link:
                pedidos_casados += 1
                if chave_gravacao not in ja_gravados:
                    try:
                        gravar(romaneio_banco, depositante_banco, link)
                        ja_gravados.add(chave_gravacao)
                        romaneios_gravados += 1
                    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                        print(f"  [romaneio {romaneio_banco} {depositante_banco}] erro gravando, pulando: {e}")
            else:
                fora.add(chave_planilha)
        cursor_seguro = item["atualizado_em"]
    return cursor_seguro, True, pedidos_casados, romaneios_gravados, len(fora)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    simular = "--simular" in argv or os.environ.get("SIMULAR") == "1"

    mapa = carregar_planilha(baixar_planilha())
    if not mapa:
        print("A planilha de romaneios nao trouxe nenhuma linha com link - nada a fazer.", file=sys.stderr)
        sys.exit(1)
    print(f"Planilha: {len(mapa)} romaneio(s) com link."
          + (" SIMULACAO: nada sera gravado." if simular else ""))

    def gravar(romaneio, depositante, link):
        if not simular:
            gravar_link_romaneio(romaneio, depositante, link)

    cursor_salvo = None if simular else ler_cursor()
    if cursor_salvo:
        cutoff, operador = cursor_salvo, "gt"
        print(f"Continuando do cursor salvo: {cutoff}")
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=JANELA_DIAS)).isoformat()
        operador = "gte"
        print(f"Comecando do inicio da janela de {JANELA_DIAS} dias: {cutoff}")

    inicio = time.monotonic()
    ja_gravados = set()
    total_pedidos = pedidos_casados = romaneios_gravados = 0
    pagina_num = 0
    while True:
        pagina_num += 1
        try:
            pagina = buscar_pagina(cutoff, operador)
        except urllib.error.HTTPError as e:
            print(f"  pagina {pagina_num} (apos {cutoff}) falhou ({e.code}), parando - "
                  f"cursor ja salvo ate a anterior.", file=sys.stderr)
            break

        if not pagina:
            if pagina_num == 1:
                print("Nenhum pedido EMBARCADO sem link de romaneio na janela.")
            if not simular:
                resetar_cursor()
            print("Fim da janela - a proxima execucao recomeca do inicio.")
            break

        cursor_pagina, completou, n_casados, n_gravados, n_fora = processar_pagina(
            pagina, mapa, inicio, ORCAMENTO_SEGUNDOS, gravar, ja_gravados
        )
        total_pedidos += len(pagina)
        pedidos_casados += n_casados
        romaneios_gravados += n_gravados
        print(f"[pagina {pagina_num}] {len(pagina)} pedido(s), {n_casados} com romaneio na planilha, "
              f"{n_gravados} romaneio(s) novo(s), {n_fora} romaneio(s) fora da planilha.")

        if cursor_pagina:
            cutoff, operador = cursor_pagina, "gt"
            if not simular:
                gravar_cursor(cutoff)

        if not completou:
            print("Orcamento de tempo esgotado - cursor salvo ate onde deu.")
            break
        if len(pagina) < PAGINA:
            if not simular:
                resetar_cursor()
            print("Fim da janela - a proxima execucao recomeca do inicio.")
            break

    acao = "casariam" if simular else "gravados"
    print(f"Resumo: {total_pedidos} pedido(s) sem link lidos, {pedidos_casados} com romaneio na planilha; "
          f"{romaneios_gravados} romaneio(s) {acao} (cada um preenche todos os pedidos dele).")


if __name__ == "__main__":
    main()
