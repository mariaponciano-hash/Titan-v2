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

EVENTOS/ITENS - EXPORT + WORKERS EM FATIA FIXA, UM POR VEZ (13-14/09/2026):
a exportacao em massa de "Informacao Pedido" (Romaneio, Situacao, datas
etc.) continua um passo UNICO e rapido (ver exportar_e_gravar_periodo).
Completar Eventos/Itens (clicar pedido por pedido, ~11s cada - ver
_ler_eventos_itens_via_filtro_cruzado) NAO roda dentro deste mesmo passo -
exportar_e_gravar_periodo levanta a lista de pendentes DESTA janela
(eventos OU itens nulos - ver _buscar_pendentes_eventos_itens) e grava num
arquivo (ARQUIVO_PENDENTES_EVENTOS_ITENS); esse arquivo viaja via artifact
pro job completar-eventos, que roda --completar-eventos-worker em N
"instancias" (ver strategy.matrix no workflow, hoje N=20) - cada uma
processando so a sua FATIA fixa da lista por posicao (ver
_fatia_do_worker), sem round-trip nenhum no banco pra "pegar o proximo
lote" e sem risco de duas instancias pegarem o mesmo pedido (fatias
disjuntas por construcao).

Apesar do nome "workers", elas rodam em SEQUENCIA (strategy.max-parallel:
1), nao em paralelo - 3a versao deste desenho, depois de duas tentativas
de paralelismo de verdade que esbarraram no mesmo tipo de problema:
1. (13/09/2026, pedido direto da Maria: "preciso que todos os pedidos
   sejam processados em uma unica rodada") 20 instancias rodando ao mesmo
   tempo, cada uma logada separadamente - 20 logins simultaneos com a
   MESMA conta derrubavam a maioria com "Ainda na tela de login" (run
   #45). Corrigido com um lock de login (so o login esperava a vez, o
   processamento continuava paralelo).
2. Mesmo com o lock, a run #50 manual mostrou 17 de 20 workers morrendo
   logo apos o login, redirecionados sozinhos pra "/login?reason=noUser" -
   o Titan parece derrubar a sessao ANTERIOR assim que uma NOVA sessao
   loga com a mesma conta (nao so rejeitar logins simultaneos). So
   sobrevivia quem abria o painel antes do proximo da fila logar (~3 de
   20 processavam algo de verdade - os outros 17 so gastavam minutos de
   Actions a toa).
A Maria decidiu (14/09/2026) rodar 1 worker por vez (strategy.max-
parallel: 1) - elimina o problema de raiz (nunca ha uma 2a sessao ativa
pra derrubar a 1a), ciente do trade-off real: throughput volta ao teto de
~11s/pedido de uma unica instancia, nao mais paralelizado - nao da mais
pra zerar uma janela de dezenas de milhares de pedidos numa unica rodada,
mas usa o tempo de Actions de verdade em vez de desperdicar a maioria em
logins que nunca chegam a processar nada. O lock de login (titan_login_
lock/_adquirir_lock_login/_liberar_lock_login) virou redundante com
max-parallel:1 e foi removido.

O escopo tambem ja tinha voltado a ser so esta janela antes disso -
a 1a versao do desenho (13/09/2026) reivindicava lotes via uma RPC
(reivindicar_recheck_eventos, SELECT FOR UPDATE SKIP LOCKED) que cobria o
backlog INTEIRO de pedidos concluidos com eventos nulo (qualquer data),
bom pra tambem zerar backlog antigo como efeito colateral, mas exigia o
worker configurar um periodo de datas amplo no Titan pra achar pedidos
antigos, o que nunca foi implementado (achado real na run #48 manual -
"Nenhum elemento visivel encontrado" em quase todo pedido, porque o
periodo padrao do Titan pos-login e estreito/recente). A Maria decidiu
voltar a escopar isso so pra esta janela (14/09/2026) - o backlog antigo
fica de fora do escopo automatico, mesma situacao de antes de 13/09
(titan_recheck_eventos.yml, que cobre esse caso, continua desativado).

ITENS VIA METABASE, EVENTOS SAIU DO ESCOPO (16/09/2026, pedido direto da
Maria): "backfill nao precisa mais buscar itens e eventos no titan" - todo
o mecanismo de clique-por-pedido acima (job completar-eventos, 20
"workers" em sequencia, _ler_eventos_itens_via_filtro_cruzado e toda a
infraestrutura ao redor) foi REMOVIDO. Eventos deixou de ser
responsabilidade deste script (nao entrou na lista de colunas que a Maria
pediu pra manter) - quem ainda tiver eventos=NULL fica assim ate outro
mecanismo cobrir isso (nenhum decidido ainda).

ITENS VIA METABASE REMOVIDO DE VEZ (28/09/2026, pedido direto da Maria -
"está dando errado a condição do metabase, não vou deixar configurado
dentro do github, pode remover essa parte para preencher itens"): entre
16/09 e 28/09/2026 itens vinha de uma consulta em lote ao Metabase
(gold.shopify_order_items, banco "Data Mart"/database_id 43), rodando
dentro deste mesmo job a cada rodada do backfill. Descoberto ao vivo
nesta mesma data que a consulta (`order_name in (...)`, marcas que nao
casam por order_number - ver MARCAS_MATCH_POR_ORDER_NUMBER, removida
junto) sofre timeout inconsistente no Metabase pra lotes de texto de
qualquer tamanho testado (chegou a falhar ate com 5-50 valores, mesmo
quando um lote de 300-600 tinha acabado de funcionar minutos antes -
sinal de degradacao do proprio warehouse, nao um problema de sintaxe ou
tamanho de lote corrigivel daqui) - foi isso que gerou o erro real "ERRO
consultando itens no Metabase pra marca barbours: The read operation
timed out" nesta rodada. Removida a funcao inteira
(_buscar_itens_via_metabase) e tudo em volta dela (_buscar_pendentes_
itens*, completar_itens_atrasados/sem_data_importado/todos, os 3 flags
--completar-itens-* e os inputs correspondentes de workflow_dispatch em
titan_backfill.yml) - itens fica sem nenhum mecanismo automatico de
preenchimento por enquanto, nenhum decidido ainda (mesma situacao de
eventos acima). O historico desta secao (16/09/2026) fica registrado
acima so como contexto de por que "Itens via Metabase" existiu.

numero_pedido virou fill-only-if-empty (16/09/2026, mesmo pedido): antes
sempre sobrescrevia quando dava pra derivar (ver historico anterior desta
secao, 04/09/2026); agora so entra no payload quando a linha AINDA nao tem
numero_pedido no Supabase (ver _buscar_numero_pedido_ja_preenchido) - nunca
sobrescreve um valor ja gravado por outra fonte (ex: consulta avulsa da
Torre via titan_watcher.py).

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
marca da mesma NF). O "Numero do Pedido" continua nunca usado como CHAVE -
mas, ao contrario do que a gente achava antes, ele TEM correspondencia com o
numero de e-commerce da Torre pras marcas Gobeaute exceto apice (corrigido
04/09/2026 - ver registro_para_supabase/scraper.extrair_numero_pedido_torre).
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

import titan_bi_scraper as scraper
import titan_watcher  # reusa _supabase_request (mesmo projeto/chave, so uma tabela)

SUPABASE_URL = "https://ozwcyrkzsqzmavjtsmsp.supabase.co"
SUPABASE_KEY = "sb_publishable_CPF6bT_HC0jkTvYWnxHmDg_61qaWqSQ"
TABELA = "infos_titan"
# Destino de tudo que NAO consegue entrar em infos_titan (marca nao
# identificavel, ou upsert que falhou mesmo depois das tentativas - ver
# _registrar_falhas) - nada mais e so descartado/logado no stderr da Action,
# fica gravado e consultavel aqui (achado real, 11/09/2026: NF 30280 e outras
# desapareciam sem NENHUM sinal em lugar nenhum que alguem realmente olhe).
TABELA_FALHAS = "infos_titan_falhas_backfill"
TAMANHO_LOTE = 200  # registros por chamada ao Supabase - evita 1 request por pedido
PASTA_EXPORTS = Path(__file__).parent / "titan_exports"  # so um local de trabalho - o arquivo e apagado apos o upload

# DIAGNOSTICO TEMPORARIO (17/09/2026, pedido direto da Maria - "registra
# aqui o print de todos os passos, nao so os erros, cada passo do titan
# ate a exportação"): grava um print numerado de CADA passo do filtro de
# data + exportacao, sucesso ou nao - ver definir_periodo/exportar_dados_
# do_painel em titan_bi_scraper.py. Tirar depois de confirmar que o fluxo
# novo do calendario/exportacao esta estavel (isso deixa a run mais lenta
# e mais pesada, so serve pra depurar ao vivo).
PASTA_PASSO_A_PASSO = Path(__file__).parent / "titan_debug" / "passo_a_passo"

def _agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


# REMOVIDAS (28/09/2026, pedido direto da Maria - "excluir as colunas
# status e erro da tabela infos_titan"): buscar_situacao_presa/
# rechecar_situacoes_presas/STATUS_FINAIS/RECHECK_INTERVALO_HORAS/
# RECHECK_ORCAMENTO_SEGUNDOS e o modo --so-recheck de main() - todo o
# mecanismo dependia de status='concluido' pra achar pedidos presos numa
# situacao intermediaria. Ja era so alcancavel via --so-recheck (nenhum
# workflow chamava isso automaticamente - titan_recheck_eventos.yml,
# citado nos comentarios removidos, nunca chegou a existir neste repo),
# entao a remocao nao tira nenhuma cobertura que estivesse rodando de
# verdade. buscar_concluidos_sem_eventos/rechecar_concluidos_sem_eventos
# ja tinham sido removidas antes (16/09/2026, ver ITENS VIA METABASE no
# topo do arquivo).


def _supabase_upsert_lote(registros):
    """
    Upsert em lote, on_conflict=numero_nf,marca. O Postgres/PostgREST so
    atualiza as colunas presentes no JSON enviado quando ha conflito
    (resolution=merge-duplicates) - quando numero_pedido/eventos/itens nao
    entram no dict (nao deu pra derivar, ou o pedido nao precisava de
    completar eventos/itens nesta rodada - ver registro_para_supabase/
    _completar_eventos_itens), um valor ja gravado antes (por uma consulta
    avulsa do titan_watcher.py ou por uma rodada anterior do backfill) NAO
    e apagado. So romaneio/situacao/datas/etc. sao sobrescritos sempre (o
    que e o esperado - o backfill sempre traz o dado mais recente do Titan
    pra esses campos).

    O PostgREST exige que TODOS os objetos de um mesmo array de upsert tenham
    exatamente as mesmas chaves - senao rejeita o POST inteiro com
    PGRST102 "All object keys must match" (achado real, 11/09/2026: como
    numero_pedido so entra no dict quando da pra derivar - ver
    registro_para_supabase -, um lote de 200 registros que misturasse
    pedidos com e sem essa chave derrubava o lote INTEIRO, silenciosamente -
    numa unica rodada isso descartou ~54% dos 150 mil registros exportados,
    sem nenhuma linha sequer chegar ao Supabase, sem erro nenhum gravado
    nelas: elas simplesmente nunca existiram na tabela). Agrupa por conjunto
    de chaves antes de mandar - um POST por grupo uniforme, em vez de um so
    pro lote inteiro, pra um formato de linha diferente nao derrubar as
    outras.
    """
    if not registros:
        return
    grupos = {}
    for r in registros:
        grupos.setdefault(frozenset(r.keys()), []).append(r)
    erros = []
    for grupo in grupos.values():
        try:
            _supabase_upsert_grupo_uniforme(grupo)
        except Exception as e:
            erros.append(str(e))
            # Antes disso o grupo so virava uma linha de log no stderr da
            # Action e sumia pra sempre (11/09/2026, mesmo achado da NF
            # 30280) - agora fica gravado e consultavel em TABELA_FALHAS,
            # mesmo que a rodada inteira ainda seja reportada como "com erro"
            # (ver lotes_com_erro em main()).
            _registrar_falhas("upsert_falhou", grupo, detalhe=str(e))
    if erros:
        raise RuntimeError("; ".join(erros))

def _supabase_upsert_grupo_uniforme(registros, tentativas=3):
    """POST de um unico grupo onde todo registro tem o mesmo conjunto de
    chaves (exigencia do PostgREST pra upsert em lote - ver
    _supabase_upsert_lote acima).

    Tenta ate `tentativas` vezes com backoff (2s, 4s, ...) pra erro
    transitorio (rede, timeout, 5xx do proprio Supabase) - achado real
    (11/09/2026): antes, qualquer excecao aqui - transitoria ou nao -
    derrubava o grupo na primeira tentativa, e uma instabilidade momentanea
    da rede/Supabase perdia pedidos do mesmo jeito que um erro de dado real.
    Erro 4xx (ex: PGRST102, coluna invalida) NAO tenta de novo - e um
    problema no formato dos dados, tentar de novo com o mesmo payload so
    atrasa sem mudar o resultado.
    """
    url = f"{SUPABASE_URL}/rest/v1/{TABELA}?on_conflict=numero_nf,marca"
    data = json.dumps(registros).encode("utf-8")
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            # Achado real (08/09/2026): um HTTPError sozinho so mostra "HTTP Error
            # 400: Bad Request", sem o motivo de verdade que o PostgREST/Postgres
            # devolve no corpo da resposta (ex: qual coluna/valor violou o que) -
            # sem ler esse corpo, um lote ruim vira um "Bad Request" mudo e a
            # unica pista sobra ser adivinhar. Le e inclui na excecao antes de
            # repropagar, pra quem chamar (o loop principal) conseguir logar o
            # motivo real e (com o fix ao lado) isolar so o lote problematico.
            corpo = e.read().decode("utf-8", errors="replace")[:1000]
            ultimo_erro = RuntimeError(f"Supabase respondeu {e.code} no upsert em lote: {corpo}")
            if e.code < 500:
                raise ultimo_erro from e
        except urllib.error.URLError as e:
            ultimo_erro = RuntimeError(f"Falha de rede no upsert em lote: {e}")
        if tentativa < tentativas:
            time.sleep(2 ** tentativa)
    raise ultimo_erro

def _registrar_falhas(motivo, itens, detalhe=None):
    """
    Grava em TABELA_FALHAS em vez de so descartar/logar. Usado tanto pra
    linha do Titan sem marca identificavel (motivo="sem_nf_ou_marca", `item`
    e a linha bruta da exportacao) quanto pro registro que um upsert em lote
    nao conseguiu gravar mesmo depois das tentativas (motivo="upsert_falhou",
    `item` e o dict ja processado por registro_para_supabase).

    marca vira "" (nao None) de proposito: o unique constraint da tabela e
    (numero_nf, marca, motivo), e o Postgres trata cada NULL como distinto
    de qualquer outro NULL - com None, duas falhas da MESMA nf+motivo sem
    marca virariam duas linhas em vez de uma so (upsert nunca colide).

    Nao deixa uma falha AQUI (ex: TABELA_FALHAS fora do ar tambem) derrubar
    o backfill - so avisa, mesmo padrao de titan_watcher.marcar_erro.

    DEDUPLICA por (numero_nf, marca, motivo) antes de enviar (11/09/2026,
    achado real - "HTTP Error 500" reproduzido em toda rodada com >1 linha
    "sem_nf_ou_marca" no mesmo lote): como marca vira "" de proposito (ver
    acima) pra colidir entre si, DUAS OU MAIS linhas sem NF/marca no mesmo
    lote geram a MESMA chave ("", "", motivo) - e um unico INSERT com
    ON CONFLICT nao aceita duas linhas com a mesma chave de conflito no
    mesmo comando (Postgres recusa com erro, que o PostgREST repassa como
    500). Sem isso, o lote inteiro falhava e nenhuma das falhas ficava
    registrada - exatamente o "sumico silencioso" que esta tabela existe
    pra evitar.
    """
    por_chave = {}
    for item in itens:
        nf = (item.get("numero_nf") or item.get("Nota Fiscal") or "").strip()
        marca = (item.get("marca") or item.get("Nome Projeto") or "").strip().lower()
        por_chave[(nf, marca)] = {
            "numero_nf": nf,
            "marca": marca,
            "motivo": motivo,
            "detalhe": (str(detalhe)[:500] if detalhe else None),
            "payload": item,
            "resolvido": False,
            "atualizado_em": _agora_iso(),
        }
    linhas = list(por_chave.values())
    if not linhas:
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/{TABELA_FALHAS}?on_conflict=numero_nf,marca,motivo"
        data = json.dumps(linhas).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        print(f"  (nao consegui registrar {len(linhas)} falha(s) de '{motivo}' em {TABELA_FALHAS}: {e})", file=sys.stderr)


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

    numero_pedido: deriva o numero de e-commerce da Torre a partir do
    "Numero do Pedido" do Titan pra marcas Gobeaute exceto apice (corrigido
    04/09/2026 - achado real da Ivna: ao contrario do que a gente achava
    antes, o "Numero do Pedido" do Titan E o numero da Torre + a NF colada no
    final, sem separador - ver scraper.extrair_numero_pedido_torre pro
    detalhe). Fica de fora quando nao der pra confirmar o sufixo.
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
    registro = {
        "numero_nf": nf,
        "marca": marca,
        "situacao": r.get("Situação"),
        "romaneio": r.get("Romaneio") or None,
        "valor_pedido": r.get("Valor Pedido"),
        "volume": r.get("Volume"),
        "observacao": r.get("Observação"),
        "nome_projeto": r.get("Nome Projeto"),
        "nome_projeto_antigo": r.get("Nome Projeto (Antigo)"),
        # so a data, sem hora (17/09/2026, pedido direto da Maria - ver
        # scraper.formatar_apenas_data pro formato M/D/AAAA e o motivo)
        "data_importado": scraper.formatar_apenas_data(r.get("Data Importado")),
        "data_expedido": r.get("Data Expedido"),
        "data_conferido": r.get("Data Conferido"),
        # Novos (25/08/2026) - so a exportacao nativa expoe texto de verdade
        # pra essas duas colunas (ver comentario grande no topo do arquivo).
        "depositante": r.get("Depositante"),
        "cliente": r.get("Cliente"),
        "atualizado_em": _agora_iso(),
    }
    # NAO promove direto pra "numero_pedido" aqui (16/09/2026, pedido direto
    # da Maria: "somente para as marcas que nao tem nada") - fica guardado
    # numa chave privada (nunca mandada ao Supabase, ver o pop() em
    # exportar_e_gravar_periodo) ate o chamador conferir em lote se a linha
    # JA tem numero_pedido gravado (ver _buscar_numero_pedido_ja_preenchido)
    # - so promove quando ainda nao tem. Continua vazio quando o sufixo nao
    # confirma (ver docstring acima).
    registro["_numero_pedido_derivado"] = scraper.extrair_numero_pedido_torre(r)
    return registro


LINHA_TRUNCAMENTO = "Exported data exceeded the allowed volume"


def _validar_filtro_aplicado(filtro_aplicado, registros, data_inicial, data_final):
    """
    Confere se o periodo que o Titan REALMENTE aplicou bate com o periodo
    pedido, ANTES de confiar nos dados.

    DOIS sinais, porque nenhum sozinho e confiavel:
    1. O texto "Filtros aplicados: ..." (ver ler_export_xlsx) quando o
       Titan inclui ele - confere se cita o dia certo. So decide algo
       quando data_inicial == data_final (um unico dia da pra comparar
       contra um texto especifico); pra periodo de varios dias, ou sem
       essa linha, esse sinal fica de fora (fica so pro sinal 2).
    2. TRUNCAMENTO (ver LINHA_TRUNCAMENTO): se o Titan avisa que a
       exportacao passou do teto de volume, os dados vieram incompletos -
       vale pra QUALQUER tamanho de periodo (nao so 1 dia), entao mais
       seguro descartar e tentar de novo do que arriscar gravar so uma
       parte do periodo pedido.
    """
    if data_inicial == data_final and filtro_aplicado:
        dia = datetime.datetime.strptime(data_inicial, "%d/%m/%Y").date()
        dia_seguinte = dia + datetime.timedelta(days=1)
        if not (dia.strftime("%d/%m/%Y") in filtro_aplicado and dia_seguinte.strftime("%d/%m/%Y") in filtro_aplicado):
            return False
    if any(LINHA_TRUNCAMENTO in str(v) for r in registros for v in r.values()):
        return False
    return True


def _buscar_numero_pedido_ja_preenchido(nfs):
    """
    Devolve o subconjunto de (numero_nf, marca), dentro das NFs desta
    janela, que JA TEM numero_pedido preenchido no Supabase - usado pra so
    incluir o numero_pedido derivado desta exportacao (ver
    registro_para_supabase/_numero_pedido_derivado) quando a linha AINDA
    nao tem nada (pedido direto da Maria, 16/09/2026: "numero_pedido
    somente para as marcas que nao tem nada"). Uma linha nova (ainda nem
    existe na tabela) nao aparece aqui - conta como "nao preenchido", e por
    isso recebe o valor derivado normalmente.
    """
    preenchidos = set()
    nfs_unicas = sorted({str(nf) for nf in nfs if nf})
    for i in range(0, len(nfs_unicas), TAMANHO_LOTE):
        lote_nfs = nfs_unicas[i:i + TAMANHO_LOTE]
        lista = ",".join(urllib.parse.quote(nf) for nf in lote_nfs)
        path = (f"{TABELA}?numero_nf=in.({lista})"
                f"&numero_pedido=not.is.null"
                f"&select=numero_nf,marca")
        linhas = titan_watcher._supabase_request("GET", path) or []
        for linha in linhas:
            preenchidos.add((linha["numero_nf"], linha["marca"]))
    return preenchidos


def exportar_e_gravar_periodo(frame, data_inicial, data_final, tentativas=2):
    """
    Faz um export + upsert de 'Informação Pedido' pro periodo
    (data_inicial, data_final) inteiro. Devolve (gravados, ignorados,
    lotes_com_erro) - (0, 0, 0) se o filtro de periodo nunca bater mesmo
    apos `tentativas` (ver _validar_filtro_aplicado - melhor pular esta
    rodada e tentar de novo na proxima do que arriscar gravar dado de um
    periodo errado ou incompleto).

    Itens vem do Metabase (ver _buscar_itens_via_metabase), consultado
    aqui mesmo dentro deste job (16/09/2026) - nao precisa mais de um job
    paralelo separado nem de artifact pra passar lista entre jobs (ver
    ITENS VIA METABASE no topo do arquivo).
    """
    registros = []
    filtro_ok = False
    for tentativa in range(1, tentativas + 1):
        sufixo = f" (tentativa {tentativa}/{tentativas})" if tentativa > 1 else ""
        print(f"Definindo periodo {data_inicial} - {data_final}{sufixo}...")
        try:
            scraper.definir_periodo(frame, data_inicial, data_final, pasta_registro=PASTA_PASSO_A_PASSO)
            # Print SEMPRE (nao so quando falha) - achado real (11/09/2026): sem
            # nenhuma evidencia visual de como o filtro fica depois do
            # definir_periodo, nao da pra saber se o bug e na digitacao (campo
            # mostra data errada) ou em como o Power BI reage a ela (campo
            # mostra certo, visual nao filtra mesmo assim). Nome do arquivo
            # inclui o dia e a tentativa pra nao sobrescrever entre chamadas.
            scraper.salvar_diagnostico(frame, f"periodo_definido_{data_inicial.replace('/', '-')}_t{tentativa}")

            print("Exportando dados do painel 'Informação Pedido'...")
            caminho_export = scraper.exportar_dados_do_painel(
                frame, "Informação Pedido", PASTA_EXPORTS, pasta_registro=PASTA_PASSO_A_PASSO
            )
            print(f"  baixado em {caminho_export}")
            filtro_aplicado, registros = scraper.ler_export_xlsx(caminho_export)
            try:
                caminho_export.unlink()
            except OSError:
                pass  # nao critico - so um arquivo de trabalho

            filtro_ok = _validar_filtro_aplicado(filtro_aplicado, registros, data_inicial, data_final)
            if filtro_ok:
                break
            print(f"  AVISO: o filtro de periodo que o Titan aplicou nao bate com o periodo pedido "
                  f"({data_inicial} a {data_final}) - texto do Titan: {filtro_aplicado!r}, "
                  f"{len(registros)} linha(s) recebidas.", file=sys.stderr)
        except Exception as e:
            # NOVO (11/09/2026, achado real - crash em producao, run #32):
            # scraper.definir_periodo as vezes estoura o timeout esperando a
            # 1a linha da tabela aparecer (Playwright TimeoutError) - sem
            # este try/except, essa excecao nunca era pega AQUI, so
            # propagava e derrubava o processo INTEIRO (sys.exit(1)) mesmo
            # com `tentativas` pra tentar de novo (o loop nunca chegava a
            # tentar a 2a vez). Trata como "filtro nao bateu" nesta
            # tentativa - tenta de novo (ou desiste e pula esta janela pra
            # proxima rodada, ver abaixo) em vez de derrubar o job inteiro.
            filtro_ok = False
            registros = []
            print(f"  ERRO tentando definir/exportar o periodo: {e}", file=sys.stderr)

    if not filtro_ok:
        print(f"  Desistindo de {data_inicial} apos {tentativas} tentativa(s) sem o filtro certo - "
              f"esse dia fica pra proxima rodada do backfill (nao grava nada agora, pra nao arriscar "
              f"gravar dado de um periodo errado). Ver titan_debug/periodo_definido_{data_inicial.replace('/', '-')}_t*"
              f" no artifact do job pra diagnosticar.", file=sys.stderr)
        return 0, 0, 0

    print(f"{len(registros)} linha(s) encontradas no periodo.")
    if not registros:
        print("Nenhum registro encontrado - confira se o periodo esta certo (ver print salvo, se houver).")
        return 0, 0, 0

    # O Power BI tem um limite de volume por exportacao - descoberto com uma
    # exportacao real (25/08/2026) que voltou com uma linha extra so com
    # este aviso no lugar de um pedido de verdade. Pra periodo de VARIOS
    # dias (nao e o caso do loop diario de main(), mas exportar_e_gravar_
    # periodo pode ser chamada com qualquer periodo) isso ainda pode
    # acontecer legitimamente (volume real grande, nao filtro quebrado) -
    # _validar_filtro_aplicado acima ja tratou o caso de 1 dia so (onde
    # truncamento e sinal de bug, nao de volume real). Aqui so filtra a
    # linha sintetica antes de processar, pro caso legitimo de periodo
    # maior mesmo.
    truncou = any(LINHA_TRUNCAMENTO in str(v) for r in registros for v in r.values())
    if truncou:
        print(f"\n  AVISO: o Titan/Power BI truncou esta exportacao (limite de volume excedido) - "
              f"o periodo {data_inicial} a {data_final} tem mais dados do que a exportacao trouxe de "
              f"uma vez so.", file=sys.stderr)
        # NAO so avisa - filtra a linha sintetica antes de processar
        # (08/09/2026, achado real: o backfill so CHECAVA essa mensagem na
        # coluna "Depositante", mas nunca excluia a linha de registros - ela
        # seguia pro loop normal como se fosse um pedido de verdade. Se o
        # Power BI colocar o aviso em outra coluna dessa vez, ela pode
        # passar pelas checagens de nf/marca de registro_para_supabase com
        # lixo em vez de um valor de verdade, envenenando o lote inteiro no
        # upsert). Confere QUALQUER coluna, nao so Depositante, pra nao
        # depender de onde o Power BI decidir colocar o aviso.
        antes = len(registros)
        registros = [r for r in registros if not any(LINHA_TRUNCAMENTO in str(v) for v in r.values())]
        print(f"  ({antes - len(registros)} linha(s) de aviso de truncamento removida(s) antes de gravar)", file=sys.stderr)

    payloads = []
    ignorados_lote = []
    ignorados = 0
    for r in registros:
        payload = registro_para_supabase(r)
        if payload is None:
            # Antes so incrementava esse contador e a linha sumia - agora
            # fica gravada em TABELA_FALHAS pra revisao manual (mesmo
            # achado da NF 30280: nada pode desaparecer sem deixar rastro
            # consultavel em algum lugar).
            ignorados += 1
            ignorados_lote.append(r)
            if len(ignorados_lote) >= TAMANHO_LOTE:
                _registrar_falhas("sem_nf_ou_marca", ignorados_lote)
                ignorados_lote = []
            continue
        payloads.append(payload)
    if ignorados_lote:
        _registrar_falhas("sem_nf_ou_marca", ignorados_lote)

    # numero_pedido fill-only-if-empty (16/09/2026, pedido direto da Maria -
    # ver ITENS VIA METABASE/registro_para_supabase): promove o valor
    # derivado (guardado em "_numero_pedido_derivado") pra "numero_pedido"
    # de verdade so nas linhas que AINDA nao tem nada gravado - o pop()
    # tira a chave privada de TODO payload antes de seguir, senao o
    # PostgREST rejeitaria o upsert com uma coluna que a tabela nao tem.
    ja_tem_numero_pedido = _buscar_numero_pedido_ja_preenchido([p["numero_nf"] for p in payloads])
    for payload in payloads:
        derivado = payload.pop("_numero_pedido_derivado", None)
        if derivado and (payload["numero_nf"], payload["marca"]) not in ja_tem_numero_pedido:
            payload["numero_pedido"] = derivado

    # Itens via Metabase REMOVIDO (28/09/2026, ver ITENS VIA METABASE no
    # topo do arquivo) - a consulta em lote ao Metabase que rodava aqui
    # (16/09/2026 a 28/09/2026) sofria timeout inconsistente no warehouse;
    # itens fica sem nenhum mecanismo automatico de preenchimento por
    # enquanto.

    lote = []
    gravados = 0
    lotes_com_erro = 0
    for payload in payloads:
        lote.append(payload)
        if len(lote) >= TAMANHO_LOTE:
            try:
                _supabase_upsert_lote(lote)
                gravados += len(lote)
                print(f"  {gravados} gravados...")
            except Exception as e:
                # Isola o lote ruim (08/09/2026, achado real: um HTTP 400
                # num unico lote de 200 derrubava o script inteiro com
                # sys.exit(1), descartando todos os lotes restantes - lote
                # ruim vira log e o resto continua, em vez de tudo ou nada).
                lotes_com_erro += 1
                print(f"  ERRO no lote (linhas {gravados + 1}-{gravados + len(lote)}), pulando: {e}", file=sys.stderr)
            lote = []
    if lote:
        try:
            _supabase_upsert_lote(lote)
            gravados += len(lote)
        except Exception as e:
            lotes_com_erro += 1
            print(f"  ERRO no ultimo lote ({len(lote)} linha(s)), pulando: {e}", file=sys.stderr)

    return gravados, ignorados, lotes_com_erro


# REMOVIDAS (16/09/2026, ver ITENS VIA METABASE no topo do arquivo):
# _marcar_eventos_itens/_erro_de_frame_invalido/_fatia_do_worker/
# _completar_eventos_itens_worker - todo o mecanismo de clique-por-pedido
# via Playwright (job completar-eventos, matrix de 20 "workers") foi
# substituido pela consulta em lote ao Metabase (ver
# _buscar_itens_via_metabase, chamada direto dentro de
# exportar_e_gravar_periodo).


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-inicial", help="formato DD/MM/AAAA, ex: 01/06/2026 (obrigatorio)")
    parser.add_argument("--data-final", help="formato DD/MM/AAAA, ex: 24/08/2026 (obrigatorio)")
    parser.add_argument("--headless", action="store_true", default=False, help="roda sem abrir janela - so use depois de validar visualmente sem esta flag")
    args = parser.parse_args()
    # --completar-itens-atrasados/--completar-itens-sem-data-importado/
    # --completar-itens-todos REMOVIDOS (28/09/2026, ver ITENS VIA METABASE
    # no topo do arquivo) junto com toda a integracao de itens via Metabase.
    # --so-recheck/--recheck-orcamento-situacao-segundos REMOVIDOS (28/09/2026,
    # ver nota grande perto de _agora_iso) junto com rechecar_situacoes_presas.
    if not args.data_inicial or not args.data_final:
        parser.error("--data-inicial e --data-final sao obrigatorios")

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

            # Export UNICO pro periodo inteiro (voltou a ser assim em
            # 11/09/2026 - tinha virado um export por dia por engano: o
            # motivo real do teto de volume do Power BI estourar era o
            # filtro de data nao aplicar direito (ja corrigido em
            # scraper.definir_periodo), nao o tamanho do periodo em si.
            # Conferido direto no Supabase: o total REAL e distinto de uma
            # janela de poucos dias fica bem abaixo do teto de ~150 mil
            # linhas - exportar por dia so multiplicava o tempo do job sem
            # necessidade. _validar_filtro_aplicado ainda pega qualquer
            # truncamento de verdade (volume real crescendo no futuro),
            # devolvendo (0, 0, 0) sem gravar nada errado.
            print(f"Exportando periodo {args.data_inicial} - {args.data_final}...")
            gravados_total, ignorados_total, lotes_com_erro_total = exportar_e_gravar_periodo(
                frame, args.data_inicial, args.data_final
            )

            print(f"\nConcluido - {gravados_total} pedido(s) gravados no Supabase.")
            if lotes_com_erro_total:
                print(f"{lotes_com_erro_total} lote(s) falharam e foram pulados (ver ERRO acima pro motivo real, e "
                      f"{TABELA_FALHAS} pros registros exatos) - esses pedidos ficam pra proxima rodada do "
                      f"backfill.", file=sys.stderr)
            if ignorados_total:
                print(f"{ignorados_total} linha(s) ignoradas por falta de Nota Fiscal ou de \"Nome Projeto\" "
                      f"(marca) - sem os dois, nao da pra identificar com seguranca. Gravadas em "
                      f"{TABELA_FALHAS} pra revisao manual, em vez de so descartadas.")

        finally:
            browser.close()


if __name__ == "__main__":
    main()
