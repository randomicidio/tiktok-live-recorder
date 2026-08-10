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

import bisect
import functools
import io
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass

import requests

import pacote as pacote_mod
import recorder
import resources
import tipografia

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Medidas do chat, em fração da largura do vídeo. Todas foram medidas pixel a
# pixel numa gravação de tela do app a 1080 px de largura; guardá-las em fração
# é o que faz a reconstrução bater com o original em qualquer resolução.
CHAT_MARGEM = 33 / 1080        # borda esquerda até a foto
CHAT_AVATAR = 79 / 1080        # diâmetro da foto
CHAT_COLUNA = 130 / 1080       # onde o texto começa
CHAT_FONTE = 39 / 1080
CHAT_ENTRELINHA = 55 / 1080    # de uma linha de base para a seguinte
CHAT_RESPIRO = 34 / 1080       # folga a mais entre uma mensagem e outra
CHAT_TEXTO_MAX = 910 / 1080    # largura antes de quebrar a linha
# Distância da base do quadro até o rodapé da pilha. No app o chat para logo
# acima da caixa de digitar; como aqui essa caixa não existe, fica a folga
# equivalente para o texto não encostar na borda de baixo do vídeo.
CHAT_DE_BAIXO = 130 / 1080
# Faixa que se dissolve no alto da pilha. Medida na referência: uma mensagem
# saindo pelo topo chega apagada de vez cerca de 95 px acima do limite.
CHAT_FADE = 95 / 1080
# Onde fica a primeira linha de base dentro do bloco de uma mensagem.
CHAT_PRIMEIRA_BASE = 0.76      # em unidades de entrelinha
CHAT_ENTRADA = 0.22            # segundos do deslize de uma mensagem nova

CHAT_LINHAS_MSG = 3            # quantas linhas uma mensagem pode ocupar
CHAT_MAX_MENSAGENS = 4         # mensagens na tela ao mesmo tempo
# A fila não expira por tempo: no TikTok a mensagem só sai quando outra a
# empurra para fora do topo.
CHAT_FPS = 30                  # acompanha o deslize das mensagens novas

# O apelido vem apagado em relação à mensagem, medido na captura.
CHAT_ALFA_APELIDO = 0.55

# --- cartão de presente, também em fração da largura --------------------
# O cartão tem largura fixa no app (o texto é que é cortado com reticências),
# então aqui ele também tem.
CONTADOR_DURACAO = 4.5
CONTADOR_MARGEM = 34 / 1080     # borda esquerda até a pílula
CONTADOR_ALTURA = 118 / 1080
CONTADOR_LARGURA = 478 / 1080   # só a pílula escura
CONTADOR_AVATAR = 90 / 1080     # a folga que sobra é a mesma nos quatro lados
CONTADOR_TEXTO_X = 121 / 1080   # da borda da pílula até o texto
CONTADOR_BASE_NOME = 49 / 1080
CONTADOR_BASE_INFO = 97 / 1080  # a contagem usa a mesma linha de base
CONTADOR_NOME_FONTE = 34 / 1080
CONTADOR_INFO_FONTE = 25 / 1080
CONTADOR_NUM_FONTE = 76 / 1080
CONTADOR_X_FONTE = 50 / 1080    # o "x" sai bem menor que os dígitos
CONTADOR_ICONE = 100 / 1080     # figura do presente, encostada na direita
CONTADOR_ICONE_CENTRO = 79 / 1080   # da borda direita da pílula até o centro
CONTADOR_NUM_X = 18 / 1080      # da borda direita da pílula até a contagem
CONTADOR_ESPACO = 10 / 1080     # entre dois cartões empilhados
CONTADOR_FUNDO = (20, 20, 24)
CONTADOR_FUNDO_ALFA = 0.64
CONTADOR_ENTRADA = 0.30         # deslize de entrada
CONTADOR_SAIDA = 0.40           # dissolve de saída
# Ao trocar de número o antigo some, dá um respiro e o novo entra grande.
CONTADOR_NUM_PAUSA = 0.10
CONTADOR_NUM_POP = 0.18
CONTADOR_NUM_ESCALA = 1.40


@dataclass
class Opcoes:
    """O que entra no vídeo exportado."""

    animacoes: bool = True
    chat: bool = False
    contador_presentes: bool = False
    volume_animacoes: float = 1.0     # 0.0 a 2.0
    # Compensação de sincronia do arquivo, em segundos: positivo atrasa o áudio
    # da live. Não toca no som das animações, que é posicionado à parte.
    atraso_audio: float = 0.0
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


def _ffprobe() -> str:
    """O ffprobe embutido no pacote; cai para o do sistema.

    Nunca uma chamada crua a "ffprobe": empacotado, o PATH da máquina pode não
    ter nenhum, e aí o erro seria um FileNotFoundError no meio de abrir o vídeo.
    """
    return resources.find_binary("ffprobe") or "ffprobe"


