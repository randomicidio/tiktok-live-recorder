"""Testes dos pontos que quebram em silêncio.

    python testes.py

Usa o `unittest` da biblioteca padrão de propósito: o projeto se orgulha de não
depender de nada, e um teste que exige `pip install` é um teste que ninguém
roda. Não cobre a interface nem a rede - cobre o que, ao regredir, só apareceria
semanas depois num arquivo que não abre mais.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import atualizacao
import gift_log
import pacote
import recorder
import segredo
import tiktok_api


class NomesDeArquivo(unittest.TestCase):
    """`sanitize` decide o nome do MP4 a partir do título da live."""

    def test_tira_o_que_o_windows_recusa(self):
        self.assertEqual(recorder.sanitize('a<b>c:d"e/f\\g|h?i*j'), "abcdefghij")

    def test_preserva_outros_idiomas(self):
        # Um título em japonês ou com emoji não pode virar o nome de reserva.
        self.assertEqual(recorder.sanitize("配信中"), "配信中")
        self.assertEqual(recorder.sanitize("live 🎤 hoje"), "live 🎤 hoje")

    def test_cai_para_o_padrao_quando_nao_sobra_nada(self):
        self.assertEqual(recorder.sanitize("///"), "live")
        self.assertEqual(recorder.sanitize(""), "live")

    def test_limita_o_tamanho(self):
        self.assertEqual(len(recorder.sanitize("a" * 200)), 70)

    def test_nao_termina_em_ponto(self):
        # O Windows recusa silenciosamente nomes terminados em ponto.
        self.assertFalse(recorder.sanitize("titulo...").endswith("."))


class PacoteIdaEVolta(unittest.TestCase):
    """O .ttgifts precisa reabrir igual: ele é a única cópia dos presentes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ida_e_volta(self):
        destino = os.path.join(self.tmp, "teste" + pacote.EXTENSAO)
        presentes = [{"t": 12.5, "nome": "Rosa", "quantidade": 3,
                      "apelido": "Fulano", "diamantes": 1, "icone": ""}]
        comentarios = [{"t": 1.0, "texto": "olá 🎉", "apelido": "Beltrano",
                        "de": "beltrano", "avatar": "", "acumulado": False}]
        pacote.criar(destino=destino, video="live.mp4", conta="fulano",
                     inicio="2026-08-08T21:14:00", presentes=presentes,
                     animacoes={}, comentarios=comentarios,
                     figuras={"emote1": b"\x89PNG\r\n\x1a\n"})

        lido = pacote.abrir(destino)
        self.assertEqual(lido.video, "live.mp4")
        self.assertEqual(lido.conta, "fulano")
        self.assertEqual(len(lido.presentes), 1)
        self.assertEqual(lido.presentes[0].nome, "Rosa")
        self.assertEqual(lido.presentes[0].quantidade, 3)
        self.assertEqual(len(lido.comentarios), 1)
        # O emoji tem que sobreviver ao ida e volta pelo zip.
        self.assertEqual(lido.comentarios[0].texto, "olá 🎉")

    def test_acha_o_pacote_do_video(self):
        video = os.path.join(self.tmp, "live.mp4")
        open(video, "wb").close()
        destino = os.path.join(self.tmp, "live" + pacote.EXTENSAO)
        pacote.criar(destino=destino, video="live.mp4", conta="c", inicio="",
                     presentes=[], animacoes={}, comentarios=[{"t": 0.0,
                     "texto": "oi", "apelido": "A", "de": "a", "avatar": ""}])
        self.assertEqual(pacote.procurar_para(video), destino)

    def test_figura_ausente_devolve_none(self):
        # O compositor conta com isso: gravação antiga não tem as figuras, e
        # ele precisa pular em vez de quebrar.
        destino = os.path.join(self.tmp, "vazio" + pacote.EXTENSAO)
        pacote.criar(destino=destino, video="v.mp4", conta="c", inicio="",
                     presentes=[], animacoes={}, comentarios=[])
        self.assertIsNone(pacote.abrir(destino).abrir_figura("nao-existe"))


