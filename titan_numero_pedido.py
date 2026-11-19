"""
titan_numero_pedido.py

Preenche infos_titan.numero_pedido (o "SH1807292KS" da Torre) a partir do Data
Mart da Intelipost, para as marcas Gobeaute que passam pelo Titan/Unilog.

POR QUE EXISTE (17/11/2026): no fim de outubro o Titan mudou a exportacao do
painel "Informacao Pedido". A coluna "Numero do Pedido" vinha como SH + NF
("SH38497BS001196008") e o titan_backfill.py recortava o SH dali
(scraper.extrair_numero_pedido_torre). Agora ela vem so com a NF
("001871628") e nenhuma coluna traz o SH. Os pedidos continuam entrando na
infos_titan, mas com numero_pedido vazio: o ultimo com SH foi gravado em
27/10, o primeiro sem SH encontrado foi importado em 04/11. O Portal de
Cancelamento e o bot procuram o pedido por esse numero e mostravam
"estagio desconhecido" em todas as marcas Unilog.

O titan_backfill.py fica intocado de proposito: ele precisa continuar
identico ao do Titan-v2 da Maria. Este script roda logo depois de cada carga
do Titan (workflow_run no titan_numero_pedido.yml).

COMO FUNCIONA:
1. Data Mart (Metabase Gobeaute, db 43, gold.intelipost_orders, pelo endpoint
   de exportacao /api/dataset/json, que nao corta em 2000 linhas): NF + SH de
   cada pedido criado em cada dia da janela (horario de Brasilia), nas 7
   marcas, ANTES de gravar qualquer coisa: uma NF que aparece com dois SH
   diferentes em qualquer ponto da janela, mesmo em dias diferentes, e pulada
   (revisao do PR #23). O SH e
   external_order_numbers->>'sales', a mesma fonte do backfill de 09/09/2026.
   A NF do Data Mart vem com zeros a esquerda ("001897995"), igual a NF que o
   Titan passou a mandar.
2. infos_titan: dessas NFs, quais existem SEM numero_pedido, em lotes por
   numero_nf=in.(...). Buscar por atualizado_em ou por numero_pedido=is.null
   sozinho estoura o statement timeout do Supabase; por NF usa o indice
   (numero_nf, marca) e volta em menos de 1s.
3. Grava so o numero_pedido, so nas linhas que o passo 2 achou vazias, com um
   PATCH por linha filtrado por numero_nf + marca + numero_pedido=is.null
   (em paralelo). Nao usa upsert de proposito (revisao do PR #23): o filtro
   is.null garante que um numero ja preenchido entre a leitura e a gravacao
   nunca e sobrescrito, e nao ha como o upsert criar linha parcial.

TRAVAS (revisao do PR #23, regra de efeito em massa do CLAUDE.md):
- So grava com GRAVAR=1. No GitHub vem da variavel do repositorio
  TITAN_NUMERO_PEDIDO_GRAVAR; sem ela, toda rodada e simulacao.
- Teto de gravacoes por rodada (LIMITE, padrao 50 mil). Ao bater no teto
  para, e a proxima rodada continua (as linhas gravadas ja nao voltam).
- A leitura do Data Mart tem 15 min. Se nao terminar a janela inteira, a
  rodada para com erro SEM gravar nada (um dia nao lido poderia trazer outro
  SH para a mesma NF).
- Orcamento de tempo (25 min, sob o timeout de 30 min do job) conferido
  tambem durante as gravacoes, a cada bloco de 300: para a tempo e a proxima
  rodada continua do mesmo jeito.
- Cada PATCH pede de volta as linhas que mudou: so conta como gravada a
  linha que de fato mudou (preenchida por outro processo no meio tempo = 0),
  e o teto conta so gravacoes reais. Uma linha que falha (depois das 3
  tentativas do _http_json) nao para as outras: a rodada termina, lista as
  falhas e sai com erro, para o job ficar vermelho.
- Dia com mais de 60 mil pedidos no Data Mart (o normal e ~18 mil) derruba a
  rodada sem gravar nada daquele dia: resposta estranha do Data Mart.

A Apice fica de fora: o numero dela vem num formato proprio, que o Titan nao
mudou (o backfill continua preenchendo). Gocase nao passa pelo Titan.

Uso:
    python titan_numero_pedido.py                        # simula os ultimos 7 dias
    GRAVAR=1 python titan_numero_pedido.py --dias 35     # grava desde o fim de outubro
    GRAVAR=1 python titan_numero_pedido.py --limite 1000 # grava no maximo 1000 linhas
Variaveis: METABASE_KEY_GOBEAUTE (obrigatoria), DIAS, LIMITE, GRAVAR=1,
SIMULAR=1 (forca simulacao mesmo com GRAVAR=1).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import titan_romaneio_links as base

METABASE_URL = "https://metabase.gobeaute.com.br"
METABASE_DB_DATAMART = 43
TABELA = base.TABELA

# brand_code do Data Mart -> marca como esta gravada na infos_titan.
MARCAS = {
    "kokeshi": "kokeshi",
    "barbours": "barbours",
    "rituaria": "rituaria",
    "lescent": "lescent",
    "aua": "aua",
    "yenzah": "yenzah",
    "by_samia": "by samia",
}

DIAS_PADRAO = 7
DIAS_MAXIMO = 45
LIMITE_PADRAO = 50000       # gravacoes por rodada
LIMITE_MAXIMO = 200000
MAXIMO_PEDIDOS_DIA = 60000  # acima disso a resposta do Data Mart e suspeita
# Corte silencioso do /api/dataset do Metabase. A simulacao de 17/11/2026 leu
# exatamente 2000 pedidos em cada um dos 35 dias (o normal e ~18 mil): o
# "constraints" nao destrava esse corte neste Metabase. Por isso a consulta vai
# pelo endpoint de exportacao, e um dia com exatamente 2000 linhas derruba a
# rodada como corte, sem gravar nada.
CORTE_METABASE = 2000
LOTE_NF = 400          # NFs por consulta numero_nf=in.(...) (URL ~5 KB)
CONEXOES = 6           # consultas paralelas ao Supabase
ORCAMENTO_SEGUNDOS = 25 * 60   # folga sob o timeout de 30 min do job
ORCAMENTO_LEITURA = 15 * 60    # o resto fica para as gravacoes
GRAVACOES_POR_BLOCO = 300      # o orcamento e conferido entre blocos
BRASILIA = timezone(timedelta(hours=-3))

RE_PEDIDO = re.compile(r"^SH\d{4,10}[A-Z]{2,3}$")
RE_NF = re.compile(r"^\d{1,15}$")


def _ler_numero(argv, env, opcao, variavel, padrao, maximo):
    valor = env.get(variavel) or ""
    if opcao in argv:
        i = argv.index(opcao)
        valor = argv[i + 1] if i + 1 < len(argv) else ""
    valor = str(valor).strip()
    if not valor:
        return padrao
    if not valor.isdigit():
        raise SystemExit(f"{opcao}/{variavel} invalido: {valor!r} (use um numero de 1 a {maximo})")
    return max(1, min(int(valor), maximo))


def ler_dias(argv, env):
    """--dias N ou DIAS=N; vazio = 7; limitado a 1..45."""
    return _ler_numero(argv, env, "--dias", "DIAS", DIAS_PADRAO, DIAS_MAXIMO)


def ler_limite(argv, env):
    """--limite N ou LIMITE=N; vazio = 50 mil; limitado a 1..200 mil."""
    return _ler_numero(argv, env, "--limite", "LIMITE", LIMITE_PADRAO, LIMITE_MAXIMO)


def dias_da_janela(hoje, dias):
    """Do mais antigo ao mais novo, terminando hoje."""
    return [hoje - timedelta(days=n) for n in range(dias - 1, -1, -1)]


def sql_do_dia(dia):
    marcas = ",".join(f"'{m}'" for m in MARCAS)
    seguinte = dia + timedelta(days=1)
    return (
        "SELECT brand_code, invoice_number, external_order_numbers->>'sales' AS pedido "
        "FROM gold.intelipost_orders "
        f"WHERE brand_code IN ({marcas}) "
        f"AND created_iso >= '{dia.isoformat()}T00:00:00-03:00' "
        f"AND created_iso < '{seguinte.isoformat()}T00:00:00-03:00' "
        "AND external_order_numbers->>'sales' LIKE 'SH%'"
    )


def consultar_metabase(sql, chave):
    """POST /api/dataset/json (exportacao do Metabase), que devolve a consulta
    inteira (ate ~1 milhao de linhas) como lista de objetos. O /api/dataset
    corta em 2000 linhas mesmo com "constraints" (ver CORTE_METABASE)."""
    consulta = json.dumps({"type": "native", "native": {"query": sql}, "database": METABASE_DB_DATAMART})
    corpo = urllib.parse.urlencode({"query": consulta, "format_rows": "false"}).encode("utf-8")
    req = urllib.request.Request(f"{METABASE_URL}/api/dataset/json", data=corpo, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "x-api-key": chave})
    ultimo_erro = None
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                j = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError(
                    f"O Metabase da Gobeaute recusou a chave METABASE_KEY_GOBEAUTE (HTTP {e.code}). "
                    "Nada foi gravado.") from e
            ultimo_erro = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            ultimo_erro = e
        time.sleep(5 * (tentativa + 1))
    else:
        raise RuntimeError(f"Consulta ao Data Mart falhou: {ultimo_erro}. Nada foi gravado.")
    # Erro de SQL volta como objeto (com "error"/"status"), nao como lista.
    if not isinstance(j, list):
        erro = j.get("error") or j.get("status") if isinstance(j, dict) else j
        raise RuntimeError(f"Consulta ao Data Mart falhou: {str(erro)[:300]}. Nada foi gravado.")
    return j


def montar_pares(linhas):
    """{(numero_nf, marca_infos): pedido}. Pula marca fora da lista, NF ou SH
    em formato estranho e NF com dois SH diferentes na mesma marca (nao da para
    saber qual e o certo). Devolve (pares, ambiguos)."""
    pares, ambiguos = {}, set()
    for linha in linhas:
        marca = MARCAS.get(str(linha.get("brand_code") or ""))
        nf = str(linha.get("invoice_number") or "").strip()
        pedido = str(linha.get("pedido") or "").strip().upper()
        if not marca or not RE_NF.match(nf) or not RE_PEDIDO.match(pedido):
            continue
        chave = (nf, marca)
        if chave in ambiguos:
            continue
        if chave in pares and pares[chave] != pedido:
            del pares[chave]
            ambiguos.add(chave)
            continue
        pares[chave] = pedido
    return pares, len(ambiguos)


def buscar_sem_pedido(nfs):
    """Das NFs informadas, as linhas da infos_titan ainda sem numero_pedido."""
    lista = ",".join(urllib.parse.quote(nf) for nf in nfs)
    caminho = f"{TABELA}?numero_nf=in.({lista})&numero_pedido=is.null&select=numero_nf,marca"
    return base._supabase_request("GET", caminho) or []


def gravar_linha(registro):
    """Um PATCH so na linha (numero_nf, marca) e so se ela continua sem
    numero_pedido: um valor preenchido nesse meio tempo nunca e trocado."""
    caminho = (
        f"{TABELA}?numero_nf=eq.{urllib.parse.quote(registro['numero_nf'])}"
        f"&marca=eq.{urllib.parse.quote(registro['marca'])}&numero_pedido=is.null"
    )
    alteradas = base._supabase_request(
        "PATCH", caminho + "&select=numero_nf", {"numero_pedido": registro["numero_pedido"]},
        prefer="return=representation")
    return 1 if alteradas else 0


def cruzar_com_infos(pares, executor):
    """Das NFs do Data Mart, as linhas da infos_titan sem numero_pedido que
    tem SH conhecido para a mesma marca: [{"numero_nf", "marca", "numero_pedido"}]."""
    nfs = sorted({nf for nf, _ in pares})
    lotes = [nfs[i:i + LOTE_NF] for i in range(0, len(nfs), LOTE_NF)]
    registros = []
    for linhas in executor.map(buscar_sem_pedido, lotes):
        for linha in linhas:
            chave = (str(linha.get("numero_nf")), str(linha.get("marca")))
            pedido = pares.get(chave)
            if pedido:
                registros.append({"numero_nf": chave[0], "marca": chave[1], "numero_pedido": pedido})
    return registros


def _tentar(gravar, registro):
    """(1 = gravou, 0 = ja estava preenchida, ou a mensagem de erro)."""
    try:
        return gravar(registro)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        return f"{type(e).__name__}: {e}"


def gravar_registros(registros, gravar, executor, limite, prazo, agora):
    """Grava em blocos ate `limite` gravacoes reais, parando antes se o prazo
    (agora() > prazo) passa. Uma linha com erro nao para as outras.
    Devolve (gravadas, ja_preenchidas, falhas, motivo_da_parada ou None);
    falhas = [(numero_nf, marca, erro)]."""
    gravadas = ja_preenchidas = 0
    falhas = []
    i = 0
    while i < len(registros):
        if gravadas >= limite:
            return gravadas, ja_preenchidas, falhas, "teto"
        if agora() > prazo:
            return gravadas, ja_preenchidas, falhas, "tempo"
        bloco = registros[i:i + min(GRAVACOES_POR_BLOCO, limite - gravadas)]
        i += len(bloco)
        for registro, resultado in zip(bloco, executor.map(lambda r: _tentar(gravar, r), bloco)):
            if resultado == 1:
                gravadas += 1
            elif resultado == 0:
                ja_preenchidas += 1
            else:
                falhas.append((registro["numero_nf"], registro["marca"], resultado))
    return gravadas, ja_preenchidas, falhas, None


def main(argv=None, env=None, hoje=None, agora=time.monotonic):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    gravar_ligado = env.get("GRAVAR") == "1"
    simular = "--simular" in argv or env.get("SIMULAR") == "1" or not gravar_ligado
    dias = ler_dias(argv, env)
    limite = ler_limite(argv, env)
    chave = env.get("METABASE_KEY_GOBEAUTE")
    if not chave:
        print("Defina METABASE_KEY_GOBEAUTE (secret do GitHub) - sem ela nao da para ler o Data Mart.",
              file=sys.stderr)
        sys.exit(1)

    hoje = hoje or datetime.now(BRASILIA).date()
    janela = dias_da_janela(hoje, dias)
    if not simular:
        aviso = f", teto de {limite} gravacao(oes)."
    elif not gravar_ligado:
        aviso = ". SIMULACAO: nada sera gravado (trava GRAVAR / TITAN_NUMERO_PEDIDO_GRAVAR desligada)."
    else:
        aviso = ". SIMULACAO: nada sera gravado."
    print(f"Janela: {janela[0].strftime('%d/%m/%Y')} a {janela[-1].strftime('%d/%m/%Y')} ({dias} dia(s))"
          + aviso)

    inicio = agora()
    # 1) Le a janela inteira antes de gravar, para pegar NF ambigua entre dias.
    linhas_janela = []
    for dia in janela:
        # Leitura incompleta nao grava nada (revisao do PR #23): um dia nao lido
        # poderia trazer outro SH para a mesma NF, e o PATCH so-se-vazio nao
        # deixaria corrigir depois. 35 dias levam ~10 min; se estourar, rodar
        # com menos dias.
        if agora() - inicio > ORCAMENTO_LEITURA:
            raise RuntimeError(
                f"Orcamento de leitura do Data Mart esgotado antes de {dia.strftime('%d/%m/%Y')} - "
                "nada foi gravado. Rode com menos dias (--dias / DIAS).")
        linhas = consultar_metabase(sql_do_dia(dia), chave)
        if len(linhas) == CORTE_METABASE:
            raise RuntimeError(
                f"O Data Mart devolveu exatamente {CORTE_METABASE} pedidos em {dia.strftime('%d/%m/%Y')}: "
                "e o corte do Metabase, nao o dia inteiro. Nada foi gravado.")
        if len(linhas) > MAXIMO_PEDIDOS_DIA:
            raise RuntimeError(
                f"O Data Mart devolveu {len(linhas)} pedidos em {dia.strftime('%d/%m/%Y')} "
                f"(o normal e ~18 mil; teto {MAXIMO_PEDIDOS_DIA}). Resposta suspeita - "
                "nada foi gravado.")
        print(f"[{dia.strftime('%d/%m/%Y')}] {len(linhas)} pedido(s) no Data Mart.")
        linhas_janela.extend(linhas)
    pares, ambiguos = montar_pares(linhas_janela)

    # 2) Cruza com a infos_titan e 3) grava.
    with ThreadPoolExecutor(max_workers=CONEXOES) as executor:
        registros = cruzar_com_infos(pares, executor)
        gravadas, ja_preenchidas, falhas, parada = (0, 0, [], None) if simular else gravar_registros(
            registros, gravar_linha, executor, limite, inicio + ORCAMENTO_SEGUNDOS, agora)

    if parada == "tempo":
        print("Orcamento de tempo esgotado durante as gravacoes - a proxima rodada continua.")
    elif parada == "teto":
        print(f"Teto de {limite} gravacao(oes) atingido - a proxima rodada continua.")
    acao = "seriam gravados" if simular else "gravados"
    print(f"Resumo: {len(pares)} pedido(s) no Data Mart, {len(registros)} sem numero_pedido na "
          f"infos_titan, {len(registros) if simular else gravadas} {acao}"
          + (f", {ja_preenchidas} ja preenchida(s) por outro processo" if ja_preenchidas else "")
          + (f", {ambiguos} NF(s) com dois SH puladas" if ambiguos else "")
          + (f", {len(falhas)} com erro" if falhas else "") + ".")
    if falhas:
        for nf, marca, erro in falhas[:20]:
            print(f"  falhou: NF {nf} ({marca}): {erro[:200]}", file=sys.stderr)
        raise RuntimeError(
            f"{len(falhas)} gravacao(oes) falharam (as outras {gravadas} foram feitas). "
            "A proxima rodada tenta de novo as que ficaram vazias.")


if __name__ == "__main__":
    main()
