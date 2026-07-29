"""Resolve efeitos de presente do TikTok e baixa as animações.

As animações de tela cheia não vêm no vídeo da live: o aplicativo de quem
assiste as desenha por cima. Mas os arquivos são públicos - o mesmo endpoint
que o TikTok LIVE Studio usa responde sem login, e devolve um .zip com o vídeo
(com transparência) e um config.json descrevendo como compor.

Não é preciso ter o LIVE Studio instalado. Se ele existir na máquina, o cache
dele é aproveitado só para poupar download.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass

import requests

import resources

API_URL = "https://webcast22-normal-c-alisg.tiktokv.com/webcast/assets/effects/"

# Os mesmos parametros que o LIVE Studio envia. `video_types` e o que pede a
# variante com transparencia - sem ele vem so a versao opaca.
PARAMS_BASE = {
    "aid": "8311",
    "app_name": "tiktok_live_studio",
    "channel": "studio",
    "device_platform": "windows",
    "version_code": "1.32.2",
    "webcast_sdk_version": "1322",
    "effect_sdk_version": "21.2.0",
    "app_language": "pt-BR",
    "language": "pt-BR",
    "webcast_language": "pt-BR",
    "live_mode": "6",
    "video_types": "h264,webm_480p,webm_480p_lowfps",
}

UA = "TikTok LIVE Studio"

# Cache do LIVE Studio, quando instalado. Serve so como atalho.
CACHE_LIVE_STUDIO = os.path.join(
    os.environ.get("APPDATA", ""), "TikTok LIVE Studio", "fileCache", "gift"
)


class EffectError(Exception):
    """Falha ao resolver ou baixar a animação de um presente."""


@dataclass
class Efeito:
    """Uma animação de presente pronta para ser composta."""

    effect_id: int
    video_md5: str = ""
    urls: list[str] = None
    pasta: str = ""          # onde o pacote foi extraido (ou achado)

    def __post_init__(self):
        if self.urls is None:
            self.urls = []

    @property
    def disponivel(self) -> bool:
        return bool(self.pasta and os.path.exists(os.path.join(self.pasta, "config.json")))

    def config(self) -> dict:
        """Geometria da composicao: onde esta a imagem e onde esta o alfa."""
        with open(os.path.join(self.pasta, "config.json"), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("portrait") or {}

    @property
    def arquivo_video(self) -> str:
        cfg = self.config()
        return os.path.join(self.pasta, cfg.get("path", ""))


def _sessao() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    return s


def resolve(effect_ids: list[int], session: requests.Session | None = None) -> dict[int, Efeito]:
    """Pergunta ao TikTok onde estão as animações desses efeitos.

    Aceita varios ids de uma vez - a API responde todos numa chamada.
    """
    ids = [int(e) for e in effect_ids if e]
    if not ids:
        return {}

    s = session or _sessao()
    params = dict(PARAMS_BASE, effect_ids=",".join(str(i) for i in ids))
    try:
        r = s.get(API_URL, params=params, timeout=30)
    except requests.RequestException as e:
        raise EffectError(f"Sem resposta da API de efeitos: {e}") from e

    if r.status_code != 200:
        raise EffectError(f"API de efeitos respondeu HTTP {r.status_code}.")

    try:
        payload = r.json()
    except ValueError as e:
        raise EffectError("Resposta da API de efeitos não veio em JSON.") from e

    saida: dict[int, Efeito] = {}
    for a in ((payload.get("data") or {}).get("assets") or []):
        try:
            eid = int(a.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not eid:
            continue
        vr = (a.get("video_resource_list") or [{}])[0]
        url_node = vr.get("video_url") or {}
        saida[eid] = Efeito(
            effect_id=eid,
            video_md5=vr.get("video_md5") or "",
            urls=list(url_node.get("url_list") or []),
        )
    return saida


def procura_local(video_md5: str) -> str:
    """Já existe no cache do LIVE Studio? Evita baixar de novo."""
    if not video_md5 or not os.path.isdir(CACHE_LIVE_STUDIO):
        return ""
    pasta = os.path.join(CACHE_LIVE_STUDIO, video_md5)
    if os.path.exists(os.path.join(pasta, "config.json")):
        return pasta
    return ""


def baixa(efeito: Efeito, destino_base: str,
          session: requests.Session | None = None) -> Efeito:
    """Garante que a animação esteja em disco. Devolve o efeito com a pasta.

    Ordem: pasta ja baixada -> cache do LIVE Studio -> download do CDN.
    """
    if not efeito.video_md5:
        raise EffectError(f"Efeito {efeito.effect_id} não tem arquivo de vídeo.")

    proprio = os.path.join(destino_base, efeito.video_md5)
    if os.path.exists(os.path.join(proprio, "config.json")):
        efeito.pasta = proprio
        return efeito

    local = procura_local(efeito.video_md5)
    if local:
        efeito.pasta = local
        return efeito

    if not efeito.urls:
        raise EffectError(f"Efeito {efeito.effect_id} sem URL de download.")

    s = session or _sessao()
    erro = None
    for url in efeito.urls:
        try:
            r = s.get(url, timeout=180)
        except requests.RequestException as e:
            erro = e
            continue
        if r.status_code != 200 or not r.content:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                os.makedirs(proprio, exist_ok=True)
                z.extractall(proprio)
        except (zipfile.BadZipFile, OSError) as e:
            erro = e
            continue
        efeito.pasta = proprio
        return efeito

    raise EffectError(f"Não consegui baixar o efeito {efeito.effect_id}: {erro}")


def guarda_copia(efeito: Efeito, destino_base: str) -> str:
    """Copia a animação para junto da gravação, para o pacote não depender de nada.

    A URL do CDN expira em horas e um presente pode ser aposentado; com a copia
    ao lado do video, a versao com animacoes pode ser gerada anos depois.
    """
    import shutil

    if not efeito.disponivel:
        return ""
    destino = os.path.join(destino_base, efeito.video_md5)
    if os.path.exists(os.path.join(destino, "config.json")):
        return destino
    try:
        os.makedirs(destino, exist_ok=True)
        for nome in os.listdir(efeito.pasta):
            origem = os.path.join(efeito.pasta, nome)
            if os.path.isfile(origem):
                shutil.copy2(origem, os.path.join(destino, nome))
        return destino
    except OSError:
        return ""
