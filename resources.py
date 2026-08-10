"""Localiza arquivos e binarios, rodando como script ou como executavel.

Empacotado com PyInstaller, o programa vive em dois lugares ao mesmo tempo:
os recursos embutidos sao extraidos para uma pasta temporaria (`sys._MEIPASS`),
enquanto o executavel em si esta onde o usuario o deixou. Config e gravacoes
tem que ir para perto do executavel (ou para a pasta do usuario), nunca para a
temporaria - que some quando o programa fecha.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

APP_NAME = "Tiktok Live Recorder"
APP_VERSION = "1.0"

IS_FROZEN = getattr(sys, "frozen", False)
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def app_dir() -> str:
    """Pasta onde o programa esta instalado."""
    if IS_FROZEN:
        exe = os.path.dirname(os.path.abspath(sys.executable))
        if IS_MAC and exe.endswith("/Contents/MacOS"):
            # Dentro de um .app: sobe ate a pasta que contem o pacote.
            return os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        return exe
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir() -> str:
    """Pasta dos recursos embutidos no executavel."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def resource(*parts: str) -> str:
    """Caminho de um recurso embutido (icone, por exemplo)."""
    return os.path.join(bundle_dir(), *parts)


def _is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        teste = os.path.join(path, ".escrita_teste")
        with open(teste, "w") as fh:
            fh.write("ok")
        os.remove(teste)
        return True
    except OSError:
        return False


def data_dir() -> str:
    """Onde guardar config e miniaturas.

    Fica ao lado do executavel quando da (uso portatil, tudo junto). Se a pasta
    for somente leitura - Program Files, /Applications, pendrive protegido -
    cai para a area do usuario.
    """
    preferida = app_dir()
    if _is_writable(preferida):
        return preferida
    if IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    elif IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    destino = os.path.join(base, "TiktokLiveRecorder")
    os.makedirs(destino, exist_ok=True)
    return destino


def default_output_dir() -> str:
    """Pasta sugerida para as gravacoes, na primeira execucao."""
    if IS_MAC:
        base = os.path.expanduser("~/Movies")
    elif IS_WINDOWS:
        base = os.path.join(os.path.expanduser("~"), "Videos")
    else:
        base = os.path.expanduser("~/Videos")
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    return os.path.join(base, "TikTok Lives")


def open_path(path: str) -> None:
    """Abre o arquivo ou a pasta no programa padrao do sistema.

    Mora aqui, e nao na janela, porque as duas abas precisam dela: o gravador
    oferece o video assim que termina, e o editor faz o mesmo com o exportado.
    """
    if not os.path.exists(path):
        return
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_explorer(path: str) -> None:
    """Mostra o arquivo ja selecionado no gerenciador de arquivos."""
    if not os.path.exists(path):
        return
    if IS_WINDOWS:
        # O explorer devolve codigo 1 mesmo dando certo; nao ha o que checar.
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])   # -R revela no Finder
    else:
        open_path(os.path.dirname(path))


def find_binary(nome: str) -> str | None:
    """Procura um executavel auxiliar: embutido primeiro, sistema depois.

    A copia embutida vem antes de proposito: garante a mesma versao em qualquer
    maquina, sem depender do que o usuario tenha instalado.
    """
    sufixo = ".exe" if IS_WINDOWS else ""
    alvo = nome + sufixo
    for pasta in (bundle_dir(), os.path.join(bundle_dir(), "bin"), app_dir()):
        caminho = os.path.join(pasta, alvo)
        if os.path.isfile(caminho):
            return caminho
    return shutil.which(nome)
