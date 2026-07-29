"""Gera o vídeo final com as camadas escolhidas.

Duas camadas, ambas opcionais:

  * Presentes - as animações oficiais do TikTok, com transparência e som. Elas
    entram enfileiradas: a próxima só começa quando a anterior termina, que é o
    comportamento da live.
  * Chat - desenhado por nós a partir do registro. Não existe arte oficial do
    chat, então é uma reconstrução: fica parecida, não idêntica.

Nada disso roda durante a transmissão. É trabalho de pós, feito quando a
pessoa quiser, sobre arquivos já gravados.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import requests

import pacote as pacote_mod
import recorder
import tipografia

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Medidas do chat, em fração da largura do vídeo. Saíram de uma captura do app
# a 1080 px: em fração porque o replay pode ter qualquer resolução, e é a
# proporção que faz a reconstrução bater com o original.
CHAT_MARGEM = 35 / 1080        # borda esquerda até a foto
CHAT_AVATAR = 76 / 1080        # diâmetro da foto
CHAT_COLUNA = 130 / 1080       # onde o texto começa
CHAT_FONTE = 37 / 1080
CHAT_ENTRELINHA = 56 / 1080    # de uma linha para a seguinte
CHAT_RESPIRO = 30 / 1080       # folga a mais entre uma mensagem e outra
CHAT_TEXTO_MAX = 880 / 1080    # largura antes de quebrar a linha
CHAT_DE_BAIXO = 0.055          # última linha até a base do vídeo (da altura)

CHAT_LINHAS_MSG = 3            # quantas linhas uma mensagem pode ocupar
CHAT_MAX_MENSAGENS = 4         # mensagens na tela ao mesmo tempo
CHAT_DURACAO = 25.0            # segundos que cada mensagem fica
CHAT_SUMICO = 1.5              # tempo do esmaecimento no fim
CHAT_FPS = 15                  # o chat não precisa de 30 fps

# O apelido vem apagado em relação à mensagem, medido na captura.
CHAT_ALFA_APELIDO = 0.55


@dataclass
class Opcoes:
    """O que entra no vídeo exportado."""

    animacoes: bool = True
    chat: bool = False
    volume_animacoes: float = 1.0     # 0.0 a 2.0
    inicio: float = 0.0
    fim: float | None = None          # None = até o fim do vídeo


@dataclass
class Agendado:
    """Um presente já posicionado na linha do tempo do vídeo exportado."""

    presente: object
    arquivo: str
    cfg: dict
    inicio: float
    dur: float

    @property
    def fim(self) -> float:
        return self.inicio + self.dur


def duracao(caminho: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", caminho],
        capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def dimensoes(caminho: str) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", caminho],
        capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
    try:
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return 720, 1280


# ---------------------------------------------------------------- presentes

def agendar(pac: pacote_mod.Pacote, temp: str, largura: int, altura: int,
            inicio: float, fim: float) -> list[Agendado]:
    """Posiciona os presentes na linha do tempo, sem sobrepor um ao outro.

    Na live as animações entram em fila: a seguinte espera a anterior acabar.
    """
    agenda: list[Agendado] = []
    livre = 0.0
    offset = pac.offset_segundos

    for p in sorted(pac.com_animacao, key=lambda x: x.t):
        t = p.t + offset
        if t > fim:
            break
        pasta = pac.extrair_animacao(p, temp)
        if not pasta:
            continue
        cfg = pac.config_da_animacao(p)
        arquivo = os.path.join(pasta, cfg.get("path", ""))
        if not os.path.exists(arquivo):
            continue

        dur = duracao(arquivo)
        if dur <= 0:
            continue
        quando = max(t, livre)
        livre = quando + dur
        if quando + dur < inicio:
            continue                      # acabou antes do trecho exportado
        agenda.append(Agendado(p, arquivo, cfg, quando, dur))
    return agenda


def _cadeia_alfa(cfg: dict, i: int, largura: int, altura: int) -> str:
    """Monta a transparência conforme o config da animação.

    .webm traz o alfa no proprio codec; .mp4 guarda o alfa como outro recorte
    do mesmo quadro, as vezes em escala menor.
    """
    rgb, alfa = cfg.get("rgbFrame"), cfg.get("aFrame")
    if not rgb or not alfa or rgb == alfa:
        return f"[{i}:v]scale={largura}:{altura}"
    rx, ry, rw, rh = rgb
    ax, ay, aw, ah = alfa
    return (f"[{i}:v]crop={rw}:{rh}:{rx}:{ry},setsar=1[rgb{i}];"
            f"[{i}:v]crop={aw}:{ah}:{ax}:{ay},scale={rw}:{rh},format=gray[al{i}];"
            f"[rgb{i}][al{i}]alphamerge,scale={largura}:{altura}")


# --------------------------------------------------------------------- chat

def _baixa_avatares(comentarios, tamanho: int, emit) -> dict[str, object]:
    """Uma foto por pessoa, recortada em círculo. Falha vira None."""
    from PIL import Image, ImageDraw

    cache: dict[str, object] = {}
    sessao = requests.Session()
    urls = {}
    for c in comentarios:
        if c.de and c.avatar and c.de not in urls:
            urls[c.de] = c.avatar

    # A máscara é desenhada grande e reduzida depois: o círculo fica com a
    # borda lisa, sem o serrilhado que o ellipse deixa no tamanho final.
    escala = 4
    mascara = Image.new("L", (tamanho * escala, tamanho * escala), 0)
    ImageDraw.Draw(mascara).ellipse(
        [0, 0, tamanho * escala - 1, tamanho * escala - 1], fill=255)
    mascara = mascara.resize((tamanho, tamanho), Image.LANCZOS)

    for quem, url in urls.items():
        try:
            r = sessao.get(url, timeout=20)
            if r.status_code != 200:
                cache[quem] = None
                continue
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            img = img.resize((tamanho, tamanho), Image.LANCZOS)
            img.putalpha(mascara)
            cache[quem] = img
        except (requests.RequestException, OSError):
            cache[quem] = None
    emit("log", f"Avatares do chat: {sum(1 for v in cache.values() if v)} de {len(urls)}")
    return cache


class PintorDeChat:
    """Desenha o painel do chat num instante qualquer.

    Usado pela prévia (que empurra o painel para a libmpv sobrepor) e pela
    exportação (que o costura em cada quadro). Sendo o mesmo código, o que
    aparece na prévia é exatamente o que sai no arquivo.

    O desenho segue o do app: foto redonda à esquerda, apelido apagado em cima
    e a mensagem em branco embaixo, sem retângulo de fundo - a legibilidade vem
    de uma sombra suave. As mensagens empilham de baixo para cima.
    """

    def __init__(self, pacote, largura: int, altura: int, emit=lambda *a: None):
        self.pacote = pacote
        self.offset = pacote.offset_segundos
        self.largura = int(largura)
        self.altura = int(altura)

        self.margem = round(CHAT_MARGEM * largura)
        self.avatar_d = round(CHAT_AVATAR * largura)
        self.coluna = round(CHAT_COLUNA * largura)
        self.entrelinha = round(CHAT_ENTRELINHA * largura)
        self.respiro = round(CHAT_RESPIRO * largura)
        self.texto_max = CHAT_TEXTO_MAX * largura
        self.tipo = tipografia.Tipografia(round(CHAT_FONTE * largura))

        self.avatares = _baixa_avatares(pacote.comentarios, self.avatar_d, emit)
        self._layout: dict[int, list] = {}

    def visiveis_em(self, t: float) -> list:
        return [c for c in self.pacote.comentarios
                if 0 <= t - (c.t + self.offset) <= CHAT_DURACAO][-CHAT_MAX_MENSAGENS:]

    def _linhas(self, c) -> list:
        """Linhas prontas de uma mensagem: apelido em cima, texto embaixo.

        Guardadas porque a mesma mensagem reaparece em dezenas de quadros
        seguidos e remontá-la a cada um seria o grosso do custo.
        """
        chave = id(c)
        if chave not in self._layout:
            # Os selos entram na mesma linha do apelido, que encolhe para caber.
            esquerda = [self.tipo.selo(f) for f in self._selos(c, "selos")]
            direita = [self.tipo.selo(f) for f in self._selos(c, "selos_direita")]
            ocupado = sum(p.largura for p in esquerda + direita)
            cabeca = self.tipo.linhas(c.apelido or c.de,
                                      max(self.texto_max * 0.2,
                                          self.texto_max - ocupado), maximo=1)[0]
            linhas = [esquerda + cabeca + direita]
            if c.texto:
                linhas += self.tipo.linhas(c.texto, self.texto_max,
                                           maximo=CHAT_LINHAS_MSG,
                                           imagens=self._emotes(c))
            self._layout[chave] = linhas
        return self._layout[chave]

    def _selos(self, c, campo: str) -> list:
        """Selos do apelido de um lado, já como imagem."""
        achados = []
        for ident in (getattr(c, campo, None) or []):
            img = self.pacote.abrir_figura(ident)
            if img is not None:
                achados.append(img)
        return achados

    def _emotes(self, c) -> dict:
        """Figuras próprias da live, na posição em que entram no texto."""
        achados = {}
        for pos, dados in (getattr(c, "emotes", None) or {}).items():
            img = self.pacote.abrir_figura(dados) if dados else None
            if img is not None:
                achados[pos] = img
        return achados

    def painel(self, t: float):
        """(imagem, x, y) do chat nesse instante, ou None se não há mensagem."""
        from PIL import Image, ImageDraw

        recentes = self.visiveis_em(t)
        if not recentes:
            return None

        blocos = [(c, self._linhas(c)) for c in recentes]
        alturas = [len(l) * self.entrelinha + self.respiro for _c, l in blocos]
        alto = sum(alturas)

        camada = Image.new("RGBA", (self.largura, alto), (0, 0, 0, 0))
        d = ImageDraw.Draw(camada)
        base = alto

        for (c, linhas), altura_bloco in zip(reversed(blocos), reversed(alturas)):
            base -= altura_bloco
            idade = t - (c.t + self.offset)
            restante = CHAT_DURACAO - idade
            alfa = 255 if restante > CHAT_SUMICO else int(255 * restante / CHAT_SUMICO)
            alfa = max(0, min(255, alfa))
            if not alfa:
                continue

            topo = base + self.respiro
            # A foto fica no meio do bloco inteiro, não da primeira linha.
            av = self.avatares.get(c.de)
            if av is not None:
                meio = topo + (len(linhas) * self.entrelinha - self.avatar_d) // 2
                camada.paste(_com_alfa(av, alfa), (self.margem, meio),
                             _com_alfa(av, alfa))

            for i, linha in enumerate(linhas):
                cor = (255, 255, 255, alfa if i else round(alfa * CHAT_ALFA_APELIDO))
                self.tipo.escrever(camada, d, self.coluna,
                                   topo + (i + 1) * self.entrelinha - self.entrelinha * 0.28,
                                   linha, cor)

        painel = _com_sombra(camada, max(1, round(self.largura / 480)))
        return painel, 0, max(0, self.altura - round(CHAT_DE_BAIXO * self.altura) - alto)


def _com_alfa(img, alfa: int):
    """A mesma figura, mais transparente. 255 devolve a original."""
    if alfa >= 255:
        return img
    from PIL import Image
    copia = img.copy()
    copia.putalpha(img.getchannel("A").point(lambda v: v * alfa // 255))
    return copia


def _com_sombra(camada, raio: int):
    """Sombra escura e macia atrás de tudo, para o texto sobreviver ao vídeo.

    Sai do próprio desenho: o alfa borrado vira a mancha. É o que o app faz -
    não há retângulo de fundo, só esse escurecimento.
    """
    from PIL import Image, ImageFilter

    alfa = camada.getchannel("A").filter(ImageFilter.GaussianBlur(raio))
    alfa = alfa.point(lambda v: min(255, v * 2))       # adensa antes de deslocar
    sombra = Image.new("RGBA", camada.size, (0, 0, 0, 0))
    sombra.putalpha(alfa.point(lambda v: v * 160 // 255))
    fundo = Image.new("RGBA", camada.size, (0, 0, 0, 0))
    fundo.paste(sombra, (0, max(1, raio // 2)), sombra)
    return Image.alpha_composite(fundo, camada)


def render_chat(pac: pacote_mod.Pacote, largura: int, altura: int,
                inicio: float, dur: float, destino: str, emit) -> str:
    """Desenha o chat numa faixa transparente, do tamanho do trecho exportado.

    Devolve o caminho do .webm com alfa, ou "" se não houver o que desenhar.
    """
    from PIL import Image

    offset = pac.offset_segundos
    fim = inicio + dur
    visiveis = [c for c in pac.comentarios if inicio - CHAT_DURACAO <= c.t + offset <= fim]
    if not visiveis:
        return ""

    emit("log", f"Desenhando o chat: {len(visiveis)} mensagens.")
    pintor = PintorDeChat(pac, largura, altura, emit)
    total_quadros = int(dur * CHAT_FPS)

    args = [
        recorder._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{largura}x{altura}",
        "-r", str(CHAT_FPS), "-i", "-",
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "34",
        "-row-mt", "1", destino,
    ]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=_NO_WINDOW)

    try:
        for n in range(total_quadros):
            t = inicio + n / CHAT_FPS
            quadro = Image.new("RGBA", (largura, altura), (0, 0, 0, 0))
            pronto = pintor.painel(t)
            if pronto is not None:
                painel, px, py = pronto
                quadro.paste(painel, (px, py), painel)

            proc.stdin.write(quadro.tobytes())
            if n % (CHAT_FPS * 20) == 0 and n:
                emit("status", ("finalizando", f"Chat: {n // CHAT_FPS}s de {int(dur)}s"))
    except (BrokenPipeError, OSError) as e:
        emit("log", f"Falha ao desenhar o chat: {e}")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=600)

    if proc.returncode != 0 or not os.path.exists(destino):
        err = proc.stderr.read().decode("utf-8", "replace")[-400:] if proc.stderr else ""
        emit("log", f"Camada de chat não saiu: {err}")
        return ""
    return destino


# ---------------------------------------------------------------- exportar

def exportar(video: str, pac: pacote_mod.Pacote, saida: str,
             opcoes: Opcoes, emit=lambda *a: None) -> bool:
    """Monta o vídeo final. Devolve True se deu certo."""
    if not os.path.exists(video):
        emit("error", "Vídeo não encontrado.")
        return False

    largura, altura = dimensoes(video)
    total = duracao(video)
    inicio = max(0.0, opcoes.inicio)
    fim = min(total, opcoes.fim if opcoes.fim else total)
    if fim <= inicio:
        emit("error", "O trecho escolhido está vazio.")
        return False
    dur = fim - inicio

    temp = tempfile.mkdtemp(prefix="export_")
    entradas = ["-ss", f"{inicio:.3f}", "-t", f"{dur:.3f}", "-i", video]
    partes, audios = [], []
    atual = "0:v"
    idx = 1

    try:
        # ---- camada dos presentes
        if opcoes.animacoes:
            agenda = agendar(pac, temp, largura, altura, inicio, fim)
            emit("log", f"Animações no trecho: {len(agenda)}")
            for item in agenda:
                if item.arquivo.endswith(".webm"):
                    entradas += ["-c:v", "libvpx-vp9"]
                entradas += ["-i", item.arquivo]

                # tempos relativos ao trecho exportado
                t0 = max(0.0, item.inicio - inicio)
                t1 = min(dur, item.fim - inicio)
                partes.append(f"{_cadeia_alfa(item.cfg, idx, largura, altura)},"
                              f"setpts=PTS-STARTPTS+{t0:.3f}/TB[anim{idx}]")
                partes.append(f"[{atual}][anim{idx}]overlay=0:0:"
                              f"enable='between(t,{t0:.3f},{t1:.3f})'[v{idx}]")
                atual = f"v{idx}"

                if item.cfg.get("has_audio") and opcoes.volume_animacoes > 0:
                    ms = int(t0 * 1000)
                    partes.append(f"[{idx}:a]adelay={ms}|{ms},"
                                  f"volume={opcoes.volume_animacoes:.2f}[au{idx}]")
                    audios.append(f"[au{idx}]")
                idx += 1

        # ---- camada do chat
        if opcoes.chat:
            chat_webm = render_chat(pac, largura, altura, inicio, dur,
                                    os.path.join(temp, "chat.webm"), emit)
            if chat_webm:
                entradas += ["-c:v", "libvpx-vp9", "-i", chat_webm]
                partes.append(f"[{idx}:v]scale={largura}:{altura}[chat]")
                partes.append(f"[{atual}][chat]overlay=0:0[v{idx}]")
                atual = f"v{idx}"
                idx += 1

        # ---- áudio
        if audios:
            partes.append(f"[0:a]{''.join(audios)}amix=inputs={len(audios) + 1}"
                          f":duration=first:normalize=0[aout]")
            mapa_audio = ["-map", "[aout]"]
        else:
            mapa_audio = ["-map", "0:a?"]

        args = ([recorder._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
                + entradas)
        if partes:
            args += ["-filter_complex", ";".join(partes), "-map", f"[{atual}]"]
        else:
            args += ["-map", "0:v"]
        args += mapa_audio + [
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", saida]

        emit("status", ("finalizando", "Gerando o vídeo..."))
        r = subprocess.run(args, capture_output=True, text=True, timeout=14400,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0:
            emit("log", f"ffmpeg falhou: {r.stderr.strip()[-500:]}")
            return False
        return True
    finally:
        shutil.rmtree(temp, ignore_errors=True)