def _perguntar(args: list[str]) -> str:
    """Roda o ffprobe e devolve a resposta, ou "" se ele não deu conta."""
    try:
        r = subprocess.run([_ffprobe()] + args, capture_output=True, text=True,
                           timeout=120, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""                    # sem ffprobe nesta máquina
    return r.stdout.strip() if r.returncode == 0 else ""


def duracao(caminho: str) -> float:
    saida = _perguntar(["-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", caminho])
    try:
        return float(saida)
    except ValueError:
        return 0.0


def dimensoes(caminho: str) -> tuple[int, int]:
    saida = _perguntar(["-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", caminho])
    try:
        w, h = saida.split(",")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return 720, 1280


def sem_ffprobe() -> bool:
    """Verdadeiro quando nem o pacote nem o sistema têm um ffprobe usável."""
    return not resources.find_binary("ffprobe")


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

# A mesma foto costuma aparecer no chat e no contador, e a exportação repete o
# trabalho que a prévia já fez. Guardar por (endereço, tamanho) evita baixar
# tudo de novo a cada painel criado.
_AVATARES: dict[tuple[str, int], object] = {}


@functools.lru_cache(maxsize=16)
def _mascara_redonda(tamanho: int):
    """Máscara circular lisa: desenhada grande e reduzida depois, senão o
    `ellipse` deixa a borda serrilhada no tamanho final."""
    from PIL import Image, ImageDraw

    escala = 4
    m = Image.new("L", (tamanho * escala, tamanho * escala), 0)
    ImageDraw.Draw(m).ellipse([0, 0, tamanho * escala - 1, tamanho * escala - 1],
                              fill=255)
    return m.resize((tamanho, tamanho), Image.LANCZOS)


def _baixa_avatares(eventos, tamanho: int, emit) -> dict[str, object]:
    """Uma foto por pessoa, recortada em círculo. Falha vira None."""
    from PIL import Image

    urls: dict[str, str] = {}
    for c in eventos:
        if c.de and c.avatar and c.de not in urls:
            urls[c.de] = c.avatar

    mascara = _mascara_redonda(tamanho)
    cache: dict[str, object] = {}
    faltando = {}
    for quem, url in urls.items():
        guardado = _AVATARES.get((url, tamanho))
        if guardado is not None or (url, tamanho) in _AVATARES:
            cache[quem] = guardado
        else:
            faltando[quem] = url

    def baixar(item):
        quem, url = item
        try:
            r = requests.get(url, timeout=6)
            if r.status_code != 200:
                return quem, url, None
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            img = img.resize((tamanho, tamanho), Image.LANCZOS)
            img.putalpha(mascara)
            return quem, url, img
        except (requests.RequestException, OSError):
            return quem, url, None

    # Uma foto indisponível não pode segurar a prévia inteira. O CDN do
    # TikTok eventualmente demora; baixar em paralelo encerra rápido.
    if faltando:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=12) as pool:
            futuros = [pool.submit(baixar, item) for item in faltando.items()]
            for futuro in as_completed(futuros):
                quem, url, img = futuro.result()
                cache[quem] = img
                _AVATARES[(url, tamanho)] = img
    emit("log", f"Fotos do chat: {sum(1 for v in cache.values() if v)} de {len(urls)}")
    return cache


def _suave(x: float) -> float:
    """Desaceleração cúbica: rápido no começo, parando macio no fim."""
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


