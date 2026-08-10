"""Desenha texto que mistura idiomas e emoji numa linha só.

O Pillow escolhe uma fonte e não troca: um caractere fora dela vira quadrado.
Como o chat do TikTok junta latino, japonês, coreano, cirílico e emoji na mesma
mensagem, aqui o texto é quebrado em trechos por cobertura - cada trecho vai
para a primeira fonte da cadeia que saiba desenhá-lo. A cobertura sai da tabela
`cmap` de cada arquivo, que é a lista de caracteres que a fonte tem.

As fontes de texto são as do sistema: no Windows, Segoe UI, Yu Gothic, Malgun,
MS YaHei e Nirmala cobrem quase tudo e não pesam nada no executável.

Os emoji não vêm de fonte nenhuma - são imagens do conjunto Noto embutido em
`assets/emoji.zip`. Três motivos: o Segoe UI Emoji não tem bandeiras de país
(decisão da Microsoft, é por isso que o Windows mostra "BO" em vez de 🇧🇴); sem
um motor de forma o Pillow não alcança tons de pele nem sequências ZWJ, que
nessas fontes só existem como ligadura; e assim o resultado fica igual em
qualquer máquina, Windows ou Mac.
"""

from __future__ import annotations

import functools
import io
import os
import struct
import zipfile

import resources

# Cadeia de fontes de texto, na ordem em que são tentadas. A primeira que tiver
# o caractere fica com ele, então a de uso geral vem antes das específicas.

# A fonte do próprio TikTok, em `assets/fonts`. Cobre latino, cirílico e grego;
# o resto continua vindo do sistema, exatamente como no app.
FONTES_EMBUTIDAS = ("TikTokSans.ttf",)

# Ela é variável, e sem escolher os eixos vem no peso mais leve. Os valores
# saíram da captura de referência: medindo a altura de caixa alta e a espessura
# do traço numa gravação de tela do app, o texto do chat bate com Medium (500).
# Pesos maiores engordavam as letras e as deixavam mais largas que o original.
PESO_TEXTO = 500.0        # chat: apelido e mensagem
PESO_FORTE = 700.0        # números do contador e selos
VARIACAO = {"Optical size": 16.0, "Weight": PESO_TEXTO}

FONTES_WINDOWS = FONTES_EMBUTIDAS + (
    "segoeui.ttf",      # latino, cirílico, grego, árabe, hebraico
    "YuGothM.ttc",      # japonês
    "malgun.ttf",       # coreano
    "msyh.ttc",         # chinês simplificado
    "msjh.ttc",         # chinês tradicional
    "Nirmala.ttf",      # devanágari, tâmil, bengali e outras da Índia
    "leelawui.ttf",     # tailandês, laosiano, birmanês
    "seguisym.ttf",     # símbolos soltos
    "arial.ttf",
    # Rede de segurança: se o conjunto de imagens não estiver junto, os emoji
    # ainda saem pela fonte do sistema - sem cor de bandeira, mas sem quadrado.
    "seguiemj.ttf",
)
FONTES_MAC = FONTES_EMBUTIDAS + (
    "Helvetica.ttc",
    "PingFang.ttc",             # chinês
    "ヒラギノ角ゴシック W3.ttc",    # japonês
    "AppleSDGothicNeo.ttc",     # coreano
    "Arial Unicode.ttf",
    "Apple Color Emoji.ttc",
)

# Modificadores que só fazem sentido com a imagem certa do emoji. Quando a
# sequência inteira não existe no conjunto, some com eles em vez de desenhar
# o quadradinho de cor solto que sobraria.
_ACESSORIOS = frozenset(
    [0x200D, 0x20E3, 0xFE0E, 0xFE0F]                    # ZWJ, teclado, seletores
    + [0xFFFC]                                          # marca de emote sem figura
    + list(range(0x1F3FB, 0x1F400))                     # tons de pele
    + list(range(0xE0020, 0xE0080))                     # etiquetas (bandeiras regionais)
)
_SEQUENCIA_MAX = 10          # codepoints por emoji (a família com ZWJ chega a 8)


# ------------------------------------------------------------------ cobertura

def _inicios(dados: bytes) -> list[int]:
    """Onde começa cada fonte do arquivo - um .ttc guarda várias."""
    if dados[:4] == b"ttcf":
        n = struct.unpack_from(">I", dados, 8)[0]
        return list(struct.unpack_from(f">{n}I", dados, 12))
    return [0]


