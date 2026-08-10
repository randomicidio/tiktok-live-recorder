"""Pacote de presentes: um único arquivo ao lado do vídeo.

Guarda o registro dos presentes e as animações correspondentes num .zip com
extensão própria. Assim a gravação vira dois arquivos e nada mais:

    live.mp4
    live.ttgifts

O pacote é autossuficiente - contém os vídeos das animações -, então a versão
com animações pode ser gerada anos depois, mesmo que a API mude ou o presente
saia de catálogo.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass, field

EXTENSAO = ".ttgifts"
MANIFESTO = "manifesto.json"
PASTA_ANIM = "animacoes"
PASTA_FIGURAS = "figuras"
VERSAO = 3

# Marca o lugar de um emote dentro do texto da mensagem. Fica gravada assim
# para a posição continuar certa mesmo que a figura não venha junto.
MARCA_EMOTE = "￼"


class PacoteError(Exception):
    """Falha ao criar ou abrir um pacote de presentes."""


@dataclass
class Presente:
    """Um presente recebido, com o instante relativo ao início da gravação."""

    t: float
    nome: str = ""
    gift_id: int = 0
    diamantes: int = 0
    quantidade: int = 1
    effect_ids: list[int] = field(default_factory=list)
    de: str = ""
    apelido: str = ""
    avatar: str = ""          # URL da foto do remetente, para o contador
    icone: str = ""           # URL da figura do presente, para o contador
    hora: str = ""
    # preenchido quando a animação existe no pacote
    animacao: str = ""

    @classmethod
    def de_dict(cls, d: dict) -> "Presente":
        return cls(
            t=float(d.get("t") or 0),
            nome=d.get("nome") or "",
            gift_id=int(d.get("gift_id") or 0),
            diamantes=int(d.get("diamantes") or 0),
            quantidade=int(d.get("quantidade") or 1),
            effect_ids=[int(i) for i in (d.get("effect_ids") or [])],
            de=d.get("de") or "",
            apelido=d.get("apelido") or "",
            avatar=d.get("avatar") or "",
            icone=d.get("icone") or "",
            hora=d.get("hora") or "",
            animacao=d.get("animacao") or "",
        )


@dataclass
class Comentario:
    """Uma mensagem do chat, no instante em que apareceu.

    Guardada mesmo sem decisão sobre desenhar o chat: o chat não pode ser
    recuperado depois, e ocupa muito pouco.
    """

    t: float
    texto: str = ""
    de: str = ""
    apelido: str = ""
    avatar: str = ""          # URL da foto, para desenhar depois
    hora: str = ""
    # Chegou na fila acumulada logo após conectar: o instante não é confiável.
    acumulado: bool = False
    # Emotes próprios da live: posição no texto -> identificador da figura.
    emotes: dict[int, str] = field(default_factory=dict)
    # Selos do apelido (nível, clube de fãs, ranking), já como figura.
    selos: list[str] = field(default_factory=list)          # antes do apelido
    selos_direita: list[str] = field(default_factory=list)   # depois

    @classmethod
    def de_dict(cls, d: dict) -> "Comentario":
        # No JSON a chave é texto; aqui ela volta a ser índice.
        emotes = {}
        for pos, ident in (d.get("emotes") or {}).items():
            try:
                emotes[int(pos)] = str(ident)
            except (TypeError, ValueError):
                continue
        return cls(
            t=float(d.get("t") or 0),
            texto=d.get("texto") or "",
            de=d.get("de") or "",
            apelido=d.get("apelido") or "",
            avatar=d.get("avatar") or "",
            hora=d.get("hora") or "",
            acumulado=bool(d.get("acumulado")),
            emotes=emotes,
            selos=[str(i) for i in (d.get("selos") or [])],
            selos_direita=[str(i) for i in (d.get("selos_direita") or [])],
        )


@dataclass
class Pacote:
    """Conteúdo de um arquivo .ttgifts já aberto."""

    caminho: str = ""
    video: str = ""
    conta: str = ""
    inicio: str = ""
    offset_segundos: float = 0.0
    presentes: list[Presente] = field(default_factory=list)
    comentarios: list[Comentario] = field(default_factory=list)
    _figuras: dict = field(default_factory=dict, repr=False)

    @property
    def com_animacao(self) -> list[Presente]:
        """Só os presentes cuja animação está guardada no pacote."""
        return [p for p in self.presentes if p.animacao]

    def abrir_figura(self, ident: str):
        """A figura de um emote da live, ou None se não veio no pacote.

        Guardada aberta: o mesmo emote reaparece em muitos quadros seguidos.
        """
        if ident in self._figuras:
            return self._figuras[ident]
        img = None
        try:
            from PIL import Image
            with zipfile.ZipFile(self.caminho) as z:
                with z.open(f"{PASTA_FIGURAS}/{ident}.png") as fh:
                    img = Image.open(io.BytesIO(fh.read())).convert("RGBA")
        except (KeyError, zipfile.BadZipFile, OSError, ValueError):
            img = None
        self._figuras[ident] = img
        return img

    def extrair_animacao(self, presente: Presente, destino: str) -> str:
        """Coloca a animação desse presente em disco e devolve a pasta."""
        if not presente.animacao:
            return ""
        alvo = os.path.join(destino, presente.animacao)
        if os.path.exists(os.path.join(alvo, "config.json")):
            return alvo
        prefixo = f"{PASTA_ANIM}/{presente.animacao}/"
        try:
            with zipfile.ZipFile(self.caminho) as z:
                nomes = [n for n in z.namelist() if n.startswith(prefixo)]
                if not nomes:
                    return ""
                os.makedirs(alvo, exist_ok=True)
                for n in nomes:
                    destino_arq = os.path.join(alvo, os.path.basename(n))
                    if n.endswith("/"):
                        continue
                    with z.open(n) as origem, open(destino_arq, "wb") as saida:
                        saida.write(origem.read())
        except (zipfile.BadZipFile, OSError) as e:
            raise PacoteError(f"Não consegui extrair a animação: {e}") from e
        return alvo

    def config_da_animacao(self, presente: Presente) -> dict:
        """Geometria da composição, lida direto do pacote."""
        if not presente.animacao:
            return {}
        alvo = f"{PASTA_ANIM}/{presente.animacao}/config.json"
        try:
            with zipfile.ZipFile(self.caminho) as z:
                with z.open(alvo) as fh:
                    return (json.loads(fh.read().decode("utf-8")) or {}).get("portrait") or {}
        except (KeyError, zipfile.BadZipFile, OSError, ValueError):
            return {}


def caminho_para(video: str) -> str:
    """O pacote que acompanha esse vídeo, pelo nome."""
    return os.path.splitext(video)[0] + EXTENSAO


def criar(destino: str, video: str, conta: str, inicio: str,
          presentes: list[dict], animacoes: dict[str, str],
          comentarios: list[dict] | None = None, offset: float = 0.0,
          figuras: dict[str, bytes] | None = None) -> str:
    """Monta o .ttgifts.

    `animacoes` mapeia o identificador da animação (video_md5) para a pasta em
    disco de onde copiar os arquivos. `figuras` são as imagens pequenas que o
    chat usa - emotes próprios da live e selos do apelido -, por identificador.
    """
    usadas = set()
    try:
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
            for chave, pasta in animacoes.items():
                if not pasta or not os.path.isdir(pasta):
                    continue
                for nome in sorted(os.listdir(pasta)):
                    origem = os.path.join(pasta, nome)
                    if os.path.isfile(origem):
                        z.write(origem, f"{PASTA_ANIM}/{chave}/{nome}")
                        usadas.add(chave)

            for ident, dados in (figuras or {}).items():
                if dados:
                    z.writestr(f"{PASTA_FIGURAS}/{ident}.png", dados)

            manifesto = {
                "versao": VERSAO,
                "video": os.path.basename(video),
                "conta": conta,
                "inicio": inicio,
                # Atraso entre o evento e o vídeo, medido uma vez e aplicado
                # na hora de gerar. Zero até ser calibrado.
                "offset_segundos": offset,
                "presentes": presentes,
                "comentarios": comentarios or [],
            }
            z.writestr(MANIFESTO, json.dumps(manifesto, ensure_ascii=False, indent=2))
    except OSError as e:
        raise PacoteError(f"Não consegui criar o pacote: {e}") from e
    return destino


def abrir(caminho: str) -> Pacote:
    """Lê um .ttgifts."""
    if not os.path.exists(caminho):
        raise PacoteError(f"Pacote não encontrado: {caminho}")
    try:
        with zipfile.ZipFile(caminho) as z:
            with z.open(MANIFESTO) as fh:
                m = json.loads(fh.read().decode("utf-8"))
            nomes = z.namelist()
            guardadas = {n.split("/")[1] for n in nomes
                         if n.startswith(PASTA_ANIM + "/") and n.count("/") >= 2}
            figuras = {n[len(PASTA_FIGURAS) + 1:-4] for n in nomes
                       if n.startswith(PASTA_FIGURAS + "/") and n.endswith(".png")}
    except (KeyError, zipfile.BadZipFile, OSError, ValueError) as e:
        raise PacoteError(f"Pacote inválido: {e}") from e

    p = Pacote(
        caminho=caminho,
        video=m.get("video") or "",
        conta=m.get("conta") or "",
        inicio=m.get("inicio") or "",
        offset_segundos=float(m.get("offset_segundos") or 0),
        presentes=[Presente.de_dict(d) for d in (m.get("presentes") or [])],
        comentarios=[Comentario.de_dict(d) for d in (m.get("comentarios") or [])],
    )
    # marca quais presentes têm animação de fato guardada
    for pres in p.presentes:
        if pres.animacao and pres.animacao not in guardadas:
            pres.animacao = ""
        if pres.icone and pres.icone not in figuras:
            pres.icone = ""
    # o mesmo para as figuras: sem a imagem, a marca no texto viraria um vazio
    for c in p.comentarios:
        if c.emotes:
            c.emotes = {pos: i for pos, i in c.emotes.items() if i in figuras}
        c.selos = [i for i in c.selos if i in figuras]
        c.selos_direita = [i for i in c.selos_direita if i in figuras]
    return p


def procurar_para(video: str) -> str:
    """Acha o pacote do vídeo: mesmo nome, ou qualquer .ttgifts na pasta."""
    direto = caminho_para(video)
    if os.path.exists(direto):
        return direto
    pasta = os.path.dirname(os.path.abspath(video))
    try:
        candidatos = [os.path.join(pasta, n) for n in os.listdir(pasta)
                      if n.lower().endswith(EXTENSAO)]
    except OSError:
        return ""
    if len(candidatos) == 1:
        return candidatos[0]
    # vários: tenta o que menciona este vídeo no manifesto
    alvo = os.path.basename(video)
    for c in candidatos:
        try:
            if abrir(c).video == alvo:
                return c
        except PacoteError:
            continue
    return ""
