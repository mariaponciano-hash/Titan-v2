"""Testes do titan_romaneio_links.py, sem rede (Supabase e planilha simulados).

Rodar da raiz do repo: python -m unittest discover -s tests/python
"""
import importlib.util
import pathlib
import os
import unittest
from unittest import mock

RAIZ = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("titan_romaneio_links", RAIZ / "titan_romaneio_links.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CSV = (
    "Romaneio Número,Depositante,Transportadora,Data,Hora,UF,Link\n"
    "137950,BARBOURS RJ,DIAS LOG,03/11/2025,18:01,RJ,https://drive.google.com/file/d/A/view\n"
    "343,BEAUTY HUB ES,L4B,31/10/2026,10:00,ES,https://drive.google.com/file/d/B/view\n"
    "343,GOBEAUTY ES,L4B,31/10/2026,10:05,ES,https://drive.google.com/file/d/C/view\n"
    "180000,Beauty  Hub ES,L4B,01/10/2026,09:00,ES,https://drive.google.com/file/d/VELHO/view\n"
    "180000,BEAUTY HUB ES,L4B,02/10/2026,09:00,ES,https://drive.google.com/file/d/NOVO/view\n"
    "999,APICE SP,CEM,01/11/2026,09:00,SP,\n"
    ",APICE SP,CEM,01/11/2026,09:00,SP,https://drive.google.com/file/d/X/view\n"
)


class CarregarPlanilha(unittest.TestCase):
    def test_chave_romaneio_e_depositante(self):
        mapa = mod.carregar_planilha(CSV)
        self.assertEqual(mapa[("343", "BEAUTY HUB ES")], "https://drive.google.com/file/d/B/view")
        self.assertEqual(mapa[("343", "GOBEAUTY ES")], "https://drive.google.com/file/d/C/view")

    def test_linha_mais_nova_vence_e_depositante_normalizado(self):
        mapa = mod.carregar_planilha(CSV)
        self.assertEqual(mapa[("180000", "BEAUTY HUB ES")], "https://drive.google.com/file/d/NOVO/view")

    def test_ignora_linha_sem_link_ou_sem_numero(self):
        mapa = mod.carregar_planilha(CSV)
        self.assertNotIn(("999", "APICE SP"), mapa)
        self.assertNotIn(("", "APICE SP"), mapa)
        self.assertEqual(len(mapa), 4)

    def test_coluna_fora_do_lugar_e_sem_acento(self):
        csv2 = "Link,Depositante,Romaneio Numero\nhttps://x/1,APICE RJ,5\n"
        self.assertEqual(mod.carregar_planilha(csv2), {("5", "APICE RJ"): "https://x/1"})

    def test_coluna_faltando_derruba(self):
        with self.assertRaises(RuntimeError):
            mod.carregar_planilha("Romaneio,Link\n1,https://x\n")

    def test_html_no_lugar_do_csv_derruba(self):
        with self.assertRaises(RuntimeError):
            mod.validar_csv("<!DOCTYPE html><html>login</html>", "text/html; charset=utf-8")
        self.assertEqual(mod.validar_csv(CSV, "text/csv"), CSV)


