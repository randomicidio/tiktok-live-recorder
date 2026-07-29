"""Aba do editor: prévia com as camadas e exportação do trecho escolhido.

A prévia usa a libmpv embutida na janela. Ela monta um filtergraph em tempo
real, então as animações de presente aparecem sobre o vídeo enquanto você
navega - sem renderizar nada antes.

O chat é desenhado por nós (não existe arte oficial), o que não dá para fazer
em tempo real; ele entra na exportação e numa prévia de trecho sob demanda.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import compositor
import pacote as pacote_mod
import resources

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
                                  keep_open="yes", ytdl=False, osc=False)
            self.disponivel = True
        except Exception as e:                       # noqa: BLE001
            self.erro = f"não consegui iniciar o player ({e})"

    # ------------------------------------------------------------ controles

    def carregar(self, caminho: str) -> None:
        if self.mpv:
            self.mpv.command("loadfile", caminho)

    def filtro(self, externos: list[str], grafo: str) -> None:
        """Troca as camadas sobrepostas. Lista vazia = vídeo limpo."""
        if not self.mpv:
            return
        try:
            self.mpv["external-files"] = externos or []
            self.mpv["lavfi-complex"] = grafo or ""
        except Exception:                            # noqa: BLE001
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

    @property
    def tocando(self) -> bool:
        try:
            return bool(self.mpv) and not self.mpv["pause"]
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

    # ------------------------------------------------------ sobreposição

    def criar_sobreposicao(self):
        """Camada de imagem que a própria libmpv compõe sobre o vídeo.

        É assim que o chat aparece na prévia: desenhamos o painel e mandamos
        para o mpv, sem renderizar vídeo nenhum antes.
        """
        if not self.mpv:
            return None
        try:
            return self.mpv.create_image_overlay()
        except Exception:                            # noqa: BLE001
            return None

    def encerrar(self) -> None:
        if self.mpv:
            try:
                self.mpv.terminate()
            except Exception:                        # noqa: BLE001
                pass
            self.mpv = None


def _premultiplicar(img):
    """Embute o alfa na cor de cada pixel.

    A sobreposição da libmpv recebe BGRA já multiplicado. Entregar o alfa solto
    faz cada pixel de borda - o antisserrilhado das letras, do círculo do avatar
    - ser somado com força total sobre o vídeo, o que acende um contorno claro
    em volta de tudo.
    """
    from PIL import Image, ImageChops

    r, g, b, a = img.split()
    return Image.merge("RGBA", (ImageChops.multiply(r, a),
                                ImageChops.multiply(g, a),
                                ImageChops.multiply(b, a), a))


def tempo(seg: float) -> str:
    seg = max(0, int(seg))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ------------------------------------------------------------------- aba

class Editor(ttk.Frame):
    """Abre um vídeo, encontra o pacote e monta a versão com as camadas."""

    JANELA_ANIM = 90.0      # segundos de animações carregados por vez na prévia

    def __init__(self, parent, emit):
        super().__init__(parent, padding=12)
        self.emit = emit
        self.video = ""
        self.pacote: pacote_mod.Pacote | None = None
        self.player: Player | None = None
        self._temp_anim = ""
        self._agenda: list[compositor.Agendado] = []
        self._grafo_atual = ""
        self._exportando = False
        # Atualizar a barra por código dispara o callback dela; sem esta trava
        # o player recebia uma busca a cada tique e a reprodução engasgava.
        self._ajustando_barra = False
        self._ultima_checagem = 0.0
        # Chat na prévia: painel desenhado por nós e composto pela libmpv
        self._pintor = None
        self._camada_chat = None
        self._chat_em_tela = False
        self._erro_chat = ""
        # Medido uma vez ao abrir: `dimensoes` roda o ffprobe e não pode
        # entrar num laço de tela.
        self._dims = (720, 1280)

        self._montar()

    # ------------------------------------------------------------- interface

    def _montar(self) -> None:
        self.columnconfigure(0, weight=1)
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
        self.tela = tk.Frame(self, bg="#000000", width=380, height=560)
        self.tela.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.tela.grid_propagate(False)

        # --- painel lateral ----------------------------------------------
        lado = ttk.Frame(self)
        lado.grid(row=1, column=1, sticky="nsew")
        lado.rowconfigure(1, weight=1)

        camadas = ttk.LabelFrame(lado, text=" Camadas ", padding=8)
        camadas.grid(row=0, column=0, sticky="ew")
        self.anim_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(camadas, text="Animações dos presentes",
                        variable=self.anim_var,
                        command=self._camadas_mudaram).pack(anchor="w")
        self.chat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(camadas, text="Chat da live",
                        variable=self.chat_var,
                        command=self._chat_mudou).pack(anchor="w", pady=(2, 6))

        ttk.Label(camadas, text="Volume das animações").pack(anchor="w")
        vol = ttk.Frame(camadas)
        vol.pack(fill="x")
        self.volume_var = tk.DoubleVar(value=1.0)
        ttk.Scale(vol, from_=0.0, to=2.0, variable=self.volume_var,
                  command=self._volume_mudou).pack(side="left", fill="x", expand=True)
        self.volume_txt = tk.StringVar(value="100%")
        ttk.Label(vol, textvariable=self.volume_txt, width=6).pack(side="left")

        lista = ttk.LabelFrame(lado, text=" Presentes com animação ", padding=6)
        lista.grid(row=1, column=0, sticky="nsew", pady=8)
        lista.rowconfigure(0, weight=1)
        lista.columnconfigure(0, weight=1)
        self.lista = tk.Listbox(lista, font=("Consolas", 9), height=10,
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
        for rotulo, var, cmd in (("Início", self.inicio_var, self._marcar_inicio),
                                 ("Fim", self.fim_var, self._marcar_fim)):
            linha = ttk.Frame(trecho)
            linha.pack(fill="x", pady=2)
            ttk.Label(linha, text=rotulo, width=7).pack(side="left")
            txt = tk.StringVar(value="0:00")
            setattr(self, f"txt_{rotulo.lower()}", txt)
            ttk.Label(linha, textvariable=txt, width=8,
                      font=("Consolas", 10)).pack(side="left")
            ttk.Button(linha, text="marcar aqui", command=cmd).pack(side="left")
        ttk.Button(trecho, text="Usar o vídeo inteiro",
                   command=self._trecho_inteiro).pack(fill="x", pady=(6, 0))

        # --- transporte ---------------------------------------------------
        transporte = ttk.Frame(self)
        transporte.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        transporte.columnconfigure(1, weight=1)
        self.btn_play = ttk.Button(transporte, text="▶", width=4,
                                   command=self._alternar_play)
        self.btn_play.grid(row=0, column=0)
        self.linha = ttk.Scale(transporte, from_=0, to=100,
                               command=self._arrastou)
        self.linha.grid(row=0, column=1, sticky="ew", padx=8)
        self.tempo_var = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(transporte, textvariable=self.tempo_var,
                  font=("Consolas", 10)).grid(row=0, column=2)

        acoes = ttk.Frame(self)
        acoes.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.btn_exportar = tk.Button(
            acoes, text="Exportar vídeo com as camadas", command=self._exportar,
            font=("Segoe UI", 10, "bold"), bg="#15803d", fg="white",
            disabledforeground="#bbf7d0", bd=2, padx=16, pady=8, cursor="hand2")
        self.btn_exportar.pack(side="left")
        self.aviso = tk.StringVar(value="")
        ttk.Label(acoes, textvariable=self.aviso, foreground="#b45309").pack(
            side="left", padx=12)

        self.after(500, self._tique)
        self.after(150, self._tique_chat)

    # ------------------------------------------------------------- abrir

    def escolher_video(self) -> None:
        caminho = filedialog.askopenfilename(
            title="Escolha o vídeo da live",
            filetypes=[("Vídeos", "*.mp4 *.mkv *.ts *.mov"), ("Todos", "*.*")])
        if caminho:
            self.abrir(caminho)

    def escolher_pacote(self) -> None:
        if not self.video:
            messagebox.showinfo("Abra o vídeo antes",
                                "Escolha primeiro o vídeo da live.")
            return
        caminho = filedialog.askopenfilename(
            title="Escolha o pacote de presentes",
            filetypes=[("Pacote de presentes", f"*{pacote_mod.EXTENSAO}"),
                       ("Todos", "*.*")])
        if caminho:
            self._carregar_pacote(caminho)

    def abrir(self, caminho: str) -> None:
        """Abre o vídeo e procura o pacote correspondente."""
        if not os.path.exists(caminho):
            messagebox.showerror("Não encontrei", f"O arquivo sumiu:\n{caminho}")
            return
        self.video = caminho
        self.arquivo_var.set(os.path.basename(caminho))

        if self.player is None:
            self.player = Player(self.tela)
            if not self.player.disponivel:
                self.aviso.set(f"Sem prévia: {self.player.erro}")
        self.player.carregar(caminho)

        self._dims = compositor.dimensoes(caminho)
        self._limpar_chat()
        self._pintor = None
        self._camada_chat = None

        dur = compositor.duracao(caminho)
        self.fim_var.set(dur)
        self.inicio_var.set(0.0)
        self._atualizar_trecho()
        self.linha.configure(to=max(1.0, dur))

        achado = pacote_mod.procurar_para(caminho)
        if achado:
            self._carregar_pacote(achado)
        else:
            self.pacote = None
            self.pacote_var.set("nenhum pacote encontrado — vincule manualmente")
            self.lista.delete(0, "end")

    def _carregar_pacote(self, caminho: str) -> None:
        try:
            self.pacote = pacote_mod.abrir(caminho)
        except pacote_mod.PacoteError as e:
            messagebox.showerror("Pacote inválido", str(e))
            return
        n_anim = len(self.pacote.com_animacao)
        self.pacote_var.set(
            f"{os.path.basename(caminho)} — {n_anim} animações, "
            f"{len(self.pacote.comentarios)} comentários")
        self._preparar_animacoes()
        self._preencher_lista()
        self._camadas_mudaram()

    def _preparar_animacoes(self) -> None:
        """Extrai as animações do pacote e calcula quando cada uma aparece."""
        if not self.pacote:
            return
        import tempfile
        self._temp_anim = self._temp_anim or tempfile.mkdtemp(prefix="editor_")
        largura, altura = compositor.dimensoes(self.video)
        dur = compositor.duracao(self.video)
        self._agenda = compositor.agendar(self.pacote, self._temp_anim,
                                          largura, altura, 0.0, dur)
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

    def _grafo_para(self, pos: float) -> tuple[list[str], str]:
        """Monta o filtergraph com as animações perto da posição atual.

        Carregar todas de uma vez incharia o grafo à toa; a janela cobre o que
        está por vir sem custo desnecessário.
        """
        if not self.anim_var.get() or not self._agenda:
            return [], ""

        perto = [a for a in self._agenda
                 if a.fim >= pos - 5 and a.inicio <= pos + self.JANELA_ANIM]
        if not perto:
            return [], ""

        externos, partes = [], []
        atual = "vid1"
        for i, item in enumerate(perto, start=2):
            externos.append(item.arquivo)
            cfg = item.cfg
            rgb, alfa = cfg.get("rgbFrame"), cfg.get("aFrame")
            if rgb and alfa and rgb != alfa:
                rx, ry, rw, rh = rgb
                ax, ay, aw, ah = alfa
                partes.append(
                    f"[vid{i}]split=2[s{i}a][s{i}b];"
                    f"[s{i}a]crop={rw}:{rh}:{rx}:{ry},setsar=1[rgb{i}];"
                    f"[s{i}b]crop={aw}:{ah}:{ax}:{ay},scale={rw}:{rh},format=gray[al{i}];"
                    f"[rgb{i}][al{i}]alphamerge,"
                    f"setpts=PTS-STARTPTS+{item.inicio:.3f}/TB[an{i}]")
            else:
                partes.append(f"[vid{i}]setpts=PTS-STARTPTS+{item.inicio:.3f}/TB[an{i}]")
            partes.append(
                f"[{atual}][an{i}]overlay=0:0:"
                f"enable='between(t,{item.inicio:.3f},{item.fim:.3f})'[ov{i}]")
            atual = f"ov{i}"

        # o mpv exige que a saída do grafo se chame [vo]
        return externos, ";".join(partes) + f";[{atual}]null[vo]"

    def _camadas_mudaram(self) -> None:
        if not self.player or not self.player.disponivel:
            return
        externos, grafo = self._grafo_para(self.player.posicao)
        if grafo != self._grafo_atual:
            self._grafo_atual = grafo
            self.player.filtro(externos, grafo)

    def _volume_mudou(self, _=None) -> None:
        self.volume_txt.set(f"{int(self.volume_var.get() * 100)}%")

    # ------------------------------------------------------ chat na prévia

    def _chat_mudou(self) -> None:
        """Liga ou desliga a camada de chat sobre a prévia."""
        if not self.chat_var.get():
            self._limpar_chat()
            return
        if not self.pacote or not self.pacote.comentarios:
            self.aviso.set("O pacote não tem chat gravado.")
            self.chat_var.set(False)
            return
        if not self.player or not self.player.disponivel:
            return

        if self._pintor is None:
            # Baixar os avatares uma vez pode demorar: fora da thread da janela.
            self._erro_chat = ""
            self.aviso.set("Preparando o chat...")
            threading.Thread(target=self._preparar_chat, daemon=True).start()
        else:
            self._desenhar_chat(self.player.posicao)

    def _preparar_chat(self) -> None:
        """Roda fora da thread da janela (baixa avatares), então NÃO toca em
        widget nenhum: só publica o pintor e deixa o tique avisar."""
        try:
            self._pintor = compositor.PintorDeChat(self.pacote, self._dims[0],
                                                   self._dims[1], self.emit)
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Não consegui preparar o chat: {e}")
            self._erro_chat = f"Não consegui preparar o chat: {e}"

    def _limpar_chat(self) -> None:
        if self._camada_chat is not None and self._chat_em_tela:
            try:
                self._camada_chat.remove()
            except Exception:                        # noqa: BLE001
                pass
        self._chat_em_tela = False

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

    def _desenhar_chat(self, pos: float) -> None:
        """Manda o painel do instante atual para a libmpv compor."""
        if not (self.chat_var.get() and self._pintor and self.player
                and self.player.disponivel):
            return
        if self._camada_chat is None:
            self._camada_chat = self.player.criar_sobreposicao()
            if self._camada_chat is None:
                self.aviso.set("Prévia do chat indisponível neste player.")
                return

        pronto = self._pintor.painel(pos)
        if pronto is None:
            self._limpar_chat()
            return
        painel, px, py = pronto

        desloc_x, desloc_y, escala = self._area_do_video()
        if escala <= 0:
            return
        largura = max(1, round(painel.width * escala))
        altura = max(1, round(painel.height * escala))
        try:
            # Multiplicar antes de reduzir, nessa ordem: é o formato que a
            # libmpv espera, e reduzir com o alfa solto mancharia as bordas
            # com a cor dos pixels transparentes.
            painel = _premultiplicar(painel)
            if (largura, altura) != painel.size:
                from PIL import Image
                # BOX é média de área: não ultrapassa os valores originais.
                # LANCZOS ultrapassaria, e cor acima do alfa acende em ponto
                # branco na hora de compor.
                painel = painel.resize((largura, altura), Image.BOX)
            self._camada_chat.update(
                painel, pos=(round(desloc_x + px * escala),
                             round(desloc_y + py * escala)))
            self._chat_em_tela = True
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Chat na prévia falhou: {e}")
            self.chat_var.set(False)

    # ---------------------------------------------------------- navegação

    def _ir_para_presente(self, _evt=None) -> None:
        sel = self.lista.curselection()
        if not sel or sel[0] >= len(self._agenda):
            return
        alvo = max(0.0, self._agenda[sel[0]].inicio - 1.5)
        if self.player:
            self.player.buscar(alvo)
        self._camadas_mudaram()

    def _alternar_play(self) -> None:
        if self.player:
            self.player.tocar(not self.player.tocando)

    def _arrastou(self, valor) -> None:
        """Só busca quando foi o usuário que mexeu, nunca no tique automático."""
        if self._ajustando_barra:
            return
        if self.player and self.player.disponivel:
            self.player.buscar(float(valor), preciso=False)

    def _marcar_inicio(self) -> None:
        self.inicio_var.set(self.player.posicao if self.player else 0.0)
        self._atualizar_trecho()

    def _marcar_fim(self) -> None:
        self.fim_var.set(self.player.posicao if self.player else 0.0)
        self._atualizar_trecho()

    def _trecho_inteiro(self) -> None:
        self.inicio_var.set(0.0)
        self.fim_var.set(compositor.duracao(self.video) if self.video else 0.0)
        self._atualizar_trecho()

    def _atualizar_trecho(self) -> None:
        self.txt_início.set(tempo(self.inicio_var.get()))
        self.txt_fim.set(tempo(self.fim_var.get()))

    def _tique(self) -> None:
        if self.player and self.player.disponivel and self.video:
            pos, dur = self.player.posicao, self.player.duracao
            self.tempo_var.set(f"{tempo(pos)} / {tempo(dur)}")
            self.btn_play.configure(text="❚❚" if self.player.tocando else "▶")
            if self.player.tocando:
                self._ajustando_barra = True
                self.linha.set(pos)
                self._ajustando_barra = False
                # Remontar o filtergraph dá um solavanco: só quando a janela de
                # animações realmente muda, não a cada tique.
                if pos - self._ultima_checagem > 5.0 or pos < self._ultima_checagem:
                    self._ultima_checagem = pos
                    self._camadas_mudaram()
        self.after(500, self._tique)

    def _tique_chat(self) -> None:
        """Ritmo próprio para o chat: o painel é pequeno e barato de desenhar,
        e a 500 ms as mensagens surgiriam aos solavancos."""
        try:
            if self._erro_chat:
                self.aviso.set(self._erro_chat)
                self._erro_chat = ""
                self.chat_var.set(False)
            elif self._pintor is not None and self.aviso.get().startswith("Preparando"):
                self.aviso.set("")
            if self.chat_var.get() and self.player and self.player.disponivel:
                self._desenhar_chat(self.player.posicao)
        except Exception:                            # noqa: BLE001
            pass
        self.after(150, self._tique_chat)

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

        base, ext = os.path.splitext(self.video)
        sugestao = os.path.basename(base) + "_com_presentes.mp4"
        destino = filedialog.asksaveasfilename(
            title="Salvar vídeo", defaultextension=".mp4",
            initialfile=sugestao, initialdir=os.path.dirname(self.video),
            filetypes=[("MP4", "*.mp4")])
        if not destino:
            return

        op = compositor.Opcoes(
            animacoes=bool(self.anim_var.get()),
            chat=bool(self.chat_var.get()),
            volume_animacoes=float(self.volume_var.get()),
            inicio=inicio, fim=fim)

        self._exportando = True
        self.btn_exportar.configure(state="disabled", text="Exportando...")
        threading.Thread(target=self._exportar_thread,
                         args=(destino, op), daemon=True).start()

    def _exportar_thread(self, destino: str, op: compositor.Opcoes) -> None:
        try:
            ok = compositor.exportar(self.video, self.pacote, destino, op, self.emit)
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Exportação falhou: {e}")
            ok = False
        self.emit("exportou", destino if ok else "")
        self._exportando = False
        try:
            self.btn_exportar.configure(state="normal",
                                        text="Exportar vídeo com as camadas")
        except tk.TclError:
            pass

    def encerrar(self) -> None:
        if self.player:
            self.player.encerrar()
        if self._temp_anim:
            import shutil
            shutil.rmtree(self._temp_anim, ignore_errors=True)