def _tabelas(dados: bytes, inicio: int) -> dict[bytes, int]:
    num = struct.unpack_from(">H", dados, inicio + 4)[0]
    saida = {}
    for i in range(num):
        tag, _soma, offset, _tam = struct.unpack_from(">4sIII", dados, inicio + 12 + i * 16)
        saida[tag] = offset
    return saida


def _subtabela(dados: bytes, off: int) -> list[tuple[int, int]]:
    """Faixas de uma subtabela cmap. Formatos 4 e 12 dão conta de tudo hoje."""
    formato = struct.unpack_from(">H", dados, off)[0]
    faixas: list[tuple[int, int]] = []

    if formato == 4:
        seg2 = struct.unpack_from(">H", dados, off + 6)[0]
        seg = seg2 // 2
        fim_off, ini_off = off + 14, off + 16 + seg2
        # o vetor de deltas fica entre os dois; só o de faixas é consultado aqui
        faixa_off = off + 16 + seg2 * 3
        for i in range(seg):
            fim = struct.unpack_from(">H", dados, fim_off + i * 2)[0]
            ini = struct.unpack_from(">H", dados, ini_off + i * 2)[0]
            if ini > fim or ini == 0xFFFF:
                continue
            desloc = struct.unpack_from(">H", dados, faixa_off + i * 2)[0]
            if desloc == 0:
                faixas.append((ini, fim))
                continue
            # O trecho é esparso: cada caractere tem que ser conferido na
            # lista de glifos, senão a fonte pareceria cobrir buracos.
            base = faixa_off + i * 2 + desloc
            atual = None
            for c in range(ini, fim + 1):
                p = base + (c - ini) * 2
                if p + 2 > len(dados):
                    break
                glifo = struct.unpack_from(">H", dados, p)[0]
                if glifo:
                    atual = (c, c) if atual is None else (atual[0], c)
                elif atual:
                    faixas.append(atual)
                    atual = None
            if atual:
                faixas.append(atual)

    elif formato == 12:
        grupos = struct.unpack_from(">I", dados, off + 12)[0]
        for i in range(grupos):
            ini, fim, _g = struct.unpack_from(">III", dados, off + 16 + i * 12)
            faixas.append((ini, fim))

    elif formato == 6:
        primeiro, quantos = struct.unpack_from(">HH", dados, off + 6)
        if quantos:
            faixas.append((primeiro, primeiro + quantos - 1))

    return faixas


@functools.lru_cache(maxsize=32)
def _cobertura(caminho: str) -> tuple[tuple[int, int], ...]:
    """Faixas de caracteres que a fonte sabe desenhar, ordenadas.

    Só as subtabelas Unicode entram: as antigas mapeiam bytes por outra tabela
    de código e fariam a fonte parecer cobrir o que não cobre.
    """
    try:
        with open(caminho, "rb") as fh:
            dados = fh.read()
    except OSError:
        return ()

    faixas: list[tuple[int, int]] = []
    try:
        for inicio in _inicios(dados):
            cmap = _tabelas(dados, inicio).get(b"cmap")
            if cmap is None:
                continue
            num = struct.unpack_from(">H", dados, cmap + 2)[0]
            for i in range(num):
                plat, enc, off = struct.unpack_from(">HHI", dados, cmap + 4 + i * 8)
                if not (plat == 0 or (plat == 3 and enc in (1, 10))):
                    continue
                faixas += _subtabela(dados, cmap + off)
    except (struct.error, IndexError):
        pass

    if not faixas:
        return ()
    faixas.sort()
    juntas = [faixas[0]]
    for ini, fim in faixas[1:]:
        if ini <= juntas[-1][1] + 1:
            juntas[-1] = (juntas[-1][0], max(juntas[-1][1], fim))
        else:
            juntas.append((ini, fim))
    return tuple(juntas)


def _cobre(faixas: tuple[tuple[int, int], ...], cp: int) -> bool:
    baixo, alto = 0, len(faixas) - 1
    while baixo <= alto:
        meio = (baixo + alto) // 2
        ini, fim = faixas[meio]
        if cp < ini:
            alto = meio - 1
        elif cp > fim:
            baixo = meio + 1
        else:
            return True
    return False