class ProcessarPagina(unittest.TestCase):
    def setUp(self):
        self.mapa = mod.carregar_planilha(CSV)
        self.gravados = []

    def gravar(self, romaneio, depositante, link):
        self.gravados.append((romaneio, depositante, link))

    def test_grava_uma_vez_por_romaneio_e_respeita_depositante(self):
        pagina = [
            {"romaneio": "343", "depositante": "BEAUTY HUB ES", "atualizado_em": "t1"},
            {"romaneio": "343", "depositante": "BEAUTY HUB ES", "atualizado_em": "t2"},
            {"romaneio": "343", "depositante": "APICE ES", "atualizado_em": "t3"},
            {"romaneio": 137950, "depositante": "Barbours RJ", "atualizado_em": "t4"},
        ]
        ja = set()
        cursor, completou, casados, gravados, fora = mod.processar_pagina(
            pagina, self.mapa, 0, 999, self.gravar, ja, agora=lambda: 0)
        self.assertEqual((cursor, completou, casados, gravados, fora), ("t4", True, 3, 2, 1))
        # Grava com o depositante como esta na infos_titan, para o eq do PostgREST bater.
        self.assertEqual(self.gravados, [
            ("343", "BEAUTY HUB ES", "https://drive.google.com/file/d/B/view"),
            ("137950", "Barbours RJ", "https://drive.google.com/file/d/A/view"),
        ])
        # Na pagina seguinte o mesmo par nao e gravado de novo.
        mod.processar_pagina(pagina[:1], self.mapa, 0, 999, self.gravar, ja, agora=lambda: 0)
        self.assertEqual(len(self.gravados), 2)

    def test_para_quando_o_orcamento_acaba(self):
        pagina = [
            {"romaneio": "343", "depositante": "GOBEAUTY ES", "atualizado_em": "t1"},
            {"romaneio": "137950", "depositante": "BARBOURS RJ", "atualizado_em": "t2"},
        ]
        relogio = iter([0, 100])
        cursor, completou, casados, gravados, _ = mod.processar_pagina(
            pagina, self.mapa, 0, 50, self.gravar, set(), agora=lambda: next(relogio))
        self.assertEqual((cursor, completou, casados, gravados), ("t1", False, 1, 1))

    def test_erro_ao_gravar_nao_derruba_a_pagina(self):
        def falha(*_):
            raise mod.urllib.error.URLError("rede caiu")
        pagina = [
            {"romaneio": "343", "depositante": "GOBEAUTY ES", "atualizado_em": "t1"},
            {"romaneio": "137950", "depositante": "BARBOURS RJ", "atualizado_em": "t2"},
        ]
        ja = set()
        cursor, completou, casados, gravados, _ = mod.processar_pagina(
            pagina, self.mapa, 0, 999, falha, ja, agora=lambda: 0)
        self.assertEqual((cursor, completou, casados, gravados, ja), ("t2", True, 2, 0, set()))


    def test_grafias_diferentes_do_depositante_viram_um_patch_cada(self):
        # Revisao do PR #15 (16/11/2026): o mesmo romaneio com o depositante
        # escrito de jeitos diferentes na infos_titan precisa de um PATCH para
        # cada grafia, com o valor exato do banco (inclusive espaco sobrando).
        pagina = [
            {"romaneio": "137950", "depositante": "Barbours RJ", "atualizado_em": "t1"},
            {"romaneio": "137950", "depositante": "BARBOURS RJ", "atualizado_em": "t2"},
            {"romaneio": "137950", "depositante": "BARBOURS RJ ", "atualizado_em": "t3"},
            {"romaneio": "137950", "depositante": "BARBOURS RJ", "atualizado_em": "t4"},
        ]
        cursor, completou, casados, gravados, fora = mod.processar_pagina(
            pagina, self.mapa, 0, 999, self.gravar, set(), agora=lambda: 0)
        self.assertEqual((cursor, completou, casados, gravados, fora), ("t4", True, 4, 3, 0))
        link = "https://drive.google.com/file/d/A/view"
        self.assertEqual(self.gravados, [
            ("137950", "Barbours RJ", link),
            ("137950", "BARBOURS RJ", link),
            ("137950", "BARBOURS RJ ", link),
        ])


class Main(unittest.TestCase):
    """O script inteiro (main) com a planilha e o Supabase simulados."""

    PAGINA = [
        {"romaneio": "343", "depositante": "BEAUTY HUB ES", "atualizado_em": "2026-11-10T10:00:00+00:00"},
        {"romaneio": "343", "depositante": "BEAUTY HUB ES", "atualizado_em": "2026-11-10T11:00:00+00:00"},
        {"romaneio": "555", "depositante": "BEAUTY HUB ES", "atualizado_em": "2026-11-10T12:00:00+00:00"},
    ]

    def rodar(self, argv, env=None):
        paginas = iter([list(self.PAGINA), []])
        with mock.patch.dict(os.environ, env or {}, clear=False),                 mock.patch.object(mod, "baixar_planilha", return_value=CSV),                 mock.patch.object(mod, "buscar_pagina", side_effect=lambda *_: next(paginas)) as buscar,                 mock.patch.object(mod, "gravar_link_romaneio") as patch_link,                 mock.patch.object(mod, "ler_cursor", return_value=None) as ler,                 mock.patch.object(mod, "gravar_cursor") as gravar_cursor,                 mock.patch.object(mod, "_supabase_request") as supabase,                 mock.patch("builtins.print"):
            mod.main(argv)
        return buscar, patch_link, ler, gravar_cursor, supabase

    def test_execucao_normal_grava_link_e_cursor(self):
        os.environ.pop("SIMULAR", None)
        buscar, patch_link, ler, gravar_cursor, supabase = self.rodar([])
        patch_link.assert_called_once_with("343", "BEAUTY HUB ES", "https://drive.google.com/file/d/B/view")
        ler.assert_called_once()
        # Salva o cursor ao fim da pagina e o zera (None) quando a janela acaba.
        self.assertEqual(gravar_cursor.call_args_list, [
            mock.call("2026-11-10T12:00:00+00:00"), mock.call(None)])
        self.assertEqual(buscar.call_count, 1)  # pagina menor que PAGINA encerra a janela
        supabase.assert_not_called()

    def test_simular_por_argumento_nao_grava_nem_mexe_no_cursor(self):
        os.environ.pop("SIMULAR", None)
        buscar, patch_link, ler, gravar_cursor, supabase = self.rodar(["--simular"])
        buscar.assert_called_once()
        patch_link.assert_not_called()
        ler.assert_not_called()
        gravar_cursor.assert_not_called()
        supabase.assert_not_called()

    def test_simular_por_variavel_de_ambiente(self):
        buscar, patch_link, ler, gravar_cursor, supabase = self.rodar([], env={"SIMULAR": "1"})
        buscar.assert_called_once()
        patch_link.assert_not_called()
        ler.assert_not_called()
        gravar_cursor.assert_not_called()
        supabase.assert_not_called()


