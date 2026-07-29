"""Gravacao da live via ffmpeg, sem re-encode.

Estrategia:
  1. Grava em .ts (MPEG-TS). Formato append-only: se faltar luz ou o processo
     morrer, o que ja foi para o disco continua valido.
  2. Se a conexao cair no meio da live, abre uma nova parte com URLs frescas
     (as URLs do TikTok sao assinadas e expiram).
  3. Ao terminar, junta as partes e remuxa para .mp4 com `-c copy`.

Em nenhum momento ha recodificacao: o custo de CPU e o de um download.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import gift_log
import resources
import tiktok_api

# Evita abrir janelas de console a cada chamada do ffmpeg no Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Grupo proprio para o ffmpeg: assim um Ctrl+C no console (quando aberto pelo
# Gravador.bat) interrompe a interface sem matar a gravacao pelo meio.
_OWN_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

# Espera minima entre tentativas de religar uma parte interrompida.
RETRY_DELAY = 4


def find_ffmpeg() -> str | None:
    """Prefere o ffmpeg embutido no pacote; cai para o do sistema."""
    return resources.find_binary("ffmpeg")


def _ffmpeg() -> str:
    """Caminho a usar nas chamadas. Nunca confia no PATH quando empacotado."""
    return find_ffmpeg() or "ffmpeg"


def sanitize(name: str, fallback: str = "live") -> str:
    """Transforma um titulo de live em algo que o Windows aceite como arquivo."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:70] or fallback


