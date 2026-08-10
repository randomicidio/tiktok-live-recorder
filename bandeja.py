"""Ícone na área de notificação do Windows, com avisos em balão.

O modo normal de usar o programa é armar o REC e esperar horas, então ocupar um
lugar na barra de tarefas esse tempo todo não faz sentido. Aqui a janela some e
o programa continua vivo num ícone ao lado do relógio.

Feito em `ctypes` puro, sem biblioteca de fora, para o executável continuar
sendo montado exatamente como antes - o projeto se orgulha de ser um arquivo só
que não depende de nada, e uma dependência que existe só na máquina que empacota
é a pior espécie: quando falta, o build sai silenciosamente sem o recurso.

O Windows exige que o ícone pertença a uma thread com laço de mensagens
próprio, e essa thread não pode ser a do Tk. Por isso tudo aqui vive numa
thread separada, e quem está de fora conversa com ela por uma fila - os
callbacks são disparados de volta na thread da bandeja, então quem os recebe
precisa reenfileirar para o Tk (é o que a janela faz com `events`).
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
from ctypes import wintypes

import resources

DISPONIVEL = resources.IS_WINDOWS

# --- constantes do Win32 ---------------------------------------------------
_WM_APP = 0x8000
_WM_BANDEJA = _WM_APP + 1          # o ícone fala com a janela por esta mensagem
_WM_COMANDO = _WM_APP + 2          # a fila pede algo à thread da bandeja
_WM_DESTROY = 0x0002
_WM_COMMAND = 0x0111
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_WM_NULL = 0x0000

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x01, 0x02, 0x04, 0x10
_NIIF_INFO = 0x01

_IMAGE_ICON = 1
_LR_LOADFROMFILE, _LR_DEFAULTSIZE = 0x0010, 0x0040
_IDI_APPLICATION = 32512

_MF_STRING, _MF_SEPARATOR = 0x0000, 0x0800
_TPM_RIGHTBUTTON, _TPM_RETURNCMD = 0x0002, 0x0100

_ID_RESTAURAR, _ID_SAIR = 1001, 1002

_user32 = ctypes.windll.user32 if DISPONIVEL else None
_shell32 = ctypes.windll.shell32 if DISPONIVEL else None
_kernel32 = ctypes.windll.kernel32 if DISPONIVEL else None

_WNDPROC = (ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM)
            if DISPONIVEL else None)


def _declarar_prototipos() -> None:
    """Diz ao ctypes o tamanho de cada argumento e de cada retorno.

    Sem isto ele assume `int` de 32 bits: os handles (HWND, HICON, HMENU) sao
    de 64 bits no Windows atual e voltariam truncados, e o `lparam` que chega
    do laco de mensagens estoura na hora de ser repassado ao DefWindowProc.
    Funciona por sorte ate o dia em que um handle passa dos 32 bits.
    """
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = ctypes.c_longlong

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
    _user32.RegisterClassW.restype = wintypes.ATOM

    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND

    _user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                   wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = wintypes.HICON

    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_void_p, wintypes.LPCWSTR]
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, ctypes.c_void_p]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]

    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                           ctypes.POINTER(_NOTIFYICONDATA)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC or ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class _NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# Depois das estruturas de propósito: os protótipos apontam para elas.
if DISPONIVEL:
    _declarar_prototipos()


class Bandeja:
    """O ícone ao lado do relógio. Só existe enquanto `ligar()` deu certo."""

    def __init__(self, ao_restaurar, ao_sair, dica: str = ""):
        self.ao_restaurar = ao_restaurar
        self.ao_sair = ao_sair
        self._dica = (dica or resources.APP_NAME)[:127]
        self._fila: queue.Queue = queue.Queue()
        self._hwnd = None
        self._hicon = None
        self._proc = None          # a referencia precisa sobreviver ao registro
        self._thread: threading.Thread | None = None
        self._pronta = threading.Event()
        self._visivel = False
        self.ligada = False

    # ------------------------------------------------------------- controle

    def ligar(self) -> bool:
        """Sobe a thread da bandeja. Devolve se deu certo."""
        if not DISPONIVEL or self.ligada:
            return self.ligada
        self._thread = threading.Thread(target=self._rodar, daemon=True)
        self._thread.start()
        # Sem esperar, um `mostrar()` logo em seguida chegaria antes da janela
        # existir. Dois segundos é folga larga para criar uma janela oculta.
        self._pronta.wait(timeout=2.0)
        return self.ligada

    def _pedir(self, comando: str, *args) -> None:
        """Enfileira e acorda o laço: o ícone só aceita ordens da dona dele."""
        if not self.ligada or not self._hwnd:
            return
        self._fila.put((comando, args))
        _user32.PostMessageW(self._hwnd, _WM_COMANDO, 0, 0)

    def mostrar(self, dica: str = "") -> None:
        self._pedir("mostrar", dica or self._dica)

    def esconder(self) -> None:
        self._pedir("esconder")

    def dica(self, texto: str) -> None:
        """Texto que aparece ao passar o mouse: é onde o estado é mostrado."""
        self._pedir("dica", texto[:127])

    def notificar(self, titulo: str, texto: str) -> None:
        self._pedir("notificar", titulo[:63], texto[:255])

    def encerrar(self) -> None:
        if not self.ligada:
            return
        self._pedir("encerrar")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.ligada = False

    # --------------------------------------------------------------- thread

    def _rodar(self) -> None:
        try:
            self._criar_janela()
        except Exception:                            # noqa: BLE001
            self._pronta.set()
            return                                    # sem bandeja; o resto segue
        self.ligada = True
        self._pronta.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        self._remover_icone()

    def _criar_janela(self) -> None:
        self._proc = _WNDPROC(self._ao_receber)
        classe = _WNDCLASS()
        classe.lpfnWndProc = self._proc
        classe.hInstance = _kernel32.GetModuleHandleW(None)
        classe.lpszClassName = "TiktokLiveRecorderBandeja"
        if not _user32.RegisterClassW(ctypes.byref(classe)):
            # Já registrada (uma segunda instância nesta mesma sessão): segue.
            if _kernel32.GetLastError() != 1410:      # ERROR_CLASS_ALREADY_EXISTS
                raise OSError("não consegui registrar a classe da janela")
        # Janela normal, nunca mostrada - e não HWND_MESSAGE: o menu de contexto
        # precisa de uma janela de verdade para receber o foco e fechar sozinho
        # quando a pessoa clica fora.
        self._hwnd = _user32.CreateWindowExW(
            0, classe.lpszClassName, resources.APP_NAME, 0,
            0, 0, 0, 0, None, None, classe.hInstance, None)
        if not self._hwnd:
            raise OSError("não consegui criar a janela oculta")
        self._hicon = self._carregar_icone()

    def _carregar_icone(self):
        caminho = resources.resource("assets", "icon.ico")
        if os.path.exists(caminho):
            achado = _user32.LoadImageW(None, caminho, _IMAGE_ICON, 16, 16,
                                        _LR_LOADFROMFILE)
            if achado:
                return achado
        # Sem o .ico o programa não fica sem bandeja: usa o ícone padrão.
        return _user32.LoadIconW(None, ctypes.c_wchar_p(_IDI_APPLICATION))

    # --------------------------------------------------------------- ícone

    def _dados(self, flags: int) -> _NOTIFYICONDATA:
        dados = _NOTIFYICONDATA()
        dados.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        dados.hWnd = self._hwnd
        dados.uID = 1
        dados.uFlags = flags
        dados.uCallbackMessage = _WM_BANDEJA
        dados.hIcon = self._hicon
        return dados

    def _remover_icone(self) -> None:
        if self._visivel and self._hwnd:
            _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._dados(0)))
            self._visivel = False

    def _executar(self, comando: str, args) -> None:
        if comando == "mostrar":
            dados = self._dados(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP)
            dados.szTip = args[0][:127]
            acao = _NIM_MODIFY if self._visivel else _NIM_ADD
            if _shell32.Shell_NotifyIconW(acao, ctypes.byref(dados)):
                self._visivel = True
        elif comando == "esconder":
            self._remover_icone()
        elif comando == "dica" and self._visivel:
            dados = self._dados(_NIF_TIP)
            dados.szTip = args[0][:127]
            _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(dados))
        elif comando == "notificar" and self._visivel:
            dados = self._dados(_NIF_INFO)
            dados.szInfoTitle = args[0][:63]
            dados.szInfo = args[1][:255]
            dados.dwInfoFlags = _NIIF_INFO
            _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(dados))
        elif comando == "encerrar":
            self._remover_icone()
            _user32.PostQuitMessage(0)

    # ------------------------------------------------------------ mensagens

    def _ao_receber(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_COMANDO:
                while True:
                    try:
                        comando, args = self._fila.get_nowait()
                    except queue.Empty:
                        break
                    self._executar(comando, args)
                return 0
            if msg == _WM_BANDEJA:
                evento = lparam & 0xFFFF
                if evento in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                    self._chamar(self.ao_restaurar)
                elif evento == _WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == _WM_COMMAND:
                escolha = wparam & 0xFFFF
                if escolha == _ID_RESTAURAR:
                    self._chamar(self.ao_restaurar)
                elif escolha == _ID_SAIR:
                    self._chamar(self.ao_sair)
                return 0
            if msg == _WM_DESTROY:
                self._remover_icone()
                _user32.PostQuitMessage(0)
                return 0
        except Exception:                            # noqa: BLE001
            # Uma excecao escapando daqui atravessa a fronteira do Win32 e
            # derruba o processo inteiro, nao so a bandeja.
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _chamar(self, funcao) -> None:
        if funcao is None:
            return
        try:
            funcao()
        except Exception:                            # noqa: BLE001
            pass

    def _menu(self) -> None:
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        try:
            _user32.AppendMenuW(menu, _MF_STRING, _ID_RESTAURAR, "Mostrar a janela")
            _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(menu, _MF_STRING, _ID_SAIR, "Sair")
            ponto = _POINT()
            _user32.GetCursorPos(ctypes.byref(ponto))
            # Sem trazer a janela para a frente o menu fica aberto para sempre
            # quando a pessoa clica fora dele - é um defeito antigo e conhecido
            # do Shell_NotifyIcon, e este é o contorno documentado.
            _user32.SetForegroundWindow(self._hwnd)
            escolha = _user32.TrackPopupMenu(
                menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD, ponto.x, ponto.y,
                0, self._hwnd, None)
            _user32.PostMessageW(self._hwnd, _WM_NULL, 0, 0)
            if escolha == _ID_RESTAURAR:
                self._chamar(self.ao_restaurar)
            elif escolha == _ID_SAIR:
                self._chamar(self.ao_sair)
        finally:
            _user32.DestroyMenu(menu)
