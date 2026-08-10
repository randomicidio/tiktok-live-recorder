"""Descobre se saiu uma versão mais nova.

Isso importa mais aqui do que na maioria dos programas: os endpoints que o
`tiktok_api` usa são internos do TikTok e podem mudar sem aviso. Quando isso
acontecer, quem baixou o .exe fica com um programa que só diz "Falhou" - e sem
nenhuma pista de que já existe conserto publicado.

Só avisa. Não baixa nada e não altera nada: atualizar continua sendo baixar o
executável novo e pôr no lugar do antigo.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import resources

REPO = "randomicidio/tiktok-live-recorder"
URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGINA = f"https://github.com/{REPO}/releases/latest"

# Uma consulta na abertura não pode segurar nada; se a rede estiver ruim,
# desistir em silêncio é melhor que insistir.
ESPERA = 6


def versao_em_tupla(texto: str) -> tuple[int, ...]:
    """"v1.2.3" -> (1, 2, 3). Devolve () para o que não der para ler.

    Comparar como texto erraria em "1.10" contra "1.9", que é exatamente o
    momento em que ninguém está olhando.
    """
    achados = re.findall(r"\d+", texto or "")
    return tuple(int(n) for n in achados) if achados else ()


def e_mais_nova(publicada: str, atual: str) -> bool:
    """A versão publicada é mais nova que a que está rodando?"""
    a, b = versao_em_tupla(publicada), versao_em_tupla(atual)
    if not a or not b:
        return False
    # Compara com o mesmo número de partes: (1, 1) contra (1, 1, 0) é empate.
    tamanho = max(len(a), len(b))
    a = a + (0,) * (tamanho - len(a))
    b = b + (0,) * (tamanho - len(b))
    return a > b


def procurar() -> str:
    """Devolve a versão publicada se ela for mais nova, senão "".

    Nunca levanta: sem rede, sem release publicada ou com o GitHub fora do ar,
    o programa abre exatamente como abriria sem esta consulta.
    """
    try:
        pedido = urllib.request.Request(
            URL, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": f"{resources.APP_NAME}/{resources.APP_VERSION}"})
        with urllib.request.urlopen(pedido, timeout=ESPERA) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return ""

    if not isinstance(dados, dict):
        return ""
    tag = str(dados.get("tag_name") or dados.get("name") or "")
    return tag if e_mais_nova(tag, resources.APP_VERSION) else ""