def _run_quiet(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


@dataclass
class SessionResult:
    """Resultado de uma sessao de gravacao ja encerrada."""

    final_path: str = ""
    parts: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    bytes_written: int = 0
    thumb_path: str = ""

    @property
    def duration_seconds(self) -> int:
        if not self.started_at or not self.ended_at:
            return 0
        return max(0, int((self.ended_at - self.started_at).total_seconds()))


def make_thumbnail(video_path: str, height: int = 76) -> str:
    """Extrai um quadro do video para servir de miniatura no registro.

    Devolve o caminho do .png, ou "" se nao der. Uma unica chamada ao ffmpeg por
    gravacao concluida - nao entra no caminho da gravacao em si.
    """
    if not video_path or not os.path.exists(video_path):
        return ""
    dest_dir = os.path.join(resources.data_dir(), ".thumbs")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return ""
    dest = os.path.join(dest_dir, os.path.splitext(os.path.basename(video_path))[0] + ".png")

    # Tenta alguns segundos adiante (evita o preto do inicio) e cai para o
    # primeiro quadro se o video for curto demais.
    for seek in ("6", "1", "0"):
        args = [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-ss", seek, "-i", video_path,
            "-frames:v", "1", "-vf", f"scale=-2:{height}",
            "-y", dest,
        ]
        try:
            res = _run_quiet(args, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        if res.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
    return ""


class Recorder:
    """Controla o ciclo monitorar -> gravar -> finalizar.

    Toda comunicacao com a interface passa por `emit`, chamado de dentro das
    threads de trabalho. Quem consome deve enfileirar e tratar na thread da GUI.
    Os eventos "status" viajam como (estado, texto) para a interface saber que
    cor mostrar sem ter que adivinhar pelo texto.
    """

    def __init__(self, emit):
        self.emit = emit
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.recording = False
        self.current_file = ""
        # Acompanhados pela interface para mostrar tempo decorrido e tamanho.
        self.session_started: datetime | None = None
        self.session_dir = ""

        # Registro de presentes e chat, gravado junto e salvo num .ttgifts.
        # Sem a biblioteca TikTokLive instalada ele simplesmente não liga.
        self.registrar_presentes = True
        self._gift_log = gift_log.GiftLogger(emit) if gift_log.DISPONIVEL else None

    def session_size(self) -> int:
        """Bytes ja gravados na sessao atual, somando todas as partes.

        Consulta os.path.getsize em cada caminho de proposito. Com os.scandir o
        tamanho vem da entrada de diretorio, que o Windows nao atualiza enquanto
        o ffmpeg mantem o arquivo aberto: o contador ficava parado em 0 MB e so
        andava quando algum outro processo por acaso tocava no arquivo.
        """
        if not self.session_dir:
            return 0
        try:
            nomes = os.listdir(self.session_dir)
        except OSError:
            return 0
        total = 0
        for nome in nomes:
            if nome.endswith(".ts"):
                try:
                    total += os.path.getsize(os.path.join(self.session_dir, nome))
                except OSError:
                    pass  # parte removida ou trocada no meio da soma
        return total

    # ---------------------------------------------------------------- estado

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- controles

    def start(
        self,
        username: str,
        outdir: str,
        quality: str = "origin",
        cookie: str = "",
        poll_seconds: int = 30,
        wait_for_live: bool = True,
    ) -> None:
        """Inicia o monitoramento (ou a gravacao imediata, se ja estiver ao vivo)."""
        if self.active:
            self.emit("log", "Já existe um monitoramento rodando.")
            return
        if not find_ffmpeg():
            self.emit("error", "ffmpeg não encontrado no PATH.")
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            args=(username, outdir, quality, cookie, poll_seconds, wait_for_live),
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Pede parada e encerra o ffmpeg com elegancia."""
        self._stop.set()
        self._terminate_proc()

    @property
    def presentes_registrados(self) -> int:
        """Quantos presentes o registro já viu nesta sessão."""
        return self._gift_log.total if self._gift_log else 0

    def _terminate_proc(self) -> None:
        """Encerra o ffmpeg rapidamente, sem medo de truncar.

        Testado neste Windows: rodando sem console, o ffmpeg ignora tanto o 'q'
        pelo stdin quanto o CTRL_BREAK (o sinal nao chega a um processo que nao
        compartilha console). Esperar por eles custava 16s a cada clique em
        Parar, sem beneficio nenhum.

        Encerrar a forca e seguro aqui porque a gravacao vai para MPEG-TS, que e
        append-only: o que ja esta no disco continua valido e o remux para .mp4
        conserta os indices no final. Damos so um instante para o 'q', caso o
        ffmpeg esteja num modo em que ele funcione, e seguimos em frente.
        """
        with self._lock:
            proc = self._proc
        if not proc or proc.poll() is not None:
            return

        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            pass

        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    # ------------------------------------------------------------- monitor

    def _monitor_loop(
        self,
        username: str,
        outdir: str,
        quality: str,
        cookie: str,
        poll_seconds: int,
        wait_for_live: bool,
    ) -> None:
        session = tiktok_api.make_session(cookie)
        os.makedirs(outdir, exist_ok=True)
        announced_wait = False

        try:
            while not self._stop.is_set():
                try:
                    info = tiktok_api.resolve(username, session)
                except tiktok_api.TikTokError as e:
                    self.emit("log", f"Consulta falhou: {e}")
                    self.emit("status", ("erro", "Erro na consulta - tentando de novo"))
                    if self._stop.wait(poll_seconds):
                        break
                    continue

                if info.is_live:
                    self.emit("log", f"@{username} está AO VIVO. Iniciando gravação.")
                    self._record_session(info, username, outdir, quality, session)
                    announced_wait = False
                    if not wait_for_live:
                        break
                    if self._stop.is_set():
                        break
                    self.emit("status", ("aguardando", "Aguardando a próxima live"))
                else:
                    if not wait_for_live:
                        self.emit("status", ("offline", "Offline"))
                        self.emit("log", f"@{username} não está ao vivo agora.")
                        break
                    if not announced_wait:
                        self.emit("log", f"@{username} offline. Verificando a cada {poll_seconds}s.")
                        announced_wait = True
                    self.emit("status", ("aguardando", "Esperando live iniciar"))

                if self._stop.wait(poll_seconds):
                    break
        finally:
            self.recording = False
            self.session_started = None
            self.session_dir = ""
            self.emit("status", ("parado", "Parado"))
            self.emit("finished", "")

    # ------------------------------------------------------------- gravacao

    def _record_session(
        self,
        info: tiktok_api.LiveInfo,
        username: str,
        outdir: str,
        quality: str,
        session,
    ) -> None:
        """Grava uma live inteira, abrindo novas partes se a conexao cair."""
        started = datetime.now()
        stamp = started.strftime("%Y-%m-%d_%H-%M")
        title = sanitize(info.title) if info.title else ""
        base = f"{username}_{stamp}" + (f"_{title}" if title else "")

        workdir = os.path.join(outdir, base)
        os.makedirs(workdir, exist_ok=True)

        parts: list[str] = []
        part_no = 0
        self.recording = True
        self.session_started = started
        self.session_dir = workdir

        # O registro de presentes usa o MESMO instante zero da gravação: e o
        # que permite casar cada animação com o segundo certo do vídeo depois.
        if self.registrar_presentes and self._gift_log is not None:
            self._gift_log.start(username, os.path.join(outdir, base), started)

        while not self._stop.is_set():
            option = info.pick(quality)
            if not option:
                self.emit("log", "Nenhuma URL de vídeo disponível nesta live.")
                break

            part_no += 1
            part_path = os.path.join(workdir, f"parte{part_no:02d}.ts")
            self.current_file = part_path
            self.emit(
                "status",
                (
                    "gravando",
                    "GRAVANDO  -  "
                    + tiktok_api.QUALITY_LABELS.get(option.quality, option.quality)
                    + (f" ({option.resolution})" if option.resolution else ""),
                ),
            )
            # "Média (HD)" e um nome de faixa do TikTok, nao um julgamento: se
            # a live nao oferece `origin`, essa PODE ser a melhor que existe.
            if option.quality == "origin":
                nota = "original, sem recompressão"
            else:
                nota = ("melhor disponível — esta live não está oferecendo a "
                        "qualidade original")
            self.emit("log", f"Parte {part_no}: {option.label} ({nota})")

            code = self._run_ffmpeg(option.best_url, part_path)

            if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                parts.append(part_path)
                mb = os.path.getsize(part_path) / (1024 * 1024)
                self.emit("log", f"Parte {part_no} fechada com {mb:.1f} MB.")
            else:
                self.emit("log", f"Parte {part_no} saiu vazia (código {code}).")

            if self._stop.is_set():
                break

            # A live acabou de verdade ou foi so a conexao que caiu?
            time.sleep(RETRY_DELAY)
            try:
                info = tiktok_api.resolve(username, session)
            except tiktok_api.TikTokError as e:
                self.emit("log", f"Não deu para reconsultar: {e}")
                break
            if not info.is_live:
                self.emit("log", "A live terminou.")
                break
            self.emit("log", "Conexão caiu mas a live continua. Reconectando...")

        self.recording = False
        self.current_file = ""
        self._finalize(parts, workdir, outdir, base, started)

        # Só depois do .mp4 existir: o pacote aponta para ele pelo nome.
        if self._gift_log is not None and self._gift_log.ativo:
            self._gift_log.stop()

        self.session_started = None
        self.session_dir = ""

    def _run_ffmpeg(self, url: str, dest: str) -> int:
        """Copia o stream para `dest`. Sem encode: `-c copy`."""
        args = [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel", "error",
            "-user_agent", tiktok_api.WEB_UA,
            "-headers", "Referer: https://www.tiktok.com/\r\n",
            # Religa sozinho em soluços curtos de rede.
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            # Desiste da leitura apos 20s sem dados (microssegundos).
            "-rw_timeout", "20000000",
            "-i", url,
            "-c", "copy",
            "-f", "mpegts",
            "-y",
            dest,
        ]
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW | _OWN_GROUP,
            )
        except OSError as e:
            self.emit("error", f"Falha ao iniciar o ffmpeg: {e}")
            return -1

        with self._lock:
            self._proc = proc

        _, err = proc.communicate()

        with self._lock:
            self._proc = None

        if proc.returncode not in (0, 255) and err:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            if detail:
                self.emit("log", f"ffmpeg: {detail[-1][:200]}")
        return proc.returncode

    # ------------------------------------------------------------ finalizar

    def _finalize(
        self,
        parts: list[str],
        workdir: str,
        outdir: str,
        base: str,
        started: datetime,
    ) -> None:
        """Junta as partes num unico .mp4 e limpa os temporarios."""
        if not parts:
            self.emit("log", "Nada foi gravado nesta sessão.")
            try:
                os.rmdir(workdir)
            except OSError:
                pass
            return

        self.emit("status", ("finalizando", "Finalizando o arquivo..."))
        final_path = os.path.join(outdir, f"{base}.mp4")

        ok = self._remux(parts, final_path, workdir)

        if ok:
            mb = os.path.getsize(final_path) / (1024 * 1024)
            dur = datetime.now() - started
            minutes = int(dur.total_seconds() // 60)
            self.emit("log", f"Pronto: {os.path.basename(final_path)} - {mb:.1f} MB, ~{minutes} min.")
            shutil.rmtree(workdir, ignore_errors=True)
            self.emit("status", ("finalizando", "Gerando a miniatura..."))
            thumb = make_thumbnail(final_path)
            self.emit(
                "done",
                SessionResult(
                    final_path=final_path,
                    parts=parts,
                    started_at=started,
                    ended_at=datetime.now(),
                    bytes_written=os.path.getsize(final_path),
                    thumb_path=thumb,
                ),
            )
        else:
            # O .ts continua assistivel; melhor guardar que descartar.
            self.emit(
                "log",
                f"Não consegui gerar o .mp4. Os arquivos .ts estão intactos em: {workdir}",
            )
            self.emit("done", SessionResult(parts=parts, started_at=started))

    def _remux(self, parts: list[str], final_path: str, workdir: str) -> bool:
        """TS -> MP4 por copia de fluxo (rapido, sem perda)."""
        listfile = os.path.join(workdir, "partes.txt")
        try:
            with open(listfile, "w", encoding="utf-8") as fh:
                for p in parts:
                    safe = p.replace("\\", "/").replace("'", "'\\''")
                    fh.write(f"file '{safe}'\n")
        except OSError as e:
            self.emit("log", f"Não consegui montar a lista de partes: {e}")
            return False

        base_args = [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listfile,
            "-c", "copy", "-movflags", "+faststart", "-y",
        ]
        # aac_adtstoasc e necessario para AAC vindo de TS; se a build reclamar,
        # tenta de novo sem o filtro.
        for extra in (["-bsf:a", "aac_adtstoasc"], []):
            try:
                res = _run_quiet(base_args[:-1] + extra + ["-y", final_path])
            except (subprocess.TimeoutExpired, OSError) as e:
                self.emit("log", f"Remux falhou: {e}")
                return False
            if res.returncode == 0 and os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                return True
            if res.stderr:
                self.emit("log", f"Remux: {res.stderr.strip().splitlines()[-1][:200]}")
        return False
