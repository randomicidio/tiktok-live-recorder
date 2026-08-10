"""Gravacao da live via ffmpeg.

Estrategia:
  1. Grava em .ts (MPEG-TS). Formato append-only: se faltar luz ou o processo
     morrer, o que ja foi para o disco continua valido.
  2. Se a conexao cair no meio da live, abre uma nova parte com URLs frescas
     (as URLs do TikTok sao assinadas e expiram).
  3. Ao terminar, junta as partes num .mp4 por copia pura - segundos, mesmo
     numa live de horas.

O video nunca e recodificado: o custo de CPU e o de um download. O audio sim:
a TikTok entrega HE-AAC, que editores como o DaVinci Resolve nao leem, entao
ele e convertido para AAC-LC ja durante a captura (~2% de um nucleo). Assim o
.ts que esta no disco ja e o audio final, e o passo 3 nao precisa converter
nada. Ver `_run_ffmpeg` e `_remux`.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import gift_log
import pacote
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


# ---------------------------------------------------------------- energia

# Sinalizadores do SetThreadExecutionState (winbase.h).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class Vigilia:
    """Impede o computador de dormir enquanto uma gravacao esta correndo.

    O caso de uso central do programa e armar o REC e sair de casa, e e
    exatamente ai que o Windows suspende sozinho depois de meia hora e mata a
    captura. So o sono do sistema e barrado: a tela pode apagar normalmente.

    No Windows o estado vale para a thread que o pediu, entao `ligar` e
    `desligar` precisam sair da mesma thread - a do laco de gravacao.
    """

    def __init__(self, emit=None):
        self.emit = emit
        self._ligada = False
        self._proc: subprocess.Popen | None = None

    def ligar(self) -> None:
        if self._ligada:
            return
        try:
            if resources.IS_WINDOWS:
                import ctypes
                anterior = ctypes.windll.kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
                if anterior == 0:
                    raise OSError("SetThreadExecutionState recusou o pedido")
            elif resources.IS_MAC:
                # -i barra o sono por ociosidade; o processo morre junto com
                # este, entao nao fica nada pendurado se o programa cair.
                self._proc = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())])
            else:
                return
        except (OSError, AttributeError) as e:
            if self.emit:
                self.emit("log", f"Não consegui impedir a suspensão do sistema: {e}")
            return
        self._ligada = True

    def desligar(self) -> None:
        if not self._ligada:
            return
        self._ligada = False
        try:
            if resources.IS_WINDOWS:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            elif self._proc is not None:
                self._proc.terminate()
                self._proc = None
        except (OSError, AttributeError):
            pass          # voltar ao normal nao pode derrubar o fechamento


# Uma live 1080x1920 a ~2,5 Mbps rende por volta de 1,1 GB por hora. Cinco GB
# sao uma tarde inteira de transmissao; abaixo disso vale avisar antes de armar.
ESPACO_CONFORTAVEL = 5 * 1024 ** 3
ESPACO_CRITICO = 1024 ** 3

# Bytes por segundo usados para estimar quanto tempo ainda cabe no disco.
_BYTES_POR_SEGUNDO = 2.5 * 1024 ** 2 / 8


def espaco_livre(pasta: str) -> int:
    """Bytes livres onde as gravacoes vao cair, ou -1 se nao der para saber."""
    try:
        return shutil.disk_usage(pasta).free
    except OSError:
        return -1


def horas_que_cabem(bytes_livres: int) -> float:
    """Quantas horas de live cabem no espaco que sobrou."""
    if bytes_livres < 0:
        return 0.0
    return bytes_livres / _BYTES_POR_SEGUNDO / 3600


def sanitize(name: str, fallback: str = "live") -> str:
    """Transforma um titulo de live em algo que o Windows aceite como arquivo."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:70] or fallback


