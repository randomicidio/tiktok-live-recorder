"""Aba do editor: prévia com as camadas e exportação do trecho escolhido.

A prévia usa a libmpv embutida na janela. O vídeo é decodificado por ela; o que
vai por cima - animação de presente, chat e contador - é desenhado aqui e
entregue à libmpv como camadas BGRA já pré-multiplicadas.

Isso é o que permite a prévia andar lisa: as animações são decodificadas uma
vez, já no tamanho da tela e com o alfa embutido, e ficam num arquivo lido por
mmap. Mostrar um quadro passa a ser copiar uma fatia desse arquivo, então dá
para atualizar a 30 quadros por segundo sem pesar. Chat e contador são
redesenhados só quando mudam de verdade.
"""

from __future__ import annotations

import ctypes
import json
import mmap
import os
import subprocess
import threading
import time
import tkinter as tk
from array import array
from tkinter import filedialog, messagebox, ttk

import compositor
import pacote as pacote_mod
import resources

_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# --------------------------------------------------- preferências do editor

# Ficam fora do config.json do gravador: são da aba de edição e o outro arquivo
# descarta chaves que não conhece.
_PREFS_PATH = os.path.join(resources.data_dir(), "editor.json")
_prefs_cache: dict | None = None


def _prefs() -> dict:
    global _prefs_cache
    if _prefs_cache is None:
        try:
            with open(_PREFS_PATH, encoding="utf-8") as fh:
                dados = json.load(fh)
            _prefs_cache = dados if isinstance(dados, dict) else {}
        except (OSError, ValueError):
            _prefs_cache = {}
    return _prefs_cache