class PintorDeChat:
    """Desenha o painel do chat num instante qualquer.

    Usado pela prévia (que empurra o painel para a libmpv sobrepor) e pela
    exportação (que o costura em cada quadro). Sendo o mesmo código, o que
    aparece na prévia é exatamente o que sai no arquivo.

    O desenho segue o do app: foto redonda à esquerda, apelido apagado em cima
    e a mensagem em branco embaixo, sem retângulo de fundo - a legibilidade vem
    de uma sombra suave. As mensagens empilham de baixo para cima e a que sai
    pelo topo se dissolve.

    Cada mensagem é desenhada uma única vez e guardada pronta: ela reaparece em
    dezenas de quadros seguidos, e remontá-la a cada um seria o grosso do custo.
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
        self.primeira_base = round(self.entrelinha * CHAT_PRIMEIRA_BASE)
        self.fade = round(CHAT_FADE * largura)
        self.raio_sombra = max(1, round(largura / 480))
        self.tipo = tipografia.Tipografia(round(CHAT_FONTE * largura))

        # Altura da janela visível: quatro mensagens de duas linhas, que é o
        # que cabe no app. Mensagens mais longas empurram as de cima para fora,
        # e é justamente aí que a dissolução do topo aparece.
        self.altura_visivel = CHAT_MAX_MENSAGENS * (2 * self.entrelinha + self.respiro)
        self.topo = self.altura - round(CHAT_DE_BAIXO * largura) - self.altura_visivel

        self.avatares = _baixa_avatares(pacote.comentarios, self.avatar_d, emit)
        self._blocos: dict[int, object] = {}
        self._esmaecidos: dict[tuple[int, int], object] = {}
        self._cache = (None, None)
        self._mascara = None

        # Ao conectar, o TikTok entrega o que já estava na tela em bloco. Não
        # há hora individual nesses eventos, então posicionamos as últimas
        # mensagens imediatamente antes do 0: elas aparecem já visíveis no
        # início do replay, em vez de todas saltarem juntas alguns segundos
        # depois. As antigas ficam fora da janela visível.
        instantes = {id(c): c.t + self.offset for c in pacote.comentarios}
        acumuladas = [c for c in pacote.comentarios if getattr(c, "acumulado", False)]
        recentes = acumuladas[-CHAT_MAX_MENSAGENS:]
        for i, c in enumerate(recentes):
            instantes[id(c)] = -(len(recentes) - 1 - i) * 0.55
        for c in acumuladas[:-CHAT_MAX_MENSAGENS]:
            instantes[id(c)] = -1_000_000
        self._instantes = instantes

        # Ordenar uma vez troca uma varredura da lista inteira por mensagem
        # desenhada - o que, numa live de horas, é a diferença entre a
        # exportação andar e a exportação arrastar.
        self._ordem = sorted(pacote.comentarios, key=lambda c: instantes[id(c)])
        self._quando = [instantes[id(c)] for c in self._ordem]

    # ------------------------------------------------------------ conteúdo

    def visiveis_em(self, t: float) -> list:
        fim = bisect.bisect_right(self._quando, t)
        return self._ordem[max(0, fim - CHAT_MAX_MENSAGENS):fim]

    def _linhas(self, c) -> list:
        """Linhas de uma mensagem: apelido (com selos) em cima, texto embaixo."""
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
        return linhas

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

    # -------------------------------------------------------------- desenho

    # Quantas mensagens ficam desenhadas na memória. Só quatro aparecem por
    # vez; guardar todas as de uma live de horas chegaria a centenas de MB.
    GUARDAR_BLOCOS = 24

    def _bloco(self, c):
        """A mensagem pronta, com foto, texto e sombra.

        Guardada porque a mesma mensagem reaparece em dezenas de quadros
        seguidos, e remontá-la a cada um seria o grosso do custo.
        """
        from PIL import Image, ImageDraw

        pronto = self._blocos.get(id(c))
        if pronto is not None:
            return pronto

        linhas = self._linhas(c)
        alto = len(linhas) * self.entrelinha + self.respiro
        camada = Image.new("RGBA", (self.largura, alto), (0, 0, 0, 0))
        d = ImageDraw.Draw(camada)

        for i, linha in enumerate(linhas):
            # O apelido vem apagado em relação à mensagem, como no app.
            cor = (255, 255, 255, 255 if i else round(255 * CHAT_ALFA_APELIDO))
            self.tipo.escrever(camada, d, self.coluna,
                               self.primeira_base + i * self.entrelinha, linha, cor)

        av = self.avatares.get(c.de)
        if av is not None:
            # A foto acompanha o miolo do texto, não a primeira linha.
            meio = (self.primeira_base + (len(linhas) - 1) * self.entrelinha / 2
                    - self.tipo.tamanho * 0.33)
            camada.alpha_composite(av, (self.margem, round(meio - self.avatar_d / 2)))

        pronto = _com_sombra(camada, self.raio_sombra)
        # Descarta a mais antiga desenhada: o dicionário guarda a ordem de
        # inserção, e a pilha visível nunca chega perto do limite.
        while len(self._blocos) >= self.GUARDAR_BLOCOS:
            del self._blocos[next(iter(self._blocos))]
        self._blocos[id(c)] = pronto
        return pronto

    def _mascara_fade(self):
        """Gradiente que dissolve a faixa de cima da pilha, calculado uma vez."""
        if self._mascara is None:
            from PIL import Image
            faixa = Image.new("L", (1, self.fade))
            faixa.putdata([round(255 * _suave(y / self.fade)) for y in range(self.fade)])
            self._mascara = faixa.resize((self.largura, self.fade))
        return self._mascara

    def painel(self, t: float):
        """(imagem, x, y) do chat nesse instante, ou None se não há mensagem."""
        from PIL import Image, ImageChops

        recentes = self.visiveis_em(t)
        if not recentes:
            return None

        # Mensagem nova empurra a pilha para cima em vez de saltar.
        chegada = self._quando[bisect.bisect_right(self._quando, t) - 1]
        fase = _suave((t - chegada) / CHAT_ENTRADA) if CHAT_ENTRADA > 0 else 1.0
        passo = round(fase * 16)                 # o cache trabalha por degraus
        chave = (tuple(id(c) for c in recentes), passo)
        if self._cache[0] == chave:
            return self._cache[1], 0, self.topo
        fase = passo / 16

        camada = Image.new("RGBA", (self.largura, self.altura_visivel), (0, 0, 0, 0))
        novo = self._bloco(recentes[-1])
        base = self.altura_visivel + round((1 - fase) * novo.height)
        for i, c in enumerate(reversed(recentes)):
            bloco = self._bloco(c)
            base -= bloco.height
            if base >= self.altura_visivel:
                continue
            if i == 0 and fase < 1.0:
                bloco = self._entrando(c, bloco, passo)
            # alpha_composite preserva a opacidade original. Usar paste com a
            # própria imagem como máscara multiplicava o alfa outra vez.
            camada.alpha_composite(bloco, (0, base))
            if base <= 0:
                break

        if self.fade > 0:
            alto = camada.crop((0, 0, self.largura, self.fade))
            alto.putalpha(ImageChops.multiply(alto.getchannel("A"), self._mascara_fade()))
            camada.paste(alto, (0, 0))

        self._cache = (chave, camada)
        return camada, 0, self.topo

    def _entrando(self, c, bloco, passo: int):
        """A mensagem que acabou de chegar também aparece surgindo."""
        guardado = self._esmaecidos.get((id(c), passo))
        if guardado is None:
            guardado = _com_alfa(bloco, round(255 * passo / 16))
            # Só a mensagem mais nova precisa disso, e por poucos quadros.
            if len(self._esmaecidos) > 20:
                self._esmaecidos.clear()
            self._esmaecidos[(id(c), passo)] = guardado
        return guardado


def _com_alfa(img, alfa: int):
    """A mesma figura, mais transparente. 255 devolve a original."""
    if alfa >= 255:
        return img
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
    fundo.alpha_composite(sombra, (0, max(1, raio // 2)))
    return Image.alpha_composite(fundo, camada)


class PintorDeContador:
    """O cartão de presente do TikTok, logo acima da pilha do chat.

    Pílula escura com a foto de quem enviou, o nome, o presente e a figura
    oficial encostada na borda direita; a contagem fica fora do fundo escuro e
    salta a cada unidade nova, como no app.
    """

    def __init__(self, pacote, largura: int, altura: int, emit=lambda *a: None):
        self.pacote = pacote
        self.offset = pacote.offset_segundos
        self.largura, self.altura = int(largura), int(altura)

        def px(fracao):
            return round(fracao * largura)

        self.margem = px(CONTADOR_MARGEM)
        self.altura_cartao = px(CONTADOR_ALTURA)
        self.largura_pilula = px(CONTADOR_LARGURA)
        self.avatar_d = px(CONTADOR_AVATAR)
        self.texto_x = px(CONTADOR_TEXTO_X)
        self.base_nome = px(CONTADOR_BASE_NOME)
        self.base_info = px(CONTADOR_BASE_INFO)
        self.icone = px(CONTADOR_ICONE)
        self.icone_centro = px(CONTADOR_ICONE_CENTRO)
        self.num_x = px(CONTADOR_NUM_X)
        self.espaco = max(4, px(CONTADOR_ESPACO))
        self.raio_sombra = max(1, round(largura / 540))

        self.tipo_nome = tipografia.Tipografia(max(12, px(CONTADOR_NOME_FONTE)))
        self.tipo_info = tipografia.Tipografia(max(10, px(CONTADOR_INFO_FONTE)))
        self.tipo_num = tipografia.Tipografia(max(20, px(CONTADOR_NUM_FONTE)),
                                              peso=tipografia.PESO_FORTE)
        self.tipo_vezes = tipografia.Tipografia(max(14, px(CONTADOR_X_FONTE)),
                                                peso=tipografia.PESO_FORTE)

        self.avatares = _baixa_avatares(pacote.presentes, self.avatar_d, emit)
        self._avisos = self._montar_fila()
        self._inicios = [a["inicio"] for a in self._avisos]
        self._cartoes: dict[tuple[int, int], object] = {}
        self._numeros: dict[int, object] = {}

        # A pilha fica ancorada: no app os cartões não sobem nem descem quando
        # o chat encolhe. O rodapé dela é a folga acima da janela do chat.
        janela = CHAT_MAX_MENSAGENS * (round(CHAT_ENTRELINHA * largura) * 2
                                       + round(CHAT_RESPIRO * largura))
        self.base_y = (self.altura - round(CHAT_DE_BAIXO * largura) - janela
                       - round(105 / 1080 * largura))

    # ---------------------------------------------------------------- fila

    def _montar_fila(self):
        """Agrupa combos e distribui os avisos em uma fila de duas vagas.

        Um mesmo presente repetido atualiza o xN do cartão existente. Presentes
        diferentes aguardam se as duas vagas já estão na tela; ao sair o cartão
        de baixo, o de cima desce e o próximo entra por cima.
        """
        # Enquanto o cartão de alguém ainda está na tela, um novo presente igual
        # dela só faz o número subir - é assim no app, e é o que evita dois
        # cartões idênticos lado a lado. O TikTok também manda um evento de
        # fecho do combo alguns segundos depois do último, e é justamente ele
        # que abria um cartão repetido antes desta regra.
        brutos = []
        vivos: dict[tuple, dict] = {}
        for p in sorted(self.pacote.presentes, key=lambda x: x.t + self.offset):
            quando = p.t + self.offset
            chave = (p.de, p.nome)
            grupo = vivos.get(chave)
            if grupo is not None and quando - grupo["ultimo"] <= CONTADOR_DURACAO:
                grupo["ultimo"] = quando
                grupo["quantidade"] = max(grupo["quantidade"], p.quantidade)
                grupo["eventos"].append(p)
            else:
                grupo = {"p": p, "chave": chave, "primeiro": quando,
                         "ultimo": quando, "quantidade": p.quantidade,
                         "eventos": [p]}
                brutos.append(grupo)
                vivos[chave] = grupo

        ativos = []
        for aviso in brutos:
            chegada = aviso["primeiro"]
            ativos = [a for a in ativos if a["fim"] > chegada]
            if len(ativos) >= 2:
                chegada = min(a["fim"] for a in ativos)
                ativos = [a for a in ativos if a["fim"] > chegada]
            aviso["inicio"] = chegada
            aviso["fim"] = max(chegada + CONTADOR_DURACAO,
                               aviso["ultimo"] + CONTADOR_DURACAO)
            # Momento em que cada número passa a valer, para o salto da contagem.
            trocas, atual = [], 0
            for ev in aviso["eventos"]:
                if ev.quantidade > atual:
                    atual = ev.quantidade
                    trocas.append((max(aviso["inicio"], ev.t + self.offset), atual))
            aviso["trocas"] = trocas or [(aviso["inicio"], aviso["quantidade"])]
            ativos.append(aviso)
        # Um aviso que esperou vaga pode entrar depois de outro criado mais
        # tarde; ordenar por `inicio` é o que garante a busca por bisseção.
        brutos.sort(key=lambda a: a["inicio"])
        return brutos

    # Quantos avisos anteriores ainda podem estar na tela. Duas vagas e uma
    # duração fixa por cartão dão folga de sobra neste número.
    VIZINHOS = 8

    def _ativos(self, t: float):
        """Os cartões visíveis nesse instante.

        A busca é por bisseção porque numa live movimentada há milhares de
        presentes, e varrer a lista inteira em cada quadro exportado somava mais
        tempo do que desenhar os cartões.
        """
        fim = bisect.bisect_right(self._inicios, t)
        return [a for a in self._avisos[max(0, fim - self.VIZINHOS):fim]
                if a["fim"] > t]

    # ------------------------------------------------------------- desenho

    def _pilula(self, aviso):
        """A parte do cartão que não muda: fundo, foto, nome e figura."""
        from PIL import Image, ImageDraw

        pronto = self._cartoes.get((id(aviso), 0))
        if pronto is not None:
            return pronto

        p = aviso["p"]
        ix = self.largura_pilula - self.icone_centro - self.icone // 2
        h = self.altura_cartao
        w = max(self.largura_pilula, ix + self.icone) + 6
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, self.largura_pilula - 1, h - 1), radius=h // 2,
                            fill=CONTADOR_FUNDO + (round(255 * CONTADOR_FUNDO_ALFA),))

        av = self.avatares.get(p.de)
        ax, ay = (h - self.avatar_d) // 2, (h - self.avatar_d) // 2
        if av is not None:
            img.alpha_composite(av, (ax, ay))
        else:
            d.ellipse((ax, ay, ax + self.avatar_d, ay + self.avatar_d),
                      fill=(92, 76, 112, 255))

        # O texto é cortado com reticências, como no app: a pílula tem largura
        # fixa e o que cede é o nome. O limite é onde a figura do presente
        # começa, não a borda da pílula.
        espaco = max(40, ix - self.texto_x)
        nome = self.tipo_nome.recortar(p.apelido or p.de or "Alguém", espaco)
        presente = self.tipo_info.recortar(f"enviou {p.nome or 'um presente'}", espaco)
        self.tipo_nome.escrever(img, d, self.texto_x, self.base_nome, nome,
                                (255, 255, 255, 255))
        self.tipo_info.escrever(img, d, self.texto_x, self.base_info, presente,
                                (222, 218, 225, 220))

        # Ícone oficial vem embutido no .ttgifts; pacotes antigos usam emoji.
        # Ele se apoia na borda da pílula e avança um pouco para fora dela.
        oficial = self.pacote.abrir_figura(p.icone) if getattr(p, "icone", "") else None
        if oficial is not None:
            oficial = oficial.resize((self.icone, self.icone), Image.LANCZOS)
            img.alpha_composite(oficial, (ix, (h - self.icone) // 2))
        else:
            chave = (p.nome or "").lower()
            emoji = "🌹" if "rose" in chave else ("👏" if "clap" in chave else "🎁")
            fig = self.tipo_num.linhas(emoji, self.icone * 2, maximo=1)[0]
            self.tipo_num.escrever(img, d, ix, h - round(h * 0.14), fig,
                                   (255, 255, 255, 255))

        pronto = _com_sombra(img, self.raio_sombra)
        # Só dois cartões ficam na tela; guardar os de uma live inteira encheria
        # a memória sem serventia nenhuma.
        if len(self._cartoes) > 16:
            self._cartoes.clear()
        self._cartoes[(id(aviso), 0)] = pronto
        return pronto

    def _numero(self, quantidade: int):
        """"xN" pronto: o x sai bem menor que os dígitos, na mesma base."""
        from PIL import Image, ImageDraw

        pronto = self._numeros.get(quantidade)
        if pronto is not None:
            return pronto

        digitos = self.tipo_num.linhas(str(quantidade), self.largura, maximo=1)[0]
        vezes = self.tipo_vezes.linhas("x", self.largura, maximo=1)[0]
        larg = round(self.tipo_vezes.largura(vezes) + self.tipo_num.largura(digitos))
        base = self.tipo_num.tamanho          # linha de base dentro do quadro
        img = Image.new("RGBA", (max(1, larg + 4), base + self.tipo_num.tamanho // 6),
                        (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x = self.tipo_vezes.escrever(img, d, 0, base, vezes, (255, 255, 255, 255))
        self.tipo_num.escrever(img, d, x, base, digitos, (255, 255, 255, 255))
        pronto = _com_sombra(img, self.raio_sombra)
        if len(self._numeros) > 64:
            self._numeros.clear()
        self._numeros[quantidade] = pronto
        return pronto

    def _contagem(self, aviso, t: float):
        """(imagem do xN, escala, alfa) ou None quando ele não aparece agora.

        No app o número não muda no lugar: some por um instante e volta grande,
        encolhendo até o tamanho normal. É esse salto que dá a sensação de que
        os presentes estão chegando em rajada.
        """
        anterior = None
        for quando, quantidade in aviso["trocas"]:
            if quando > t:
                break
            anterior = (quando, quantidade)
        if anterior is None:
            return None
        quando, quantidade = anterior
        # Presente único não mostra contagem nenhuma, igual ao app.
        if quantidade <= 1:
            return None

        primeiro = quando <= aviso["trocas"][0][0]
        pausa = 0.0 if primeiro else CONTADOR_NUM_PAUSA
        idade = t - quando
        if idade < pausa:
            return None                      # o intervalo apagado entre números
        fase = _suave((idade - pausa) / CONTADOR_NUM_POP)
        escala = 1 + (CONTADOR_NUM_ESCALA - 1) * (1 - fase)
        return self._numero(quantidade), escala, fase

    def _cartao(self, aviso, t: float):
        """(imagem, deslocamento x) do cartão nesse instante."""
        from PIL import Image

        idade = t - aviso["inicio"]
        sobra = aviso["fim"] - t
        entrada = _suave(idade / CONTADOR_ENTRADA)
        alfa = round(255 * min(1.0, sobra / CONTADOR_SAIDA) * entrada)

        pilula = self._pilula(aviso)
        contagem = self._contagem(aviso, t)
        # O quadro reserva o maior tamanho que o número pode alcançar, senão o
        # salto o cortaria justamente no instante em que ele é maior.
        reserva = 0
        if contagem is not None:
            reserva = round(contagem[0].width * CONTADOR_NUM_ESCALA) + 4
        largura = self.largura_pilula + self.num_x + reserva
        img = Image.new("RGBA", (max(pilula.width, largura), pilula.height),
                        (0, 0, 0, 0))
        img.paste(pilula, (0, 0))

        if contagem is not None:
            numero, escala, forca = contagem
            cx = self.largura_pilula + self.num_x + numero.width / 2
            cy = self.base_info - self.tipo_num.tamanho + numero.height / 2
            if escala > 1.001:
                alvo = (max(1, round(numero.width * escala)),
                        max(1, round(numero.height * escala)))
                numero = numero.resize(alvo, Image.BICUBIC)
            if forca < 1.0:
                numero = _com_alfa(numero, round(255 * forca))
            img.paste(numero, (round(cx - numero.width / 2),
                               round(cy - numero.height / 2)), numero)

        if alfa < 255:
            img = _com_alfa(img, alfa)
        # Entra pela esquerda, como o aviso do app.
        return img, round(-img.width * (1.0 - entrada))

    def painel(self, t: float):
        from PIL import Image

        ativos = self._ativos(t)
        if not ativos:
            return None
        # Cronologia: o mais antigo ocupa a vaga de baixo; no máximo dois.
        ativos = ativos[-2:]
        passo = self.altura_cartao + self.espaco
        # O quadro tem sempre as duas vagas, mesmo com um cartão só. É isso que
        # deixa o rodapé da pilha ancorado: se a altura variasse com o número
        # de cartões, mover um deles dentro do quadro não moveria nada na tela.
        alto = 2 * self.altura_cartao + self.espaco
        pecas = []
        for indice, aviso in enumerate(ativos):
            img, x = self._cartao(aviso, t)
            if len(ativos) > 1:
                y = passo if indice == 0 else 0   # o mais antigo fica embaixo
            else:
                # Vaga de baixo. Se o cartão que estava embaixo acabou de sair,
                # este desce da vaga de cima em vez de saltar para o lugar.
                y = passo
                corte = bisect.bisect_right(self._inicios, t)
                saidas = [a["fim"] for a in
                          self._avisos[max(0, corte - self.VIZINHOS):corte]
                          if aviso["inicio"] < a["fim"] <= t]
                if saidas:
                    y = round(passo * _suave((t - max(saidas)) / 0.20))
            pecas.append((img, x, y))

        largura = max(img.width for img, _x, _y in pecas)
        camada = Image.new("RGBA", (largura, alto), (0, 0, 0, 0))
        for img, x, y in pecas:
            camada.alpha_composite(img, (x, y))
        return camada, self.margem, self.base_y - alto


class Cancelado(Exception):
    """A pessoa desistiu da exportação no meio."""


def render_chat(pac: pacote_mod.Pacote, largura: int, altura: int,
                inicio: float, dur: float, destino: str, emit,
                mostrar_chat: bool = True, mostrar_contador: bool = False,
                progresso=None, cancelar=None) -> tuple[str, int]:
    """Desenha o chat numa faixa transparente, do tamanho do trecho exportado.

    Devolve (caminho do .webm com alfa, altura em que ele entra no quadro), ou
    ("", 0) se não houver o que desenhar. `progresso` recebe uma fração de 0 a
    1 conforme os quadros saem.

    A faixa cobre só a parte do quadro onde as camadas podem aparecer. O chat
    ocupa a metade de baixo e o resto seria transparente do começo ao fim -
    escrever esses pixels custaria metade do tempo da exportação à toa.
    """
    from PIL import Image

    offset = pac.offset_segundos
    fim = inicio + dur
    visiveis = [c for c in pac.comentarios if c.t + offset <= fim]
    # Também há camada quando o pacote traz a fotografia inicial do chat.
    if mostrar_chat and any(getattr(c, "acumulado", False) for c in pac.comentarios):
        visiveis.append(object())
    presentes = [p for p in pac.presentes if inicio - CONTADOR_DURACAO <= p.t + offset <= fim]
    if not (mostrar_chat and visiveis) and not (mostrar_contador and presentes):
        return "", 0

    emit("log", f"Desenhando interações: {len(visiveis)} mensagens, {len(presentes)} presentes.")
    pintor = PintorDeChat(pac, largura, altura, emit) if mostrar_chat else None
    contador = PintorDeContador(pac, largura, altura, emit) if mostrar_contador else None
    total_quadros = max(1, int(dur * CHAT_FPS))

    # Onde as camadas podem chegar, com uma folga para a sombra.
    limites = []
    if pintor is not None:
        limites += [pintor.topo, pintor.topo + pintor.altura_visivel]
    if contador is not None:
        limites += [contador.base_y - 2 * contador.altura_cartao - contador.espaco,
                    contador.base_y]
    folga = round(0.02 * altura)
    y0 = max(0, (min(limites) - folga) // 2 * 2)
    y1 = min(altura, (max(limites) + folga + 1) // 2 * 2)
    faixa = max(2, y1 - y0)

    args = [
        recorder._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{largura}x{faixa}",
        "-r", str(CHAT_FPS), "-i", "-",
        # Esta camada é intermediária: ela vai ser recomprimida junto com o
        # vídeo no fim. Vale mais gastar bits (crf baixo) e ganhar tempo
        # (deadline realtime) do que economizar espaço num arquivo temporário.
        # Medido: 4x mais rápido que o ajuste conservador, e com menos erro.
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "22",
        "-row-mt", "1", "-deadline", "realtime", "-cpu-used", "8", destino,
    ]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=_NO_WINDOW)

    vazio = Image.new("RGBA", (largura, faixa), (0, 0, 0, 0))
    interrompido = False
    try:
        for n in range(total_quadros):
            if cancelar is not None and cancelar():
                interrompido = True
                break
            t = inicio + n / CHAT_FPS
            quadro = vazio.copy()
            pronto = pintor.painel(t) if pintor else None
            if pronto is not None:
                painel, px, py = pronto
                quadro.alpha_composite(painel, (px, py - y0))
            aviso = contador.painel(t) if contador else None
            if aviso is not None:
                painel, px, py = aviso
                quadro.alpha_composite(painel, (px, py - y0))

            proc.stdin.write(quadro.tobytes())
            if progresso is not None and n % 8 == 0:
                progresso(n / total_quadros)
    except (BrokenPipeError, OSError) as e:
        emit("log", f"Falha ao desenhar o chat: {e}")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=600)

    if interrompido:
        raise Cancelado()
    if proc.returncode != 0 or not os.path.exists(destino):
        err = proc.stderr.read().decode("utf-8", "replace")[-400:] if proc.stderr else ""
        emit("log", f"Camada de chat não saiu: {err}")
        return "", 0
    if progresso is not None:
        progresso(1.0)
    return destino, y0


# ---------------------------------------------------------------- exportar

def _rodar_ffmpeg(args: list[str], dur: float, progresso=None,
                  cancelar=None) -> tuple[int, str]:
    """Roda o ffmpeg acompanhando quanto do trecho já saiu.

    O `-progress` faz o ffmpeg escrever o relógio da saída em pares
    `chave=valor`; é a única forma confiável de saber o andamento, já que o
    tempo de codificação não anda junto com o tempo de vídeo.
    """
    args = args[:1] + ["-progress", "pipe:1", "-nostats"] + args[1:]
    erros: list[str] = []
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_NO_WINDOW)

    # O stderr precisa ser esvaziado em paralelo: cheio, ele trava o ffmpeg.
    def drenar():
        for linha in proc.stderr:
            texto = linha.decode("utf-8", "replace").rstrip()
            if texto:
                erros.append(texto)

    fio = threading.Thread(target=drenar, daemon=True)
    fio.start()

    interrompido = False
    try:
        for linha in proc.stdout:
            if cancelar is not None and cancelar():
                interrompido = True
                proc.terminate()
                break
            texto = linha.decode("ascii", "replace").strip()
            if texto.startswith("out_time_us=") and progresso is not None and dur > 0:
                try:
                    segundos = int(texto.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progresso(max(0.0, min(1.0, segundos / dur)))
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait(timeout=600)
        # O stderr só é fechado depois que a thread que o lê termina: fechá-lo
        # por baixo dela levantaria erro justamente na hora de relatar a falha.
        fio.join(timeout=5)
        try:
            proc.stderr.close()
        except OSError:
            pass

    if interrompido:
        raise Cancelado()
    return proc.returncode, "\n".join(erros[-8:])


def exportar(video: str, pac: pacote_mod.Pacote, saida: str,
             opcoes: Opcoes, emit=lambda *a: None, cancelar=None) -> bool:
    """Monta o vídeo final. Devolve True se deu certo.

    `cancelar` é consultado de tempos em tempos; devolvendo True, a exportação
    para e o arquivo pela metade é apagado.
    """
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

    # Medido em exportações reais: desenhar o chat fica perto da metade do
    # trabalho; sem ele, a codificação é tudo. É o suficiente para a barra
    # andar de forma honesta em vez de saltar de 0 a 100.
    desenha_chat = bool(opcoes.chat or opcoes.contador_presentes)
    peso_chat = 0.5 if desenha_chat else 0.0

    def andou(fracao: float, fase: str) -> None:
        if fase == "chat":
            emit("progresso", (fracao * peso_chat, "Desenhando o chat"))
        else:
            emit("progresso", (peso_chat + fracao * (1 - peso_chat),
                               "Montando o vídeo"))

    temp = tempfile.mkdtemp(prefix="export_")
    entradas = ["-ss", f"{inicio:.3f}", "-t", f"{dur:.3f}", "-i", video]
    partes, audios = [], []
    atual = "0:v"
    idx = 1

    # ---- compensação de sincronia
    # Quem se desloca é o áudio da live, não o vídeo: as animações, o chat e o
    # contador são posicionados pelo relógio do vídeo, e mexer nele arrastaria
    # os três junto. Em vez de empurrar o áudio já cortado - o que abriria um
    # silêncio no começo do trecho -, o mesmo arquivo entra uma segunda vez com
    # o corte deslocado, então o som continua inteiro nas duas pontas.
    atraso = float(opcoes.atraso_audio or 0.0)
    fonte_audio, cabeca_muda = "0:a", 0.0
    if abs(atraso) >= 0.001:
        ini_audio = inicio - atraso
        # Só quando o trecho começa perto do zero e o áudio teria de vir de
        # antes do arquivo: aí o começo é silêncio mesmo, não há de onde tirar.
        cabeca_muda = max(0.0, -ini_audio)
        ini_audio = max(0.0, ini_audio)
        entradas += ["-ss", f"{ini_audio:.3f}",
                     "-t", f"{max(0.05, dur - cabeca_muda):.3f}", "-i", video]
        fonte_audio = f"{idx}:a"
        idx += 1
        emit("log", f"Sincronia: áudio deslocado em {atraso * 1000:+.0f} ms")

    try:
        andou(0.0, "chat" if desenha_chat else "video")
        # ---- chat e contador
        # Esta camada entra antes: a animação oficial precisa passar por cima
        # das mensagens, como acontece no aplicativo.
        if desenha_chat:
            interacoes, faixa_y = render_chat(
                pac, largura, altura, inicio, dur,
                os.path.join(temp, "interacoes.webm"), emit,
                mostrar_chat=opcoes.chat,
                mostrar_contador=opcoes.contador_presentes,
                progresso=lambda f: andou(f, "chat"), cancelar=cancelar)
            if interacoes:
                entradas += ["-c:v", "libvpx-vp9", "-i", interacoes]
                partes.append(f"[{atual}][{idx}:v]overlay=0:{faixa_y}[v{idx}]")
                atual = f"v{idx}"
                idx += 1

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

        # ---- áudio
        voz = f"[{fonte_audio}]"
        if cabeca_muda > 0.001:
            ms = int(cabeca_muda * 1000)
            partes.append(f"[{fonte_audio}]adelay={ms}:all=1[vozsinc]")
            voz = "[vozsinc]"
        if audios:
            partes.append(f"{voz}{''.join(audios)}amix=inputs={len(audios) + 1}"
                          f":duration=first:normalize=0[aout]")
            mapa_audio = ["-map", "[aout]"]
        elif voz != f"[{fonte_audio}]":
            mapa_audio = ["-map", voz]
        else:
            mapa_audio = ["-map", f"{fonte_audio}?"]

        args = ([recorder._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
                + entradas)
        if partes:
            args += ["-filter_complex", ";".join(partes)]
        # O filtro pode existir só por causa do áudio; aí o vídeo continua
        # saindo direto da entrada, sem colchetes.
        args += ["-map", f"[{atual}]" if atual != "0:v" else "0:v"]
        args += mapa_audio + [
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", saida]

        emit("status", ("finalizando", "Gerando o vídeo..."))
        codigo, erro = _rodar_ffmpeg(args, dur,
                                     progresso=lambda f: andou(f, "video"),
                                     cancelar=cancelar)
        if codigo != 0:
            emit("log", f"ffmpeg falhou: {erro[-500:]}")
            return False
        emit("progresso", (1.0, "Pronto"))
        return True
    except Cancelado:
        emit("log", "Exportação cancelada.")
        try:
            os.remove(saida)
        except OSError:
            pass
        return False
    finally:
        shutil.rmtree(temp, ignore_errors=True)