@functools.lru_cache(maxsize=1)
def tem_encoder_aac() -> bool:
    """A build de ffmpeg em uso sabe codificar AAC?

    O encoder nativo vem em qualquer build normal, mas a gravacao inteira passa
    a depender dele: melhor perguntar uma vez, na largada, do que descobrir no
    meio de uma live.
    """
    try:
        res = _run_quiet([_ffmpeg(), "-hide_banner", "-encoders"], timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return res.returncode == 0 and re.search(r"^\s*\S*A\S*\s+aac\s", res.stdout, re.M) is not None


def _tem_bytes(caminho: str) -> bool:
    try:
        return os.path.getsize(caminho) > 0
    except OSError:
        return False


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
        # Audio da sessao: se a captura ja gravou em AAC-LC, o fechamento e uma
        # copia instantanea; senao, a conversao acontece la (ver `_remux`).
        self._converter_audio = True
        self._audio_convertido = False
        # Acompanhados pela interface para mostrar tempo decorrido e tamanho.
        self.session_started: datetime | None = None
        # Partes .ts desta sessao. Substituiu a varredura de uma subpasta: com
        # as partes soltas na pasta de saida, listar o diretorio somaria tambem
        # as partes orfas de gravacoes interrompidas antes desta.
        self._partes: list[str] = []

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
        total = 0
        for caminho in list(self._partes):
            try:
                total += os.path.getsize(caminho)
            except OSError:
                pass  # parte removida ou trocada no meio da soma
        return total

    @property
    def partes_ativas(self) -> list[str]:
        """Partes .ts que esta sessao esta escrevendo agora.

        A varredura de gravacoes interrompidas roda em thread na abertura; se o
        REC automatico armar e comecar a gravar no meio dela, a sessao nova nao
        pode ser oferecida como resgate.
        """
        return list(self._partes)

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
        # Algumas respostas do TikTok mantêm a sala como "ao vivo" por um
        # tempo depois do encerramento, mesmo quando a URL já não transmite.
        # Nunca reabrimos uma gravação para a mesma sala até vê-la offline.
        sala_encerrada = ""

        # A vigilia cobre o ciclo armado inteiro, nao so a captura: se o
        # computador dormir enquanto espera a live comecar, a verificacao
        # periodica para e a live e perdida do mesmo jeito. Sai daqui porque no
        # Windows o estado pertence a thread que o pediu.
        vigilia = Vigilia(self.emit)
        vigilia.ligar()

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
                    if sala_encerrada and info.room_id == sala_encerrada:
                        if not announced_wait:
                            self.emit("log", "A live anterior já foi encerrada; aguardando o TikTok confirmar o fim.")
                            announced_wait = True
                        self.emit("status", ("aguardando", "Aguardando a próxima live"))
                        if self._stop.wait(poll_seconds):
                            break
                        continue
                    self.emit("log", f"@{username} está AO VIVO. Iniciando gravação.")
                    self._record_session(info, username, outdir, quality, session)
                    sala_encerrada = info.room_id
                    announced_wait = False
                    if not wait_for_live:
                        break
                    if self._stop.is_set():
                        break
                    self.emit("status", ("aguardando", "Aguardando a próxima live"))
                else:
                    # Só uma confirmação offline libera a mesma conta/sala
                    # para uma próxima gravação.
                    sala_encerrada = ""
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
            vigilia.desligar()
            self.recording = False
            self.session_started = None
            self._partes = []
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

        # As partes ficam soltas na pasta de saida, com o nome da gravacao no
        # proprio arquivo. Numa subpasta elas sumiam da vista quando a gravacao
        # era interrompida; aqui um `..._parte01.ts` ao lado dos .mp4 se explica
        # sozinho, e e o que o resgate procura na abertura seguinte.
        os.makedirs(outdir, exist_ok=True)

        # `parts` guarda so as partes que renderam bytes - e o que vai para o
        # remux. `_partes` acompanha tambem a que esta sendo escrita agora,
        # senao o tamanho na tela ficaria parado ate a parte fechar.
        parts: list[str] = []
        self._partes = []
        part_no = 0
        falhas_sem_video = 0

        # Converter o audio ja na captura e o caminho normal; so nao dá se a
        # build de ffmpeg nao tiver o encoder, e ai a conversao volta a ser
        # feita no fechamento (mais lenta, mas o resultado e o mesmo).
        self._converter_audio = tem_encoder_aac()
        self._audio_convertido = False
        if not self._converter_audio:
            self.emit("log", "Esta build do ffmpeg não codifica AAC; o áudio "
                             "será convertido só no fim da gravação.")

        self.recording = True
        self.session_started = started

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
            # A prévia é um segundo leitor, opcional, da mesma URL; ela não
            # participa do ffmpeg nem muda o arquivo que está sendo gravado.
            self.emit("preview", option.best_url)
            part_path = os.path.join(outdir, f"{base}_parte{part_no:02d}.ts")
            self.current_file = part_path
            self._partes.append(part_path)
            self.emit(
                "status",
                (
                    "gravando",
                    "GRAVANDO  -  "
                    + tiktok_api.QUALITY_LABELS.get(option.quality, option.quality)
                    + (f" ({option.resolution})" if option.resolution else ""),
                ),
            )
            # Sem `origin` nao ha o que anunciar: a faixa escolhida ja e a
            # unica que a live oferece, e dizer isso so assusta quem le o log.
            nota = "  (sem recompressão)" if option.quality == "origin" else ""
            self.emit("log", f"Parte {part_no}: {option.label}{nota}")

            code = self._run_ffmpeg(option.best_url, part_path, self._converter_audio)

            # Se a primeira parte com conversao nao rendeu um byte, a suspeita
            # recai sobre o encoder: tenta de novo sem ele antes de contabilizar
            # falha. Sem esta rede, um encoder problematico esvaziaria duas
            # partes seguidas e o guarda de `falhas_sem_video` abortaria a live
            # inteira. Depois que o encoder provou funcionar uma vez, parte
            # vazia e problema de rede, e nao mexemos mais nisso.
            if (self._converter_audio and not self._audio_convertido
                    and not self._stop.is_set() and not _tem_bytes(part_path)):
                self.emit("log", "A conversão de áudio não funcionou nesta live; "
                                 "gravando sem converter.")
                self._converter_audio = False
                code = self._run_ffmpeg(option.best_url, part_path, False)
            elif self._converter_audio and _tem_bytes(part_path):
                self._audio_convertido = True

            if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                parts.append(part_path)
                mb = os.path.getsize(part_path) / (1024 * 1024)
                self.emit("log", f"Parte {part_no} fechada com {mb:.1f} MB.")
                falhas_sem_video = 0
            else:
                self.emit("log", f"Parte {part_no} saiu vazia (código {code}).")
                falhas_sem_video += 1

            if self._stop.is_set():
                break

            # A sala pode permanecer marcada como live por alguns minutos
            # depois que o CDN para. Duas tentativas consecutivas sem um único
            # byte são evidência suficiente para salvar o que existe, em vez
            # de deixar o REC preso em erros de reconexão.
            if falhas_sem_video >= 2:
                self.emit("log", "A transmissão deixou de entregar vídeo. Encerrando e salvando a gravação.")
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
        self.emit("preview", "")
        self._finalize(parts, outdir, base, started)

        self.session_started = None
        self._partes = []

    def _run_ffmpeg(self, url: str, dest: str, converter_audio: bool = True) -> int:
        """Grava o stream em `dest` (.ts).

        O video vai sempre por copia. O audio e reencodado de HE-AAC para
        AAC-LC ja aqui, durante a captura: custa ~2% de um nucleo e faz o
        fechamento do arquivo continuar sendo uma copia instantanea, em vez de
        minutos de conversao no fim de uma live longa.

        Isso nao abre mao da rede de seguranca: o destino continua sendo
        MPEG-TS, append-only, valido mesmo se o processo morrer no meio.
        """
        if converter_audio:
            # aresample=async=1 preenche com silencio os buracos que a rede
            # deixa no audio. Sem ele, o audio reencodado "encolhe" a cada
            # perda e sai andando na frente do video, que e copia crua.
            saida_audio = ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-af", "aresample=async=1"]
        else:
            saida_audio = ["-c", "copy"]

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
            *saida_audio,
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
        outdir: str,
        base: str,
        started: datetime,
    ) -> None:
        """Junta as partes num unico .mp4 e limpa os temporarios."""
        if not parts:
            self.emit("log", "Nada foi gravado nesta sessão.")
            return

        self.emit("status", ("finalizando", "Finalizando o arquivo..."))
        final_path = os.path.join(outdir, f"{base}.mp4")

        ok = remontar(parts, final_path, self._converter_audio, self.emit)

        if ok:
            mb = os.path.getsize(final_path) / (1024 * 1024)
            dur = datetime.now() - started
            minutes = int(dur.total_seconds() // 60)
            self.emit("log", f"Pronto: {os.path.basename(final_path)} - {mb:.1f} MB, ~{minutes} min.")
            descartar_partes(parts)
            # O pacote aponta para o .mp4, portanto só é fechado depois do
            # remux. Ele precisa terminar (e escrever os próprios avisos no
            # registro) antes do cartão/miniatura aparecer como conclusão.
            self._finalizar_registro_presente()
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
            # O .ts continua assistivel; melhor guardar que descartar. Ficam
            # onde estao para o resgate da proxima abertura reencontra-los.
            self.emit(
                "log",
                "Não consegui gerar o .mp4. As partes .ts estão intactas em "
                f"{outdir} e o programa vai oferecer o resgate ao abrir de novo.",
            )
            self._finalizar_registro_presente()
            self.emit("done", SessionResult(parts=parts, started_at=started))

    def _finalizar_registro_presente(self) -> None:
        """Fecha o .ttgifts antes de anunciar visualmente a conclusão."""
        if self._gift_log is not None and self._gift_log.ativo:
            self.emit("status", ("finalizando", "Preparando o pacote de presentes e chat..."))
            self._gift_log.stop()


def remontar(parts: list[str], final_path: str, converter_audio: bool,
             emit) -> bool:
    """Junta as partes .ts num .mp4.

    No caminho normal e uma copia pura, instantanea: as partes ja saem da
    captura com o audio em AAC-LC. So quando a captura nao pôde converter
    (build sem encoder) a conversao acontece aqui, e ai leva minutos.

    Funcao de modulo, e nao metodo, porque o resgate de uma gravacao
    interrompida precisa dela sem ter um `Recorder` com sessao viva.
    """
    listfile = final_path + ".partes.txt"
    try:
        with open(listfile, "w", encoding="utf-8") as fh:
            for p in parts:
                safe = p.replace("\\", "/").replace("'", "'\\''")
                fh.write(f"file '{safe}'\n")
    except OSError as e:
        emit("log", f"Não consegui montar a lista de partes: {e}")
        return False

    entrada = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", listfile,
    ]
    saida = ["-movflags", "+faststart", "-y", final_path]

    # aac_adtstoasc e necessario para AAC vindo de TS; se a build reclamar,
    # tenta de novo sem o filtro.
    copiar = [
        ["-c", "copy", "-bsf:a", "aac_adtstoasc"],
        ["-c", "copy"],
    ]
    reencodava_aqui = not converter_audio
    if converter_audio:
        # O caminho normal: as partes ja estao em AAC-LC, entao juntar e so
        # copiar - questao de segundos, mesmo numa live de horas.
        tentativas = copiar
    else:
        # A captura nao pôde converter, entao a conversao acontece aqui.
        # Mais lenta (~1 min por hora de live), mesmo resultado.
        emit("status", ("finalizando", "Convertendo o áudio..."))
        tentativas = [
            ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"],
        ] + copiar

    # Reencodar audio de uma live longa leva minutos; o teto fixo de 15 min
    # do _run_quiet nao serve. Reserva 1s por MB, com piso generoso.
    try:
        total_mb = sum(os.path.getsize(p) for p in parts) / (1024 * 1024)
    except OSError:
        total_mb = 0
    limite = max(900, int(total_mb))

    try:
        for idx, meio in enumerate(tentativas):
            try:
                res = _run_quiet(entrada + meio + saida, timeout=limite)
            except (subprocess.TimeoutExpired, OSError) as e:
                emit("log", f"Remux falhou: {e}")
                return False
            if res.returncode == 0 and os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                # So avisa quando a conversao era pra acontecer aqui e falhou;
                # nas outras vezes o audio ja saiu convertido da captura.
                if reencodava_aqui and idx > 0:
                    emit(
                        "log",
                        "Aviso: não consegui converter o áudio para AAC-LC. O arquivo "
                        "está completo, mas alguns editores (DaVinci Resolve) podem "
                        "não reconhecer a faixa de áudio.",
                    )
                return True
            if res.stderr:
                emit("log", f"Remux: {res.stderr.strip().splitlines()[-1][:200]}")
        return False
    finally:
        # A lista fica ao lado do .mp4; sem isso um `.partes.txt` sobraria na
        # pasta de saida a cada gravacao.
        try:
            os.remove(listfile)
        except OSError:
            pass