def _lembrar(chave: str, valor) -> None:
    if _prefs().get(chave) == valor:
        return
    _prefs()[chave] = valor
    try:
        with open(_PREFS_PATH, "w", encoding="utf-8") as fh:
            json.dump(_prefs(), fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


# Abrir e exportar guardam pastas diferentes de propósito: o replay da live fica
# numa pasta e os cortes vão para outra. Sem isto o Tk lembra de uma pasta só
# para todos os diálogos, então depois de exportar a busca pelo vídeo começava
# na pasta dos cortes.
def _pasta_lembrada(chave: str, alternativa: str = "") -> str:
    """A pasta guardada para essa chave; a alternativa se ela sumiu do disco.

    Devolver "" é aceitável: o diálogo então abre onde o sistema quiser.
    """
    caminho = _prefs().get(chave, "")
    if isinstance(caminho, str) and caminho and os.path.isdir(caminho):
        return caminho
    return alternativa if alternativa and os.path.isdir(alternativa) else ""


def _lembrar_pasta(chave: str, arquivo: str) -> None:
    pasta = os.path.dirname(os.path.abspath(arquivo))
    if os.path.isdir(pasta):
        _lembrar(chave, pasta)


# ------------------------------------------------------------------ libmpv

def _acha_libmpv() -> str:
    """Procura a DLL embutida no pacote, ao lado do programa, ou no sistema."""
    nomes = ("libmpv-2.dll", "mpv-2.dll", "mpv-1.dll") if resources.IS_WINDOWS \
        else ("libmpv.dylib", "libmpv.so.2", "libmpv.so")
    pastas = [resources.bundle_dir(),
              os.path.join(resources.bundle_dir(), "mpvlib"),
              os.path.join(resources.app_dir(), "mpvlib"),
              resources.app_dir()]
    for pasta in pastas:
        for nome in nomes:
            caminho = os.path.join(pasta, nome)
            if os.path.isfile(caminho):
                return caminho
    return ""


class Sobreposicao:
    """Uma camada BGRA que a própria libmpv compõe sobre o vídeo.

    A classe que vem com a biblioteca refaz um `alpha_composite` da imagem
    inteira a cada atualização - o suficiente para a prévia engasgar quando o
    painel é grande. Aqui os bytes já chegam prontos e só são copiados para o
    buffer que a libmpv lê.
    """

    def __init__(self, mpv):
        self.mpv = mpv
        self.ident = mpv.allocate_overlay_id()
        self._buf = None
        self._tam = 0
        self.visivel = False

    def desenhar(self, dados: bytes, largura: int, altura: int, x: int, y: int) -> None:
        n = largura * altura * 4
        if self._tam != n:
            self._buf = ctypes.create_string_buffer(n)
            self._tam = n
        ctypes.memmove(self._buf, dados, n)
        self.mpv.overlay_add(self.ident, int(x), int(y),
                             "&" + str(ctypes.addressof(self._buf)), 0,
                             "bgra", largura, altura, largura * 4)
        self.visivel = True

    def esconder(self) -> None:
        if self.visivel:
            try:
                self.mpv.overlay_remove(self.ident)
            except Exception:                        # noqa: BLE001
                pass
            self.visivel = False

    def encerrar(self) -> None:
        self.esconder()
        try:
            self.mpv.free_overlay_id(self.ident)
        except Exception:                            # noqa: BLE001
            pass


class Player:
    """Player embutido. Se a libmpv faltar, o editor segue sem prévia."""

    def __init__(self, widget):
        self.mpv = None
        self.disponivel = False
        self.erro = ""

        dll = _acha_libmpv()
        if dll:
            pasta = os.path.dirname(dll)
            try:
                os.add_dll_directory(pasta)
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = pasta + os.pathsep + os.environ.get("PATH", "")

        try:
            import mpv as libmpv
        except (ImportError, OSError) as e:
            self.erro = f"prévia indisponível ({e})"
            return

        try:
            self.mpv = libmpv.MPV(wid=str(widget.winfo_id()), vo="gpu",
                                  keep_open="yes", pause=True, ytdl=False, osc=False,
                                  hr_seek="yes")
            self.disponivel = True
        except Exception as e:                       # noqa: BLE001
            self.erro = f"não consegui iniciar o player ({e})"

    # ------------------------------------------------------------ controles

    def carregar(self, caminho: str) -> None:
        if self.mpv:
            try:
                # Abrir pausado é importante no editor: evita que a troca das
                # camadas aconteça enquanto o primeiro quadro ainda decodifica.
                self.mpv["pause"] = True
                self.mpv.command("loadfile", caminho, "replace")
            except Exception:                        # noqa: BLE001
                pass

    def buscar(self, t: float, preciso: bool = True) -> None:
        """Busca uma posição. Sem precisão vai para o keyframe mais próximo,
        que é muito mais rápido - o certo enquanto se arrasta a barra."""
        if self.mpv:
            try:
                self.mpv.command("seek", str(max(0.0, t)), "absolute",
                                 "exact" if preciso else "keyframes")
            except Exception:                        # noqa: BLE001
                pass

    def tocar(self, sim: bool) -> None:
        if self.mpv:
            try:
                self.mpv["pause"] = not sim
            except Exception:                        # noqa: BLE001
                pass

    def passo_quadro(self, direcao: int) -> None:
        """Anda um quadro exato. A própria libmpv sabe onde ele começa."""
        if self.mpv:
            try:
                self.mpv.command("frame-step" if direcao > 0 else "frame-back-step")
            except Exception:                        # noqa: BLE001
                pass

    def audio(self, ligado: bool, volume: float = 50.0) -> None:
        """Controla o som sem interferir no áudio salvo pelo gravador."""
        if self.mpv:
            try:
                self.mpv["mute"] = not ligado
                self.mpv["volume"] = max(0, min(100, float(volume)))
            except Exception:                        # noqa: BLE001
                pass

    def atraso_audio(self, segundos: float) -> None:
        """Compensa a sincronia do arquivo: positivo atrasa o áudio.

        É a própria libmpv que reencaixa os tempos, então a prévia continua
        andando lisa - e as camadas, que seguem o relógio do vídeo, não se
        mexem.
        """
        if self.mpv:
            try:
                self.mpv["audio-delay"] = float(segundos)
            except Exception:                        # noqa: BLE001
                pass

    def parar(self) -> None:
        if self.mpv:
            try:
                self.mpv.command("stop")
            except Exception:                        # noqa: BLE001
                pass

    @property
    def tocando(self) -> bool:
        try:
            return bool(self.mpv) and not self.mpv["pause"]
        except Exception:                            # noqa: BLE001
            return False

    @property
    def fim_do_arquivo(self) -> bool:
        """Com `keep_open`, chegar ao fim pausa em vez de fechar. Quem repete
        um trecho que termina no fim do vídeo precisa saber diferenciar isso
        de uma pausa que o usuário pediu."""
        try:
            return bool(self.mpv) and bool(self.mpv.eof_reached)
        except Exception:                            # noqa: BLE001
            return False

    @property
    def posicao(self) -> float:
        try:
            return float(self.mpv.time_pos or 0.0)
        except Exception:                            # noqa: BLE001
            return 0.0

    @property
    def duracao(self) -> float:
        try:
            return float(self.mpv.duration or 0.0)
        except Exception:                            # noqa: BLE001
            return 0.0

    @property
    def tamanho_osd(self) -> tuple[int, int]:
        """Tamanho da área de vídeo na janela, em pixels.

        É nesse espaço que a sobreposição é posicionada - não no do vídeo.
        """
        try:
            return int(self.mpv.osd_width or 0), int(self.mpv.osd_height or 0)
        except Exception:                            # noqa: BLE001
            return 0, 0

    def criar_camada(self):
        if not self.mpv:
            return None
        try:
            return Sobreposicao(self.mpv)
        except Exception:                            # noqa: BLE001
            return None

    def encerrar(self) -> None:
        if self.mpv:
            try:
                self.mpv.terminate()
            except Exception:                        # noqa: BLE001
                pass
            self.mpv = None


# --------------------------------------------------- animações da prévia

class Animacao:
    """Uma animação de presente pronta para aparecer na prévia.

    Os quadros ficam em disco no tamanho exato da tela, em BGRA com o alfa já
    embutido na cor. Mostrar um deles é copiar uma fatia do arquivo - não há
    conversão nem redimensionamento no meio do caminho, que é o que fazia a
    animação andar aos trancos.
    """

    def __init__(self, item, largura: int, altura: int, fps: int, destino: str):
        self.item = item
        self.largura = max(2, largura)
        self.altura = max(2, altura)
        self.fps = fps
        self.destino = destino
        self.quadros = 0
        self.erro = ""
        self._mm = None
        self._fh = None
        self._por_quadro = self.largura * self.altura * 4

    # O filtro `premultiply` faz o trabalho que teria de ser feito em Python a
    # cada quadro; deixá-lo com o ffmpeg sai de graça.
    def _argumentos(self) -> list[str]:
        cfg = self.item.cfg
        rgb, alfa = cfg.get("rgbFrame"), cfg.get("aFrame")
        acerto = (f"scale={self.largura}:{self.altura},format=rgba,"
                  f"premultiply=inplace=1,format=bgra")
        ffmpeg = compositor.recorder._ffmpeg()
        # No .webm o alfa do VP9 vem em dados anexos ao quadro, e só a libvpx
        # sabe lê-los: com o decodificador nativo a animação sai opaca, com
        # tarja preta em volta.
        entrada = ["-c:v", "libvpx-vp9"] if self.item.arquivo.endswith(".webm") else []
        entrada += ["-i", self.item.arquivo]
        if rgb and alfa and rgb != alfa:
            rx, ry, rw, rh = rgb
            ax, ay, aw, ah = alfa
            grafo = (f"[0:v]fps={self.fps},split=2[a][b];"
                     f"[a]crop={rw}:{rh}:{rx}:{ry},setsar=1[rgb];"
                     f"[b]crop={aw}:{ah}:{ax}:{ay},scale={rw}:{rh},format=gray[al];"
                     f"[rgb][al]alphamerge,{acerto}[out]")
            meio = ["-filter_complex", grafo, "-map", "[out]"]
        else:
            meio = ["-vf", f"fps={self.fps},{acerto}"]
        return ([ffmpeg, "-hide_banner", "-loglevel", "error", "-y"] + entrada
                + meio + ["-f", "rawvideo", "-pix_fmt", "bgra", self.destino])

    def preparar(self) -> bool:
        """Decodifica tudo de uma vez. Roda fora da thread da janela."""
        try:
            r = subprocess.run(self._argumentos(), capture_output=True, timeout=180,
                               creationflags=_SEM_JANELA)
            tamanho = os.path.getsize(self.destino) if os.path.exists(self.destino) else 0
            if r.returncode or tamanho < self._por_quadro:
                self.erro = (r.stderr.decode("utf-8", "replace") or "sem quadros")[-180:]
                return False
            self.quadros = tamanho // self._por_quadro
            self._fh = open(self.destino, "rb")
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
            return True
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self.erro = str(e)
            return False

    def quadro(self, indice: int):
        if self._mm is None or not self.quadros:
            return None
        i = max(0, min(self.quadros - 1, indice))
        return self._mm[i * self._por_quadro:(i + 1) * self._por_quadro]

    def fechar(self) -> None:
        for alvo in (self._mm, self._fh):
            try:
                if alvo is not None:
                    alvo.close()
            except OSError:
                pass
        self._mm = self._fh = None
        try:
            os.remove(self.destino)
        except OSError:
            pass


# ------------------------------------------------------ linha do tempo

class LinhaDoTempo(tk.Canvas):
    """Barra de navegação do editor.

    Clicar leva direto para o ponto clicado - sem passinhos. As marcas de
    início e fim do trecho ficam na própria barra e podem ser arrastadas, e a
    roda do mouse aproxima a vista em volta do cursor, para dar precisão de
    quadro em vídeos longos. Arrastar com o botão direito marca o trecho
    inteiro de uma vez, as duas pontas no mesmo gesto. Correr a vista para o
    lado fica na rodinha pressionada, no Alt+roda e na barra de rolagem. Ao
    fundo vai a forma de onda do áudio, que é o que permite achar a hora certa
    de cortar sem ter que assistir.

    Um gesto de cada vez: quem está marcando o trecho ou arrastando a vista não
    move o playhead junto.
    """

    FUNDO = "#111827"
    ONDA = "#38bdf8"
    ONDA_FORA = "#334155"
    SELECAO = "#1e3a8a"
    REGUA = "#0b1220"
    ALTURA = 96
    REGUA_H = 18
    PEGA = 9                    # meia largura da alça, em pixels
    SNAP_PX = 12                # distância visual para prender o playhead

    def __init__(self, parent, ao_buscar, ao_marcar, ao_teclar=None,
                 snap_ativo=None, ao_rolar=None, ao_selecionar=None, **kw):
        super().__init__(parent, height=self.ALTURA, bg=self.FUNDO,
                         highlightthickness=0, bd=0, takefocus=True, **kw)
        self.ao_buscar = ao_buscar
        self.ao_marcar = ao_marcar
        self.ao_teclar = ao_teclar or (lambda _acao: None)
        self.snap_ativo = snap_ativo or (lambda: False)
        self.ao_rolar = ao_rolar or (lambda _ini, _fim: None)
        self.ao_selecionar = ao_selecionar or (lambda _ini, _fim: None)

        self.duracao = 0.0
        self.posicao = 0.0
        self.inicio = 0.0
        self.fim = 0.0
        self.vis_ini = 0.0
        self.vis_fim = 1.0
        self.envelope: array | None = None
        self.env_hz = 40.0
        # Compensação de sincronia, em segundos: desloca só o desenho da onda,
        # que passa a mostrar o áudio onde ele vai ser ouvido.
        self.atraso = 0.0

        self._arrastando = ""       # "" | "cursor" | "inicio" | "fim"
        self._panning = False
        self._pan_x = 0
        self._pan_ini = 0.0
        self._selecionando = False
        self._sel_ancora = 0.0      # onde o botão direito foi pressionado
        self._sel_x = 0
        self._fundo = None          # PhotoImage da onda + régua
        self._id_fundo = None
        self._redesenhar_id = None
        self._ultimo_fundo = 0.0
        self._niveis = None         # envelope em reduções sucessivas

        self.bind("<Configure>", self._mudou_tamanho)
        self.bind("<ButtonPress-1>", self._pressionou)
        self.bind("<B1-Motion>", self._moveu)
        self.bind("<ButtonRelease-1>", self._soltou)
        # Botão direito arrastando marca o trecho de uma vez só, as duas pontas
        # no mesmo gesto. Arrastar a vista ficou na rodinha pressionada - e
        # também no Alt+roda e na barra de rolagem, para quem não tem os dois.
        self.bind("<ButtonPress-3>", self._sel_comeco)
        self.bind("<B3-Motion>", self._sel_move)
        self.bind("<ButtonRelease-3>", self._sel_fim)
        self.bind("<ButtonPress-2>", self._pan_comeco)
        self.bind("<B2-Motion>", self._pan_move)
        self.bind("<ButtonRelease-2>", self._pan_fim)
        # Roda limpa dá zoom; com Shift ou Alt, corre a vista. Quem decide é o
        # próprio Tk, por binding: ler a máscara de modificadores na mão dava
        # errado no Windows, onde o bit 0x0008 é do Num Lock e não do Alt - com
        # ele ligado, que é o normal, a roda nunca chegava a dar zoom.
        self.bind("<MouseWheel>", lambda e: self._roda(e))
        for prefixo in ("Shift", "Alt"):
            self.bind(f"<{prefixo}-MouseWheel>", lambda e: self._roda(e, pan=True))
        for botao, passo in ((4, 120), (5, -120)):   # roda no X11
            self.bind(f"<Button-{botao}>", lambda e, d=passo: self._roda(e, d))
            for prefixo in ("Shift", "Alt"):
                self.bind(f"<{prefixo}-Button-{botao}>",
                          lambda e, d=passo: self._roda(e, d, pan=True))
        self.bind("<Motion>", self._cursor_do_mouse)
        # Com a barra em foco, o teclado dá o ajuste fino: quadro a quadro nas
        # setas, um segundo com Shift, e I/O ou [ ] marcam o trecho onde o
        # cursor está. Os atalhos que não dependem da barra (espaço, W, [ ])
        # também estão na janela toda - veja `Editor._ligar_atalhos`.
        for sequencia, acao in (("<space>", "play"),
                                ("<Left>", "quadro-"), ("<Right>", "quadro+"),
                                ("<Shift-Left>", "seg-"), ("<Shift-Right>", "seg+"),
                                ("<i>", "inicio"), ("<o>", "fim"),
                                ("<I>", "inicio"), ("<O>", "fim"),
                                ("<bracketleft>", "inicio"), ("<bracketright>", "fim"),
                                ("<w>", "rewind"), ("<W>", "rewind")):
            self.bind(sequencia, lambda _e, a=acao: self._tecla(a))

    # ------------------------------------------------------------ conversão

    def _x(self, t: float) -> float:
        janela = max(1e-6, self.vis_fim - self.vis_ini)
        return (t - self.vis_ini) / janela * max(1, self.winfo_width())

    def _t(self, x: float) -> float:
        janela = max(1e-6, self.vis_fim - self.vis_ini)
        return self.vis_ini + x / max(1, self.winfo_width()) * janela

    # --------------------------------------------------------------- estado

    def definir_video(self, duracao: float, inicio: float, fim: float) -> None:
        self.duracao = max(0.0, duracao)
        self.inicio, self.fim = inicio, fim
        self.vis_ini, self.vis_fim = 0.0, max(1.0, self.duracao)
        self.envelope = None
        self._niveis = None
        self.posicao = 0.0
        self._arrastando = ""
        self._panning = False
        self._agendar_fundo()

    def definir_onda(self, envelope: array, hz: float) -> None:
        self.envelope, self.env_hz = envelope, hz
        self._niveis = None          # a onda cresceu: refazer as reduções
        self._agendar_fundo()

    def definir_atraso(self, segundos: float) -> None:
        if abs(segundos - self.atraso) < 1e-6:
            return
        self.atraso = segundos
        self._agendar_fundo()

    def definir_marcas(self, inicio: float, fim: float) -> None:
        self.inicio, self.fim = inicio, fim
        self._agendar_fundo()

    def definir_posicao(self, t: float) -> None:
        if abs(t - self.posicao) < 1e-3:
            return
        self.posicao = t
        self._desenhar_agulhas()

    @property
    def interagindo(self) -> bool:
        """Indica se o cursor, uma marca ou o trecho está sendo arrastado."""
        return bool(self._arrastando) or self._selecionando

    def tudo_a_vista(self) -> None:
        self.vis_ini, self.vis_fim = 0.0, max(1.0, self.duracao)
        self._agendar_fundo()

    def aproximar(self, fator: float, centro: float | None = None) -> None:
        centro = self.posicao if centro is None else centro
        janela = (self.vis_fim - self.vis_ini) / fator
        # Menos de dois segundos à vista já é precisão de quadro; mais que a
        # duração inteira não faz sentido.
        janela = max(min(2.0, self.duracao or 2.0), min(janela, max(1.0, self.duracao)))
        proporcao = 0.5
        if self.vis_fim > self.vis_ini:
            proporcao = min(1.0, max(0.0, (centro - self.vis_ini)
                                     / (self.vis_fim - self.vis_ini)))
        ini = centro - janela * proporcao
        ini = max(0.0, min(ini, max(0.0, (self.duracao or janela) - janela)))
        self.vis_ini, self.vis_fim = ini, ini + janela
        self._agendar_fundo()

    # -------------------------------------------------------------- desenho

    def _mudou_tamanho(self, _evt=None) -> None:
        self._agendar_fundo()

    def _agendar_fundo(self) -> None:
        """Agrupa vários pedidos seguidos num redesenho só.

        O jeito óbvio - cancelar o redesenho pendente e marcar outro - tem um
        defeito sério com o mouse arrastando: os eventos de movimento chegam
        mais rápido que os 16 ms do agrupamento, então o cancelamento acontecia
        sempre antes do desenho, e a onda só se atualizava quando a pessoa
        parava de arrastar. Aqui o pedido pendente nunca é cancelado - ele só
        não é duplicado -, o que garante um desenho a cada 16 ms mesmo com o
        mouse em movimento contínuo.
        """
        # A rolagem fica fora do agrupamento: é um `set` e nada mais, e assim
        # ela nunca mostra uma vista que já mudou.
        self.ao_rolar(*self._fracoes())
        if self._redesenhar_id:
            return
        atraso = int(max(0.0, 16.0 - (time.monotonic() - self._ultimo_fundo) * 1000))
        self._redesenhar_id = self.after(atraso, self._desenhar_fundo)
        # As agulhas são itens do canvas e custam quase nada: desenhá-las já
        # mantém cursor e marcas colados no gesto, sem esperar o fundo.
        self._desenhar_agulhas()

    # ------------------------------------------------------------- rolagem

    def _fracoes(self) -> tuple[float, float]:
        """Onde a vista está dentro do vídeo, de 0 a 1 - o que a barra espera."""
        total = max(1e-6, self.duracao or (self.vis_fim - self.vis_ini))
        return (max(0.0, min(1.0, self.vis_ini / total)),
                max(0.0, min(1.0, self.vis_fim / total)))

    def xview(self, *args):
        """Interface de rolagem do Tk, para a barra de baixo comandar a vista.

        Existe porque nem todo mouse tem rodinha ou terceiro botão: sem ela, um
        vídeo ampliado só poderia ser percorrido com gestos que essa pessoa não
        tem. O canvas nunca usou a rolagem nativa (a vista aqui é em segundos,
        não em pixels), então trocar o método por este não atropela nada.
        """
        if not args:
            return self._fracoes()
        janela = self.vis_fim - self.vis_ini
        if args[0] == "moveto":
            ini = float(args[1]) * (self.duracao or janela)
        elif args[0] == "scroll":
            # "units" é o clique nas setinhas; "pages", o clique na calha.
            passo = janela if args[2] == "pages" else janela * 0.1
            ini = self.vis_ini + float(args[1]) * passo
        else:
            return None
        ini = max(0.0, min(ini, max(0.0, (self.duracao or janela) - janela)))
        if abs(ini - self.vis_ini) > 1e-9:
            self.vis_ini, self.vis_fim = ini, ini + janela
            self._agendar_fundo()
        return None

    def _piramide(self) -> list:
        """O envelope em reduções sucessivas, cada uma com metade das amostras.

        Sem elas, desenhar a onda com o vídeo inteiro à vista significa varrer
        todas as amostras do áudio a cada quadro do arrasto - uma hora de live
        são 144 mil delas, e é esse custo que fazia a barra engasgar. Com as
        reduções o desenho lê sempre perto de uma amostra por pixel, seja qual
        for o zoom. Cada nível guarda o maior valor do par, então o pico de um
        som curto continua aparecendo mesmo na vista mais afastada.
        """
        if self._niveis is None:
            niveis = [self.envelope]
            while len(niveis[-1]) > 4096:
                atual = niveis[-1]
                niveis.append(array("i", [max(atual[i], atual[i + 1])
                                          for i in range(0, len(atual) - 1, 2)]))
            self._niveis = niveis
        return self._niveis

    def _nivel_para(self, passo: float) -> tuple:
        """A redução em que cada pixel cobre no máximo duas amostras."""
        niveis = self._piramide()
        i, por_pixel = 0, passo * self.env_hz
        while i + 1 < len(niveis) and por_pixel >= 2:
            por_pixel /= 2
            i += 1
        return niveis[i], self.env_hz / (1 << i)

    def _desenhar_fundo(self) -> None:
        self._redesenhar_id = None
        self._ultimo_fundo = time.monotonic()
        largura, altura = self.winfo_width(), self.winfo_height()
        if largura < 10 or altura < 10:
            return
        from PIL import Image, ImageDraw, ImageTk

        onda_h = altura - self.REGUA_H
        img = Image.new("RGB", (largura, altura), self.FUNDO)
        d = ImageDraw.Draw(img)

        # trecho escolhido, para o corte ser visível de longe
        xa, xb = self._x(self.inicio), self._x(self.fim)
        if xb > xa:
            d.rectangle((max(0, xa), 0, min(largura, xb), onda_h), fill=self.SELECAO)

        if self.envelope:
            meio = onda_h / 2
            passo = (self.vis_fim - self.vis_ini) / max(1, largura)
            amostras, hz = self._nivel_para(passo)
            n = len(amostras)
            escala_y = (meio - 2) / 32768
            for x in range(largura):
                # O `- atraso` é a compensação de sincronia: a amostra gravada
                # em t aparece onde ela vai ser ouvida, t + atraso.
                t0 = self.vis_ini + x * passo - self.atraso
                i0 = int(t0 * hz)
                if i0 >= n:
                    break
                if i0 < 0:                   # antes do começo do áudio
                    continue
                i1 = min(n, max(i0 + 1, int((t0 + passo) * hz)))
                # No nível certo quase todo pixel cobre uma amostra só; evitar
                # a fatia nesse caso é o que tira a alocação do laço.
                pico = amostras[i0] if i1 - i0 <= 1 else max(amostras[i0:i1])
                h = pico * escala_y
                cor = self.ONDA if xa <= x <= xb else self.ONDA_FORA
                d.line((x, meio - h, x, meio + h), fill=cor)

        d.rectangle((0, onda_h, largura, altura), fill=self.REGUA)
        self._regua(d, largura, onda_h, altura)

        # Criar um PhotoImage novo a cada quadro custava 5 ms dos 9 do
        # redesenho - mais que desenhar a onda inteira. Enquanto o tamanho não
        # muda, dá para escrever por cima do que já está lá.
        if self._fundo is not None and (self._fundo.width(), self._fundo.height()) \
                == (largura, altura):
            self._fundo.paste(img)
        else:
            self._fundo = ImageTk.PhotoImage(img)
            if self._id_fundo is None:
                self._id_fundo = self.create_image(0, 0, anchor="nw",
                                                   image=self._fundo)
            else:
                self.itemconfigure(self._id_fundo, image=self._fundo)
        self.tag_lower(self._id_fundo)
        self._desenhar_agulhas()

    def _regua(self, d, largura: int, topo: int, altura: int) -> None:
        """Marcas de tempo com um passo redondo para o zoom atual."""
        janela = max(1e-6, self.vis_fim - self.vis_ini)
        for passo in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if janela / passo <= 12:
                break
        t = (int(self.vis_ini) // passo) * passo
        while t <= self.vis_fim:
            x = self._x(t)
            if 0 <= x <= largura:
                d.line((x, topo, x, altura), fill="#475569")
                d.text((x + 3, topo + 3), tempo(t), fill="#94a3b8")
            t += passo

    def _desenhar_agulhas(self) -> None:
        """Cursor e alças: itens do canvas, para mexer sem refazer o fundo."""
        altura = self.winfo_height()
        onda_h = altura - self.REGUA_H
        self.delete("agulha")
        for t, cor, nome in ((self.inicio, "#22c55e", "inicio"),
                             (self.fim, "#ef4444", "fim")):
            x = self._x(t)
            if -20 <= x <= self.winfo_width() + 20:
                self.create_line(x, 0, x, onda_h, fill=cor, width=2, tags="agulha")
                lado = self.PEGA
                topo = 0 if nome == "inicio" else onda_h - 14
                pontos = ((x, topo + 7), (x + lado if nome == "inicio" else x - lado, topo),
                          (x + lado if nome == "inicio" else x - lado, topo + 14))
                self.create_polygon(pontos, fill=cor, outline="", tags="agulha")
        x = self._x(self.posicao)
        self.create_line(x, 0, x, onda_h, fill="#f8fafc", width=1, tags="agulha")
        self.create_polygon((x - 6, 0), (x + 6, 0), (x, 8),
                            fill="#f8fafc", outline="", tags="agulha")

    # ---------------------------------------------------------- interação

    def _perto(self, x: float, y: float) -> str:
        """Qual setinha de marca está sob o ponteiro, se alguma.

        As linhas coloridas servem como referência; só as setas são alças.
        Assim um clique na forma de onda sempre move o playhead, mesmo perto
        das marcas de início e fim.
        """
        onda_h = self.winfo_height() - self.REGUA_H
        for t, nome in ((self.inicio, "inicio"), (self.fim, "fim")):
            na_seta = (0 <= y <= 14 if nome == "inicio"
                        else onda_h - 14 <= y <= onda_h)
            if na_seta and abs(self._x(t) - x) <= self.PEGA + 3:
                return nome
        return ""

    def _cursor_do_mouse(self, evt) -> None:
        if self._panning or self._selecionando:      # o gesto em curso manda
            return
        self.configure(cursor="sb_h_double_arrow" if self._perto(evt.x, evt.y) else "hand2")

    def _pressionou(self, evt) -> None:
        # Um gesto de cada vez: com a vista ou o trecho sendo arrastados, o
        # clique esquerdo não pode ainda por cima levar o playhead junto.
        if not self.duracao or self._panning or self._selecionando:
            return
        self.focus_set()
        alca = self._perto(evt.x, evt.y)
        if alca:
            self._arrastando = alca
            return
        # Clique na barra vai direto para o ponto clicado; é o mínimo que se
        # espera de uma barra de vídeo.
        self._arrastando = "cursor"
        self._levar(evt.x, preciso=False)

    def _moveu(self, evt) -> None:
        if self._arrastando == "cursor":
            self._levar(evt.x, preciso=False)
        elif self._arrastando in ("inicio", "fim"):
            self.ao_marcar(self._arrastando, self._limitar(self._t(evt.x)))

    def _soltou(self, evt) -> None:
        # O estado sai da frente antes da busca. Se algo falhar no meio dela, o
        # que não pode acontecer é a barra continuar se achando arrastada: é
        # `interagindo` que segura o playhead e o loop, e eles ficariam parados
        # até reabrir o vídeo.
        arrasto, self._arrastando = self._arrastando, ""
        if arrasto == "cursor":
            self._levar(evt.x, preciso=True)

    def _levar(self, x: float, preciso: bool) -> None:
        t = self._limitar(self._t(x))
        if self.snap_ativo():
            # O snap é medido em pixels, não em segundos: ele tem a mesma
            # sensação com a timeline inteira ou muito ampliada.
            perto = [(abs(self._x(marca) - x), marca)
                     for marca in (self.inicio, self.fim)]
            distancia, marca = min(perto)
            if distancia <= self.SNAP_PX:
                t = marca
        self.definir_posicao(t)
        self.ao_buscar(t, preciso)

    def _limitar(self, t: float) -> float:
        return max(0.0, min(t, self.duracao or t))

    def _tecla(self, acao: str) -> str:
        self.ao_teclar(acao)
        return "break"                   # não deixa a seta mover o foco

    # ------------------------------------------------------ seleção direta

    MIN_SEL_PX = 3              # abaixo disso o gesto ainda é um clique

    def _sel_comeco(self, evt) -> None:
        """Fixa a ponta de onde o trecho vai crescer."""
        if self._arrastando or self._panning or not self.duracao:
            return
        self._selecionando = True
        self._sel_x = evt.x
        self._sel_ancora = self._limitar(self._t(evt.x))

    def _sel_move(self, evt) -> None:
        """Vai definindo início e fim juntos, conforme o arrasto.

        As duas marcas saem daqui na mesma chamada. Mexer numa de cada vez -
        como faz o arrasto das alças - não serviria: elas se limitam uma à
        outra, e a que ficasse para trás travaria a que está sendo puxada assim
        que o arrasto cruzasse o ponto de partida.
        """
        if not self._selecionando:
            return
        # Um clique com a mão trêmula não pode apagar o trecho já marcado.
        if abs(evt.x - self._sel_x) < self.MIN_SEL_PX:
            return
        self.configure(cursor="sb_h_double_arrow")
        t = self._limitar(self._t(evt.x))
        self.ao_selecionar(min(self._sel_ancora, t), max(self._sel_ancora, t))

    def _sel_fim(self, _evt=None) -> None:
        self._selecionando = False
        self.configure(cursor="hand2")

    def _pan_comeco(self, evt) -> None:
        """Começa a arrastar a vista - rodinha pressionada."""
        if self._arrastando or self._selecionando or not self.duracao:
            return
        self._panning = True
        self._pan_x, self._pan_ini = evt.x, self.vis_ini
        self.configure(cursor="fleur")

    def _pan_move(self, evt) -> None:
        if not self._panning:
            return
        janela = self.vis_fim - self.vis_ini
        desloc = (self._pan_x - evt.x) / max(1, self.winfo_width()) * janela
        ini = max(0.0, min(self._pan_ini + desloc, max(0.0, self.duracao - janela)))
        if abs(ini - self.vis_ini) < 1e-9:           # esbarrou na ponta
            return
        self.vis_ini, self.vis_fim = ini, ini + janela
        self._agendar_fundo()

    def _pan_fim(self, _evt=None) -> None:
        self._panning = False
        self.configure(cursor="hand2")

    def _roda(self, evt, delta=None, pan=False) -> None:
        delta = evt.delta if delta is None else delta
        if not self.duracao:
            return
        if pan:
            janela = self.vis_fim - self.vis_ini
            passo = janela * 0.15 * (-1 if delta > 0 else 1)
            ini = max(0.0, min(self.vis_ini + passo, max(0.0, self.duracao - janela)))
            self.vis_ini, self.vis_fim = ini, ini + janela
            self._agendar_fundo()
        else:
            self.aproximar(1.25 if delta > 0 else 1 / 1.25, centro=self._t(evt.x))
        if self._panning:
            # Zoom com o botão de arrastar ainda pressionado: o ponto de
            # referência do arrasto virou outro, e sem reancorar aqui a vista
            # dava um salto no movimento seguinte.
            self._pan_x, self._pan_ini = evt.x, self.vis_ini


# Os gestos da barra não se anunciam sozinhos; o rótulo da onda fica livre
# assim que ela termina de carregar, e é ali que eles cabem.
DICA_NAVEGACAO = ("na barra: roda = zoom · Alt+roda ou rodinha = navegar · "
                  "botão direito arrastando = marcar o trecho")


def tempo(seg: float) -> str:
    seg = max(0, int(seg))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _extrair_onda(caminho: str, hz: float = 40.0, parcial=None):
    """Envelope do áudio: o maior valor absoluto de cada fatia de 1/hz.

    Sai do próprio ffmpeg em PCM de 1 kHz, que é resolução de sobra para a
    barra e uma fração do custo de ler o áudio inteiro.

    O PCM é lido em pedaços, e não de uma vez, para `parcial` receber o
    envelope enquanto ele cresce - assim a onda vai aparecendo da esquerda
    para a direita em vez de surgir inteira no fim. Cada chamada leva uma
    cópia: quem desenha está em outra thread e não pode ler um array que
    ainda está sendo escrito.
    """
    taxa = 1000
    args = [compositor.recorder._ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", caminho, "-vn", "-ac", "1", "-ar", str(taxa),
            "-f", "s16le", "-"]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                creationflags=_SEM_JANELA)
    except (OSError, subprocess.SubprocessError):
        return None

    janela = max(1, int(taxa / hz))
    passo = janela * 2                           # bytes de uma fatia
    envelope = array("i")
    resto = b""
    ultimo_aviso = 0.0
    try:
        with proc:
            while True:
                pedaco = proc.stdout.read(passo * 64)
                if not pedaco:
                    break
                dados = resto + pedaco
                inteiro = len(dados) // passo * passo
                resto = dados[inteiro:]
                amostras = array("h")
                amostras.frombytes(dados[:inteiro])
                for i in range(0, len(amostras), janela):
                    fatia = amostras[i:i + janela]
                    envelope.append(max(max(fatia), -min(fatia)))
                agora = time.monotonic()
                if parcial is not None and agora - ultimo_aviso > 0.3 and envelope:
                    ultimo_aviso = agora
                    parcial(envelope[:])
    except (OSError, ValueError):
        proc.kill()
        return envelope or None
    if proc.returncode not in (0, None) and not envelope:
        return None
    return envelope


# ------------------------------------------------------------------- aba

class Editor(ttk.Frame):
    """Abre um vídeo, encontra o pacote e monta a versão com as camadas."""

    # Ritmo do desenho das camadas. A 30 por segundo a animação de presente
    # acompanha o vídeo sem que se perceba a diferença.
    INTERVALO = 33
    ANIM_FPS = 30
    # Teto do volume das animações: o ajuste aqui é quase sempre para baixo,
    # então 100% cai a 80% do curso do slider em vez de no meio.
    VOLUME_MAX = 1.25
    # Quanto antes de a animação começar ela já é preparada.
    JANELA_ANIM = 3.0
    GUARDAR_ANIM = 3            # animações decodificadas mantidas em disco

    def __init__(self, parent, emit):
        super().__init__(parent, padding=12)
        self.emit = emit
        self.video = ""
        self.pacote: pacote_mod.Pacote | None = None
        self.player: Player | None = None
        self._temp_anim = ""
        self._agenda: list[compositor.Agendado] = []

        self._camada_anim = None
        self._camada_chat = None
        self._camada_contador = None
        self._animacoes: dict[int, Animacao] = {}
        self._preparando: set[int] = set()
        self._ordem_anim: list[int] = []
        self._geracao_anim = 0
        self._ultimo_quadro = (None, -1)
        self._onda_pronta = None

        self._pintor = None
        self._pintor_contador = None
        self._chat_pronto = (None, None)        # (imagem original, bytes prontos)
        self._contador_pronto = (None, None)
        self._erro_chat = ""

        self._exportando = False
        self._cancelar = False
        self._escala_anim = 0.0
        self._resize_id = None
        # Medido uma vez ao abrir: `dimensoes` roda o ffprobe e não pode
        # entrar num laço de tela.
        self._dims = (720, 1280)
        self._duracao = 0.0
        # O mpv pode informar o keyframe anterior enquanto uma busca precisa
        # ainda termina; este alvo mantém a agulha estável nesse intervalo.
        self._seek_pendente: float | None = None
        # O play que o usuário pediu, que não é o mesmo que "está tocando".
        self._quer_tocar = False
        # Envelope ainda incompleto, entregue pela thread que lê o áudio.
        self._onda_parcial = None
        self._polegar_px = 0
        # Camadas já pedidas ao fundo, ligadas ou não, para não pedir duas vezes.
        self._preparando_chat = False
        self._preparando_contador = False
        # Invalida preparações em segundo plano quando outro vídeo é aberto.
        self._carregamento_id = 0
        self._video_pronto_id = 0

        self._montar()

    # ------------------------------------------------------------- interface

    def _montar(self) -> None:
        # A prévia é a área principal; o painel segue com espaço para os
        # controles, mas não toma metade da janela.
        self.columnconfigure(0, weight=3, minsize=420)
        self.columnconfigure(1, weight=1, minsize=310)
        self.rowconfigure(1, weight=1)

        topo = ttk.Frame(self)
        topo.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        topo.columnconfigure(1, weight=1)
        ttk.Button(topo, text="Abrir vídeo...", command=self.escolher_video).grid(
            row=0, column=0)
        self.arquivo_var = tk.StringVar(value="nenhum vídeo aberto")
        ttk.Label(topo, textvariable=self.arquivo_var, foreground="#475569").grid(
            row=0, column=1, sticky="w", padx=10)
        self.pacote_var = tk.StringVar(value="")
        ttk.Label(topo, textvariable=self.pacote_var, foreground="#64748b").grid(
            row=1, column=1, sticky="w", padx=10)
        ttk.Button(topo, text="Vincular pacote...", command=self.escolher_pacote).grid(
            row=1, column=0, pady=(4, 0))

        # --- prévia -------------------------------------------------------
        # O player preenche a área disponível. A libmpv preserva a proporção
        # do vídeo e usa tarjas pretas quando necessário, sem recortar nada.
        self.moldura = tk.Frame(self, bg="#0b0b0b")
        self.moldura.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.tela = tk.Frame(self.moldura, bg="#000000")
        self.tela.place(relx=0.5, rely=0.5, anchor="center",
                        relwidth=1.0, relheight=1.0)

        # --- painel lateral ----------------------------------------------
        lado = ttk.Frame(self)
        lado.grid(row=1, column=1, sticky="nsew")
        # O minsize é o que impede a lista de presentes de encolher até
        # desaparecer quando a janela está baixa.
        lado.rowconfigure(1, weight=1, minsize=110)
        lado.columnconfigure(0, weight=1)

        camadas = ttk.LabelFrame(lado, text=" Camadas e som ", padding=8)
        camadas.grid(row=0, column=0, sticky="ew")
        self.anim_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(camadas, text="Animações dos presentes",
                        variable=self.anim_var,
                        command=self._camadas_mudaram).pack(anchor="w")
        self.chat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(camadas, text="Chat da live",
                        variable=self.chat_var,
                        command=self._chat_mudou).pack(anchor="w", pady=(2, 6))
        self.contador_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(camadas, text="Contador de presentes",
                        variable=self.contador_var,
                        command=self._contador_mudou).pack(anchor="w", pady=(0, 6))

        ttk.Label(camadas, text="Volume das animações").pack(anchor="w")
        vol = ttk.Frame(camadas)
        vol.pack(fill="x")
        self.volume_var = tk.DoubleVar(value=1.0)
        self.volume_slider = ttk.Scale(vol, from_=0.0, to=self.VOLUME_MAX,
                                       variable=self.volume_var,
                                       command=self._volume_mudou)
        self.volume_slider.pack(side="left", fill="x", expand=True)
        # O mouse passa a mandar direto, sem o vai-de-pouquinho do ttk.
        for sequencia in ("<Button-1>", "<B1-Motion>"):
            self.volume_slider.bind(sequencia, self._volume_no_clique)
        self.volume_slider.bind("<ButtonRelease-1>", lambda _e: "break")
        self.volume_txt = tk.StringVar(value="100%")
        ttk.Label(vol, textvariable=self.volume_txt, width=6).pack(side="left")

        # Sincronia do áudio da live. Fica aqui, e não num quadro só dela, para
        # não empurrar o painel para fora da janela: com 700 px de altura o que
        # está embaixo da lista já não cabe. Zera a cada vídeo aberto - um
        # atraso esquecido de outro arquivo estragaria o corte seguinte sem
        # ninguém perceber.
        sinc = ttk.Frame(camadas)
        sinc.pack(fill="x", pady=(8, 0))
        ttk.Label(sinc, text="Sincronia do áudio").pack(side="left")
        self.sinc_var = tk.StringVar(value="0")
        self.sinc_spin = ttk.Spinbox(sinc, from_=-5000, to=5000, increment=10,
                                     width=7, textvariable=self.sinc_var)
        self.sinc_spin.pack(side="left", padx=6)
        ttk.Label(sinc, text="ms").pack(side="left")
        ttk.Button(sinc, text="zerar", width=6,
                   command=lambda: self.sinc_var.set("0")).pack(side="left",
                                                                padx=(8, 0))
        ttk.Label(camadas, foreground="#64748b",
                  text="+ atrasa o áudio da live, − adianta.").pack(
                      anchor="w", pady=(2, 0))
        # O espaço é do play em qualquer canto da aba, e num campo de números
        # ele não teria uso nenhum. A ligação fica no próprio campo para vir
        # antes da do ttk, que é quem digitaria o espaço.
        self.sinc_spin.bind("<space>", self._espaco_na_sincronia)

        lista = ttk.LabelFrame(lado, text=" Presentes com animação ", padding=6)
        lista.grid(row=1, column=0, sticky="nsew", pady=8)
        lista.rowconfigure(0, weight=1)
        lista.columnconfigure(0, weight=1)
        self.lista = tk.Listbox(lista, font=("Consolas", 9), height=8,
                                activestyle="none")
        self.lista.grid(row=0, column=0, sticky="nsew")
        self.lista.bind("<<ListboxSelect>>", self._ir_para_presente)
        sb = ttk.Scrollbar(lista, command=self.lista.yview)
        self.lista.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

        trecho = ttk.LabelFrame(lado, text=" Trecho a exportar ", padding=8)
        trecho.grid(row=2, column=0, sticky="ew")
        self.inicio_var = tk.DoubleVar(value=0.0)
        self.fim_var = tk.DoubleVar(value=0.0)
        for rotulo, cmd in (("Início", self._marcar_inicio), ("Fim", self._marcar_fim)):
            linha = ttk.Frame(trecho)
            linha.pack(fill="x", pady=2)
            ttk.Label(linha, text=rotulo, width=7).pack(side="left")
            txt = tk.StringVar(value="0:00")
            setattr(self, f"txt_{'inicio' if rotulo == 'Início' else 'fim'}", txt)
            ttk.Label(linha, textvariable=txt, width=8,
                      font=("Consolas", 10)).pack(side="left")
            atalho = "[" if rotulo == "Início" else "]"
            ttk.Button(linha, text=f"marcar aqui  {atalho}", command=cmd).pack(side="left")
        self.duracao_var = tk.StringVar(value="duração do trecho: 0:00")
        ttk.Label(trecho, textvariable=self.duracao_var, foreground="#0f766e",
                  font=("Consolas", 10, "bold")).pack(anchor="w", pady=(4, 0))
        ttk.Button(trecho, text="Usar o vídeo inteiro",
                   command=self._trecho_inteiro).pack(fill="x", pady=(6, 0))
        self.snap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(trecho, text="Snap do playhead nas marcas",
                        variable=self.snap_var).pack(anchor="w", pady=(6, 0))

        # --- linha do tempo ------------------------------------------------
        faixa = ttk.Frame(self)
        faixa.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        faixa.columnconfigure(0, weight=1)
        self.linha = LinhaDoTempo(faixa, self._buscou_na_linha, self._marcou_na_linha,
                                  self._tecla_na_linha,
                                  snap_ativo=lambda: self.snap_var.get(),
                                  ao_rolar=self._vista_mudou,
                                  ao_selecionar=self._selecionou_na_linha)
        self.linha.grid(row=0, column=0, sticky="ew")
        zoom = ttk.Frame(faixa)
        zoom.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        ttk.Button(zoom, text="+", width=3,
                   command=lambda: self.linha.aproximar(2.0)).pack(pady=1)
        ttk.Button(zoom, text="−", width=3,
                   command=lambda: self.linha.aproximar(0.5)).pack(pady=1)
        ttk.Button(zoom, text="⤢", width=3,
                   command=self.linha.tudo_a_vista).pack(pady=1)
        # Rolagem para quem não tem rodinha nem terceiro botão. Fica sempre
        # visível, mesmo com o vídeo inteiro à vista: fazê-la aparecer e sumir
        # mexia na altura da faixa, e tudo daqui para baixo dava um pulo bem no
        # meio do gesto de quem estava usando a barra.
        self.rolagem = ttk.Scrollbar(faixa, orient="horizontal",
                                     command=self.linha.xview)
        self.rolagem.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.rolagem.set(0.0, 1.0)
        self.onda_var = tk.StringVar(value="")
        ttk.Label(faixa, textvariable=self.onda_var, foreground="#64748b").grid(
            row=2, column=0, sticky="w")
        ttk.Label(faixa, text="com a barra selecionada: ← → = um quadro · "
                              "Shift+← → = um segundo · I ou [ = início · "
                              "O ou ] = fim",
                  foreground="#94a3b8").grid(row=2, column=0, sticky="e")

        # --- transporte ---------------------------------------------------
        transporte = ttk.Frame(self)
        transporte.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        transporte.columnconfigure(5, weight=1)
        # Um triângulo só, e é o do play. Os saltos de 1s e 10s sairam daqui:
        # continuam no Shift+← →, que é por onde eles eram usados.
        self.btn_play = ttk.Button(transporte, text="▶  Tocar", width=10,
                                   command=self._alternar_play)
        self.btn_play.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(transporte, text="⏮  Início", width=10,
                   command=self._rewind).grid(row=0, column=1, padx=(0, 14))
        ttk.Button(transporte, text="◄ quadro", width=10,
                   command=lambda: self._passo_quadro(-1)).grid(row=0, column=2, padx=2)
        ttk.Button(transporte, text="quadro ►", width=10,
                   command=lambda: self._passo_quadro(1)).grid(row=0, column=3, padx=2)
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(transporte, text="⟲ Loop no trecho", variable=self.loop_var,
                        command=self._loop_mudou).grid(row=0, column=4, padx=(14, 2))
        # Dois relógios: o do vídeo inteiro e o do trecho. Quem está montando o
        # corte pensa em "1:04 de clipe", não na posição dentro da live.
        relogio = ttk.Frame(transporte)
        relogio.grid(row=0, column=5, padx=14)
        self.tempo_var = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(relogio, textvariable=self.tempo_var,
                  font=("Consolas", 11)).pack(side="left")
        self.tempo_trecho_var = tk.StringVar(value="trecho 0:00 / 0:00")
        ttk.Label(relogio, textvariable=self.tempo_trecho_var,
                  font=("Consolas", 11), foreground="#0f766e").pack(
                      side="left", padx=(14, 0))
        ttk.Label(transporte,
                  text="espaço = tocar · W = voltar ao início · [ ] = marcar trecho",
                  foreground="#94a3b8").grid(row=0, column=6, sticky="e")

        # --- exportação ----------------------------------------------------
        acoes = ttk.Frame(self)
        acoes.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        acoes.columnconfigure(1, weight=1)
        self.btn_exportar = tk.Button(
            acoes, text="Exportar vídeo com as camadas", command=self._exportar,
            font=("Segoe UI", 10, "bold"), bg="#15803d", fg="white",
            disabledforeground="#bbf7d0", bd=2, padx=16, pady=8, cursor="hand2")
        self.btn_exportar.grid(row=0, column=0, rowspan=2, sticky="w")

        self.progresso = ttk.Progressbar(acoes, mode="determinate", maximum=1000)
        self.progresso.grid(row=0, column=1, sticky="ew", padx=12)
        self.progresso.grid_remove()
        self.progresso_var = tk.StringVar(value="")
        ttk.Label(acoes, textvariable=self.progresso_var,
                  foreground="#334155").grid(row=1, column=1, sticky="w", padx=12)
        self.btn_cancelar = ttk.Button(acoes, text="Cancelar",
                                       command=self._cancelar_exportacao)
        self.btn_cancelar.grid(row=0, column=2, rowspan=2)
        self.btn_cancelar.grid_remove()
        self.aviso = tk.StringVar(value="")
        ttk.Label(acoes, textvariable=self.aviso, foreground="#b45309").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # O que fazer com o vídeo pronto. Fica na mesma linha do botão de
        # exportar - são poucos botões e a altura da aba é disputada. Já nasce
        # montado, com os botões apagados: se a linha só aparecesse no fim da
        # exportação, tudo acima subiria de lugar bem na hora de clicar.
        self._exportado = ""
        pronto = ttk.Frame(acoes)
        pronto.grid(row=0, column=3, rowspan=2, sticky="e", padx=(12, 0))
        self.exportado_var = tk.StringVar(value="")
        ttk.Label(pronto, textvariable=self.exportado_var,
                  foreground="#15803d").pack(side="left", padx=(0, 10))
        self.btn_abrir = tk.Button(
            pronto, text="▶  Abrir vídeo", command=self._abrir_exportado,
            font=("Segoe UI", 9), bg="#e2e8f0", fg="#0f172a", bd=1,
            padx=10, pady=3, cursor="hand2", state="disabled")
        self.btn_abrir.pack(side="left")
        self.btn_pasta = tk.Button(
            pronto, text="Ver na pasta", command=self._ver_exportado_na_pasta,
            font=("Segoe UI", 9), bg="#e2e8f0", fg="#0f172a", bd=1,
            padx=10, pady=3, cursor="hand2", state="disabled")
        self.btn_pasta.pack(side="left", padx=8)

        self.bind("<Configure>", self._janela_mudou, add="+")
        self._blindar_foco()
        self._ligar_atalhos()
        # Só agora: o campo da sincronia nasce antes da barra do tempo, e o
        # primeiro disparo do trace já procura por ela.
        self.sinc_var.trace_add("write", lambda *_a: self._sincronia_mudou())
        self._sincronia_mudou()
        self.after(200, self._tique)
        self.after(self.INTERVALO, self._tique_camadas)

    # ------------------------------------------------------------ atalhos

    def _blindar_foco(self, raiz=None) -> None:
        """Tira botões e caixas de marcar da roda do foco.

        A queixa era concreta: com o foco numa caixa recém-marcada, o espaço
        desmarcava a caixa em vez de tocar. Resolver na origem é melhor do que
        interceptar depois - a ligação de classe do ttk, que é quem consome o
        espaço, nem chega a ser consultada se o widget não puder receber foco.
        O slider de volume entra pelo mesmo motivo, mas por causa das setas:
        elas agora são do playhead, e não do volume. A lista de presentes e a
        barra do tempo seguem focáveis: nelas o teclado faz falta.
        """
        for filho in (raiz or self).winfo_children():
            if isinstance(filho, (ttk.Button, ttk.Checkbutton, ttk.Scale,
                                  tk.Button, tk.Checkbutton, tk.Scale)):
                try:
                    filho.configure(takefocus=False)
                except tk.TclError:
                    pass
            self._blindar_foco(filho)

    def assumir_foco(self) -> None:
        """Foco na barra do tempo, que é quem sabe o que fazer com o teclado.

        Existe por causa do Notebook: clicar numa aba deixa o foco nele, e a
        ligação de classe dele manda as setas trocarem de aba - antes de
        qualquer atalho nosso. Com o foco na barra, ela consome as setas e
        devolve "break", e a aba fica onde está.
        """
        try:
            self.linha.focus_set()
        except tk.TclError:
            pass

    def _ligar_atalhos(self) -> None:
        """Atalhos que valem em qualquer canto da aba, e não só na barra.

        Ficam na janela, que é o último elo da cadeia de binds de todo widget
        descendente. Quem tratar a tecla antes e devolver "break" - a própria
        barra do tempo faz isso - impede a repetição aqui.
        """
        topo = self.winfo_toplevel()
        for sequencia, acao in (("<space>", "play"),
                                ("<w>", "rewind"), ("<W>", "rewind"),
                                ("<bracketleft>", "inicio"),
                                ("<bracketright>", "fim"),
                                ("<Left>", "quadro-"), ("<Right>", "quadro+"),
                                ("<Shift-Left>", "seg-"), ("<Shift-Right>", "seg+")):
            topo.bind(sequencia, lambda e, a=acao: self._atalho_da_aba(a, e), add="+")

    def _atalho_da_aba(self, acao: str, evt):
        # A aba do gravador divide a mesma janela e tem campos de texto: um
        # espaço digitado no @ do usuário não pode virar play.
        if not self.winfo_ismapped():
            return None
        if isinstance(getattr(evt, "widget", None), (tk.Entry, tk.Text)):
            return None
        self._tecla_na_linha(acao)
        return "break"

    # ------------------------------------------------------------- abrir

    def escolher_video(self) -> None:
        caminho = filedialog.askopenfilename(
            title="Escolha o vídeo da live",
            initialdir=_pasta_lembrada("pasta_abrir", os.path.dirname(self.video)),
            filetypes=[("Vídeos", "*.mp4 *.mkv *.ts *.mov"), ("Todos", "*.*")])
        if caminho:
            _lembrar_pasta("pasta_abrir", caminho)
            self.abrir(caminho)

    def escolher_pacote(self) -> None:
        if not self.video:
            messagebox.showinfo("Abra o vídeo antes",
                                "Escolha primeiro o vídeo da live.")
            return
        caminho = filedialog.askopenfilename(
            title="Escolha o pacote de presentes",
            # O pacote nasce ao lado do vídeo, então começa por lá - e não onde
            # o último diálogo parou.
            initialdir=os.path.dirname(self.video),
            filetypes=[("Pacote de presentes", f"*{pacote_mod.EXTENSAO}"),
                       ("Todos", "*.*")])
        if caminho:
            self._carregar_pacote(caminho)

    def abrir(self, caminho: str) -> None:
        """Abre o vídeo e procura o pacote correspondente.

        Envolvido num `try` de propósito: uma falha aqui no meio deixava a aba
        com o nome do arquivo e nada mais - sem pacote, sem duração, sem prévia
        -, e a pessoa não tinha como saber que algo deu errado.
        """
        try:
            self._abrir(caminho)
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Não consegui abrir o vídeo: {e}")
            self.aviso.set(f"Não consegui abrir este vídeo: {e}")
            messagebox.showerror("Não consegui abrir",
                                 f"Algo falhou ao abrir o vídeo:\n\n{e}")

    def _abrir(self, caminho: str) -> None:
        if not os.path.exists(caminho):
            messagebox.showerror("Não encontrei", f"O arquivo sumiu:\n{caminho}")
            return
        self.video = caminho
        self._seek_pendente = None
        self._carregamento_id += 1
        self._video_pronto_id += 1
        pronto_id = self._video_pronto_id
        self.arquivo_var.set(os.path.basename(caminho))
        # A defasagem é deste arquivo, não da pessoa: sai do caminho antes de o
        # player nascer, senão o vídeo novo herdaria o acerto do anterior.
        self.sinc_var.set("0")

        # A área de vídeo é acertada antes de criar o player: assim a libmpv
        # já nasce com a superfície do tamanho certo.
        self._dims = compositor.dimensoes(caminho)
        self._duracao = compositor.duracao(caminho)
        self._ajustar_tela()
        self.update_idletasks()

        # O mpv pode manter um decoder da abertura anterior mesmo após
        # loadfile. Recriar o player embutido por vídeo é barato e evita que
        # um estado antigo deixe a superfície preta permanentemente.
        self._descartar_camadas()
        if self.player is not None:
            self.player.encerrar()
        self.player = Player(self.tela)
        self._quer_tocar = False
        if not self.player.disponivel:
            self.aviso.set(f"Sem prévia: {self.player.erro}")
        else:
            self.player.tocar(False)
            self.player.atraso_audio(self._atraso_audio())
            self.player.carregar(caminho)
            # Dá tempo para o mpv criar o decoder antes de qualquer seek.
            self.after(400, lambda: self._fixar_primeiro_quadro(pronto_id))

        self._pintor = None
        self._pintor_contador = None
        self._escala_anim = 0.0

        self.fim_var.set(self._duracao)
        self.inicio_var.set(0.0)
        self._atualizar_trecho()
        self.linha.definir_video(self._duracao, 0.0, self._duracao)
        self._onda_parcial = None
        self.onda_var.set("Lendo a forma de onda do áudio...")
        threading.Thread(target=self._preparar_onda,
                         args=(caminho,), daemon=True).start()

        achado = pacote_mod.procurar_para(caminho)
        if achado:
            self._carregar_pacote(achado)
        else:
            self.pacote = None
            self._agenda = []
            self.pacote_var.set("nenhum pacote encontrado — vincule manualmente")
            self.lista.delete(0, "end")

    def _fixar_primeiro_quadro(self, pronto_id: int) -> None:
        """Força um quadro estável antes de montar as camadas do editor."""
        if pronto_id != self._video_pronto_id or not self.player:
            return
        self.player.tocar(False)
        self.player.buscar(0.0, preciso=True)

    def _preparar_onda(self, caminho: str) -> None:
        """A forma de onda é do vídeo, não do pacote: quem manda é o caminho.

        Os pedaços chegam aqui em thread de fundo e ficam numa caixa só - o
        `_tique` pega o último a cada 200 ms. Guardar em vez de enfileirar é
        de propósito: se o desenho ficar para trás, o que interessa é o
        envelope mais recente, não todos os intermediários.
        """
        def parcial(envelope) -> None:
            if caminho == self.video:
                self._onda_parcial = (envelope, caminho)

        envelope = _extrair_onda(caminho, parcial=parcial)
        if caminho == self.video:
            self._onda_parcial = None
            self._onda_pronta = (envelope, caminho)

    def _carregar_pacote(self, caminho: str) -> None:
        try:
            self.pacote = pacote_mod.abrir(caminho)
        except pacote_mod.PacoteError as e:
            messagebox.showerror("Pacote inválido", str(e))
            return
        # Também vale quando a pessoa vincula outro pacote manualmente ao
        # mesmo vídeo: não reaproveitar painéis, avatares ou animações antigas.
        self._carregamento_id += 1
        self._descartar_camadas()
        self._pintor = None
        self._pintor_contador = None
        self._preparando_chat = False
        self._preparando_contador = False
        n_anim = len(self.pacote.com_animacao)
        self.pacote_var.set(
            f"{os.path.basename(caminho)} — {n_anim} animações, "
            f"{len(self.pacote.comentarios)} comentários")
        self._preparar_animacoes()
        self._preencher_lista()
        self._adiantar_camadas()

    def _preparar_animacoes(self) -> None:
        """Extrai as animações do pacote e calcula quando cada uma aparece."""
        if not self.pacote:
            return
        import tempfile
        self._temp_anim = self._temp_anim or tempfile.mkdtemp(prefix="editor_")
        self._agenda = compositor.agendar(self.pacote, self._temp_anim,
                                          self._dims[0], self._dims[1],
                                          0.0, self._duracao)
        self.emit("log", f"Editor: {len(self._agenda)} animações posicionadas.")

    def _preencher_lista(self) -> None:
        self.lista.delete(0, "end")
        for item in self._agenda:
            p = item.presente
            self.lista.insert(
                "end", f"{tempo(item.inicio):>7}  {p.nome[:22]:<22} "
                       f"{p.diamantes:>6}")
        if not self._agenda:
            self.lista.insert("end", "  nenhum presente com animação")

    # ------------------------------------------------------------ camadas

    def _descartar_camadas(self) -> None:
        """Some com tudo que está por cima e joga fora o que foi decodificado.

        As camadas voltam a ser criadas sozinhas no próximo desenho; soltá-las
        aqui é o que impede uma sobreposição de continuar apontando para um
        player que já foi encerrado.
        """
        for camada in (self._camada_anim, self._camada_chat, self._camada_contador):
            if camada is not None:
                camada.encerrar()
        self._camada_anim = self._camada_chat = self._camada_contador = None
        for anim in self._animacoes.values():
            anim.fechar()
        self._animacoes.clear()
        self._ordem_anim.clear()
        self._preparando.clear()
        self._geracao_anim += 1
        self._ultimo_quadro = (None, -1)
        self._chat_pronto = (None, None)
        self._contador_pronto = (None, None)

    def _area_do_video(self) -> tuple[float, float, float]:
        """Onde o vídeo aparece na janela: (desloc. x, desloc. y, escala).

        A sobreposição da libmpv é posicionada em pixels da janela, e o vídeo
        entra nela reduzido e com tarjas. Sem converter, o painel do chat -
        que é medido no quadro do vídeo - cai fora da tela.
        """
        largura_v, altura_v = self._dims
        largura_j, altura_j = self.player.tamanho_osd
        if largura_j <= 0 or altura_j <= 0:          # antes do primeiro quadro
            largura_j = self.tela.winfo_width()
            altura_j = self.tela.winfo_height()
        if largura_v <= 0 or altura_v <= 0 or largura_j <= 0 or altura_j <= 0:
            return 0.0, 0.0, 0.0
        escala = min(largura_j / largura_v, altura_j / altura_v)
        return ((largura_j - largura_v * escala) / 2,
                (altura_j - altura_v * escala) / 2, escala)

    def _janela_mudou(self, _evt=None) -> None:
        """Redimensionar muda o tamanho em que as animações têm de estar."""
        self._ajustar_tela()
        if self._resize_id:
            self.after_cancel(self._resize_id)
        self._resize_id = self.after(400, self._conferir_escala)

    def _ajustar_tela(self) -> None:
        """Mantém o player do tamanho total da área de prévia.

        A libmpv encaixa a imagem dentro dela preservando a proporção, então
        a prévia acompanha o redimensionamento da janela sem crop.
        """
        self.tela.place_configure(relwidth=1.0, relheight=1.0)

    def _conferir_escala(self) -> None:
        self._resize_id = None
        if not (self.player and self.player.disponivel):
            return
        _dx, _dy, escala = self._area_do_video()
        if escala <= 0 or not self._escala_anim:
            return
        if abs(escala - self._escala_anim) / self._escala_anim > 0.02:
            # Os quadros guardados são de outro tamanho: não servem mais. A
            # geração nova também descarta o que ainda estiver decodificando.
            for anim in self._animacoes.values():
                anim.fechar()
            self._animacoes.clear()
            self._ordem_anim.clear()
            self._preparando.clear()
            self._geracao_anim += 1
            self._ultimo_quadro = (None, -1)
            self._chat_pronto = (None, None)
            self._contador_pronto = (None, None)
            self._escala_anim = escala

    def _camadas_mudaram(self) -> None:
        if not self.anim_var.get() and self._camada_anim is not None:
            self._camada_anim.esconder()

    def _volume_mudou(self, _=None) -> None:
        self.volume_txt.set(f"{int(self.volume_var.get() * 100)}%")

    # -------------------------------------------------- sincronia do áudio

    def _atraso_audio(self) -> float:
        """A compensação em segundos. Campo pela metade ou inválido vale zero.

        Quem digita passa por "-", "-1"... e não faz sentido reclamar de cada
        estado intermediário: o valor só vira ajuste quando dá para ler.
        """
        try:
            ms = float(str(self.sinc_var.get()).replace(",", ".").strip() or 0)
        except ValueError:
            return 0.0
        return max(-5.0, min(5.0, ms / 1000.0))

    def _sincronia_mudou(self) -> None:
        atraso = self._atraso_audio()
        if self.player:
            self.player.atraso_audio(atraso)
        self.linha.definir_atraso(atraso)

    def _espaco_na_sincronia(self, _evt=None) -> str:
        """Espaço no campo da sincronia toca o vídeo em vez de digitar.

        O foco volta para a barra do tempo junto: quem acabou de acertar a
        sincronia quer ouvir o resultado e seguir navegando pelo teclado, e
        deixar o cursor piscando no campo faria a próxima tecla ir para lá.
        """
        self.assumir_foco()
        self._tecla_na_linha("play")
        return "break"

    def _largura_do_polegar(self, escala) -> int:
        """Largura do polegar do slider, medida uma vez no tema em uso.

        Ela entra na conta porque o cursor mira o CENTRO do polegar: o curso
        útil é a largura menos ele, e sem descontar isso as pontas do slider
        ficariam inalcançáveis. Medir sai mais barato do que chutar - basta
        varrer o `identify`, que diz qual elemento está sob cada x.
        """
        if self._polegar_px:
            return self._polegar_px
        meio = max(1, escala.winfo_height() // 2)
        largura = escala.winfo_width()
        achados = [x for x in range(largura)
                   if "slider" in (escala.identify(x, meio) or "")]
        self._polegar_px = len(achados) or 12
        return self._polegar_px

    def _volume_no_clique(self, evt) -> str:
        """Leva o volume para onde o cursor está, no clique e no arrasto.

        O padrão do ttk é caminhar de um degrau por vez enquanto o botão fica
        preso - bom de teclado, ruim de mouse. Como o ttk decide isso na
        ligação de classe, que roda depois desta, não adianta só ajustar o
        valor: é preciso devolver "break" e tocar o arrasto por conta própria.
        """
        escala = evt.widget
        largura = escala.winfo_width()
        if largura <= 1:
            return "break"
        polegar = self._largura_do_polegar(escala)
        util = max(1.0, largura - 2 - polegar)       # 1 px de borda de cada lado
        fracao = (evt.x - 1 - polegar / 2) / util
        self.volume_var.set(round(min(1.0, max(0.0, fracao)) * self.VOLUME_MAX, 3))
        self._volume_mudou()
        return "break"

    # ----------------------------------------------------- animação na tela

    def _animacao_no_instante(self, pos: float):
        for item in self._agenda:
            if item.inicio - self.JANELA_ANIM <= pos <= item.fim:
                return item
        return None

    def _pedir_animacao(self, item, escala: float) -> None:
        chave = id(item)
        if chave in self._animacoes or chave in self._preparando or not self._temp_anim:
            return
        self._preparando.add(chave)
        anim = Animacao(item,
                        max(2, round(self._dims[0] * escala)),
                        max(2, round(self._dims[1] * escala)),
                        self.ANIM_FPS,
                        os.path.join(self._temp_anim, f"anim_{chave}.raw"))
        threading.Thread(target=self._preparar_animacao,
                         args=(anim, chave, self._geracao_anim),
                         daemon=True).start()

    def _preparar_animacao(self, anim: Animacao, chave: int, geracao: int) -> None:
        ok = anim.preparar()
        if geracao != self._geracao_anim:
            anim.fechar()               # o tamanho ou o vídeo mudaram no meio
            return
        if not ok:
            self.emit("log", f"Prévia da animação não pôde ser preparada: {anim.erro}")
            anim.fechar()
        else:
            self._animacoes[chave] = anim
            self._ordem_anim.append(chave)
            # Só o que está à vista precisa ficar em disco.
            while len(self._ordem_anim) > self.GUARDAR_ANIM:
                velha = self._ordem_anim.pop(0)
                antiga = self._animacoes.pop(velha, None)
                if antiga is not None:
                    antiga.fechar()
        self._preparando.discard(chave)

    def _desenhar_animacao(self, pos: float, dx: float, dy: float, escala: float) -> None:
        if not self._agenda:
            if self._camada_anim is not None:
                self._camada_anim.esconder()
            return
        item = self._animacao_no_instante(pos)
        if item is None:
            if self._camada_anim is not None:
                self._camada_anim.esconder()
            self._ultimo_quadro = (None, -1)
            return
        anim = self._animacoes.get(id(item))
        if anim is None:
            # Decodifica mesmo com a caixa desligada: assim marcar a caixa no
            # meio de uma animação já mostra o quadro, sem a espera do ffmpeg.
            self._pedir_animacao(item, escala)
            return
        if not self.anim_var.get():                  # pronta, mas fora de cena
            if self._camada_anim is not None:
                self._camada_anim.esconder()
            return
        if pos < item.inicio:
            return
        indice = int((pos - item.inicio) * self.ANIM_FPS)
        # Mesmo quadro e camada ainda na tela: não há nada a fazer. Conferir se
        # ela está visível é o que faz a animação voltar sozinha depois de uma
        # exportação, que esconde tudo enquanto roda.
        if ((anim, indice) == self._ultimo_quadro and self._camada_anim is not None
                and self._camada_anim.visivel):
            return
        dados = anim.quadro(indice)
        if dados is None:
            return
        if self._camada_anim is None:
            self._camada_anim = self.player.criar_camada()
            if self._camada_anim is None:
                return
        self._camada_anim.desenhar(dados, anim.largura, anim.altura,
                                   round(dx), round(dy))
        self._ultimo_quadro = (anim, indice)

    # ------------------------------------------------------ chat na prévia

    def _adiantar_camadas(self) -> None:
        """Prepara chat e contador assim que o pacote entra, ligados ou não.

        Quem marca a caixa quer ver a camada agora, não daqui a alguns
        segundos - e o custo (baixar avatares, montar os painéis) é o mesmo
        pago mais tarde. Em silêncio: sem aviso na tela e sem desmarcar nada
        se falhar. Se falhar mesmo, `_pintor` fica em None e marcar a caixa
        tenta de novo pelo caminho normal, aí sim com mensagem.
        """
        if self.pacote and self.pacote.comentarios:
            self._pedir_chat(silencioso=True)
        if self.pacote:
            self._pedir_contador(silencioso=True)

    def _pedir_chat(self, silencioso: bool = False) -> None:
        if self._pintor is not None or self._preparando_chat:
            return
        self._preparando_chat = True
        # Baixar os avatares uma vez pode demorar: fora da thread da janela.
        threading.Thread(target=self._preparar_chat,
                         args=(self.pacote, self._dims, self._carregamento_id,
                               silencioso),
                         daemon=True).start()

    def _pedir_contador(self, silencioso: bool = False) -> None:
        if self._pintor_contador is not None or self._preparando_contador:
            return
        self._preparando_contador = True
        threading.Thread(target=self._preparar_contador,
                         args=(self.pacote, self._dims, self._carregamento_id,
                               silencioso),
                         daemon=True).start()

    def _chat_mudou(self) -> None:
        """Liga ou desliga a camada de chat sobre a prévia."""
        if not self.chat_var.get():
            if self._camada_chat is not None:
                self._camada_chat.esconder()
            return
        if not self.pacote or not self.pacote.comentarios:
            self.aviso.set("O pacote não tem chat gravado.")
            self.chat_var.set(False)
            return
        if self._pintor is None:
            self._erro_chat = ""
            self.aviso.set("Preparando o chat...")
            self._pedir_chat()

    def _contador_mudou(self) -> None:
        """Liga ou desliga o contador de presentes também na prévia."""
        if not self.contador_var.get():
            if self._camada_contador is not None:
                self._camada_contador.esconder()
            return
        if not self.pacote:
            self.aviso.set("Abra o vídeo e o pacote antes de ligar o contador.")
            self.contador_var.set(False)
            return
        if self._pintor_contador is None:
            self.aviso.set("Preparando o contador...")
            self._pedir_contador()

    def _preparar_chat(self, pacote, dims, carregamento_id: int,
                       silencioso: bool = False) -> None:
        """Roda fora da thread da janela (baixa avatares), então NÃO toca em
        widget nenhum: só publica o pintor e deixa o tique avisar."""
        try:
            pintor = compositor.PintorDeChat(pacote, dims[0], dims[1], self.emit)
            if carregamento_id == self._carregamento_id:
                self._pintor = pintor
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Não consegui preparar o chat: {e}")
            if not silencioso:
                self._erro_chat = f"Não consegui preparar o chat: {e}"
        finally:
            # Thread de um carregamento antigo não libera a vez do atual.
            if carregamento_id == self._carregamento_id:
                self._preparando_chat = False

    def _preparar_contador(self, pacote, dims, carregamento_id: int,
                           silencioso: bool = False) -> None:
        """Baixa as fotos sem bloquear a janela antes da primeira prévia."""
        try:
            pintor = compositor.PintorDeContador(pacote, dims[0], dims[1], self.emit)
            if carregamento_id == self._carregamento_id:
                self._pintor_contador = pintor
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Não consegui preparar o contador: {e}")
            if not silencioso:
                self._erro_chat = f"Não consegui preparar o contador: {e}"
        finally:
            if carregamento_id == self._carregamento_id:
                self._preparando_contador = False

    @staticmethod
    def _para_bgra(painel, escala: float):
        """Converte um painel desenhado em BGRA pronto para a libmpv.

        Multiplicar antes de reduzir, nessa ordem: é o formato que a libmpv
        espera, e reduzir com o alfa solto mancharia as bordas com a cor dos
        pixels transparentes.
        """
        from PIL import Image, ImageChops

        r, g, b, a = painel.split()
        pronto = Image.merge("RGBA", (ImageChops.multiply(r, a),
                                      ImageChops.multiply(g, a),
                                      ImageChops.multiply(b, a), a))
        largura = max(1, round(painel.width * escala))
        altura = max(1, round(painel.height * escala))
        if (largura, altura) != pronto.size:
            # BOX é média de área: não ultrapassa os valores originais. LANCZOS
            # ultrapassaria, e cor acima do alfa acende em ponto branco.
            pronto = pronto.resize((largura, altura), Image.BOX)
        return pronto.tobytes("raw", "BGRA"), largura, altura

    def _desenhar_painel(self, pintor, camada_atual, guardado, pos: float,
                         dx: float, dy: float, escala: float):
        """Desenha chat ou contador, reaproveitando o que não mudou."""
        pronto = pintor.painel(pos)
        if pronto is None:
            if camada_atual is not None:
                camada_atual.esconder()
            return camada_atual, (None, None)
        painel, px, py = pronto
        # O pintor devolve o mesmo objeto enquanto nada muda; comparar por
        # identidade evita refazer a conversão dezenas de vezes por segundo.
        if guardado[0] is painel:
            dados, largura, altura = guardado[1]
        else:
            dados, largura, altura = self._para_bgra(painel, escala)
            guardado = (painel, (dados, largura, altura))
        if camada_atual is None:
            camada_atual = self.player.criar_camada()
            if camada_atual is None:
                return None, guardado
        camada_atual.desenhar(dados, largura, altura,
                              round(dx + px * escala), round(dy + py * escala))
        return camada_atual, guardado

    # ---------------------------------------------------------- navegação

    def _ir_para_presente(self, _evt=None) -> None:
        sel = self.lista.curselection()
        if not sel or sel[0] >= len(self._agenda):
            return
        alvo = max(0.0, self._agenda[sel[0]].inicio - 1.5)
        if self.player:
            self.player.buscar(alvo)
        self.linha.definir_posicao(alvo)

    def _alternar_play(self) -> None:
        if self.player:
            # `tocando` sozinho nao serve ao loop: no fim do arquivo a libmpv
            # pausa por conta propria, e isso e indistinguivel de uma pausa
            # pedida. `_quer_tocar` guarda o que o usuario mandou fazer.
            self._quer_tocar = not self.player.tocando
            self.player.tocar(self._quer_tocar)

    def _trecho_do_loop(self) -> tuple[float, float] | None:
        """As marcas, se elas formarem um trecho que dá para repetir."""
        inicio, fim = self.inicio_var.get(), self.fim_var.get()
        if fim - inicio < 0.2:                       # marcas ainda no lugar ou coladas
            return None
        return inicio, fim

    def _loop_mudou(self) -> None:
        """Ligar o loop com a agulha fora do trecho joga ela para o início:
        quem marca o loop quer ver o trecho, não esperar chegar nele."""
        trecho = self._trecho_do_loop()
        if not (self.loop_var.get() and trecho and self.player and self.player.disponivel):
            return
        inicio, fim = trecho
        if not (inicio <= self.player.posicao < fim):
            self._ir_para(inicio)

    def _ir_para(self, t: float) -> None:
        self._seek_pendente = t
        self.player.buscar(t, preciso=True)
        self.linha.definir_posicao(t)

    def _rewind(self) -> None:
        """Volta para a marca de início. Quem estava tocando continua tocando -
        inclusive se o vídeo tinha chegado ao fim, onde a libmpv pausa sozinha."""
        if not (self.player and self.player.disponivel):
            return
        parado_no_fim = self.player.fim_do_arquivo
        self._ir_para(self.inicio_var.get())
        if parado_no_fim and self._quer_tocar:
            self.player.tocar(True)

    def _pular(self, segundos: float) -> None:
        if self.player and self.player.disponivel:
            self.player.buscar(max(0.0, self.player.posicao + segundos))

    def _passo_quadro(self, direcao: int) -> None:
        """Um quadro para frente ou para trás - a precisão que o corte pede."""
        if self.player and self.player.disponivel:
            self.player.passo_quadro(direcao)

    def _vista_mudou(self, ini: float, fim: float) -> None:
        """A vista da barra andou ou mudou de zoom: acerta a rolagem."""
        if not hasattr(self, "rolagem"):             # ainda montando a aba
            return
        try:
            self.rolagem.set(ini, fim)
        except tk.TclError:
            pass

    def _buscou_na_linha(self, t: float, preciso: bool) -> None:
        if self.player and self.player.disponivel:
            if preciso:
                self._seek_pendente = t
            self.player.buscar(t, preciso=preciso)

    def _tecla_na_linha(self, acao: str) -> None:
        """Atalhos com a barra em foco. Só funcionam se houver vídeo aberto."""
        if not (self.player and self.player.disponivel):
            return
        if acao == "play":
            self._alternar_play()
        elif acao == "rewind":
            self._rewind()
        elif acao == "quadro-":
            self._passo_quadro(-1)
        elif acao == "quadro+":
            self._passo_quadro(1)
        elif acao == "seg-":
            self._pular(-1)
        elif acao == "seg+":
            self._pular(1)
        elif acao in ("inicio", "fim"):
            self._marcou_na_linha(acao, self.player.posicao)

    def _marcou_na_linha(self, qual: str, t: float) -> None:
        if qual == "inicio":
            self.inicio_var.set(min(t, max(0.0, self.fim_var.get() - 0.1)))
        else:
            self.fim_var.set(max(t, self.inicio_var.get() + 0.1))
        self._atualizar_trecho()

    def _selecionou_na_linha(self, inicio: float, fim: float) -> None:
        """Trecho inteiro marcado num gesto só, pelo arrasto do botão direito."""
        self.inicio_var.set(inicio)
        self.fim_var.set(max(fim, inicio + 0.1))
        self._atualizar_trecho()

    def _marcar_inicio(self) -> None:
        self._marcou_na_linha("inicio", self.player.posicao if self.player else 0.0)

    def _marcar_fim(self) -> None:
        self._marcou_na_linha("fim", self.player.posicao if self.player else 0.0)

    def _trecho_inteiro(self) -> None:
        self.inicio_var.set(0.0)
        self.fim_var.set(self._duracao)
        self._atualizar_trecho()

    def _rotulo_trecho(self, pos: float) -> str:
        """Minutagem contada de dentro do trecho, e a duração só dele.

        Fora das marcas o rótulo mostra a distância até entrar ou desde que
        saiu, com sinal: quem está procurando o ponto de corte precisa saber
        de que lado da marca está, e não ver o contador preso em 0:00.
        """
        inicio, fim = self.inicio_var.get(), self.fim_var.get()
        total = tempo(max(0.0, fim - inicio))
        if pos < inicio:
            return f"trecho −{tempo(inicio - pos)} / {total}"
        if pos > fim:
            return f"trecho +{tempo(pos - fim)} / {total}"
        return f"trecho {tempo(pos - inicio)} / {total}"

    def _atualizar_trecho(self) -> None:
        inicio, fim = self.inicio_var.get(), self.fim_var.get()
        self.txt_inicio.set(tempo(inicio))
        self.txt_fim.set(tempo(fim))
        self.duracao_var.set(f"duração do trecho: {tempo(max(0.0, fim - inicio))}")
        self.linha.definir_marcas(inicio, fim)
        # Arrastar uma marca muda o relógio do trecho na hora, mesmo com o
        # vídeo parado - o tique rápido só corre quando há prévia andando.
        pos = self.player.posicao if (self.player and self.player.disponivel) else inicio
        self.tempo_trecho_var.set(self._rotulo_trecho(pos))

    # ------------------------------------------------------------- tiques

    def _tique(self) -> None:
        """Ritmo lento: rótulos, avisos e o que chegou das threads de fundo."""
        try:
            onda = self._onda_pronta
            if onda is not None:
                self._onda_pronta = None
                envelope, caminho = onda
                if caminho == self.video:
                    if envelope:
                        self.linha.definir_onda(envelope, 40.0)
                        self.onda_var.set(DICA_NAVEGACAO)
                    else:
                        self.onda_var.set("Este vídeo não tem áudio para mostrar.")
            elif self._onda_parcial is not None:
                envelope, caminho = self._onda_parcial
                self._onda_parcial = None
                if caminho == self.video:
                    self.linha.definir_onda(envelope, 40.0)
            if self._erro_chat:
                self.aviso.set(self._erro_chat)
                self._erro_chat = ""
                self.chat_var.set(False)
                self.contador_var.set(False)
            elif self.aviso.get().startswith("Preparando") and (
                    (self._pintor is not None or not self.chat_var.get())
                    and (self._pintor_contador is not None or not self.contador_var.get())):
                self.aviso.set("")
            if self.player and self.player.disponivel and self.video:
                self.btn_play.configure(
                    text="❚❚  Pausar" if self.player.tocando else "▶  Tocar")
        except tk.TclError:
            return
        self.after(200, self._tique)

    def _tique_camadas(self) -> None:
        """Ritmo rápido: é ele que faz a prévia parecer vídeo, e não slideshow."""
        try:
            self._passo_camadas()
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Prévia: {e}")
        self.after(self.INTERVALO, self._tique_camadas)

    def _passo_camadas(self) -> None:
        if not (self.player and self.player.disponivel and self.video):
            return
        # Exportando, a prévia sai da frente: o trabalho pesado é o outro.
        if self._exportando or not self.winfo_ismapped():
            return
        pos = self.player.posicao
        rotulo = f"{tempo(pos)} / {tempo(self._duracao)}"
        if rotulo != self.tempo_var.get():
            self.tempo_var.set(rotulo)
        no_trecho = self._rotulo_trecho(pos)
        if no_trecho != self.tempo_trecho_var.get():
            self.tempo_trecho_var.set(no_trecho)

        # Loop: só a borda de saída volta a agulha. Quem está antes do início
        # chega lá tocando, e não custa nada deixar ver a entrada do trecho.
        # O `_seek_pendente` segura o gatilho enquanto a busca não assenta -
        # sem isso a posição antiga dispararia o salto de novo a cada tique.
        no_fim = self.player.fim_do_arquivo
        if (self.loop_var.get() and self._quer_tocar
                and (self.player.tocando or no_fim)
                and self._seek_pendente is None and not self.linha.interagindo):
            trecho = self._trecho_do_loop()
            # No EOF o `time_pos` pode parar alguns quadros antes da duração
            # anunciada, entao ele nao serve de comparacao; chegar ao fim do
            # arquivo ja e passar de qualquer marca.
            if trecho and (no_fim or pos >= trecho[1]):
                self._ir_para(trecho[0])
                if no_fim:                           # o EOF pausou; retoma
                    self.player.tocar(True)
                return

        if self._seek_pendente is not None:
            # A busca exata pode passar por um keyframe antes de chegar ao
            # quadro pedido. Não deixe isso fazer a agulha saltar para trás.
            if abs(pos - self._seek_pendente) <= 0.12:
                self._seek_pendente = None
            else:
                self.linha.definir_posicao(self._seek_pendente)
        elif not self.linha.interagindo:
            self.linha.definir_posicao(pos)

        dx, dy, escala = self._area_do_video()
        if escala <= 0:
            return
        if not self._escala_anim:
            self._escala_anim = escala

        self._desenhar_animacao(pos, dx, dy, self._escala_anim)
        if self.chat_var.get() and self._pintor is not None:
            self._camada_chat, self._chat_pronto = self._desenhar_painel(
                self._pintor, self._camada_chat, self._chat_pronto, pos, dx, dy, escala)
        if self.contador_var.get() and self._pintor_contador is not None:
            self._camada_contador, self._contador_pronto = self._desenhar_painel(
                self._pintor_contador, self._camada_contador, self._contador_pronto,
                pos, dx, dy, escala)

    # ---------------------------------------------------------- exportar

    def _exportar(self) -> None:
        if self._exportando:
            return
        if not self.video or not self.pacote:
            messagebox.showinfo("Falta o pacote",
                                "Abra o vídeo e vincule o pacote de presentes.")
            return
        inicio, fim = self.inicio_var.get(), self.fim_var.get()
        if fim <= inicio:
            messagebox.showinfo("Trecho inválido",
                                "O fim precisa vir depois do início.")
            return

        base, _ext = os.path.splitext(self.video)
        sugestao = os.path.basename(base) + "_com_presentes.mp4"
        destino = filedialog.asksaveasfilename(
            title="Salvar vídeo", defaultextension=".mp4",
            initialfile=sugestao,
            initialdir=_pasta_lembrada("pasta_exportar", os.path.dirname(self.video)),
            filetypes=[("MP4", "*.mp4")])
        if not destino:
            return
        _lembrar_pasta("pasta_exportar", destino)

        op = compositor.Opcoes(
            animacoes=bool(self.anim_var.get()),
            chat=bool(self.chat_var.get()),
            contador_presentes=bool(self.contador_var.get()),
            volume_animacoes=float(self.volume_var.get()),
            atraso_audio=self._atraso_audio(),
            inicio=inicio, fim=fim)

        self._exportando = True
        self._cancelar = False
        self.exportou("")                    # o vídeo anterior saiu de cena
        # A prévia para de desenhar durante a exportação, mas o player fica de
        # pé: a camada some sozinha e volta quando termina.
        for camada in (self._camada_anim, self._camada_chat, self._camada_contador):
            if camada is not None:
                camada.esconder()
        if self.player:
            self._quer_tocar = False
            self.player.tocar(False)
        self.btn_exportar.configure(state="disabled", text="Exportando...")
        self.progresso.grid()
        self.btn_cancelar.grid()
        self.progresso["value"] = 0
        self.progresso_var.set("Começando...")
        self.aviso.set("")
        threading.Thread(target=self._exportar_thread,
                         args=(destino, op), daemon=True).start()

    def exportou(self, caminho: str) -> None:
        """Liga os botões do vídeo pronto. Chamada pela thread da interface.

        Quem avisa é a janela, ao receber o evento da fila - a exportação roda
        em outra thread, e mexer em widget de fora dela é pedir problema.
        """
        self._exportado = caminho if caminho and os.path.exists(caminho) else ""
        try:
            if self._exportado:
                # Agora que o rótulo divide a linha com o botão de exportar, um
                # nome comprido roubaria a largura da barra de progresso.
                nome = os.path.basename(self._exportado)
                if len(nome) > 34:
                    nome = f"{nome[:18]}…{nome[-15:]}"
                self.exportado_var.set(f"pronto: {nome}")
                estado = "normal"
            else:
                self.exportado_var.set("")
                estado = "disabled"
            self.btn_abrir.configure(state=estado)
            self.btn_pasta.configure(state=estado)
        except tk.TclError:
            pass

    def _abrir_exportado(self) -> None:
        if self._exportado:
            resources.open_path(self._exportado)

    def _ver_exportado_na_pasta(self) -> None:
        if self._exportado:
            resources.reveal_in_explorer(self._exportado)

    def _cancelar_exportacao(self) -> None:
        self._cancelar = True
        self.progresso_var.set("Cancelando...")

    def andou(self, fracao: float, texto: str) -> None:
        """Chamado pela thread da interface quando o compositor reporta."""
        try:
            self.progresso["value"] = max(0, min(1000, round(fracao * 1000)))
            self.progresso_var.set(f"{texto} — {fracao * 100:.0f}%")
        except tk.TclError:
            pass

    def _exportar_thread(self, destino: str, op: compositor.Opcoes) -> None:
        try:
            ok = compositor.exportar(self.video, self.pacote, destino, op,
                                     self.emit, cancelar=lambda: self._cancelar)
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Exportação falhou: {e}")
            ok = False
        self.emit("exportou", destino if ok else "")
        self._exportando = False
        try:
            self.btn_exportar.configure(state="normal",
                                        text="Exportar vídeo com as camadas")
            self.progresso.grid_remove()
            self.btn_cancelar.grid_remove()
            self.progresso_var.set("")
            if not ok and not self._cancelar:
                self.aviso.set("A exportação não terminou. Veja o registro.")
        except tk.TclError:
            pass

    def encerrar(self) -> None:
        self._cancelar = True
        self._descartar_camadas()
        if self.player:
            self.player.encerrar()
        if self._temp_anim:
            import shutil
            shutil.rmtree(self._temp_anim, ignore_errors=True)
