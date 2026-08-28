"""
titan_romaneio_links.py

Roda periodicamente (GitHub Actions, ver .github/workflows/titan_romaneio_links.yml)
e preenche infos_titan.romaneio_link pra pedidos ja EMBARCADO, casando o
numero do romaneio com o PDF correspondente numa pasta do Google Drive
("Romaneios", compartilhada com a conta gogroup, id ROMANEIOS_FOLDER_ID
abaixo).

QUEM RODA ISSO: o proprio GitHub Actions, sem supervisao - mesmo padrao ja
aceito pro titan_backfill.py e pro titan-watcher-worker (Cloudflare):
nenhuma dessas automacoes pede confirmacao humana, todas usam credenciais
de servico guardadas como secret.

AUTENTICACAO: OAuth2 (nao Service Account - a Ivna ja tinha criado um
OAuth Client no Google Cloud e preferiu seguir por ali). Usa um refresh
token gerado uma vez manualmente (OAuth Playground, 27/08/2026) pra pedir
um access token novo a cada execucao - nunca expira sozinho, so se
revogado. Precisa de 3 variaveis de ambiente:
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    GOOGLE_OAUTH_REFRESH_TOKEN

BUSCA ESCOPADA POR SUBPASTA (corrigido 27/08/2026 - primeira versao
buscava "name contains {romaneio}" sem restringir pasta e nao achou NADA
em 1000 tentativas): a API do Drive nao indexa de forma confiavel uma
busca por nome que atravesse subpastas de uma pasta so compartilhada (nao
"adicionada ao Meu Drive") - precisa escopar explicitamente pelos ids das
subpastas (uma por transportadora: ANJUN EXPRESS, CORREIOS, DIALOGO
LOGISTICA, DIASLOG, J&T EXPRESS, L4B, LOG SERVICOS, PDFs - listadas uma
vez no inicio da execucao, nunca hardcoded, pra sobreviver se criarem uma
pasta nova). Como infos_titan nao guarda qual transportadora fez o
pedido, a query busca em TODAS as subpastas de uma vez (clausula "OR" de
parents), nao uma por vez.

PADRAO DE NOME DO ARQUIVO (confirmado com print real da pasta,
27/08/2026): "{romaneio} - {TRANSPORTADORA} - {DD-MM-AAAA}_{HH.MM}.pdf",
ex: "166015 - ANJUN EXPRESS - 10-06-2026_17.37.pdf". Filtra em Python por
nome comecando com "{romaneio} " pra nao confundir com um romaneio que
seja substring de outro (ex: buscar "166" nao deveria bater com
"1660158").

DEDUP POR ROMANEIO (corrigido 27/08/2026): um romaneio e um lote que
cobre VARIOS pedidos - o log da primeira versao mostrou o mesmo romaneio
repetido ate 6x. Busca no Drive so uma vez por romaneio unico, aplica o
link achado a todos os pedidos daquele romaneio.

DUPLICATA DE ARQUIVO: se mais de um PDF bater pro mesmo romaneio
(aconteceu no print real - 166282 apareceu duas vezes, datas diferentes),
pega o mais recente por modifiedTime.

INDICES NO POSTGRES (27/08/2026 - ver infos_titan_add_status_atualizado_index.sql):
a consulta filtra por situacao+romaneio_link+atualizado_em - sem indice
pra essa combinacao, o Postgres varria a tabela inteira e estourava
statement timeout (57014) toda vez, nao importa como a paginacao do lado
do cliente fosse feita. Precisa rodar aquele SQL antes deste script
funcionar de verdade.

PAGINACAO POR CURSOR, NAO OFFSET: OFFSET fica mais lento quanto mais
fundo vai (o Postgres precisa escanear/pular todas as linhas anteriores
toda vez) - com uma janela de JANELA_DIAS dias ainda sobram centenas de
milhares de linhas reais (volume legitimo do negocio - varias dezenas de
milhares de pedidos por dia), entao OFFSET profundo estourava statement
timeout mesmo com indice. Cursor por atualizado_em (ordenado, "pegue so o
que e mais novo que o ultimo que eu vi") nao degrada com a profundidade.
A partir da 2a pagina o corte usa "gt" (estritamente maior), nunca "gte" -
de proposito, pra garantir que o cursor sempre avanca (com "gte" repetido,
uma pagina cheia so de linhas com timestamp identico faria o corte nunca
mudar = loop infinito). Isso pode deixar de fora, so nesta passada, linhas
empatadas com a ultima vista que nao couberam na mesma pagina - sem
problema, ainda batem romaneio_link=is.null e entram na proxima passada.

CURSOR PERSISTIDO ENTRE EXECUCOES (27/08/2026, a pedido da Ivna - ver
infos_titan_romaneio_links_cursor.sql): sem isso, toda execucao
recomecava do inicio da janela de JANELA_DIAS dias - um romaneio sem PDF
na pasta (ainda nao subiu) ficava sempre na FRENTE da fila (ordenada por
atualizado_em) e era retentado em toda execucao, gastando o tempo do
timeout de 30min antes do script alcancar romaneios mais novos nunca
tentados. Agora guarda em titan_romaneio_links_cursor (tabela singleton)
o atualizado_em ate onde uma PAGINA INTEIRA foi processada (buscada +
todos os romaneios dela tentados no Drive) - a proxima execucao continua
dali via "gt", em vez de "gte" do inicio da janela. Quando uma pagina
volta menor que PAGINA (alcancou o fim - nao ha mais nada mais novo na
janela), o cursor e resetado (apagado) de proposito: fecha uma volta
completa e a proxima execucao recomeca do zero, dando aos romaneios que
nunca foram achados uma nova chance (o PDF pode ter subido nesse meio
tempo). Se o job for interrompido no meio de uma pagina (timeout do
GitHub Actions), so aquela pagina em andamento e re-tentada na proxima
execucao - nao a janela inteira de novo.

SEM LIMITE DE TENTATIVAS: um romaneio que nunca acha PDF correspondente
(documento ainda nao subiu na pasta) e retentado a cada volta completa do
cursor - aceitavel porque agora e barato (1 busca por romaneio UNICO, nao
por pedido) e se autocura sozinho assim que o documento aparecer.

NAO MEXE em nada alem de romaneio_link - status/situacao/romaneio em si
continuam vindo so do titan_cf_worker/titan_backfill.py.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"
CURSOR_TABELA = "titan_romaneio_links_cursor"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
ROMANEIOS_FOLDER_ID = "1EHi3gB7b0fYZ7nDODjWWyzcRRAlBFdlj"

PAGINA = 1000
# So considera pedidos EMBARCADO recentemente (por atualizado_em, que pra um
# pedido ja EMBARCADO reflete quando ele chegou nesse status - o recheck
# nao toca mais nele depois, EMBARCADO e status final). So usado quando NAO
# ha cursor salvo (primeira execucao, ou logo apos uma volta completa) -
# ver CURSOR PERSISTIDO no docstring acima.
JANELA_DIAS = 45


def _http_json(method, url, headers=None, data=None, form=False, tentativas=3):
    """3 tentativas com backoff curto (500ms/1500ms) - o runner do GitHub
    Actions ja mostrou um 500 pontual (27/08/2026) numa consulta que
    funcionou normal rodando de outro lugar, entao provavelmente e rede/rate
    limit transitorio, nao erro de sintaxe. Na ultima falha, imprime o corpo
    da resposta de erro (a versao anterior so mostrava o codigo HTTP, sem
    dizer o motivo real)."""
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    for tentativa in range(tentativas):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
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


def obter_access_token():
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        print("Defina GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / "
              "GOOGLE_OAUTH_REFRESH_TOKEN como variaveis de ambiente.", file=sys.stderr)
        sys.exit(1)
    resp = _http_json("POST", GOOGLE_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, form=True)
    return resp["access_token"]


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
        f"&select=numero_nf,marca,romaneio,atualizado_em"
        f"&order=atualizado_em.asc&limit={PAGINA}"
    )
    return _supabase_request("GET", q) or []


def gravar_link(numero_nf, marca, link):
    path = (
        f"{TABELA}?numero_nf=eq.{urllib.parse.quote(numero_nf)}"
        f"&marca=eq.{urllib.parse.quote(marca)}"
    )
    _supabase_request("PATCH", path, {"romaneio_link": link})


def listar_subpastas(access_token, pasta_id):
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"'{pasta_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    params = urllib.parse.urlencode({"q": query, "fields": "files(id,name)", "pageSize": 100})
    resp = _http_json("GET", f"{GOOGLE_DRIVE_FILES_URL}?{params}", headers=headers)
    return resp.get("files") or []


def achar_arquivo_do_romaneio(access_token, romaneio, subpasta_ids):
    headers = {"Authorization": f"Bearer {access_token}"}
    clausula_pastas = " or ".join(f"'{pid}' in parents" for pid in subpasta_ids)
    query = (
        f"({clausula_pastas}) and name contains '{romaneio}' "
        f"and mimeType = 'application/pdf' and trashed = false"
    )
    params = urllib.parse.urlencode({
        "q": query,
        "fields": "files(id,name,webViewLink,modifiedTime)",
        "pageSize": 20,
    })
    resp = _http_json("GET", f"{GOOGLE_DRIVE_FILES_URL}?{params}", headers=headers)
    candidatos = [
        f for f in (resp.get("files") or [])
        if f.get("name", "").startswith(f"{romaneio} ")
    ]
    if not candidatos:
        return None
    candidatos.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return candidatos[0]


def processar_pagina(access_token, pagina, subpasta_ids):
    """Agrupa a pagina por romaneio unico, busca cada um no Drive UMA vez e
    replica o link achado pra todos os pedidos daquele romaneio. Retorna
    (romaneios_tentados, romaneios_achados, pedidos_gravados)."""
    por_romaneio = {}
    for item in pagina:
        romaneio = str(item.get("romaneio") or "").strip()
        numero_nf = str(item.get("numero_nf") or "").strip()
        marca = str(item.get("marca") or "").strip()
        if not romaneio or not numero_nf or not marca:
            continue
        por_romaneio.setdefault(romaneio, []).append((numero_nf, marca))

    romaneios_achados = 0
    pedidos_gravados = 0
    for romaneio, pares in por_romaneio.items():
        # Um romaneio que falha (mesmo apos as 3 tentativas do _http_json)
        # nao pode derrubar o resto do lote - segue pro proximo e reporta no
        # final. O que ja foi gravado antes do erro fica gravado (PATCH
        # direto no Supabase, nao ha rollback a fazer).
        try:
            arquivo = achar_arquivo_do_romaneio(access_token, romaneio, subpasta_ids)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"  [romaneio {romaneio}] erro consultando o Drive, pulando: {e}")
            continue
        if not arquivo:
            print(f"  [romaneio {romaneio}] nao achei PDF ({len(pares)} pedido(s) aguardando).")
            continue
        romaneios_achados += 1
        for numero_nf, marca in pares:
            try:
                gravar_link(numero_nf, marca, arquivo["webViewLink"])
                pedidos_gravados += 1
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                print(f"  [romaneio {romaneio}] erro gravando NF {numero_nf}/{marca}, pulando: {e}")
        print(f"  [romaneio {romaneio}] {arquivo['name']} -> gravado em {len(pares)} pedido(s).")

    return len(por_romaneio), romaneios_achados, pedidos_gravados


def main():
    access_token = obter_access_token()

    subpastas = listar_subpastas(access_token, ROMANEIOS_FOLDER_ID)
    if not subpastas:
        print("Nao achei nenhuma subpasta dentro de Romaneios - confira o acesso da conta autorizada.", file=sys.stderr)
        sys.exit(1)
    subpasta_ids = [f["id"] for f in subpastas]
    print(f"{len(subpastas)} subpasta(s) de transportadora: {', '.join(f['name'] for f in subpastas)}")

    cursor_salvo = ler_cursor()
    if cursor_salvo:
        cutoff, operador = cursor_salvo, "gt"
        print(f"Continuando do cursor salvo: {cutoff}")
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=JANELA_DIAS)).isoformat()
        operador = "gte"
        print(f"Sem cursor salvo - comecando do inicio da janela de {JANELA_DIAS} dias: {cutoff}")

    total_pedidos = total_romaneios = romaneios_achados = pedidos_gravados = 0
    pagina_num = 0
    while True:
        pagina_num += 1
        try:
            pagina = buscar_pagina(cutoff, operador)
        except urllib.error.HTTPError as e:
            print(f"  pagina {pagina_num} (apos {cutoff}) falhou ({e.code}), parando por aqui - cursor ja salvo ate a pagina anterior.", file=sys.stderr)
            break

        if not pagina:
            if pagina_num == 1:
                print("Nenhum pedido EMBARCADO sem link de romaneio.")
            resetar_cursor()
            print("Alcancei o fim da janela - cursor resetado, proxima execucao comeca do zero (nova chance pros romaneios ainda sem PDF).")
            break

        print(f"[pagina {pagina_num}] {len(pagina)} pedido(s) - procurando na pasta Romaneios...")
        n_romaneios, n_achados, n_gravados = processar_pagina(access_token, pagina, subpasta_ids)
        total_pedidos += len(pagina)
        total_romaneios += n_romaneios
        romaneios_achados += n_achados
        pedidos_gravados += n_gravados

        # So avanca/salva o cursor DEPOIS de tentar todos os romaneios desta
        # pagina no Drive - se o job for morto no meio, a proxima execucao
        # re-tenta essa pagina inteira em vez de pular pedidos nunca tentados.
        cutoff = pagina[-1]["atualizado_em"]
        operador = "gt"
        gravar_cursor(cutoff)

        if len(pagina) < PAGINA:
            resetar_cursor()
            print("Alcancei o fim da janela - cursor resetado, proxima execucao comeca do zero (nova chance pros romaneios ainda sem PDF).")
            break

    print(f"{romaneios_achados}/{total_romaneios} romaneio(s) achado(s), {pedidos_gravados}/{total_pedidos} pedido(s) atualizado(s).")


if __name__ == "__main__":
    main()