class BaixarPlanilha(unittest.TestCase):
    """Acesso a planilha (17/11/2026): com os secrets GOOGLE_OAUTH_* baixa pela
    API do Drive; sem eles, pelo export publico. Rede toda simulada."""

    ENV_OAUTH = {"GOOGLE_OAUTH_CLIENT_ID": "id-falso", "GOOGLE_OAUTH_CLIENT_SECRET": "segredo-falso",
                 "GOOGLE_OAUTH_REFRESH_TOKEN": "refresh-falso"}

    class Resposta:
        def __init__(self, corpo, tipo="text/csv"):
            self.corpo, self.headers = corpo.encode("utf-8"), {"Content-Type": tipo}
        def read(self):
            return self.corpo
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    def erro_http(self, url, codigo, corpo=""):
        import io
        return mod.urllib.error.HTTPError(url, codigo, "erro", {}, io.BytesIO(corpo.encode("utf-8")))

    def test_sem_secrets_usa_export_publico(self):
        pedidos = []
        def abrir(req, timeout=None):
            pedidos.append(req)
            return self.Resposta(CSV)
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=abrir), mock.patch("builtins.print"):
            self.assertEqual(mod.baixar_planilha(env={}), CSV)
        self.assertEqual([r.full_url for r in pedidos], [mod.PLANILHA_CSV_URL])
        self.assertIsNone(pedidos[0].get_header("Authorization"))

    def test_com_secrets_baixa_pela_api_do_drive_com_bearer(self):
        pedidos = []
        def abrir(req, timeout=None):
            pedidos.append(req)
            if req.full_url == mod.GOOGLE_TOKEN_URL:
                return self.Resposta('{"access_token": "acesso-falso"}', "application/json")
            return self.Resposta(CSV)
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=abrir), mock.patch("builtins.print"):
            self.assertEqual(mod.baixar_planilha(env=self.ENV_OAUTH), CSV)
        self.assertEqual([r.full_url for r in pedidos], [mod.GOOGLE_TOKEN_URL, mod.DRIVE_EXPORT_URL])
        self.assertIn(b"grant_type=refresh_token", pedidos[0].data)
        self.assertEqual(pedidos[1].get_header("Authorization"), "Bearer acesso-falso")

    def test_refresh_token_recusado_derruba_com_motivo(self):
        def abrir(req, timeout=None):
            raise self.erro_http(req.full_url, 400, '{"error": "invalid_grant"}')
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=abrir):
            with self.assertRaisesRegex(RuntimeError, "invalid_grant"):
                mod.baixar_planilha(env=self.ENV_OAUTH)

    def test_planilha_fechada_no_export_publico_derruba(self):
        def abrir(req, timeout=None):
            raise self.erro_http(req.full_url, 401)
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=abrir):
            with self.assertRaisesRegex(RuntimeError, "GOOGLE_OAUTH_"):
                mod.baixar_planilha(env={})

    def test_conta_sem_acesso_na_api_do_drive_derruba(self):
        def abrir(req, timeout=None):
            if req.full_url == mod.GOOGLE_TOKEN_URL:
                return self.Resposta('{"access_token": "acesso-falso"}', "application/json")
            raise self.erro_http(req.full_url, 404)
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=abrir):
            with self.assertRaisesRegex(RuntimeError, "acesso de leitura"):
                mod.baixar_planilha(env=self.ENV_OAUTH)


if __name__ == "__main__":
    unittest.main()