def descartar_partes(parts: list[str]) -> None:
    """Apaga as partes .ts depois que o .mp4 saiu inteiro."""
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    # Gravacoes antigas guardavam as partes numa subpasta; se ela ficou vazia,
    # sai junto. Vazia so: uma pasta com qualquer outra coisa dentro fica.
    for pasta in {os.path.dirname(p) for p in parts}:
        try:
            if pasta and not os.listdir(pasta):
                os.rmdir(pasta)
        except OSError:
            pass


# -------------------------------------------------------------------- resgate

# Partes soltas na pasta de saida (formato atual) e o nome que elas tinham
# dentro da subpasta (gravacoes feitas antes desta versao).
_PARTE_SOLTA = re.compile(r"^(?P<base>.+)_parte\d{2,}\.ts$", re.IGNORECASE)
_PARTE_ANTIGA = re.compile(r"^parte\d{2,}\.ts$", re.IGNORECASE)
_SUFIXO_JSONL = "_presentes.jsonl"


@dataclass
class Interrompida:
    """Uma gravacao que ficou pela metade, esperando resgate."""

    base: str = ""                              # nome sem extensao
    outdir: str = ""
    partes: list[str] = field(default_factory=list)
    jsonl: str = ""
    bytes_total: int = 0
    quando: datetime | None = None

    @property
    def destino(self) -> str:
        return os.path.join(self.outdir, f"{self.base}.mp4")


