"""Registro em arquivo, para quando algo dá errado na máquina de outra pessoa.

O registro da tela vive num widget e morre junto com a janela. Como o programa
é distribuído como .exe para quem não tem Python, "não funcionou aqui" chegava
sem nenhum rastro. Aqui as mesmas linhas vão também para um arquivo ao lado do
`config.json`, e qualquer exceção não tratada é anotada antes de a janela sumir.

O arquivo é rotativo e pequeno de propósito: interessa a última sessão, não o
histórico completo.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime

import resources

# Acima disto o arquivo vira `.1` e um novo começa. Duas sessões longas cabem
# folgadas em 2 MB, e o que interessa para o suporte é sempre a mais recente.
LIMITE_BYTES = 2 * 1024 * 1024

_trava = threading.Lock()
_caminho = ""
_ligado = False


def caminho() -> str:
    """Onde o arquivo está, ou "" se o registro não pôde ser aberto."""
    return _caminho


def _rotacionar() -> None:
    """Guarda o arquivo cheio como `.1` e recomeça. Só uma geração antiga."""
    try:
        if os.path.getsize(_caminho) < LIMITE_BYTES:
            return
    except OSError:
        return
    antigo = _caminho + ".1"
    try:
        if os.path.exists(antigo):
            os.remove(antigo)
        os.replace(_caminho, antigo)
    except OSError:
        pass          # sem permissão para rotacionar: segue escrevendo no mesmo


def escrever(msg: str) -> None:
    """Anota uma linha. Nunca levanta: registro que derruba o programa é pior
    que registro nenhum."""
    if not _ligado or not _caminho:
        return
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    with _trava:
        try:
            with open(_caminho, "a", encoding="utf-8") as fh:
                fh.write(linha)
            _rotacionar()
        except OSError:
            pass


def _anotar_excecao(tipo, valor, tb) -> None:
    escrever("EXCEÇÃO NÃO TRATADA:\n"
             + "".join(traceback.format_exception(tipo, valor, tb)).rstrip())


def iniciar() -> str:
    """Abre o arquivo e passa a capturar exceções não tratadas.

    Devolve o caminho, ou "" se a pasta não aceitar escrita - nesse caso o
    programa segue normalmente, só sem registro em arquivo.
    """
    global _caminho, _ligado
    if _ligado:
        return _caminho

    try:
        pasta = resources.data_dir()
        os.makedirs(pasta, exist_ok=True)
        _caminho = os.path.join(pasta, "registro.log")
        # Confere agora que dá para escrever: descobrir isso só na primeira
        # exceção seria descobrir tarde demais.
        with open(_caminho, "a", encoding="utf-8"):
            pass
    except OSError:
        _caminho = ""
        return ""

    _ligado = True
    _rotacionar()
    escrever(f"--- {resources.APP_NAME} v{resources.APP_VERSION} "
             f"({'empacotado' if resources.IS_FROZEN else 'script'}, "
             f"python {sys.version.split()[0]}) ---")

    # Três caminhos diferentes por onde uma exceção escapa: a thread principal,
    # qualquer outra thread, e os callbacks do Tk (que têm o próprio gancho,
    # instalado em `vigiar_tk`).
    anterior = sys.excepthook

    def gancho(tipo, valor, tb):
        _anotar_excecao(tipo, valor, tb)
        anterior(tipo, valor, tb)

    sys.excepthook = gancho

    anterior_thread = getattr(threading, "excepthook", None)

    def gancho_thread(args):
        _anotar_excecao(args.exc_type, args.exc_value, args.exc_traceback)
        if anterior_thread is not None:
            anterior_thread(args)

    if anterior_thread is not None:
        threading.excepthook = gancho_thread

    return _caminho


def vigiar_tk(raiz) -> None:
    """Faz as exceções dos callbacks do Tk caírem no arquivo também.

    Sem isso elas iriam só para o stderr - que num `.exe` empacotado com
    `--windowed` não existe, e a falha sumiria sem deixar nada.
    """
    def relatar(tipo, valor, tb):
        _anotar_excecao(tipo, valor, tb)

    try:
        raiz.report_callback_exception = relatar
    except AttributeError:
        pass
