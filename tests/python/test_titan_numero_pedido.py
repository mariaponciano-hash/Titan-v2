"""Testes do titan_numero_pedido.py, sem rede (Metabase e Supabase simulados).

Rodar da raiz do repo: python -m unittest discover -s tests/python
"""
import importlib.util
import io
import json
import pathlib
import sys
import unittest
from datetime import date
from unittest import mock

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))  # o script importa titan_romaneio_links
spec = importlib.util.spec_from_file_location("titan_numero_pedido", RAIZ / "titan_numero_pedido.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ENV = {"METABASE_KEY_GOBEAUTE": "chave-falsa"}
ENV_GRAVAR = {**ENV, "GRAVAR": "1"}


class Regras(unittest.TestCase):
    def test_dias_padrao_limite_e_argumento(self):
        self.assertEqual(mod.ler_dias([], {}), 7)
        self.assertEqual(mod.ler_dias([], {"DIAS": "35"}), 35)
        self.assertEqual(mod.ler_dias(["--dias", "3"], {"DIAS": "35"}), 3)
        self.assertEqual(mod.ler_dias(["--dias", "999"], {}), 45)
        self.assertEqual(mod.ler_dias(["--dias", "0"], {}), 1)
        with self.assertRaises(SystemExit):
            mod.ler_dias(["--dias", "abc"], {})

    def test_limite_padrao_e_teto(self):
        self.assertEqual(mod.ler_limite([], {}), 50000)
        self.assertEqual(mod.ler_limite([], {"LIMITE": "1000"}), 1000)
        self.assertEqual(mod.ler_limite(["--limite", "999999"], {}), 200000)

    def test_janela_do_mais_antigo_ao_mais_novo(self):
        self.assertEqual(mod.dias_da_janela(date(2026, 11, 17), 3),
                         [date(2026, 11, 15), date(2026, 11, 16), date(2026, 11, 17)])

    def test_sql_do_dia_em_horario_de_brasilia_e_so_as_7_marcas(self):
        sql = mod.sql_do_dia(date(2026, 11, 16))
        self.assertIn("created_iso >= '2026-11-16T00:00:00-03:00'", sql)
        self.assertIn("created_iso < '2026-11-17T00:00:00-03:00'", sql)
        self.assertIn("'by_samia'", sql)
        self.assertNotIn("apice", sql)

    def test_pares_mapeiam_marca_e_pulam_ambiguos_e_formatos_estranhos(self):
        pares, ambiguos = mod.montar_pares([
            {"brand_code": "kokeshi", "invoice_number": "001897995", "pedido": "SH1807292KS"},
            {"brand_code": "by_samia", "invoice_number": "000123456", "pedido": "SH48707BS"},
            {"brand_code": "barbours", "invoice_number": "001594764", "pedido": "SH2898070BB"},
            {"brand_code": "barbours", "invoice_number": "001594764", "pedido": "SH2898071BB"},  # dois SH
            {"brand_code": "barbours", "invoice_number": "001594764", "pedido": "SH2898070BB"},
            {"brand_code": "apice", "invoice_number": "000000001", "pedido": "SH1AP"},           # fora
            {"brand_code": "rituaria", "invoice_number": "", "pedido": "SH1529164RT"},           # sem NF
            {"brand_code": "rituaria", "invoice_number": "001597549", "pedido": "1529164"},      # sem SH
        ])
        self.assertEqual(pares, {
            ("001897995", "kokeshi"): "SH1807292KS",
            ("000123456", "by samia"): "SH48707BS",
        })
        self.assertEqual(ambiguos, 1)


class Resposta:
    def __init__(self, corpo):
        self.corpo = corpo.encode("utf-8") if isinstance(corpo, str) else corpo
        self.headers = {}

    def read(self):
        return self.corpo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class PontaAPonta(unittest.TestCase):
    """O main inteiro: so a rede (urllib.request.urlopen) e simulada, com uma
    infos_titan de mentira em memoria. Qualquer chamada externa nao prevista
    derruba o teste."""

    DATAMART = [
        ["kokeshi", "001897995", "SH1807292KS"],
        ["kokeshi", "001754221", "SH1674546KS"],
        ["barbours", "001594764", "SH2898070BB"],
        ["rituaria", "001597549", "SH1529164RT"],
        ["lescent", "001999999", "SH999999LC"],   # nao chegou na infos_titan ainda
    ]

    def setUp(self):
        # (numero_nf, marca) -> numero_pedido. A 001754221 ja tem numero; a
        # 001594764 existe tambem na kokeshi sem pedido, e essa nao pode
        # receber o SH da Barbour's.
        self.banco = {
            ("001897995", "kokeshi"): None,
            ("001754221", "kokeshi"): "SH1674546KS",
            ("001594764", "barbours"): None,
            ("001594764", "kokeshi"): None,
            ("001597549", "rituaria"): None,
        }
        self.antes_de_gravar = None   # gancho para simular outra escrita no meio
        self.por_dia = {}             # "2026-11-16" -> linhas do Data Mart so daquele dia
        self.falhar_patch = set()     # NFs cujo PATCH devolve erro

    def rodar(self, argv, env, agora=None):
        pedidos = []

        def rede(req, timeout=None):
            metodo, url = req.get_method(), req.full_url
            if url == f"{mod.METABASE_URL}/api/dataset/json":
                # Exportacao do Metabase: formulario com query=<json> e resposta
                # como lista de objetos (sem o corte de 2000 do /api/dataset).
                form = mod.urllib.parse.parse_qs(req.data.decode("utf-8"))
                corpo = {**json.loads(form["query"][0]), "format_rows": form["format_rows"][0]}
                pedidos.append((metodo, url, req.get_header("X-api-key"), corpo, None))
                dia = corpo["native"]["query"].split("created_iso >= '")[1][:10]
                linhas = self.por_dia.get(dia, self.DATAMART)
                cols = ("brand_code", "invoice_number", "pedido")
                return Resposta(json.dumps([dict(zip(cols, linha)) for linha in linhas]))
            corpo = json.loads(req.data) if req.data else None
            pedidos.append((metodo, url, req.get_header("X-api-key"), corpo, req.get_header("Prefer")))
            prefixo = f"{mod.base.SUPABASE_URL}/rest/v1/{mod.TABELA}?"
            if metodo == "GET" and url.startswith(prefixo + "numero_nf=in."):
                assert "numero_pedido=is.null" in url
                pedidas = set(url.split("in.(")[1].split(")")[0].split(","))
                return Resposta(json.dumps([{"numero_nf": nf, "marca": m} for (nf, m), v in self.banco.items()
                                            if nf in pedidas and v is None]))
            if metodo == "PATCH" and url.startswith(prefixo):
                if self.antes_de_gravar:
                    self.antes_de_gravar()
                filtros = dict(f.split("=", 1) for f in url[len(prefixo):].split("&"))
                chave = (filtros["numero_nf"][3:], mod.urllib.parse.unquote(filtros["marca"][3:]))
                assert filtros["numero_pedido"] == "is.null"
                if chave[0] in self.falhar_patch:
                    raise mod.urllib.error.HTTPError(url, 400, "erro", {}, io.BytesIO(b'{"message":"x"}'))
                # Como o PostgREST com Prefer return=representation: devolve
                # as linhas que mudaram (lista vazia se o filtro nao achou).
                if chave in self.banco and self.banco[chave] is None:
                    self.banco[chave] = corpo["numero_pedido"]
                    return Resposta(json.dumps([{"numero_nf": chave[0]}]))
                return Resposta("[]")
            raise AssertionError(f"chamada externa nao simulada: {metodo} {url}")

        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=rede), \
                mock.patch("builtins.print"), mock.patch.object(mod.time, "sleep"):
            if agora:
                mod.main(argv, env=env, hoje=date(2026, 11, 17), agora=agora)
            else:
                mod.main(argv, env=env, hoje=date(2026, 11, 17))
        return pedidos

    def escritas(self, pedidos):
        return [p for p in pedidos if p[0] != "GET" and mod.base.SUPABASE_URL in p[1]]

    def test_grava_so_o_numero_pedido_das_linhas_vazias_da_mesma_marca(self):
        pedidos = self.rodar(["--dias", "1"], ENV_GRAVAR)
        metabase = [p for p in pedidos if "/api/dataset" in p[1]]
        self.assertEqual(len(metabase), 1)
        self.assertEqual(metabase[0][2], "chave-falsa")
        self.assertEqual(metabase[0][1], f"{mod.METABASE_URL}/api/dataset/json")
        self.assertEqual(metabase[0][3]["database"], 43)
        self.assertEqual(metabase[0][3]["format_rows"], "false")
        self.assertEqual({p[0] for p in self.escritas(pedidos)}, {"PATCH"})  # nada de upsert
        for _, url, _, corpo, prefer in self.escritas(pedidos):
            self.assertEqual(set(corpo), {"numero_pedido"})
            self.assertEqual(prefer, "return=representation")
            self.assertIn("&select=numero_nf", url)
        self.assertEqual(self.banco, {
            ("001897995", "kokeshi"): "SH1807292KS",
            ("001754221", "kokeshi"): "SH1674546KS",
            ("001594764", "barbours"): "SH2898070BB",
            ("001594764", "kokeshi"): None,
            ("001597549", "rituaria"): "SH1529164RT",
        })

    def test_sem_a_trava_gravar_e_so_simulacao(self):
        pedidos = self.rodar(["--dias", "2"], ENV)
        self.assertEqual(len([p for p in pedidos if "/api/dataset" in p[1]]), 2)  # um por dia
        self.assertFalse(self.escritas(pedidos))
        self.assertIsNone(self.banco[("001897995", "kokeshi")])

    def test_simular_vence_a_trava_ligada(self):
        self.assertFalse(self.escritas(self.rodar(["--simular", "--dias", "1"], ENV_GRAVAR)))
        self.assertFalse(self.escritas(self.rodar(["--dias", "1"], {**ENV_GRAVAR, "SIMULAR": "1"})))

    def test_teto_de_gravacoes_por_rodada(self):
        pedidos = self.rodar(["--dias", "3", "--limite", "2"], ENV_GRAVAR)
        self.assertEqual(len(self.escritas(pedidos)), 2)
        # Le a janela inteira (um dia por consulta) antes de gravar.
        self.assertEqual(len([p for p in pedidos if "/api/dataset" in p[1]]), 3)

    def test_nf_com_dois_sh_em_dias_diferentes_nao_e_gravada(self):
        # Revisao do PR #23: o conflito entre dias tem que ser visto antes de
        # qualquer gravacao, senao o primeiro dia grava um SH que o segundo
        # desmente.
        self.por_dia = {
            "2026-11-16": [["kokeshi", "001897995", "SH1807292KS"], ["rituaria", "001597549", "SH1529164RT"]],
            "2026-11-17": [["kokeshi", "001897995", "SH1807299KS"]],
        }
        pedidos = self.rodar(["--dias", "2"], ENV_GRAVAR)
        self.assertIsNone(self.banco[("001897995", "kokeshi")])
        self.assertEqual(self.banco[("001597549", "rituaria")], "SH1529164RT")
        self.assertEqual(len(self.escritas(pedidos)), 1)

    def test_para_de_gravar_quando_o_tempo_acaba_e_deixa_o_resto_vazio(self):
        # Relogio: leitura e cruzamento dentro do prazo; depois do primeiro
        # bloco de gravacoes o tempo estoura.
        tempos = iter([0, 0, 0] + [mod.ORCAMENTO_SEGUNDOS + 1] * 50)
        with mock.patch.object(mod, "GRAVACOES_POR_BLOCO", 1):
            pedidos = self.rodar(["--dias", "1"], ENV_GRAVAR, agora=lambda: next(tempos))
        self.assertEqual(len(self.escritas(pedidos)), 1)
        self.assertEqual(sum(v is None for v in self.banco.values()), 3)  # 2 sobram + a da kokeshi

    def test_numero_preenchido_entre_a_leitura_e_a_gravacao_nao_e_sobrescrito(self):
        def outra_escrita():
            if self.banco[("001897995", "kokeshi")] is None:
                self.banco[("001897995", "kokeshi")] = "SH0000001KS"
        self.antes_de_gravar = outra_escrita
        self.rodar(["--dias", "1"], ENV_GRAVAR)
        self.assertEqual(self.banco[("001897995", "kokeshi")], "SH0000001KS")
        self.assertEqual(self.banco[("001597549", "rituaria")], "SH1529164RT")

    def test_teto_conta_so_a_linha_que_de_fato_mudou(self):
        # Revisao do PR #23: a linha preenchida por outro processo no meio
        # tempo volta vazia do PATCH, nao conta como gravada e nao consome o
        # teto - com teto 2, as duas linhas vazias de verdade sao gravadas.
        def outra_escrita():
            if self.banco[("001594764", "barbours")] is None:
                self.banco[("001594764", "barbours")] = "SH0000002BB"
        self.antes_de_gravar = outra_escrita
        with mock.patch.object(mod, "GRAVACOES_POR_BLOCO", 1):
            self.rodar(["--dias", "1", "--limite", "2"], ENV_GRAVAR)
        self.assertEqual(self.banco[("001594764", "barbours")], "SH0000002BB")
        self.assertEqual(self.banco[("001897995", "kokeshi")], "SH1807292KS")
        self.assertEqual(self.banco[("001597549", "rituaria")], "SH1529164RT")

    def test_leitura_incompleta_da_janela_nao_grava_nada(self):
        # Revisao do PR #23: o dia lido tem um SH e o dia que ficaria sem
        # leitura teria outro; com a leitura cortada, nada pode ser gravado.
        self.por_dia = {
            "2026-11-16": [["kokeshi", "001897995", "SH1807292KS"]],
            "2026-11-17": [["kokeshi", "001897995", "SH1807299KS"]],
        }
        # inicio, checagem do 1o dia (dentro), checagem do 2o dia (estourou).
        tempos = iter([0, 0, mod.ORCAMENTO_LEITURA + 1])
        with self.assertRaisesRegex(RuntimeError, "nada foi gravado"):
            self.rodar(["--dias", "2"], ENV_GRAVAR, agora=lambda: next(tempos))
        self.assertIsNone(self.banco[("001897995", "kokeshi")])
        self.assertTrue(all(v is None or v == "SH1674546KS" for v in self.banco.values()))

    def test_linha_com_erro_nao_para_as_outras_e_a_rodada_termina_vermelha(self):
        self.falhar_patch = {"001594764"}
        with self.assertRaisesRegex(RuntimeError, "1 gravacao"):
            self.rodar(["--dias", "1"], ENV_GRAVAR)
        self.assertEqual(self.banco[("001897995", "kokeshi")], "SH1807292KS")
        self.assertEqual(self.banco[("001597549", "rituaria")], "SH1529164RT")
        self.assertIsNone(self.banco[("001594764", "barbours")])

    def test_dia_com_volume_suspeito_derruba_sem_gravar(self):
        with mock.patch.object(mod, "MAXIMO_PEDIDOS_DIA", 3):
            with self.assertRaisesRegex(RuntimeError, "suspeita"):
                self.rodar(["--dias", "1"], ENV_GRAVAR)
        self.assertIsNone(self.banco[("001897995", "kokeshi")])

    def test_dia_cortado_em_2000_linhas_derruba_sem_gravar(self):
        # Simulacao real de 17/11/2026: 2000 pedidos em cada um dos 35 dias,
        # o corte silencioso do Metabase. Um dia com exatamente 2000 linhas
        # nao pode virar gravacao.
        self.por_dia = {"2026-11-17": [["kokeshi", f"{n:09d}", f"SH{n}KS"] for n in range(1, 2001)]}
        with self.assertRaisesRegex(RuntimeError, "corte do Metabase"):
            self.rodar(["--dias", "1"], ENV_GRAVAR)
        self.assertTrue(all(v is None or v == "SH1674546KS" for v in self.banco.values()))

    def test_erro_de_sql_no_metabase_derruba_sem_gravar(self):
        def rede(req, timeout=None):
            return Resposta(json.dumps({"status": "failed", "error": "column x does not exist"}))
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=rede), mock.patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "column x does not exist"):
                mod.main(["--dias", "1"], env=ENV_GRAVAR, hoje=date(2026, 11, 17))

    def test_sem_chave_do_metabase_para_sem_chamar_nada(self):
        with mock.patch.object(mod.urllib.request, "urlopen") as rede, mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                mod.main([], env={"GRAVAR": "1"}, hoje=date(2026, 11, 17))
        rede.assert_not_called()

    def test_chave_recusada_pelo_metabase_derruba_sem_gravar(self):
        def rede(req, timeout=None):
            raise mod.urllib.error.HTTPError(req.full_url, 401, "erro", {}, io.BytesIO(b"{}"))
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=rede) as chamada, \
                mock.patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "METABASE_KEY_GOBEAUTE"):
                mod.main(["--dias", "1"], env=ENV_GRAVAR, hoje=date(2026, 11, 17))
        self.assertEqual(chamada.call_count, 1)


if __name__ == "__main__":
    unittest.main()