def _ajustar_variacao(fonte, peso: float | None = None) -> None:
    """Fixa os eixos das fontes variáveis. Nas comuns não faz nada."""
    try:
        eixos = fonte.get_variation_axes()
    except (OSError, AttributeError):
        return                       # fonte sem eixos
    valores = []
    for eixo in eixos:
        nome = eixo.get("name", "")
        if isinstance(nome, bytes):
            nome = nome.decode("latin-1", "replace")
        alvo = peso if (nome == "Weight" and peso is not None) else VARIACAO.get(nome)
        valores.append(eixo["default"] if alvo is None
                       else max(eixo["minimum"], min(eixo["maximum"], alvo)))
    try:
        fonte.set_variation_by_axes(valores)
    except OSError:
        pass


def _arquivos_de_fonte() -> list[str]:
    """Caminhos da cadeia que existem nesta máquina, na ordem de preferência."""
    pastas = [os.path.join(resources.bundle_dir(), "assets", "fonts"),
              os.path.join(resources.app_dir(), "assets", "fonts")]
    if resources.IS_WINDOWS:
        nomes = FONTES_WINDOWS
        pastas.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    else:
        nomes = FONTES_MAC
        pastas += ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
                   "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
                   "/usr/share/fonts/truetype/dejavu"]
        nomes = nomes + ("DejaVuSans.ttf",)

    achados = []
    for nome in nomes:
        for pasta in pastas:
            caminho = os.path.join(pasta, nome)
            if os.path.isfile(caminho):
                achados.append(caminho)
                break
    return achados


# --------------------------------------------------------------------- emoji

@functools.lru_cache(maxsize=1)
def _catalogo() -> tuple[str, frozenset[str]]:
    """Onde está o conjunto de imagens e quais sequências ele tem."""
    for pasta in (os.path.join(resources.bundle_dir(), "assets"),
                  os.path.join(resources.app_dir(), "assets")):
        caminho = os.path.join(pasta, "emoji.zip")
        if os.path.isfile(caminho):
            try:
                with zipfile.ZipFile(caminho) as z:
                    nomes = {n[:-4] for n in z.namelist() if n.endswith(".png")}
                return caminho, frozenset(nomes)
            except (zipfile.BadZipFile, OSError):
                break
    return "", frozenset()


@functools.lru_cache(maxsize=512)
def _imagem_emoji(chave: str, tamanho: int):
    """Uma imagem do conjunto, já no tamanho pedido."""
    from PIL import Image

    caminho, _ = _catalogo()
    if not caminho:
        return None
    try:
        with zipfile.ZipFile(caminho) as z:
            bruto = z.read(chave + ".png")
        img = Image.open(io.BytesIO(bruto)).convert("RGBA")
    except (KeyError, zipfile.BadZipFile, OSError):
        return None
    return img.resize((tamanho, tamanho), Image.LANCZOS)


def _casar_emoji(texto: str, i: int) -> tuple[str, int]:
    """Maior sequência a partir de `i` que existe no conjunto.

    Devolve (chave, quantos caracteres consumidos) ou ("", 0). A busca vai do
    maior para o menor porque 🇧🇴 são dois codepoints que também existem
    sozinhos, e a bandeira só aparece se o par ganhar do primeiro.
    """
    _, disponiveis = _catalogo()
    if not disponiveis:
        return "", 0

    limite = min(len(texto), i + _SEQUENCIA_MAX)
    for fim in range(limite, i, -1):
        cps = [ord(c) for c in texto[i:fim]]
        if cps[-1] in _ACESSORIOS and fim < limite:
            continue                      # não termina no meio de uma sequência
        tentativas = ["-".join(f"{c:04x}" for c in cps)]
        sem_acessorio = [c for c in cps if c not in _ACESSORIOS]
        if sem_acessorio and sem_acessorio != cps:
            tentativas.append("-".join(f"{c:04x}" for c in sem_acessorio))
        if len(cps) == 1:
            # ❤ e ❤️ são a mesma figura; o conjunto guarda só a segunda forma.
            tentativas.append(f"{cps[0]:04x}-fe0f")
        for chave in tentativas:
            if chave in disponiveis:
                return chave, fim - i
    return "", 0


# ------------------------------------------------------------------- desenho

class Pedaco:
    """Um trecho já resolvido: ou texto de uma fonte só, ou uma imagem."""

    __slots__ = ("texto", "fonte", "imagem", "largura", "assento")

    def __init__(self, texto="", fonte=None, imagem=None, largura=0.0,
                 assento=0.88):
        self.texto = texto
        self.fonte = fonte
        self.imagem = imagem        # PIL.Image quando é emoji ou emote
        self.largura = largura
        # Quanto da altura da figura fica acima da linha de base. Emoji descem
        # um pouco abaixo dela; os selos do apelido, um pouco menos.
        self.assento = assento


