"""Catálogo de presentes com animação, montado dos pacotes que estão no disco.

Um replay baixado de outro lugar não vem com .ttgifts, e sem ele o editor não
tem presente nenhum para pôr por cima do vídeo. As gravações antigas, sim: cada
.ttgifts carrega o vídeo da animação, o ícone oficial e o nome do presente, e
continua servindo anos depois - foi para isso que ele nasceu autossuficiente.

Varrendo as pastas de gravação dá para oferecer, para qualquer vídeo aberto,
tudo o que já passou por aqui alguma vez. É de propósito que a fonte seja o
disco e não a API do TikTok: o catálogo de lá muda, exige rede e um presente
aposentado some dele, enquanto o que já foi gravado é para sempre.
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field

import pacote as pacote_mod

# Tetos da varredura. Uma pasta de gravações com anos de uso ainda abre rápido
# (18 pacotes levam 0,2 s), mas apontar para a raiz de um disco não pode virar
# uma espera de minutos.
MAX_PACOTES = 300
MAX_PASTAS = 500


@dataclass
class Item:
    """Um presente que pode ser adicionado à mão, e de onde tirar a animação."""

    nome: str
    gift_id: int
    diamantes: int
    animacao: str                    # video_md5, o nome da pasta no pacote
    icone: str = ""                  # identificador da figura dentro do pacote
    icone_bytes: bytes = b""         # o PNG do ícone, já lido
    pacote: str = ""                 # o .ttgifts de onde a animação sai
    variacao: int = 0                # >0 quando o presente tem mais de uma

    @property
    def rotulo(self) -> str:
        if self.variacao:
            return f"{self.nome} ({self.variacao})"
        return self.nome or "presente sem nome"


def pastas_para_varrer(video: str = "", pacote_atual: str = "",
                       extras: list[str] | None = None) -> list[str]:
    """As pastas onde faz sentido procurar pacotes, sem repetir nenhuma."""
    candidatas = []
    for caminho in (video, pacote_atual):
        if caminho:
            candidatas.append(os.path.dirname(os.path.abspath(caminho)))
    candidatas.extend(extras or [])
    vistas, saida = set(), []
    for pasta in candidatas:
        if not pasta:
            continue
        chave = os.path.normcase(os.path.abspath(pasta))
        if chave in vistas or not os.path.isdir(pasta):
            continue
        vistas.add(chave)
        saida.append(pasta)
    return saida


def _pacotes_em(pastas: list[str]) -> list[str]:
    """Os .ttgifts das pastas e de suas subpastas, até os tetos da varredura."""
    achados, vistos, visitadas = [], set(), 0
    for pasta in pastas:
        for raiz, subpastas, arquivos in os.walk(pasta):
            visitadas += 1
            if visitadas > MAX_PASTAS:
                subpastas[:] = []
                break
            for nome in arquivos:
                if not nome.lower().endswith(pacote_mod.EXTENSAO):
                    continue
                caminho = os.path.join(raiz, nome)
                chave = os.path.normcase(os.path.abspath(caminho))
                if chave in vistos:
                    continue
                vistos.add(chave)
                achados.append(caminho)
                if len(achados) >= MAX_PACOTES:
                    return achados
    return achados


def _icones(caminho: str, idents: set[str]) -> dict[str, bytes]:
    """Lê de uma vez os ícones desse pacote.

    Um zip aberto por pacote, e não um por presente: são 30 MB de animação
    para cada punhado de PNGs de 5 KB, e abrir o arquivo de novo a cada ícone
    era o que fazia a janela demorar a aparecer.
    """
    if not idents:
        return {}
    saida = {}
    try:
        with zipfile.ZipFile(caminho) as z:
            for ident in idents:
                try:
                    with z.open(f"{pacote_mod.PASTA_FIGURAS}/{ident}.png") as fh:
                        saida[ident] = fh.read()
                except KeyError:
                    continue
    except (zipfile.BadZipFile, OSError):
        return {}
    return saida


def varrer(pastas: list[str]) -> list[Item]:
    """Monta o catálogo a partir dos pacotes dessas pastas.

    Um presente entra uma vez por animação: o mesmo nome pode ter mais de uma
    (o TikTok troca o vídeo conforme a quantidade enviada), e as duas valem
    como escolha.
    """
    achados: dict[tuple, Item] = {}
    for caminho in _pacotes_em(pastas):
        try:
            pac = pacote_mod.abrir(caminho)
        except pacote_mod.PacoteError:
            continue
        novos: dict[tuple, Item] = {}
        for p in pac.com_animacao:
            chave = (p.gift_id or p.nome.lower(), p.animacao)
            if chave in achados or chave in novos:
                # Já temos essa animação; fica a primeira, que veio de um
                # pacote que já sabemos abrir.
                continue
            novos[chave] = Item(
                nome=p.nome, gift_id=p.gift_id, diamantes=p.diamantes,
                animacao=p.animacao, icone=p.icone, pacote=caminho)
        figuras = _icones(caminho, {i.icone for i in novos.values() if i.icone})
        for chave, item in novos.items():
            item.icone_bytes = figuras.get(item.icone, b"")
            achados[chave] = item

    itens = sorted(achados.values(), key=lambda i: (i.nome.lower(), i.animacao))
    # Numera as variações só de quem tem mais de uma, para o nome do presente
    # continuar limpo no caso comum.
    por_nome: dict[str, list[Item]] = {}
    for item in itens:
        por_nome.setdefault(item.nome.lower(), []).append(item)
    for iguais in por_nome.values():
        if len(iguais) > 1:
            for n, item in enumerate(iguais, start=1):
                item.variacao = n
    return itens
