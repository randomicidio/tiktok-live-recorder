"""TikTok Replay Downloader - interface grafica.

Uma tela so: informe a conta, aperte REC e pronto. O programa fica de olho e
grava a live inteira em MP4 assim que ela comeca, sem recodificar nada.

Nao ha escolha de qualidade nem de FPS de proposito: a variante `origin` do
TikTok e o seu proprio video sem recompressao, entao gravar ela ja significa a
melhor resolucao e a taxa de quadros original, seja 30 ou 60.

As threads de trabalho nunca tocam nos widgets: elas empurram eventos numa fila
que a thread da interface consome em `_drain`.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import editor
import recorder
import resources
import tiktok_api

BASE_DIR = resources.data_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULTS = {
    "username": "",
    "outdir": resources.default_output_dir(),
    "poll_seconds": 30,
    "keep_waiting": True,
    "registrar_presentes": True,
    "cookie": "",
}

APP_TITLE = resources.APP_NAME

# Cada estado tem sua cor. `prefix` vai para o titulo da janela, para o estado
# aparecer tambem na barra de tarefas quando a janela estiver minimizada.
NEUTRO = {"fg": "#374151", "bg": "#f3f4f6", "dot": "#9ca3af", "border": "#d1d5db", "prefix": ""}
PALETTE = {
    "parado": NEUTRO,
    "offline": NEUTRO,
    "info": NEUTRO,
    "aguardando": {
        "fg": "#92400e", "bg": "#fef3c7", "dot": "#f59e0b",
        "border": "#fcd34d", "prefix": "Aguardando - ",
    },
    "aovivo": {
        "fg": "#9a3412", "bg": "#ffedd5", "dot": "#f97316",
        "border": "#fdba74", "prefix": "Ao vivo - ",
    },
    "gravando": {
        "fg": "#b91c1c", "bg": "#fee2e2", "dot": "#dc2626",
        "border": "#f87171", "prefix": "● GRAVANDO - ",
    },
    "finalizando": {
        "fg": "#1e40af", "bg": "#dbeafe", "dot": "#3b82f6",
        "border": "#93c5fd", "prefix": "Finalizando - ",
    },
    "concluido": {
        "fg": "#166534", "bg": "#dcfce7", "dot": "#22c55e",
        "border": "#86efac", "prefix": "",
    },
    "erro": {
        "fg": "#b91c1c", "bg": "#fee2e2", "dot": "#dc2626",
        "border": "#f87171", "prefix": "Erro - ",
    },
}

CARD_BG = "#f8fafc"
CARD_BORDER = "#cbd5e1"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    # Descarta chaves de versoes antigas (qualidade e navegador ja nao existem)
    # para o arquivo nao acumular lixo a cada gravacao.
    return {k: v for k, v in cfg.items() if k in DEFAULTS}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def open_path(path: str) -> None:
    """Abre o arquivo ou a pasta no programa padrao do sistema."""
    if not os.path.exists(path):
        return
    if resources.IS_WINDOWS:
        os.startfile(path)  # noqa: S606
    elif resources.IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_explorer(path: str) -> None:
    """Mostra o arquivo ja selecionado no gerenciador de arquivos."""
    if not os.path.exists(path):
        return
    if resources.IS_WINDOWS:
        # O explorer devolve codigo 1 mesmo dando certo; nao ha o que checar.
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    elif resources.IS_MAC:
        subprocess.Popen(["open", "-R", path])   # -R revela no Finder
    else:
        open_path(os.path.dirname(path))


def formata_tamanho(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb >= 100:
        return f"{mb:.0f} MB"
    return f"{mb:.1f} MB"


def formata_duracao(segundos: int) -> str:
    horas, resto = divmod(int(segundos), 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{seg:02d}"
    return f"{minutos:02d}:{seg:02d}"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("700x620")
        self.minsize(640, 560)
        self.configure(bg="#ffffff")

        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self.recorder = recorder.Recorder(lambda k, v="": self.events.put((k, v)))

        # Referencias das miniaturas: sem isso o Tk descarta a imagem e o
        # cartao aparece vazio.
        self._thumbs: list[tk.PhotoImage] = []
        self._state_key = "parado"
        self._blink_on = False
        self._disarming = False

        self._build_ui()
        self._check_dependencies()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain)
        self.after(550, self._pulse)

    # ------------------------------------------------------------------ UI

    def _set_window_icon(self) -> None:
        """Icone da janela e da barra de tarefas."""
        try:
            if resources.IS_WINDOWS:
                ico = resources.resource("assets", "icon.ico")
                if os.path.exists(ico):
                    self.iconbitmap(default=ico)
                    return
            png = resources.resource("assets", "icon.png")
            if os.path.exists(png):
                self._icon_img = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass  # sem icone o programa funciona igual

    def _build_header(self, parent) -> None:
        """Faixa de identidade no topo: marca do programa e versao."""
        barra = tk.Frame(parent, bg="#0f172a")
        barra.pack(fill="x")
        interno = tk.Frame(barra, bg="#0f172a")
        interno.pack(fill="x", padx=18, pady=12)

        marca = tk.Canvas(interno, width=26, height=26, bg="#0f172a",
                          highlightthickness=0, bd=0)
        marca.create_oval(4, 4, 22, 22, outline="#f87171", width=2)
        marca.create_oval(8, 8, 18, 18, fill="#ef4444", outline="")
        marca.pack(side="left")

        tk.Label(interno, text=resources.APP_NAME, bg="#0f172a", fg="#f8fafc",
                 font=("Segoe UI Semibold", 13)).pack(side="left", padx=(10, 0))
        tk.Label(interno, text=f"v{resources.APP_VERSION}", bg="#0f172a", fg="#64748b",
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 0), pady=(3, 0))

    def _build_ui(self) -> None:
        self._set_window_icon()
        self._build_header(self)

        self.abas = ttk.Notebook(self)
        self.abas.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        main = ttk.Frame(self.abas, padding=18)
        self.abas.add(main, text="  Gravar  ")

        self.editor = editor.Editor(self.abas,
                                    lambda k, v="": self.events.put((k, v)))
        self.abas.add(self.editor, text="  Editar  ")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(7, weight=1)

        rotulo = {"font": ("Segoe UI", 10)}

        # --- conta ---------------------------------------------------------
        ttk.Label(main, text="Sua conta", **rotulo).grid(row=0, column=0, sticky="w", pady=(0, 8))
        conta = ttk.Frame(main)
        conta.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        conta.columnconfigure(1, weight=1)
        ttk.Label(conta, text="@", font=("Segoe UI", 11, "bold")).grid(row=0, column=0)
        self.user_var = tk.StringVar(value=self.cfg["username"])
        ttk.Entry(conta, textvariable=self.user_var, font=("Segoe UI", 11)).grid(
            row=0, column=1, sticky="ew", padx=(2, 8)
        )
        ttk.Button(conta, text="Verificar agora", command=self._check_now).grid(row=0, column=2)

        # --- pasta ---------------------------------------------------------
        ttk.Label(main, text="Salvar em", **rotulo).grid(row=1, column=0, sticky="w", pady=8)
        pasta = ttk.Frame(main)
        pasta.grid(row=1, column=1, sticky="ew", pady=8)
        pasta.columnconfigure(0, weight=1)
        self.outdir_var = tk.StringVar(value=self.cfg["outdir"])
        ttk.Entry(pasta, textvariable=self.outdir_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(pasta, text="Escolher...", command=self._pick_dir).grid(row=0, column=1)
        ttk.Button(pasta, text="Abrir pasta", command=self._open_outdir).grid(
            row=0, column=2, padx=(8, 0)
        )

        # --- opcoes --------------------------------------------------------
        opcoes = ttk.Frame(main)
        opcoes.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.keep_var = tk.BooleanVar(value=bool(self.cfg["keep_waiting"]))
        ttk.Checkbutton(
            opcoes,
            text="Continuar aguardando as próximas lives depois que uma terminar",
            variable=self.keep_var,
        ).pack(anchor="w")

        self.presentes_var = tk.BooleanVar(value=bool(self.cfg["registrar_presentes"]))
        ttk.Checkbutton(
            opcoes,
            text="Registrar presentes e chat (gera o pacote para inserir as animações depois)",
            variable=self.presentes_var,
        ).pack(anchor="w", pady=(4, 0))

        intervalo = ttk.Frame(main)
        intervalo.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(intervalo, text="Verificar se estou ao vivo a cada").pack(side="left")
        self.poll_var = tk.IntVar(value=int(self.cfg["poll_seconds"]))
        ttk.Spinbox(
            intervalo, from_=10, to=600, increment=5, width=5, textvariable=self.poll_var
        ).pack(side="left", padx=6)
        ttk.Label(intervalo, text="segundos").pack(side="left")

        ttk.Separator(main, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=16
        )

        # --- botoes REC ----------------------------------------------------
        botoes = ttk.Frame(main)
        botoes.grid(row=5, column=0, columnspan=2, sticky="ew")
        # tk.Button e nao ttk: no Windows o tema nativo do ttk ignora as cores
        # de fundo, e sem cor nao ha cara de REC.
        self.start_btn = tk.Button(
            botoes,
            command=self._toggle,
            font=("Segoe UI", 12, "bold"),
            fg="white",
            disabledforeground="#fecaca",
            activeforeground="white",
            bd=3,
            padx=22,
            pady=10,
            cursor="hand2",
        )
        self.start_btn.pack(side="left")
        self.start_btn.bind("<Enter>", lambda _e: self._hover_rec(True))
        self.start_btn.bind("<Leave>", lambda _e: self._hover_rec(False))
        self.stop_btn = tk.Button(
            botoes,
            text="■  Parar e salvar",
            command=self._stop,
            font=("Segoe UI", 10),
            bg="#e5e7eb",
            fg="#374151",
            disabledforeground="#9ca3af",
            bd=2,
            padx=16,
            pady=10,
            cursor="hand2",
        )
        self.stop_btn.pack(side="left", padx=12)

        # --- faixa de estado, logo abaixo dos botoes -----------------------
        self.status_var = tk.StringVar(value="Parado")
        self.detail_var = tk.StringVar(value="")

        self.banner = tk.Frame(main, highlightthickness=1, bd=0)
        self.banner.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(16, 4))
        inner = tk.Frame(self.banner, bd=0)
        inner.pack(fill="x", padx=12, pady=10)
        self._banner_parts = [self.banner, inner]

        self.dot = tk.Canvas(inner, width=16, height=16, highlightthickness=0, bd=0)
        self.dot_item = self.dot.create_oval(3, 3, 14, 14, outline="")
        self.dot.pack(side="left")
        self.status_label = tk.Label(
            inner, textvariable=self.status_var, font=("Segoe UI", 12, "bold"), bd=0
        )
        self.status_label.pack(side="left", padx=(9, 0))
        self.detail_label = tk.Label(
            inner, textvariable=self.detail_var, font=("Consolas", 10), bd=0
        )
        self.detail_label.pack(side="right")

        # --- registro ------------------------------------------------------
        registro = ttk.LabelFrame(main, text=" Registro ", padding=6)
        registro.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        registro.rowconfigure(0, weight=1)
        registro.columnconfigure(0, weight=1)
        self.log = tk.Text(
            registro, height=10, wrap="word", state="disabled",
            font=("Consolas", 9), bd=0, relief="flat", bg="#ffffff",
            padx=6, pady=4, cursor="arrow",
        )
        scroll = ttk.Scrollbar(registro, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self._apply_state("parado", "Parado")

    # ------------------------------------------------------- estado visual

    def _sync_buttons(self) -> None:
        """Deixa o botao com cara de REC: solto, armado ou gravando.

        Armado e gravando ficam afundados e escuros, para que bater o olho no
        botao ja diga que a gravacao esta engatilhada - mesmo que a live ainda
        nao tenha comecado. Armado continua clicavel para poder desarmar;
        gravando fica travado, para ninguem derrubar a gravacao sem querer.
        """
        if not hasattr(self, "start_btn"):
            return
        if not self.recorder.active:
            self._disarming = False
            self.start_btn.configure(
                text="●  Iniciar Gravação",
                bg="#dc2626", activebackground="#b91c1c",
                relief="raised", state="normal", cursor="hand2",
            )
            self.stop_btn.configure(state="disabled", cursor="")
        elif self._disarming:
            # Desarmar pode demorar ate a consulta em curso responder; sem este
            # aviso o botao ficaria mudo e o clique pareceria ignorado.
            self.start_btn.configure(
                text="Desarmando...",
                bg="#7f1d1d", relief="sunken", state="disabled", cursor="",
            )
            self.stop_btn.configure(state="disabled", cursor="")
        elif self.recorder.recording:
            self.start_btn.configure(
                text="●  GRAVANDO",
                bg="#991b1b", activebackground="#7f1d1d",
                relief="sunken", state="disabled", cursor="",
            )
            self.stop_btn.configure(state="normal", cursor="hand2")
        else:
            self.start_btn.configure(
                text="●  REC armado  (clique para desarmar)",
                bg="#7f1d1d", activebackground="#991b1b",
                relief="sunken", state="normal", cursor="hand2",
            )
            self.stop_btn.configure(state="disabled", cursor="")

    def _hover_rec(self, dentro: bool) -> None:
        """Clareia o botao sob o cursor, so quando ele aceita clique."""
        if str(self.start_btn.cget("state")) != "normal":
            return
        if self.recorder.active:
            self.start_btn.configure(bg="#991b1b" if dentro else "#7f1d1d")
        else:
            self.start_btn.configure(bg="#ef4444" if dentro else "#dc2626")

    def _apply_state(self, key: str, text: str) -> None:
        """Pinta a faixa inteira com as cores do estado informado."""
        pal = PALETTE.get(key, NEUTRO)
        self._state_key = key
        self.status_var.set(text)

        for w in self._banner_parts:
            w.configure(bg=pal["bg"])
        self.banner.configure(highlightbackground=pal["border"], highlightcolor=pal["border"])
        self.dot.configure(bg=pal["bg"])
        self.dot.itemconfigure(self.dot_item, fill=pal["dot"])
        self.status_label.configure(bg=pal["bg"], fg=pal["fg"])
        self.detail_label.configure(bg=pal["bg"], fg=pal["fg"])
        self.title(pal["prefix"] + APP_TITLE)

        if key != "gravando":
            self.detail_var.set("")
        self._sync_buttons()

    def _pulse(self) -> None:
        """Pisca a bolinha durante a gravacao e atualiza tempo/tamanho."""
        pal = PALETTE.get(self._state_key, NEUTRO)
        if self._state_key == "gravando":
            self._blink_on = not self._blink_on
            self.dot.itemconfigure(
                self.dot_item, fill=pal["dot"] if self._blink_on else pal["bg"]
            )
            self._update_detail()
        elif self._blink_on:
            self._blink_on = False
            self.dot.itemconfigure(self.dot_item, fill=pal["dot"])
        self.after(550, self._pulse)

    def _update_detail(self) -> None:
        started = self.recorder.session_started
        if not started or not self.recorder.recording:
            self.detail_var.set("")
            return
        tempo = formata_duracao((datetime.now() - started).total_seconds())
        self.detail_var.set(f"{tempo}     {formata_tamanho(self.recorder.session_size())}")

    # ------------------------------------------------------------- registro

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now():%H:%M:%S}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_card(self, result: recorder.SessionResult) -> None:
        """Insere no registro um cartao com miniatura e atalhos do arquivo."""
        caminho = result.final_path
        card = tk.Frame(self.log, bg=CARD_BG, highlightthickness=1,
                        highlightbackground=CARD_BORDER, bd=0)

        if result.thumb_path and os.path.exists(result.thumb_path):
            try:
                img = tk.PhotoImage(file=result.thumb_path)
                self._thumbs.append(img)
                tk.Label(card, image=img, bg=CARD_BG, bd=0).pack(side="left", padx=10, pady=10)
            except tk.TclError:
                pass  # miniatura ilegivel: o cartao vale sem ela

        info = tk.Frame(card, bg=CARD_BG)
        info.pack(side="left", fill="both", expand=True, padx=(2, 10), pady=10)

        tk.Label(
            info, text=os.path.basename(caminho), bg=CARD_BG, fg="#0f172a",
            font=("Segoe UI", 9, "bold"), anchor="w", justify="left", wraplength=330,
        ).pack(anchor="w")

        detalhes = formata_tamanho(result.bytes_written)
        if result.duration_seconds:
            detalhes += f"   ·   {formata_duracao(result.duration_seconds)}"
        tk.Label(info, text=detalhes, bg=CARD_BG, fg="#64748b",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 6))

        acoes = tk.Frame(info, bg=CARD_BG)
        acoes.pack(anchor="w")
        tk.Button(
            acoes, text="▶  Abrir vídeo", command=lambda p=caminho: open_path(p),
            font=("Segoe UI", 9), bg="#e2e8f0", fg="#0f172a", bd=1,
            padx=10, pady=3, cursor="hand2",
        ).pack(side="left")
        tk.Button(
            acoes, text="Ver na pasta", command=lambda p=caminho: reveal_in_explorer(p),
            font=("Segoe UI", 9), bg="#e2e8f0", fg="#0f172a", bd=1,
            padx=10, pady=3, cursor="hand2",
        ).pack(side="left", padx=8)
        tk.Button(
            acoes, text="✂  Abrir no editor",
            command=lambda p=caminho: self._abrir_no_editor(p),
            font=("Segoe UI", 9), bg="#dcfce7", fg="#14532d", bd=1,
            padx=10, pady=3, cursor="hand2",
        ).pack(side="left")

        self.log.configure(state="normal")
        self.log.insert("end", "\n")
        self.log.window_create("end", window=card)
        self.log.insert("end", "\n\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -------------------------------------------------------------- helpers

    def _abrir_no_editor(self, caminho: str) -> None:
        """Manda a gravação recém-terminada direto para a aba de edição."""
        self.abas.select(self.editor)
        self.editor.abrir(caminho)

    def _check_dependencies(self) -> None:
        if not recorder.find_ffmpeg():
            self._log("AVISO: ffmpeg não encontrado no PATH - a gravação não vai funcionar.")
        if editor._acha_libmpv():
            self._log("Prévia do editor: disponível.")
        else:
            self._log("AVISO: prévia do editor indisponível (libmpv ausente) - "
                      "a exportação funciona mesmo assim.")
        if recorder.gift_log.DISPONIVEL:
            self._log("Registro de presentes e chat: disponível.")
        else:
            self._log("AVISO: registro de presentes indisponível (falta a biblioteca "
                      "TikTokLive) - a gravação funciona normalmente.")
        self._log("Pronto.")

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.outdir_var.get() or BASE_DIR)
        if d:
            self.outdir_var.set(d)

    def _open_outdir(self) -> None:
        d = self.outdir_var.get()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return
        open_path(d)

    def _persist(self) -> None:
        self.cfg.update(
            {
                "username": self.user_var.get().strip().lstrip("@"),
                "outdir": self.outdir_var.get().strip(),
                "poll_seconds": int(self.poll_var.get()),
                "keep_waiting": bool(self.keep_var.get()),
                "registrar_presentes": bool(self.presentes_var.get()),
            }
        )
        save_config(self.cfg)

    # --------------------------------------------------------------- acoes

    def _check_now(self) -> None:
        user = self.user_var.get().strip().lstrip("@")
        if not user:
            messagebox.showinfo("Falta o usuário", "Digite o seu @ do TikTok.")
            return
        self._log(f"Consultando @{user}...")
        if not self.recorder.active:
            self._apply_state("info", "Verificando...")
        threading.Thread(target=self._check_worker, args=(user,), daemon=True).start()

    def _check_worker(self, user: str) -> None:
        try:
            info = tiktok_api.resolve(user, tiktok_api.make_session(self.cfg.get("cookie", "")))
        except tiktok_api.TikTokError as e:
            self.events.put(("log", f"Falhou: {e}"))
            return
        self.events.put(("info", info))

    def _apply_info(self, info: tiktok_api.LiveInfo) -> None:
        who = info.nickname or info.username
        if info.is_live:
            self._log(f"{who} está AO VIVO." + (f' Título: "{info.title}"' if info.title else ""))
        else:
            self._log(f"{who} não está ao vivo no momento.")

        opcao = info.pick("origin")
        if opcao:
            extra = ("" if opcao.quality == "origin"
                     else "  (a original não está sendo oferecida nesta live)")
            self._log(f"Melhor qualidade disponível: {opcao.label}{extra}")

        # Uma verificacao manual nao pode atropelar o estado de quem ja esta
        # gravando ou armado.
        if not self.recorder.active:
            self._apply_state("aovivo" if info.is_live else "offline",
                              "Ao vivo agora" if info.is_live else "Offline")

    def _toggle(self) -> None:
        """O botao REC liga e desliga: armar quando solto, desarmar quando armado."""
        if self.recorder.active:
            self._disarm()
        else:
            self._start()

    def _start(self) -> None:
        user = self.user_var.get().strip().lstrip("@")
        if not user:
            messagebox.showinfo("Falta o usuário", "Digite o seu @ do TikTok.")
            return
        outdir = self.outdir_var.get().strip() or DEFAULTS["outdir"]
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Pasta inválida", f"Não consegui usar essa pasta:\n{e}")
            return

        self._persist()
        self._apply_state("aguardando", "Verificando...")
        self.recorder.registrar_presentes = bool(self.presentes_var.get())
        self.recorder.start(
            username=user,
            outdir=outdir,
            quality="origin",
            cookie=self.cfg.get("cookie", ""),
            poll_seconds=int(self.poll_var.get()),
            wait_for_live=bool(self.keep_var.get()),
        )
        self._sync_buttons()

    def _disarm(self) -> None:
        """Cancela a espera sem gravar nada (so vale antes da live comecar)."""
        self._log("Gravação desarmada.")
        self._disarming = True
        self._apply_state("info", "Desarmando...")
        threading.Thread(target=self.recorder.stop, daemon=True).start()

    def _stop(self) -> None:
        self._log("Parando... o arquivo está sendo fechado.")
        self.stop_btn.configure(state="disabled")
        if self.recorder.recording:
            self._apply_state("finalizando", "Fechando o arquivo...")
        threading.Thread(target=self.recorder.stop, daemon=True).start()

    # ---------------------------------------------------------------- fila

    def _drain(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._log(str(value))
                elif kind == "status":
                    # (estado, texto) das threads; string solta ainda funciona.
                    if isinstance(value, tuple):
                        self._apply_state(value[0], value[1])
                    else:
                        self._apply_state("info", str(value))
                elif kind == "error":
                    self._log(f"ERRO: {value}")
                    self._apply_state("erro", str(value)[:70])
                    messagebox.showerror("Erro", str(value))
                elif kind == "info":
                    self._apply_info(value)
                elif kind == "done":
                    if getattr(value, "final_path", ""):
                        self._apply_state("concluido", "Gravação salva")
                        self._log_card(value)
                elif kind == "exportou":
                    if value:
                        self._log(f"Vídeo exportado: {os.path.basename(str(value))}")
                        self._apply_state("concluido", "Vídeo exportado")
                    else:
                        self._log("A exportação não terminou. Veja o registro.")
                elif kind == "finished":
                    self._sync_buttons()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _on_close(self) -> None:
        if self.recorder.recording:
            if not messagebox.askyesno(
                "Gravação em andamento",
                "Uma live está sendo gravada agora.\n\n"
                "Fechar agora encerra a gravação e salva o que já foi baixado. Continuar?",
            ):
                return
        self._persist()
        self.recorder.stop()
        try:
            self.editor.encerrar()
        except Exception:                            # noqa: BLE001
            pass
        self.destroy()


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        sys.exit("Precisa de Python 3.9 ou mais novo.")
    App().mainloop()
