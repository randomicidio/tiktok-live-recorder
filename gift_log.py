"""Registra os presentes da live, sincronizados com a gravação.

As animações de presente não existem no vídeo gravado - elas são desenhadas no
aparelho de quem assiste. O que dá para guardar é QUANDO cada presente chegou,
em relação ao início da gravação, e QUAL animação corresponde a ele.

Com esse registro e o vídeo, a versão com animações pode ser gerada depois, a
qualquer momento, sem ter decidido nada durante a transmissão.

Durante a live o custo é escrever uma linha de texto por presente.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta

import effects_api
import pacote

# A biblioteca de websocket e opcional: sem ela o gravador funciona igual,
# apenas sem registrar presentes.
try:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent, GiftEvent
    DISPONIVEL = True
except ImportError:  # pragma: no cover
    TikTokLiveClient = None
    GiftEvent = None
    CommentEvent = None
    DISPONIVEL = False

# Mensagem feita só de emote é outro evento, e nem toda versão da biblioteca
# expõe: sem ele o resto continua igual, só não entram essas mensagens.
try:
    from TikTokLive.events import EmoteChatEvent
except ImportError:  # pragma: no cover
    EmoteChatEvent = None


# Atalhos que o cliente do TikTok às vezes envia como texto puro. Quando o
# websocket traz a imagem oficial, ela é sempre preferida; esta lista só evita
# que códigos sem metadados como "[heart]" ou "[rosiecute]" apareçam crus no
# vídeo. São equivalentes Unicode, não cópias das artes proprietárias.
_EMOJIS_DE_ATALHO = {
    "smile": "🙂", "happy": "😄", "angry": "😠", "cry": "😢",
    "embarrassed": "😳", "surprised": "😮", "wronged": "🥺", "shout": "😱",
    "flushed": "😳", "yummy": "😋", "complacent": "😌", "drool": "🤤",
    "scream": "😱", "weep": "😭", "speechless": "😶", "funnyface": "😜",
    "laughwithtears": "😂", "laughcry": "😂", "wicked": "😈",
    "facewithrollingeyes": "🙄", "sulk": "😞", "thinking": "🤔",
    "think": "🤔", "lovely": "🥰", "greedy": "🤑", "wow": "😮",
    "joyful": "😁", "hehe": "😏", "slap": "👋", "tears": "😭",
    "stun": "😵", "cute": "😍", "blink": "😉", "disdain": "😒",
    "astonish": "😲", "rage": "🤬", "cool": "😎", "excited": "🤩",
    "proud": "😌", "smileface": "😊", "evil": "😈", "angel": "😇",
    "laugh": "😂", "pride": "😤", "nap": "😴", "loveface": "😍",
    "awkward": "😅", "shock": "😱", "heart": "❤️", "love": "❤️",
    # Pacotes mais novos de personagens do TikTok.
    "rockyserious": "😐", "rockyloveit": "😍", "rockyproud": "😤",
    "rockycool": "😎", "rosiedislike": "😒", "rosiekisskiss": "😘",
    "rosieawkward": "😅", "rosiecute": "🥰", "jolliekissingface": "😘",
    "jolliewow": "😮", "jolliesatisfied": "😌", "jolliespeechless": "😶",
    "sagethink": "🤔", "sagefulfilled": "😌", "sageclever": "🧐",
    "sagemoney": "🤑",
}
_ATALHO_TIKTOK = re.compile(r"\[([a-z0-9_]+)\]", re.IGNORECASE)


def _num(valor, padrao=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def _ids_de_efeito(gift) -> list[int]:
    """Todos os identificadores de efeito que o presente carrega.

    O `primary_effect_id` costuma ser o da animação de tela cheia, mas alguns
    presentes trazem variantes em `cross_screen_effect_info`. Guardamos todos e
    deixamos a escolha para a hora de gerar o vídeo.
    """
    ids = []
    primario = _num(getattr(gift, "primary_effect_id", 0))
    if primario:
        ids.append(primario)

    cse = getattr(gift, "cross_screen_effect_info", None)
    if cse is not None:
        for campo in ("single_action_effect_ids", "action_effect_ids",
                      "reaction_effect_ids"):
            mapa = getattr(cse, campo, None) or {}
            valores = mapa.values() if hasattr(mapa, "values") else mapa
            for v in valores:
                n = _num(v)
                if n and n not in ids:
                    ids.append(n)
    return ids


def _figura_do_emote(modelo) -> tuple[str, str]:
    """(identificador, URL) de um emote próprio da live."""
    if modelo is None:
        return "", ""
    imagem = getattr(modelo, "image", None)
    urls = (getattr(imagem, "m_urls", None) or getattr(imagem, "url_list", None) or [])
    url = urls[0] if urls else ""
    if not url:
        return "", ""
    ident = str(getattr(modelo, "emote_id", "") or "").strip()
    if not ident:
        # Alguns não vêm com id; a própria URL identifica bem o suficiente.
        ident = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return ident, url


def _emotes_do_evento(event, so_emotes: bool = False):
    """Lê os emotes que o TikTok mandou, tolerando nomes de campos novos.

    O protocolo do TikTok muda com frequência. Comentários normais trazem os
    emotes indexados em ``f315_emotes`` hoje, enquanto mensagens só de emote
    usam ``emote_list``. Procurar as duas formas mantém a gravação compatível
    quando a biblioteca expõe uma delas com outro nome.
    """
    campos = (("emote_list", "emotes", "f315_emotes") if so_emotes
              else ("f315_emotes", "emote_list", "emotes"))
    for campo in campos:
        itens = getattr(event, campo, None)
        if itens:
            return itens
    return []


def _traduzir_atalhos_tiktok(texto: str) -> str:
    """Converte somente os atalhos conhecidos que vieram sem imagem."""
    return _ATALHO_TIKTOK.sub(
        lambda m: _EMOJIS_DE_ATALHO.get(m.group(1).lower(), m.group(0)), texto)


def _imagem(modelo) -> str:
    """Primeira URL de um ImageModel, venha ela com que nome vier."""
    urls = (getattr(modelo, "m_urls", None) or getattr(modelo, "url_list", None) or [])
    return urls[0] if urls else ""


def _receita_do_selo(url: str, texto: str = "", cor: str = "") -> tuple[str, dict]:
    """Identificador e o que é preciso para montar a figura do selo."""
    chave = hashlib.md5(f"{url}|{texto}|{cor}".encode("utf-8")).hexdigest()[:16]
    receita = {"url": url}
    if texto:
        receita["texto"] = texto
        receita["cor"] = cor
    return chave, receita


def _selos_do_usuario(usuario) -> tuple[list[str], list[str], dict[str, dict]]:
    """Selos que acompanham o apelido: nível, clube de fãs, posição no ranking.

    O TikTok manda a maioria já desenhada, então o melhor é usar a imagem dele
    em vez de tentar redesenhar. Só os `combine_badge` vêm partidos em ícone
    mais texto, e esses são montados depois, ao fechar o pacote.
    """
    esquerda: list[str] = []
    direita: list[str] = []
    receitas: dict[str, dict] = {}

    def juntar(url, lado, texto="", cor=""):
        if not url:
            return
        chave, receita = _receita_do_selo(url, texto, cor)
        if chave in receitas:
            return                       # o mesmo selo veio por dois caminhos
        receitas[chave] = receita
        (direita if lado == 2 else esquerda).append(chave)

    lista = getattr(usuario, "badge_list", None) or []
    for selo in lista:
        lado = int(getattr(selo, "position", 0) or 0)
        imagem = getattr(selo, "image_badge", None)
        if imagem is not None and _imagem(getattr(imagem, "image_model", None)):
            juntar(_imagem(imagem.image_model), lado)
            continue
        combinado = getattr(selo, "combine_badge_struct", None)
        if combinado is not None:
            fundo = getattr(combinado, "background", None)
            texto = getattr(combinado, "str", "") or ""
            if not texto:
                bt = getattr(combinado, "text", None)
                texto = getattr(bt, "default_pattern", "") or ""
            juntar(_imagem(getattr(combinado, "icon", None)), lado, texto,
                   getattr(fundo, "background_color_code", "") or "")

    if not lista:
        # Sem a lista pronta, monta a partir dos campos soltos - a ordem é a
        # que o app usa: nível, depois clube de fãs.
        honra = getattr(usuario, "user_honor", None)
        if honra is not None:
            juntar(_imagem(getattr(honra, "im_icon_with_level", None)) or
                   _imagem(getattr(honra, "im_icon", None)), 1)
        clube = getattr(usuario, "fans_club", None)
        dados = getattr(clube, "data", None) if clube is not None else None
        icones = getattr(getattr(dados, "badge", None), "icons", None) or {}
        for _k, modelo in sorted(icones.items()):
            juntar(_imagem(modelo), 1)
            break
        for modelo in (getattr(usuario, "user_badges", None) or []):
            juntar(_imagem(modelo), 1)

    return esquerda, direita, receitas


SELO_ALTURA = 72          # px na figura guardada; o chat reduz na hora de usar


def _montar_selo(icone: bytes, texto: str, cor: str | None) -> bytes:
    """Desenha o selo que veio partido em ícone mais texto.

    Alguns selos - o do clube de fãs, por exemplo - não vêm prontos: o TikTok
    manda o ícone, o nome e a cor de fundo, e o app junta. É o único pedaço do
    chat que a gente redesenha em vez de copiar, então fica próximo, não igual.
    """
    import io as _io

    from PIL import Image, ImageDraw

    import tipografia

    try:
        fundo_cor = _cor(cor) or (255, 90, 60, 255)
        try:
            ico = Image.open(_io.BytesIO(icone)).convert("RGBA")
        except OSError:
            ico = None

        alt = SELO_ALTURA
        tipo = tipografia.Tipografia(round(alt * 0.62))
        pedacos = tipo.pedacos(texto[:16])
        larg_texto = round(tipo.largura(pedacos))
        ico_l = round(ico.width * (alt * 0.72) / ico.height) if ico else 0
        margem = round(alt * 0.16)
        larg = margem + (ico_l + margem // 2 if ico else 0) + larg_texto + margem

        selo = Image.new("RGBA", (larg, alt), (0, 0, 0, 0))
        d = ImageDraw.Draw(selo)
        d.rounded_rectangle([0, 0, larg - 1, alt - 1], radius=round(alt * 0.28),
                            fill=fundo_cor)
        x = margem
        if ico:
            ico = ico.resize((ico_l, round(alt * 0.72)), Image.LANCZOS)
            selo.paste(ico, (x, (alt - ico.height) // 2), ico)
            x += ico_l + margem // 2
        tipo.escrever(selo, d, x, alt * 0.72, pedacos, (255, 255, 255, 255))

        saida = _io.BytesIO()
        selo.save(saida, "PNG")
        return saida.getvalue()
    except Exception:                                # noqa: BLE001
        return b""


def _cor(codigo: str | None) -> tuple[int, int, int, int] | None:
    """Converte '#RRGGBB' (ou '#AARRGGBB') no que o Pillow entende."""
    if not codigo:
        return None
    h = codigo.strip().lstrip("#")
    try:
        if len(h) == 6:
            return (*(int(h[i:i + 2], 16) for i in (0, 2, 4)), 255)
        if len(h) == 8:
            return (*(int(h[i:i + 2], 16) for i in (2, 4, 6)), int(h[0:2], 16))
    except ValueError:
        pass
    return None


def _com_emotes(texto: str, marcados) -> tuple[str, dict[str, str], dict[str, dict]]:
    """Encaixa os emotes da live no texto, devolvendo texto e mapeamentos.

    O evento diz em que posição do texto cada figura entra. Uma marca entra ali
    para a posição continuar válida no pacote, onde só existe o texto salvo -
    e para a mensagem feita só de emotes ter onde pendurá-los.
    """
    entradas = []
    receitas = {}
    for n, item in enumerate(marcados or []):
        modelo = getattr(item, "emote_model", None) or getattr(item, "emote", None) or item
        ident, url = _figura_do_emote(modelo)
        if not ident:
            continue
        pos = getattr(item, "index", None)
        entradas.append((len(texto) if pos is None else max(0, min(len(texto), int(pos))),
                         n, ident))
        receitas[ident] = {"url": url}

    # De trás para a frente: alterar o fim não desloca o que vem antes. A
    # ordem original desempata, senão emotes na mesma posição sairiam trocados.
    posicoes = {}
    for pos, _n, ident in sorted(entradas, key=lambda e: (-e[0], -e[1])):
        # Alguns clientes enviam o atalho literal (por exemplo [rosiecute])
        # junto do índice e da imagem. Nesse caso a marca substitui o atalho;
        # inserir antes dele era o que deixava o texto entre colchetes visível
        # no chat exportado.
        fim = texto.find("]", pos, min(len(texto), pos + 96))
        if pos < len(texto) and texto[pos:pos + 1] == "[" and fim >= pos:
            removidos = fim + 1 - pos
            texto = texto[:pos] + pacote.MARCA_EMOTE + texto[fim + 1:]
            deslocamento = 1 - removidos
            posicoes = {p + deslocamento if p >= fim + 1 else p: i
                         for p, i in posicoes.items()}
        else:
            texto = texto[:pos] + pacote.MARCA_EMOTE + texto[pos:]
            posicoes = {p + 1 if p >= pos else p: i for p, i in posicoes.items()}
        posicoes[pos] = ident
    return texto, {str(p): i for p, i in posicoes.items()}, receitas


class GiftLogger:
    """Acompanha a live por websocket e anota os presentes recebidos."""

    def __init__(self, emit):
        self.emit = emit
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._parar = threading.Event()
        self._eventos: list[dict] = []
        self._comentarios: list[dict] = []
        # Figuras do chat (emotes da live e selos do apelido): identificador ->
        # como montá-la. Só são baixadas no fim, para não gastar rede durante
        # a transmissão.
        self._figuras: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Ao (re)conectar o TikTok despeja a fila recente de uma vez; esses
        # eventos chegam todos no mesmo instante e não valem como sincronia.
        self._conectado_em = 0.0
        self._caiu_antes = False

        self.inicio: datetime | None = None
        self.caminho_jsonl = ""       # temporário, à prova de queda
        self.caminho_pacote = ""      # o .ttgifts final
        self._temp_animacoes = ""
        self.username = ""
        self.video = ""

    # ------------------------------------------------------------ controle

    @property
    def ativo(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._eventos)

    @property
    def total_chat(self) -> int:
        with self._lock:
            return len(self._comentarios)

    def start(self, username: str, base_saida: str, inicio: datetime) -> bool:
        """Começa a registrar. `base_saida` é o caminho do vídeo sem extensão."""
        if not DISPONIVEL:
            self.emit("log", "Registro de presentes indisponível (falta a "
                             "biblioteca TikTokLive).")
            return False
        if self.ativo:
            return False

        self.username = username.strip().lstrip("@")
        self.inicio = inicio
        self.video = base_saida + ".mp4"
        self.caminho_jsonl = base_saida + "_presentes.jsonl"
        self.caminho_pacote = base_saida + pacote.EXTENSAO
        self._temp_animacoes = base_saida + "_anim_tmp"

        with self._lock:
            self._eventos.clear()
        self._parar.clear()
        self._thread = threading.Thread(target=self._rodar, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> str:
        """Encerra e monta o pacote. Devolve o caminho do .ttgifts."""
        self._parar.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=15)
        return self.finalizar()

    # -------------------------------------------------------------- thread

    def _rodar(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._acompanhar())
        except (asyncio.CancelledError, RuntimeError):
            pass
        except Exception as e:                       # noqa: BLE001
            self.emit("log", f"Registro de presentes parou: {e}")
        finally:
            try:
                self._loop.close()
            except RuntimeError:
                pass

    async def _acompanhar(self) -> None:
        cliente = TikTokLiveClient(unique_id=self.username)

        @cliente.on(GiftEvent)
        async def _ao_receber(event):               # noqa: ANN001
            try:
                self._anotar(event)
            except Exception as e:                   # noqa: BLE001
                self.emit("log", f"Presente ignorado por erro: {e}")

        @cliente.on(CommentEvent)
        async def _ao_comentar(event):              # noqa: ANN001
            try:
                self._anotar_comentario(event)
            except Exception as e:                   # noqa: BLE001
                self.emit("log", f"Comentário ignorado por erro: {e}")

        if EmoteChatEvent is not None:
            @cliente.on(EmoteChatEvent)
            async def _ao_emotar(event):            # noqa: ANN001
                try:
                    self._anotar_comentario(event, so_emotes=True)
                except Exception as e:               # noqa: BLE001
                    self.emit("log", f"Emote ignorado por erro: {e}")

        @cliente.on(ConnectEvent)
        async def _ao_conectar(event):              # noqa: ANN001
            self._conectado_em = time.monotonic()
            if self._caiu_antes:
                self._caiu_antes = False
                self.emit("log", "Websocket reconectado. Presentes e chat "
                                 "voltaram a ser registrados normalmente.")
            else:
                self.emit("log", "Websocket conectado. Registrando presentes e chat.")

        self.emit("log", "Registro de presentes e chat ligado.")
        while not self._parar.is_set():
            try:
                await cliente.connect(process_connect_events=False,
                                      fetch_live_check=True)
            except Exception as e:                   # noqa: BLE001
                if self._parar.is_set():
                    break
                self._caiu_antes = True
                # A gravação do vídeo não depende disso - deixar claro evita
                # susto de quem está transmitindo.
                self.emit("log", f"Websocket caiu ({e}). Religando em 10s "
                                 f"(a gravação do vídeo continua normal).")
                await asyncio.sleep(10)
                continue
            if self._parar.is_set():
                break
            await asyncio.sleep(5)

    # ------------------------------------------------------------- anotar

    def _anexar(self, linha: dict) -> bool:
        """Grava uma linha no .jsonl na hora, com flush.

        O arquivo e a unica copia que sobrevive a uma queda de energia: o
        pacote so e montado no fim. Por isso cada linha vai para o disco assim
        que acontece, em vez de esperar um buffer encher.
        """
        try:
            with open(self.caminho_jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(linha, ensure_ascii=False) + "\n")
                fh.flush()
            return True
        except OSError:
            return False

    def _anotar(self, event) -> None:
        gift = event.gift
        usuario = getattr(event, "user", None) or getattr(event, "user_info", None)
        foto = getattr(usuario, "avatar_thumb", None)
        urls = getattr(foto, "url_list", None) or getattr(foto, "m_urls", None) or []
        figura = getattr(gift, "image", None) or getattr(gift, "icon", None)
        icones = (getattr(figura, "url_list", None)
                  or getattr(figura, "m_urls", None) or [])
        agora = datetime.now()
        segundos = (agora - self.inicio).total_seconds() if self.inicio else 0.0

        registro = {
            "t": round(segundos, 3),
            "hora": agora.isoformat(timespec="seconds"),
            "gift_id": _num(getattr(gift, "id", 0)),
            "nome": getattr(gift, "name", "") or "",
            "diamantes": _num(getattr(gift, "diamond_count", 0)),
            "quantidade": _num(getattr(event, "repeat_count", 1), 1),
            "effect_ids": _ids_de_efeito(gift),
            "de": getattr(usuario, "unique_id", "") or "",
            "apelido": getattr(usuario, "nickname", "") or "",
            "avatar": urls[0] if urls else "",
            "icone": icones[0] if icones else "",
        }

        with self._lock:
            self._eventos.append(registro)
            n = len(self._eventos)

        # Uma linha por presente, gravada na hora: se faltar luz, o que ja
        # aconteceu esta salvo.
        if not self._anexar({"tipo": "presente", **registro}):
            self.emit("log", "Não consegui gravar o registro deste presente.")

        self.emit("log", f"[presente {n}] {registro['apelido']} → "
                         f"{registro['nome']} x{registro['quantidade']} "
                         f"aos {segundos:.0f}s")

    def _anotar_comentario(self, event, so_emotes: bool = False) -> None:
        """Guarda uma mensagem do chat.

        Não há decisão aqui sobre desenhar o chat no vídeo - só registro. O
        chat não pode ser recuperado depois, e cada linha custa pouco.
        """
        agora = datetime.now()
        usuario = getattr(event, "user", None) or getattr(event, "user_info", None)
        avatar = getattr(usuario, "avatar_thumb", None)
        urls = getattr(avatar, "url_list", None) or getattr(avatar, "m_urls", None) or []

        # Emotes próprios da live: dentro do texto num comentário comum, ou
        # sozinhos quando a mensagem inteira é um emote.
        marcados = _emotes_do_evento(event, so_emotes)
        texto, posicoes, achados = _com_emotes(
            "" if so_emotes else (getattr(event, "comment", "") or ""), marcados)
        # Só sobra aqui o que o TikTok enviou sem modelo/imagem estruturada.
        texto = _traduzir_atalhos_tiktok(texto)
        esquerda, direita, selos = _selos_do_usuario(usuario)
        if achados or selos:
            with self._lock:
                novas = {c: r for c, r in {**achados, **selos}.items()
                         if c not in self._figuras}
                self._figuras.update(novas)
            # As receitas viviam so na memoria, e uma queda de luz levava junto
            # os emotes e os selos de todo o chat - o texto se recuperava pelo
            # .jsonl, as figuras nao. Uma linha por receita nova (nao por
            # mensagem) resolve: sao poucas dezenas numa live inteira.
            for chave, receita in novas.items():
                self._anexar({"tipo": "figura", "chave": chave, "receita": receita})

        # O evento não traz hora própria (só um `screen_time` zerado), então o
        # instante é o da chegada. Logo após conectar vem a fila acumulada:
        # esses chegam juntos e ficam marcados, para o editor não confiar neles.
        acumulado = (time.monotonic() - self._conectado_em) < 2.0 if self._conectado_em else False

        registro = {
            "t": round((agora - self.inicio).total_seconds() if self.inicio else 0.0, 3),
            "hora": agora.isoformat(timespec="seconds"),
            "texto": texto,
            "de": getattr(usuario, "unique_id", "") or "",
            "apelido": getattr(usuario, "nickname", "") or "",
            "avatar": (urls[0] if urls else ""),
            "acumulado": acumulado,
        }
        if posicoes:
            registro["emotes"] = posicoes
        if esquerda:
            registro["selos"] = esquerda
        if direita:
            registro["selos_direita"] = direita

        with self._lock:
            self._comentarios.append(registro)

        # Perder um comentário não pode derrubar a gravação: sem aviso aqui.
        self._anexar({"tipo": "chat", **registro})

    # ----------------------------------------------------------- finalizar

    def finalizar(self) -> str:
        """Junta registro e animações num único .ttgifts ao lado do vídeo.

        As animações vão embutidas de propósito: a URL do CDN expira em horas e
        um presente pode sair de catálogo, mas o pacote continua servindo.
        """
        with self._lock:
            eventos = list(self._eventos)
            comentarios = list(self._comentarios)
            receitas = dict(self._figuras)
        if not eventos and not comentarios:
            return ""

        self.emit("status", ("finalizando", "Guardando as animações..."))
        animacoes = self._reunir_animacoes(eventos) if eventos else {}
        figuras = self._reunir_figuras(receitas)
        figuras.update(self._reunir_icones_presentes(eventos))

        try:
            caminho = pacote.criar(
                destino=self.caminho_pacote,
                video=self.video,
                conta=self.username,
                inicio=self.inicio.isoformat(timespec="seconds") if self.inicio else "",
                presentes=eventos,
                animacoes=animacoes,
                comentarios=comentarios,
                figuras=figuras,
            )
        except pacote.PacoteError as e:
            self.emit("log", f"Falha ao criar o pacote: {e}")
            return ""

        # os temporários já estão dentro do pacote
        shutil.rmtree(self._temp_animacoes, ignore_errors=True)
        try:
            if os.path.exists(self.caminho_jsonl):
                os.remove(self.caminho_jsonl)
        except OSError:
            pass

        mb = os.path.getsize(caminho) / (1024 * 1024)
        self.emit("log", f"Pacote salvo: {os.path.basename(caminho)} "
                         f"({len(eventos)} presentes, {len(animacoes)} animações, "
                         f"{len(comentarios)} comentários, {len(figuras)} figuras, "
                         f"{mb:.1f} MB)")
        return caminho

    def _reunir_figuras(self, receitas: dict[str, dict]) -> dict[str, bytes]:
        """Baixa as figuras do chat: emotes da live e selos do apelido.

        Vão embutidas pelo mesmo motivo das animações: a URL do CDN expira, e
        um emote de assinante some do ar quando a pessoa deixa de assinar.
        """
        if not receitas:
            return {}
        import requests

        self.emit("status", ("finalizando", "Guardando os emotes e selos do chat..."))
        sessao = requests.Session()
        achados: dict[str, bytes] = {}
        for ident, receita in receitas.items():
            try:
                r = sessao.get(receita.get("url", ""), timeout=20)
                if r.status_code != 200 or not r.content:
                    continue
            except requests.RequestException:
                continue
            if receita.get("texto"):
                montado = _montar_selo(r.content, receita["texto"], receita.get("cor"))
                if montado:
                    achados[ident] = montado
            else:
                achados[ident] = r.content
        if len(achados) < len(receitas):
            self.emit("log", f"Figuras do chat: {len(achados)} de {len(receitas)}.")
        return achados

    def _reunir_icones_presentes(self, eventos: list[dict]) -> dict[str, bytes]:
        """Guarda a imagem oficial de cada presente para o contador futuro."""
        import requests

        achados = {}
        por_url: dict[str, str] = {}
        for n, evento in enumerate(eventos):
            url = evento.get("icone") or ""
            if not url:
                continue
            if url in por_url:
                evento["icone"] = por_url[url]
                continue
            ident = f"presente-{n}"
            try:
                r = requests.get(url, timeout=20)
                if r.status_code == 200 and r.content:
                    achados[ident] = r.content
                    por_url[url] = ident
                    evento["icone"] = ident
                    continue
            except requests.RequestException:
                pass
            evento["icone"] = ""
        return achados

    def _reunir_animacoes(self, eventos: list[dict]) -> dict[str, str]:
        """Resolve e baixa a animação de cada presente. Anota qual ficou em cada um."""
        ids = []
        for e in eventos:
            for i in e.get("effect_ids") or []:
                if i not in ids:
                    ids.append(i)
        if not ids:
            return {}

        try:
            efeitos = effects_api.resolve(ids)
        except effects_api.EffectError as e:
            self.emit("log", f"Não consegui consultar as animações: {e}")
            return {}

        os.makedirs(self._temp_animacoes, exist_ok=True)
        prontas: dict[int, str] = {}          # effect_id -> video_md5
        pastas: dict[str, str] = {}           # video_md5 -> pasta

        for eid, efeito in efeitos.items():
            try:
                effects_api.baixa(efeito, self._temp_animacoes)
            except effects_api.EffectError:
                continue
            if efeito.disponivel:
                prontas[eid] = efeito.video_md5
                pastas[efeito.video_md5] = efeito.pasta

        # cada presente aponta para a animação que de fato existe
        for e in eventos:
            for i in e.get("effect_ids") or []:
                if i in prontas:
                    e["animacao"] = prontas[i]
                    break

        sem = sum(1 for e in eventos if not e.get("animacao"))
        if sem:
            self.emit("log", f"{sem} presente(s) sem animação de tela (normal para "
                             f"os pequenos).")
        return pastas


# ------------------------------------------------------------------- resgate

def ler_jsonl(caminho: str) -> tuple[list[dict], list[dict], dict[str, dict]]:
    """Relê um registro cru: (presentes, comentários, receitas de figuras).

    Linhas quebradas são puladas em silêncio - uma queda de energia costuma
    cortar a última no meio, e perder um comentário não pode custar o resto.
    """
    presentes: list[dict] = []
    comentarios: list[dict] = []
    figuras: dict[str, dict] = {}
    try:
        with open(caminho, encoding="utf-8") as fh:
            for linha in fh:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    dado = json.loads(linha)
                except ValueError:
                    continue
                tipo = dado.pop("tipo", "")
                if tipo == "presente":
                    presentes.append(dado)
                elif tipo == "chat":
                    comentarios.append(dado)
                elif tipo == "figura" and dado.get("chave"):
                    figuras[dado["chave"]] = dado.get("receita") or {}
    except OSError:
        return [], [], {}
    return presentes, comentarios, figuras


def recuperar(jsonl: str, video: str, emit) -> str:
    """Monta o .ttgifts de uma gravação que não chegou a fechar.

    Reusa o `finalizar()` do caminho normal: é ele que baixa as animações e as
    figuras, e é justamente esse download que a queda de energia impediu. Por
    isso o resgate corre contra o relógio - as URLs do CDN expiram em horas.
    """
    presentes, comentarios, figuras = ler_jsonl(jsonl)
    if not presentes and not comentarios:
        return ""

    logger = GiftLogger(emit)
    base = jsonl[: -len("_presentes.jsonl")] if jsonl.endswith("_presentes.jsonl") \
        else os.path.splitext(jsonl)[0]
    logger.caminho_jsonl = jsonl
    logger.caminho_pacote = base + pacote.EXTENSAO
    logger._temp_animacoes = base + "_anim_tmp"
    logger.video = video
    logger.username = os.path.basename(base).split("_")[0]
    logger._eventos = presentes
    logger._comentarios = comentarios
    logger._figuras = figuras

    # O instante zero vem da primeira linha gravada: os `t` de cada evento são
    # relativos a ele, então o que importa aqui é só registrar a data no
    # manifesto - a sincronia já está nos próprios `t`.
    for linha in (presentes + comentarios):
        try:
            logger.inicio = datetime.fromisoformat(linha["hora"]) - \
                timedelta(seconds=float(linha.get("t") or 0))
            break
        except (KeyError, ValueError):
            continue

    if not figuras and comentarios:
        # Gravações anteriores a esta versão não guardavam as receitas.
        perdidas = sum(1 for c in comentarios if c.get("emotes") or c.get("selos")
                       or c.get("selos_direita"))
        if perdidas:
            emit("log", f"{perdidas} mensagem(ns) tinham emotes ou selos que este "
                        f"registro não guardou; o texto vem inteiro, as figuras não.")

    return logger.finalizar()
