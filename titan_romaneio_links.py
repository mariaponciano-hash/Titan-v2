"""
titan_romaneio_links.py

Roda periodicamente (GitHub Actions, ver .github/workflows/titan_romaneio_links.yml)
e preenche infos_titan.romaneio_link pra pedidos ja EMBARCADO, casando o
numero do romaneio com o PDF correspondente numa pasta do Google Drive
("Romaneios", compartilhada com a conta gogroup).

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

PADRAO DE NOME DO ARQUIVO (confirmado com print real da pasta,
27/08/2026): "{romaneio} - {TRANSPORTADORA} - {DD-MM-AAAA}_{HH.MM}.pdf",
ex: "166015 - ANJUN EXPRESS - 10-06-2026_17.37.pdf". Busca por nome
contendo o romaneio, SEM restringir subpasta (a pasta tem uma subpasta
por transportadora - buscar sem filtro de pasta poupa o trabalho de
mapear nome de transportadora -> nome de subpasta), depois filtra em
Python por nome comecando com "{romaneio} " pra nao confundir com um
romaneio que seja substring de outro (ex: buscar "166" nao deveria bater
com "1660158").

DUPLICATA: se mais de um arquivo bater pro mesmo romaneio (aconteceu no
print real - 166282 apareceu duas vezes, datas diferentes), pega o mais
recente por modifiedTime.

SEM LIMITE DE TENTATIVAS: um pedido que nunca acha PDF correspondente
(documento ainda nao subiu na pasta) e retentado em toda execucao pra
sempre - aceitavel porque e barato (poucas dezenas de pedidos por vez,
2x/dia) e se autocura sozinho assim que o documento aparecer. Revisar se
o volume crescer muito.

NAO MEXE em nada alem de romaneio_link - status/situacao/romaneio em si
continuam vindo so do titan_cf_worker/titan_backfill.py.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


def _http_json(method, url, headers=None, data=None, form=False):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        corpo = resp.read()
        return json.loads(corpo) if corpo else None


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


def _supabase_request(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    if method == "PATCH":
        headers["Prefer"] = "return=minimal"
    return _http_json(method, url, headers=headers, data=body)


def buscar_pendentes_de_link():
    """Pedidos EMBARCADO com romaneio conhecido mas sem link ainda."""
    q = (
        f"{TABELA}?situacao=eq.EMBARCADO&romaneio=not.is.null"
        f"&romaneio_link=is.null&select=numero_nf,marca,romaneio"
    )
    return _supabase_request("GET", q) or []


def gravar_link(numero_nf, marca, link):
    path = (
        f"{TABELA}?numero_nf=eq.{urllib.parse.quote(numero_nf)}"
        f"&marca=eq.{urllib.parse.quote(marca)}"
    )
    _supabase_request("PATCH", path, {"romaneio_link": link})


def achar_arquivo_do_romaneio(access_token, romaneio):
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"name contains '{romaneio}' and mimeType = 'application/pdf' and trashed = false"
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


def main():
    access_token = obter_access_token()
    pendentes = buscar_pendentes_de_link()
    if not pendentes:
        print("Nenhum pedido EMBARCADO sem link de romaneio.")
        return

    print(f"{len(pendentes)} pedido(s) EMBARCADO sem link - procurando na pasta Romaneios...")
    achados = 0
    for item in pendentes:
        romaneio = str(item.get("romaneio") or "").strip()
        numero_nf = str(item.get("numero_nf") or "").strip()
        marca = str(item.get("marca") or "").strip()
        if not romaneio or not numero_nf or not marca:
            continue
        arquivo = achar_arquivo_do_romaneio(access_token, romaneio)
        if not arquivo:
            print(f"  [romaneio {romaneio}] nao achei PDF na pasta Romaneios.")
            continue
        gravar_link(numero_nf, marca, arquivo["webViewLink"])
        achados += 1
        print(f"  [romaneio {romaneio}] {arquivo['name']} -> gravado.")

    print(f"{achados}/{len(pendentes)} link(s) gravado(s).")


if __name__ == "__main__":
    main()