class LeituraDoRegistroCru(unittest.TestCase):
    """O .jsonl é o que sobrevive a uma queda de energia."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.caminho = os.path.join(self.tmp, "live_presentes.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _escrever(self, linhas, cru=""):
        with open(self.caminho, "w", encoding="utf-8") as fh:
            for linha in linhas:
                fh.write(json.dumps(linha, ensure_ascii=False) + "\n")
            fh.write(cru)

    def test_separa_os_tres_tipos(self):
        self._escrever([
            {"tipo": "presente", "t": 5.0, "nome": "Rosa"},
            {"tipo": "chat", "t": 1.0, "texto": "oi"},
            {"tipo": "figura", "chave": "e1", "receita": {"url": "http://x/e.png"}},
        ])
        presentes, comentarios, figuras = gift_log.ler_jsonl(self.caminho)
        self.assertEqual(len(presentes), 1)
        self.assertEqual(len(comentarios), 1)
        self.assertEqual(figuras, {"e1": {"url": "http://x/e.png"}})
        # `tipo` não pode vazar para dentro do pacote.
        self.assertNotIn("tipo", presentes[0])

    def test_ignora_a_linha_cortada_pela_queda(self):
        # É o caso real: a energia cai no meio de uma escrita.
        self._escrever([{"tipo": "chat", "t": 1.0, "texto": "oi"}],
                       cru='{"tipo":"chat","t":2.0,"tex')
        _, comentarios, _ = gift_log.ler_jsonl(self.caminho)
        self.assertEqual(len(comentarios), 1)

    def test_arquivo_inexistente_nao_levanta(self):
        self.assertEqual(gift_log.ler_jsonl(os.path.join(self.tmp, "nada.jsonl")),
                         ([], [], {}))


class VarreduraDeInterrompidas(unittest.TestCase):
    """A varredura decide o que aparece como resgatável na abertura."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _criar(self, nome, conteudo=b"x" * 1024):
        caminho = os.path.join(self.tmp, nome)
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        with open(caminho, "wb") as fh:
            fh.write(conteudo)
        return caminho

    def test_acha_partes_soltas(self):
        self._criar("conta_2026-08-08_21-14_Titulo_parte01.ts")
        self._criar("conta_2026-08-08_21-14_Titulo_parte02.ts")
        achadas = recorder.procurar_interrompidas(self.tmp)
        self.assertEqual(len(achadas), 1)
        self.assertEqual(len(achadas[0].partes), 2)
        self.assertEqual(achadas[0].bytes_total, 2048)

    def test_le_a_data_do_nome(self):
        self._criar("conta_2026-08-08_21-14_Titulo_parte01.ts")
        quando = recorder.procurar_interrompidas(self.tmp)[0].quando
        self.assertEqual((quando.year, quando.month, quando.day), (2026, 8, 8))
        self.assertEqual((quando.hour, quando.minute), (21, 14))

    def test_ignora_gravacao_que_terminou_bem(self):
        self._criar("pronta_2026-08-01_09-00_parte01.ts")
        self._criar("pronta_2026-08-01_09-00.mp4")
        self.assertEqual(recorder.procurar_interrompidas(self.tmp), [])

    def test_acha_formato_antigo_em_subpasta(self):
        self._criar(os.path.join("velha_2026-08-07_10-00", "parte01.ts"))
        achadas = recorder.procurar_interrompidas(self.tmp)
        self.assertEqual(len(achadas), 1)
        self.assertEqual(achadas[0].base, "velha_2026-08-07_10-00")

    def test_jsonl_orfao_sem_pacote(self):
        self._criar("so_pacote_2026-08-05_18-30.mp4")
        self._criar("so_pacote_2026-08-05_18-30_presentes.jsonl")
        achadas = recorder.procurar_interrompidas(self.tmp)
        self.assertEqual(len(achadas), 1)
        self.assertEqual(achadas[0].partes, [])
        self.assertTrue(achadas[0].jsonl)

    def test_jsonl_com_pacote_nao_aparece(self):
        self._criar("completa_2026-08-05_18-30.mp4")
        self._criar("completa_2026-08-05_18-30_presentes.jsonl")
        self._criar("completa_2026-08-05_18-30" + pacote.EXTENSAO)
        self.assertEqual(recorder.procurar_interrompidas(self.tmp), [])

    def test_pasta_inexistente(self):
        self.assertEqual(recorder.procurar_interrompidas(
            os.path.join(self.tmp, "nao-existe")), [])