def _quando_de(base: str, referencia: str) -> datetime | None:
    """Data da gravacao pelo nome (`conta_AAAA-MM-DD_HH-MM_titulo`).

    O nome e a fonte certa: a data de modificacao do arquivo muda se alguem
    copiar a pasta. So cai para o mtime quando o nome nao segue o padrao.
    """
    achado = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})", base)
    if achado:
        try:
            return datetime.strptime(
                f"{achado.group(1)} {achado.group(2)}:{achado.group(3)}",
                "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(referencia))
    except OSError:
        return None


def procurar_interrompidas(outdir: str) -> list[Interrompida]:
    """Acha gravacoes que nao chegaram a virar .mp4 (ou .ttgifts).

    Varredura rasa e sem ffprobe: um scandir da pasta de saida, com os tamanhos
    vindo da propria enumeracao de diretorio. Le so o que a entrada ja informa,
    para poder rodar na abertura do programa sem custo perceptivel.
    """
    if not outdir or not os.path.isdir(outdir):
        return []

    partes: dict[str, list[str]] = {}
    tamanhos: dict[str, int] = {}
    jsonls: dict[str, str] = {}
    prontos: set[str] = set()          # ja tem .mp4
    empacotados: set[str] = set()      # ja tem .ttgifts

    try:
        entradas = list(os.scandir(outdir))
    except OSError:
        return []

    for entrada in entradas:
        nome = entrada.name
        try:
            e_dir = entrada.is_dir()
        except OSError:
            continue

        if e_dir:
            # Formato antigo: subpasta com o nome da gravacao e parteNN.ts.
            try:
                internos = [n for n in os.listdir(entrada.path)
                            if _PARTE_ANTIGA.match(n)]
            except OSError:
                continue
            for n in sorted(internos):
                caminho = os.path.join(entrada.path, n)
                partes.setdefault(nome, []).append(caminho)
                try:
                    tamanhos[nome] = tamanhos.get(nome, 0) + os.path.getsize(caminho)
                except OSError:
                    pass
            continue

        if nome.lower().endswith(".mp4"):
            prontos.add(nome[:-4])
        elif nome.lower().endswith(pacote.EXTENSAO):
            empacotados.add(nome[: -len(pacote.EXTENSAO)])
        elif nome.endswith(_SUFIXO_JSONL):
            jsonls[nome[: -len(_SUFIXO_JSONL)]] = entrada.path
        else:
            achado = _PARTE_SOLTA.match(nome)
            if achado:
                base = achado.group("base")
                partes.setdefault(base, []).append(entrada.path)
                try:
                    tamanhos[base] = tamanhos.get(base, 0) + entrada.stat().st_size
                except OSError:
                    pass

    achadas: list[Interrompida] = []
    for base in sorted(set(partes) | set(jsonls)):
        # Ha .ts sem .mp4, ou registro cru sem pacote? Se as duas metades ja
        # existem, a gravacao terminou bem e nao ha o que resgatar.
        falta_video = base in partes and base not in prontos
        falta_pacote = base in jsonls and base not in empacotados
        if not falta_video and not falta_pacote:
            continue
        lista = sorted(partes.get(base, [])) if falta_video else []
        referencia = lista[0] if lista else jsonls.get(base, "")
        achadas.append(Interrompida(
            base=base,
            outdir=outdir,
            partes=lista,
            jsonl=jsonls.get(base, "") if falta_pacote else "",
            bytes_total=tamanhos.get(base, 0) if falta_video else 0,
            quando=_quando_de(base, referencia),
        ))
    return achadas


def _audio_ja_convertido(parte: str) -> bool:
    """A parte .ts ja esta em AAC-LC?

    A captura normalmente converte HE-AAC para AAC-LC durante a gravacao, mas
    uma sessao interrompida pode ter caido no caminho alternativo. Perguntar ao
    arquivo custa ~30 ms e evita entregar um MP4 que o DaVinci nao abre.
    """
    try:
        res = _run_quiet([
            resources.find_binary("ffprobe") or "ffprobe",
            "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=profile", "-of", "csv=p=0", parte,
        ], timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return True          # na duvida, copia: um remux rapido e reversivel
    perfil = (res.stdout or "").strip().upper()
    if not perfil:
        return True
    return "HE-AAC" not in perfil and "HE_AAC" not in perfil


def recuperar(item: Interrompida, emit) -> SessionResult:
    """Monta o .mp4 e o .ttgifts de uma gravacao interrompida."""
    resultado = SessionResult(parts=list(item.partes), started_at=item.quando)
    destino = item.destino

    if item.partes:
        emit("status", ("finalizando", "Remontando a gravação interrompida..."))
        emit("log", f"Resgate: juntando {len(item.partes)} parte(s) de "
                    f"{os.path.basename(item.base)}.")
        ok = remontar(item.partes, destino, _audio_ja_convertido(item.partes[0]), emit)
        if not ok:
            emit("log", "Não consegui remontar as partes; elas continuam intactas "
                        "na pasta.")
            emit("status", ("erro", "Falha no resgate"))
            return resultado
        descartar_partes(item.partes)
        resultado.final_path = destino
        try:
            resultado.bytes_written = os.path.getsize(destino)
        except OSError:
            pass
        mb = resultado.bytes_written / (1024 * 1024)
        emit("log", f"Resgate: {os.path.basename(destino)} - {mb:.1f} MB.")
    elif os.path.exists(destino):
        # So faltava o pacote: o video ja estava inteiro.
        resultado.final_path = destino
        try:
            resultado.bytes_written = os.path.getsize(destino)
        except OSError:
            pass

    if item.jsonl and os.path.exists(item.jsonl):
        emit("status", ("finalizando", "Recuperando presentes e chat..."))
        try:
            caminho = gift_log.recuperar(item.jsonl, resultado.final_path or destino,
                                         emit)
        except Exception as e:                    # noqa: BLE001
            emit("log", f"Não consegui recuperar o pacote de presentes: {e}")
            caminho = ""
        if not caminho:
            emit("log", "O registro de presentes e chat não rendeu um pacote "
                        "(pode não ter havido nenhum evento).")

    if resultado.final_path:
        emit("status", ("finalizando", "Gerando a miniatura..."))
        resultado.thumb_path = make_thumbnail(resultado.final_path)
        # `ended_at` fica vazio de proposito: a duracao do SessionResult e
        # inicio-a-fim, e aqui o "fim" seria agora - dias depois da live. O
        # cartao entao mostra so o tamanho, que e verdade.
    emit("status", ("parado", "Parado"))
    return resultado