class Tipografia:
    """Uma cadeia de fontes num tamanho, capaz de medir e desenhar."""

    def __init__(self, tamanho: int, peso: float = PESO_TEXTO):
        from PIL import ImageFont

        self.tamanho = max(1, int(tamanho))
        self.peso = float(peso)
        # Medido na referência: o emoji ocupa pouco mais que a altura de caixa
        # alta e se apoia na linha de base, não na altura inteira da fonte.
        self.emoji = round(self.tamanho * 1.10)
        self._font = ImageFont
        self._carregadas: dict[str, object] = {}
        self._caminhos = _arquivos_de_fonte()
        self.padrao = self._fonte(self._caminhos[0]) if self._caminhos else \
            ImageFont.load_default()

    def _fonte(self, caminho: str):
        """Abre sob demanda: carregar a cadeia inteira sempre seria desperdício."""
        if caminho not in self._carregadas:
            try:
                fonte = self._font.truetype(caminho, self.tamanho)
                _ajustar_variacao(fonte, self.peso)
            except OSError:
                fonte = None
            self._carregadas[caminho] = fonte
        return self._carregadas[caminho]

    def _para(self, cp: int):
        """A primeira fonte da cadeia que tem esse caractere, ou None.

        None quer dizer que nenhuma fonte instalada sabe desenhá-lo - tibetano
        e afins no Windows, por exemplo. Sai melhor omitir o caractere do que
        encher o apelido de quadradinhos vazios.
        """
        for caminho in self._caminhos:
            if _cobre(_cobertura(caminho), cp):
                fonte = self._fonte(caminho)
                if fonte is not None:
                    return fonte
        # Máquina sem nenhuma das fontes da cadeia: melhor a de emergência do
        # Pillow do que devolver o chat vazio.
        return None if self._caminhos else self.padrao

    # ------------------------------------------------------------- montagem

    def pedacos(self, texto: str, imagens: dict[int, object] | None = None) -> list[Pedaco]:
        """Quebra o texto em trechos desenháveis, na ordem em que aparecem.

        `imagens` mapeia posição no texto para uma figura a encaixar ali - é
        por onde entram os emotes próprios da live.
        """
        imagens = imagens or {}
        saida: list[Pedaco] = []
        acumulado, fonte_atual = [], None
        i = 0

        def fechar():
            nonlocal acumulado
            if acumulado:
                s = "".join(acumulado)
                saida.append(Pedaco(s, fonte_atual, largura=fonte_atual.getlength(s)))
                acumulado = []

        while i < len(texto):
            if i in imagens:
                fechar()
                saida.append(self._pedaco_imagem(imagens[i]))
                i += 1
                continue

            chave, quantos = _casar_emoji(texto, i)
            if chave:
                img = _imagem_emoji(chave, self.emoji)
                if img is not None:
                    fechar()
                    saida.append(self._pedaco_imagem(img))
                    i += quantos
                    continue

            cp = ord(texto[i])
            fonte = None if cp in _ACESSORIOS else self._para(cp)
            if fonte is None:
                i += 1               # acessório solto ou escrita sem fonte aqui
                continue
            if fonte is not fonte_atual:
                fechar()
                fonte_atual = fonte
            acumulado.append(texto[i])
            i += 1

        fechar()
        return saida

    def _pedaco_imagem(self, img, altura: int = 0, folga: float = 0.04,
                       assento: float = 0.88) -> Pedaco:
        alt = altura or self.emoji
        larg = max(1, round(img.width * alt / img.height)) if img.height else alt
        if (larg, alt) != img.size:
            from PIL import Image
            img = img.resize((larg, alt), Image.LANCZOS)
        return Pedaco(imagem=img, largura=larg + self.tamanho * folga,
                      assento=assento)

    def selo(self, img) -> Pedaco:
        """Um selo do apelido, na altura da linha e com o vão que o app deixa."""
        return self._pedaco_imagem(img, altura=round(self.tamanho * 0.97),
                                   folga=0.33, assento=0.79)

    # ------------------------------------------------------------- quebra

    def linhas(self, texto: str, largura_max: float, maximo: int = 3,
               imagens: dict[int, object] | None = None) -> list[list[Pedaco]]:
        """Distribui os pedaços em linhas que cabem em `largura_max`."""
        linhas: list[list[Pedaco]] = [[]]
        usado = 0.0

        for pedaco in self.pedacos(texto, imagens):
            for parte in self._fatias(pedaco, largura_max):
                if usado + parte.largura > largura_max and linhas[-1]:
                    if len(linhas) >= maximo:
                        return linhas
                    linhas.append([])
                    usado = 0.0
                    if parte.fonte is not None and not parte.texto.strip():
                        continue          # espaço não abre linha nova
                linhas[-1].append(parte)
                usado += parte.largura
        return [l for l in linhas if l] or [[]]

    def recortar(self, texto: str, largura_max: float) -> list[Pedaco]:
        """Uma linha só, cortada com reticências quando não cabe.

        É o que o cartão de presente do app faz: a pílula tem largura fixa e o
        nome comprido termina em "…" em vez de empurrar o resto para fora.
        """
        pedacos = self.pedacos(texto)
        if sum(p.largura for p in pedacos) <= largura_max:
            return pedacos

        fonte = next((p.fonte for p in pedacos if p.fonte is not None), None) \
            or self.padrao
        ponto = Pedaco("…", fonte, largura=fonte.getlength("…"))
        cabe = largura_max - ponto.largura
        saida, usado = [], 0.0
        for pedaco in pedacos:
            if usado + pedaco.largura <= cabe:
                saida.append(pedaco)
                usado += pedaco.largura
                continue
            if pedaco.fonte is not None:
                # Corta no meio da palavra mesmo: um apelido comprido de uma
                # palavra só precisa terminar em algum lugar.
                for k in range(len(pedaco.texto) - 1, 0, -1):
                    larg = pedaco.fonte.getlength(pedaco.texto[:k])
                    if usado + larg <= cabe:
                        saida.append(Pedaco(pedaco.texto[:k], pedaco.fonte,
                                            largura=larg))
                        break
            break
        return saida + [ponto]

    def _fatias(self, pedaco: Pedaco, largura_max: float) -> list[Pedaco]:
        """Divide um trecho de texto em pontos onde a linha pode quebrar.

        Palavras nas escritas com espaço; caractere a caractere nas que não
        têm, como o japonês e o chinês - é onde elas quebram de verdade.
        """
        if pedaco.fonte is None or pedaco.largura <= largura_max:
            if pedaco.fonte is None or len(pedaco.texto) <= 1:
                return [pedaco]

        partes, atual = [], ""
        for ch in pedaco.texto:
            se_quebra = _sem_espacos(ord(ch))
            if se_quebra or ch == " ":
                if ch == " ":
                    atual += ch
                if atual:
                    partes.append(atual)
                    atual = ""
                if se_quebra:
                    partes.append(ch)
            else:
                atual += ch
        if atual:
            partes.append(atual)

        return [Pedaco(s, pedaco.fonte, largura=pedaco.fonte.getlength(s))
                for s in partes]

    # ------------------------------------------------------------- desenho

    def escrever(self, img, desenho, x: float, base: float,
                 linha: list[Pedaco], cor: tuple[int, int, int, int]) -> float:
        """Desenha uma linha já montada, alinhada pela base do texto."""
        for pedaco in linha:
            if pedaco.imagem is not None:
                # A figura desce um pouco abaixo da base, como nas fontes de
                # emoji - alinhada pelo topo ela pareceria flutuando.
                topo = round(base - pedaco.imagem.height * pedaco.assento)
                img.paste(pedaco.imagem, (round(x), topo), pedaco.imagem)
            elif pedaco.texto:
                # `embedded_color` só muda algo nas fontes que trazem a cor
                # dentro do glifo; nas comuns o `fill` continua mandando.
                desenho.text((x, base), pedaco.texto, font=pedaco.fonte,
                             fill=cor, anchor="ls", embedded_color=True)
            x += pedaco.largura
        return x

    def largura(self, linha: list[Pedaco]) -> float:
        return sum(p.largura for p in linha)


def _sem_espacos(cp: int) -> bool:
    """Escritas que quebram entre caracteres, sem depender de espaço."""
    return (0x2E80 <= cp <= 0x9FFF          # radicais, kana, ideogramas
            or 0xAC00 <= cp <= 0xD7AF       # hangul
            or 0xF900 <= cp <= 0xFAFF       # ideogramas de compatibilidade
            or 0xFF00 <= cp <= 0xFF60       # formas largas
            or 0x3000 <= cp <= 0x303F)      # pontuação CJK