class LeituraDaApi(unittest.TestCase):
    """Quando o TikTok mudar o formato, é aqui que o teste avisa primeiro."""

    def test_escolhe_origin_e_le_a_resolucao(self):
        cru = json.dumps({"data": {
            "origin": {"main": {"flv": "http://cdn/origin.flv",
                                "sdk_params": '{"vbitrate":2500000,"resolution":"1080x1920"}'}},
            "uhd": {"main": {"flv": "http://cdn/uhd.flv",
                             "sdk_params": '{"resolution":"720x1280"}'}},
        }})
        opcoes = tiktok_api._parse_stream_data(cru)
        self.assertIn("origin", opcoes)
        self.assertEqual(opcoes["origin"].resolution, "1080x1920")
        self.assertEqual(opcoes["origin"].best_url, "http://cdn/origin.flv")

    def test_pick_prefere_origin_e_cai_para_o_que_existe(self):
        info = tiktok_api.LiveInfo(username="x", is_live=True)
        info.streams = tiktok_api._parse_stream_data(json.dumps({"data": {
            "sd": {"main": {"flv": "http://cdn/sd.flv", "sdk_params": "{}"}}}}))
        escolhida = info.pick("origin")
        self.assertIsNotNone(escolhida)
        self.assertEqual(escolhida.quality, "sd")

    def test_formato_desconhecido_nao_levanta(self):
        # O TikTok mudar o formato tem que dar "nenhuma URL", nunca um crash.
        self.assertEqual(tiktok_api._parse_stream_data("isso não é json"), {})
        self.assertEqual(tiktok_api._parse_stream_data(None), {})


class ComparacaoDeVersao(unittest.TestCase):
    """Comparar como texto erraria justamente quando ninguém está olhando."""

    def test_maior_e_menor(self):
        self.assertTrue(atualizacao.e_mais_nova("v1.1", "1.0"))
        self.assertFalse(atualizacao.e_mais_nova("v1.0", "1.1"))

    def test_dez_e_maior_que_nove(self):
        # Em texto, "1.10" < "1.9". É o caso que motiva a função existir.
        self.assertTrue(atualizacao.e_mais_nova("v1.10", "1.9"))
        self.assertFalse(atualizacao.e_mais_nova("v1.9", "1.10"))

    def test_igual_nao_avisa(self):
        self.assertFalse(atualizacao.e_mais_nova("v1.0", "1.0"))
        # Partes a mais valendo zero: 1.0.0 é a mesma coisa que 1.0.
        self.assertFalse(atualizacao.e_mais_nova("v1.0.0", "1.0"))
        self.assertTrue(atualizacao.e_mais_nova("v1.0.1", "1.0"))

    def test_texto_ilegivel_nao_avisa(self):
        # Uma tag estranha não pode virar um aviso falso de atualização.
        self.assertFalse(atualizacao.e_mais_nova("nightly", "1.0"))
        self.assertFalse(atualizacao.e_mais_nova("", "1.0"))
        self.assertEqual(atualizacao.versao_em_tupla("sem numeros"), ())


@unittest.skipUnless(segredo.disponivel(), "DPAPI só existe no Windows")
class CookieCifrado(unittest.TestCase):
    """O cookie é a sessão da conta: não pode voltar a ficar legível em disco."""

    COOKIE = "sessionid=abc123; tt_csrf_token=XYZ"

    def test_ida_e_volta(self):
        cifrado = segredo.cifrar(self.COOKIE)
        self.assertTrue(cifrado.startswith(segredo.PREFIXO))
        self.assertEqual(segredo.decifrar(cifrado), self.COOKIE)

    def test_o_texto_nao_aparece_no_arquivo(self):
        cifrado = segredo.cifrar(self.COOKIE)
        self.assertNotIn("sessionid", cifrado)
        self.assertNotIn("abc123", cifrado)

    def test_cifrar_de_novo_nao_empilha(self):
        uma = segredo.cifrar(self.COOKIE)
        self.assertEqual(segredo.cifrar(uma), uma)

    def test_config_antigo_em_texto_puro_continua_valendo(self):
        # Quem já usava o programa tem o cookie sem prefixo no config.
        self.assertEqual(segredo.decifrar("cookie-antigo"), "cookie-antigo")

    def test_vazio(self):
        self.assertEqual(segredo.cifrar(""), "")
        self.assertEqual(segredo.decifrar(""), "")

    def test_blob_corrompido_devolve_vazio(self):
        # Config vindo de outra máquina: melhor "sem cookie" que um erro.
        self.assertEqual(segredo.decifrar(segredo.PREFIXO + "!!!lixo"), "")


class EspacoEmDisco(unittest.TestCase):
    def test_pasta_inexistente_devolve_menos_um(self):
        self.assertEqual(recorder.espaco_livre(os.path.join(
            tempfile.gettempdir(), "nao-existe-mesmo-123")), -1)

    def test_estimativa_de_horas(self):
        # ~1,1 GB por hora: 11 GB devem dar por volta de 10 h.
        horas = recorder.horas_que_cabem(11 * 1024 ** 3)
        self.assertGreater(horas, 8)
        self.assertLess(horas, 12)

    def test_sem_espaco_conhecido_nao_estima(self):
        self.assertEqual(recorder.horas_que_cabem(-1), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
