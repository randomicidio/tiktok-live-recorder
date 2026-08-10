"""Acesso aos endpoints de LIVE do TikTok.

Resolve um @usuario para o estado atual da live e as URLs de video disponiveis.
Uma unica requisicao ao endpoint `api-live/user/room` costuma bastar; o
`webcast/room/info` fica como plano B quando o primeiro nao traz o streamData.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Da melhor para a pior. `origin` e o stream original que voce empurra para o
# TikTok (nao passa por recompressao), por isso vem primeiro.
QUALITY_ORDER = ["origin", "uhd", "hd", "sd", "ld"]

# Só o nome da faixa, sem adjetivo. "Média (HD)" soava como se o usuário
# estivesse perdendo alguma coisa, quando na maioria das lives essa é a única
# faixa que o TikTok entrega.
QUALITY_LABELS = {
    "origin": "Original",
    "uhd": "UHD",
    "hd": "HD",
    "sd": "SD",
    "ld": "LD",
}

# status devolvido em data.user.status
STATUS_LIVE = 2
STATUS_OFFLINE = 4


class TikTokError(Exception):
    """Falha ao falar com o TikTok ou ao interpretar a resposta."""


@dataclass
class StreamOption:
    """Uma qualidade disponivel da live."""

    quality: str
    flv: str = ""
    hls: str = ""
    resolution: str = ""
    vbitrate: int = 0

    @property
    def label(self) -> str:
        base = QUALITY_LABELS.get(self.quality, self.quality)
        if self.resolution:
            base += f" - {self.resolution}"
        if self.vbitrate:
            base += f" - {self.vbitrate // 1000} kbps"
        return base

    @property
    def best_url(self) -> str:
        """FLV e preferido: fluxo continuo, sem playlist, menos overhead."""
        return self.flv or self.hls


@dataclass
class LiveInfo:
    """Estado da live de um usuario num dado momento."""

    username: str
    room_id: str = ""
    is_live: bool = False
    title: str = ""
    nickname: str = ""
    streams: dict[str, StreamOption] = field(default_factory=dict)

    def pick(self, preferred: str = "origin") -> StreamOption | None:
        """Melhor qualidade disponivel, caindo para a proxima se faltar."""
        if preferred in self.streams and self.streams[preferred].best_url:
            return self.streams[preferred]
        for q in QUALITY_ORDER:
            opt = self.streams.get(q)
            if opt and opt.best_url:
                return opt
        return None

    @property
    def available_qualities(self) -> list[str]:
        return [q for q in QUALITY_ORDER if q in self.streams and self.streams[q].best_url]


def make_session(cookie_string: str = "") -> requests.Session:
    """Sessao HTTP com os cabecalhos que o TikTok espera de um navegador."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": WEB_UA,
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }
    )
    if cookie_string.strip():
        s.headers["Cookie"] = cookie_string.strip()
    return s


def _parse_stream_data(raw: object) -> dict[str, StreamOption]:
    """Le o blob `stream_data` e devolve as qualidades encontradas.

    O campo chega ora como string JSON, ora ja desserializado, dependendo do
    endpoint que respondeu.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, StreamOption] = {}
    for quality, node in (raw.get("data") or {}).items():
        if not isinstance(node, dict):
            continue
        main = node.get("main") or {}
        opt = StreamOption(
            quality=quality,
            flv=(main.get("flv") or "").strip(),
            hls=(main.get("hls") or "").strip(),
        )
        # sdk_params traz resolucao e bitrate reais dessa variante
        try:
            params = json.loads(main.get("sdk_params") or "{}")
            opt.resolution = str(params.get("resolution") or "")
            opt.vbitrate = int(params.get("vbitrate") or 0)
        except (ValueError, TypeError):
            pass
        if opt.best_url:
            out[quality] = opt
    return out


def _streams_from_flv_map(flv_map: dict) -> dict[str, StreamOption]:
    """Plano C: monta as qualidades a partir do mapa flv_pull_url cru."""
    alias = {"FULL_HD1": "uhd", "HD1": "hd", "SD1": "ld", "SD2": "sd"}
    out: dict[str, StreamOption] = {}
    for key, url in (flv_map or {}).items():
        q = alias.get(key)
        if q and url:
            out[q] = StreamOption(quality=q, flv=url)
    return out


def resolve(username: str, session: requests.Session | None = None) -> LiveInfo:
    """Consulta o TikTok e devolve o estado atual da live de `username`."""
    user = username.strip().lstrip("@")
    if not user:
        raise TikTokError("Informe o nome de usuário.")

    s = session or make_session()
    info = LiveInfo(username=user)

    try:
        r = s.get(
            "https://www.tiktok.com/api-live/user/room/",
            params={"aid": "1988", "sourceType": "54", "uniqueId": user},
            timeout=20,
        )
    except requests.RequestException as e:
        raise TikTokError(f"Sem resposta do TikTok: {e}") from e

    if r.status_code != 200:
        raise TikTokError(f"TikTok respondeu HTTP {r.status_code}.")

    try:
        payload = r.json()
    except ValueError as e:
        raise TikTokError("Resposta do TikTok não veio em JSON (bloqueio ou captcha?).") from e

    data = payload.get("data") or {}
    if not data:
        msg = payload.get("message") or "usuário não encontrado"
        raise TikTokError(f"Não consegui ler a conta @{user}: {msg}.")

    user_node = data.get("user") or {}
    live_room = data.get("liveRoom") or {}

    info.room_id = str(user_node.get("roomId") or "")
    info.nickname = str(user_node.get("nickname") or "")
    info.title = str(live_room.get("title") or "")

    status = user_node.get("status")
    if status is None:
        status = live_room.get("status")
    info.is_live = status == STATUS_LIVE and info.room_id not in ("", "0")

    # Caminho rapido: o proprio api-live ja embute os streams.
    stream_data = ((live_room.get("streamData") or {}).get("pull_data") or {}).get("stream_data")
    info.streams = _parse_stream_data(stream_data)

    # O webcast às vezes oferece uma variante maior que não veio no api-live.
    # Combinar as duas respostas é importante: não basta usar o segundo
    # endpoint só quando o primeiro falhou, pois ele pode ter trazido apenas
    # HD enquanto o outro já anuncia origin/UHD.
    if info.room_id not in ("", "0"):
        alternativos = _fetch_via_webcast(s, info)
        for qualidade, opcao in alternativos.items():
            atual = info.streams.get(qualidade)
            if atual is None or not atual.best_url:
                info.streams[qualidade] = opcao

    return info


def _fetch_via_webcast(session: requests.Session, info: LiveInfo) -> dict[str, StreamOption]:
    """Busca as URLs pelo endpoint webcast quando o api-live nao trouxe."""
    try:
        r = session.get(
            "https://webcast.tiktok.com/webcast/room/info/",
            params={"aid": "1988", "room_id": info.room_id},
            timeout=20,
        )
        payload = r.json()
    except (requests.RequestException, ValueError):
        return {}

    room = payload.get("data") or {}
    if not info.title:
        info.title = str(room.get("title") or "")

    stream_url = room.get("stream_url") or {}
    raw = ((stream_url.get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data")
    streams = _parse_stream_data(raw)
    if not streams:
        streams = _streams_from_flv_map(stream_url.get("flv_pull_url") or {})
    return streams
