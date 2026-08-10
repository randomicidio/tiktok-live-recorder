"""Guarda o cookie do TikTok cifrado, em vez de legível no `config.json`.

O cookie é a sessão da conta: quem o tem entra como você. Ele ficava em texto
puro num arquivo ao lado do executável - e o README incentiva copiar esse
executável para pendrive, então a sessão ia de carona sem ninguém perceber.

No Windows usa a DPAPI, que é do próprio sistema: a chave vem da conta de
usuário logada, sem senha para inventar nem arquivo de chave para guardar. O
efeito colateral é o desejado - o `config.json` copiado para outra máquina
simplesmente não abre o cookie lá.

Fora do Windows não há equivalente igualmente simples, então o valor continua
como está e o programa avisa. Nada de cifra caseira: dar aparência de proteção
sem a proteção é pior que assumir que não há.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

import resources

# Marca o que já passou pela DPAPI. Sem ela não dá para distinguir um cookie
# cifrado de um cookie que a pessoa colou à mão.
PREFIXO = "dpapi:"


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(dados: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(dados, len(dados))
    return _BLOB(len(dados), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _ler_blob(saida: _BLOB) -> bytes:
    dados = ctypes.string_at(saida.pbData, saida.cbData)
    ctypes.windll.kernel32.LocalFree(saida.pbData)
    return dados


def disponivel() -> bool:
    """Dá para cifrar nesta máquina?"""
    return resources.IS_WINDOWS


def cifrar(texto: str) -> str:
    """Devolve o valor pronto para gravar no config.

    Em caso de falha devolve o texto original: perder o cookie por causa da
    cifra seria pior que guardá-lo como antes.
    """
    if not texto or texto.startswith(PREFIXO) or not disponivel():
        return texto
    entrada = _blob(texto.encode("utf-8"))
    saida = _BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(entrada), "cookie TikTok", None, None, None, 0,
            ctypes.byref(saida))
    except (OSError, AttributeError):
        return texto
    if not ok:
        return texto
    return PREFIXO + base64.b64encode(_ler_blob(saida)).decode("ascii")


def decifrar(valor: str) -> str:
    """Lê o que veio do config, cifrado ou não.

    Valores antigos, em texto puro, continuam funcionando: eles não têm o
    prefixo e passam direto. Na primeira vez que o config for salvo de novo,
    passam a ser gravados cifrados.
    """
    if not valor or not valor.startswith(PREFIXO):
        return valor
    if not disponivel():
        return ""          # cifrado noutra maquina; nao ha o que fazer aqui
    try:
        bruto = base64.b64decode(valor[len(PREFIXO):])
    except (ValueError, TypeError):
        return ""
    entrada = _blob(bruto)
    saida = _BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(entrada), None, None, None, None, 0,
            ctypes.byref(saida))
    except (OSError, AttributeError):
        return ""
    if not ok:
        # Config copiado de outra conta ou de outra maquina: a DPAPI recusa.
        return ""
    return _ler_blob(saida).decode("utf-8", "replace")
