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
import shutil
import threading
import time
from datetime import datetime

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

    # De trás para a frente: inserir no fim não desloca o que vem antes. A
    # ordem original desempata, senão emotes na mesma posição sairiam trocados.
    posicoes = {}
    for pos, _n, ident in sorted(entradas, key=lambda e: (-e[0], -e[1])):
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

    def _anotar(self, event) -> None:
        gift = event.gift
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
            "de": getattr(getattr(event, "user", None), "unique_id", "") or "",
            "apelido": getattr(getattr(event, "user", None), "nickname", "") or "",
        }

        with self._lock:
            self._eventos.append(registro)
            n = len(self._eventos)

        # Uma linha por presente, gravada na hora: se faltar luz, o que ja
        # aconteceu esta salvo.
        try:
            with open(self.caminho_jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tipo": "presente", **registro},
                                    ensure_ascii=False) + "\n")
                fh.flush()
        except OSError as e:
            self.emit("log", f"Não consegui gravar o registro: {e}")

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
        marcados = (getattr(event, "emote_list", None) if so_emotes
                    else getattr(event, "f315_emotes", None))
        texto, posicoes, achados = _com_emotes(
            "" if so_emotes else (getattr(event, "comment", "") or ""), marcados)
        esquerda, direita, selos = _selos_do_usuario(usuario)
        if achados or selos:
            with self._lock:
                self._figuras.update(achados)
                self._figuras.update(selos)

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

        try:
            with open(self.caminho_jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tipo": "chat", **registro},
                                    ensure_ascii=False) + "\n")
                fh.flush()
        except OSError:
            pass          # perder um comentário não pode derrubar a gravação

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
