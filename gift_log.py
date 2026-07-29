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


class GiftLogger:
    """Acompanha a live por websocket e anota os presentes recebidos."""

    def __init__(self, emit):
        self.emit = emit
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._parar = threading.Event()
        self._eventos: list[dict] = []
        self._comentarios: list[dict] = []
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

    def _anotar_comentario(self, event) -> None:
        """Guarda uma mensagem do chat.

        Não há decisão aqui sobre desenhar o chat no vídeo - só registro. O
        chat não pode ser recuperado depois, e cada linha custa pouco.
        """
        agora = datetime.now()
        usuario = getattr(event, "user", None)
        avatar = getattr(usuario, "avatar_thumb", None)
        urls = getattr(avatar, "url_list", None) or getattr(avatar, "m_urls", None) or []

        # O evento não traz hora própria (só um `screen_time` zerado), então o
        # instante é o da chegada. Logo após conectar vem a fila acumulada:
        # esses chegam juntos e ficam marcados, para o editor não confiar neles.
        acumulado = (time.monotonic() - self._conectado_em) < 2.0 if self._conectado_em else False

        registro = {
            "t": round((agora - self.inicio).total_seconds() if self.inicio else 0.0, 3),
            "hora": agora.isoformat(timespec="seconds"),
            "texto": getattr(event, "comment", "") or "",
            "de": getattr(usuario, "unique_id", "") or "",
            "apelido": getattr(usuario, "nickname", "") or "",
            "avatar": (urls[0] if urls else ""),
            "acumulado": acumulado,
        }

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
        if not eventos and not comentarios:
            return ""

        self.emit("status", ("finalizando", "Guardando as animações..."))
        animacoes = self._reunir_animacoes(eventos) if eventos else {}

        try:
            caminho = pacote.criar(
                destino=self.caminho_pacote,
                video=self.video,
                conta=self.username,
                inicio=self.inicio.isoformat(timespec="seconds") if self.inicio else "",
                presentes=eventos,
                animacoes=animacoes,
                comentarios=comentarios,
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
                         f"{len(comentarios)} comentários, {mb:.1f} MB)")
        return caminho

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
