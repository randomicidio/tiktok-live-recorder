"""Catálogo de presentes: o que dá para pôr por cima de um vídeo.

A lista vem do TikTok - a mesma que o painel de presentes da live mostra, com
os 650 presentes e o efeito de cada um - e fica guardada em disco, para a
segunda vez ser instantânea. A animação em si só é baixada quando alguém
escolhe o presente, e também fica: baixar de novo o mesmo leão seria só
esperar à toa.

Os .ttgifts das gravações entram na mesma lista. São eles que respondem quando
não há rede, e são a única fonte de um presente que já saiu de catálogo - foi
para isso que o pacote nasceu autossuficiente.

Vale para um replay baixado de outro lugar, sem registro nenhum: mesmo assim
todos os presentes com animação estão à mão.
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from dataclasses import dataclass, field

import effects_api
import pacote as pacote_mod
import resources

# A lista que o painel de presentes da live carrega. Responde sem login e sem
# sala nenhuma, que é o que permite montar o catálogo com o programa parado.
URL_LISTA = "https://webcast.tiktok.com/webcast/gift/list/"
# Os três parâmetros de idioma são o que traz o nome oficial em português
# ("Leão", "Casquinha de sorvete"); sem eles a lista volta em inglês. O
# cabeçalho Accept-Language sozinho não adianta - já foi tentado.
PARAMS_LISTA = {"aid": "1988", "app_language": "pt-BR", "language": "pt-BR",
                "webcast_language": "pt-BR"}

# Tetos da varredura dos pacotes. Uma pasta de gravações com anos de uso ainda
# abre rápido (18 pacotes levam 0,2 s), mas apontar para a raiz de um disco não
# pode virar uma espera de minutos.
MAX_PACOTES = 300
MAX_PASTAS = 500

# Os efeitos são resolvidos em lote; a API responde 200 ids numa tacada.
LOTE_EFEITOS = 200

# Depois disso a lista guardada é considerada velha e o editor procura de novo
# ao fundo. O TikTok põe presente novo o tempo todo, mas não de hora em hora.
DIAS_ATE_ENVELHECER = 7


def _pasta(nome: str) -> str:
    caminho = os.path.join(resources.data_dir(), nome)
    os.makedirs(caminho, exist_ok=True)
    return caminho


def arquivo_da_lista() -> str:
    return os.path.join(resources.data_dir(), "presentes.json")


def pasta_dos_icones() -> str:
    return _pasta("icones")


def pasta_das_animacoes() -> str:
    return _pasta("animacoes")


@dataclass
class Item:
    """Um presente que pode ser adicionado à mão, e de onde tirar a animação."""

    nome: str
    gift_id: int = 0
    diamantes: int = 0
    effect_id: int = 0
    animacao: str = ""               # video_md5: o nome da pasta da animação
    icone_url: str = ""              # ícone oficial, quando vem da API
    icone: str = ""                  # identificador da figura dentro do pacote
    icone_bytes: bytes = field(default=b"", repr=False)
    pacote: str = ""                 # .ttgifts de onde a animação sai, se houver
    variacao: int = 0                # >0 quando o presente tem mais de uma

    @property
    def rotulo(self) -> str:
        if self.variacao:
            return f"{self.nome} ({self.variacao})"
        return self.nome or "presente sem nome"

    @property
    def chave(self) -> tuple:
        """O que faz dois itens serem o mesmo presente na lista.

        É o arquivo da animação, e nada mais. O TikTok tem o mesmo presente
        cadastrado várias vezes - a "Arma de diamante" aparece com quatro ids -
        e o mesmo arquivo ainda volta com o nome em português pela API e em
        inglês pelo .ttgifts de uma gravação antiga. Pelo id ou pelo nome, a
        lista repetia a mesma animação; pelo arquivo, ela aparece uma vez.

        Duas escolhas só são duas quando os vídeos são diferentes - aí sim o
        rótulo ganha (1), (2).
        """
        return (self.animacao,)

    @property
    def em_disco(self) -> bool:
        """A animação já está aqui: ou baixada, ou dentro de um pacote."""
        pronta = os.path.join(pasta_das_animacoes(), self.animacao,
                              "config.json")
        return bool(self.pacote) or (bool(self.animacao)
                                     and os.path.exists(pronta))

    def como_dict(self) -> dict:
        return {"nome": self.nome, "gift_id": self.gift_id,
                "diamantes": self.diamantes, "effect_id": self.effect_id,
                "animacao": self.animacao, "icone_url": self.icone_url}


# ------------------------------------------------------------ lista do TikTok

def da_api(session=None, progresso=None) -> list[Item]:
    """Pergunta ao TikTok a lista inteira e quais presentes têm animação.

    Duas etapas: a lista do painel diz nome, diamantes, ícone e o efeito de
    cada presente; a API de efeitos diz quais desses efeitos têm mesmo um vídeo
    para baixar. Só os que têm entram - oferecer um presente que não daria em
    animação nenhuma seria enganar quem escolhe.
    """
    import requests

    s = session or _sessao_web()
    if progresso:
        progresso("Buscando a lista de presentes...")
    try:
        r = s.get(URL_LISTA, params=PARAMS_LISTA, timeout=30)
        dados = (r.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as e:
        raise CatalogoError(f"Não consegui ler a lista de presentes: {e}") from e

    presentes = dados.get("gifts") or []
    if not presentes:
        raise CatalogoError("O TikTok respondeu sem nenhum presente na lista.")

    por_efeito: dict[int, list[Item]] = {}
    for g in presentes:
        try:
            efeito = int(g.get("primary_effect_id") or 0)
        except (TypeError, ValueError):
            efeito = 0
        if not efeito:
            continue                     # presente sem animação de tela cheia
        item = Item(
            nome=str(g.get("name") or ""),
            gift_id=int(g.get("id") or 0),
            diamantes=int(g.get("diamond_count") or 0),
            effect_id=efeito,
            icone_url=_primeira_url(g.get("icon") or g.get("image")))
        por_efeito.setdefault(efeito, []).append(item)

    if progresso:
        progresso(f"Vendo quais dos {len(por_efeito)} efeitos têm animação...")
    ids = sorted(por_efeito)
    saida: list[Item] = []
    ses = effects_api._sessao()
    for i in range(0, len(ids), LOTE_EFEITOS):
        try:
            efeitos = effects_api.resolve(ids[i:i + LOTE_EFEITOS], ses)
        except effects_api.EffectError:
            continue                     # o lote que falhar fica de fora
        for eid, efeito in efeitos.items():
            if not efeito.video_md5:
                continue                 # efeito sem vídeo: não vira animação
            for item in por_efeito.get(eid, []):
                item.animacao = efeito.video_md5
                saida.append(item)
    if not saida:
        raise CatalogoError("Nenhum presente da lista tem animação para baixar.")
    return saida


def _primeira_url(no) -> str:
    if isinstance(no, dict):
        for url in (no.get("url_list") or []):
            if url:
                return str(url)
    return ""


def _sessao_web():
    import requests

    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.tiktok.com/",
        "Accept": "application/json, text/plain, */*",
    })
    return s


class CatalogoError(Exception):
    """Não deu para montar o catálogo pela rede."""


# --------------------------------------------------------- lista guardada

def guardar(itens: list[Item]) -> None:
    """Grava a lista para a próxima vez abrir na hora."""
    try:
        with open(arquivo_da_lista(), "w", encoding="utf-8") as fh:
            json.dump({"quando": time.time(),
                       "itens": [i.como_dict() for i in itens]},
                      fh, ensure_ascii=False)
    except OSError:
        pass            # sem o cache o editor funciona igual, só mais devagar


def guardados() -> list[Item]:
    """A última lista do TikTok que ficou em disco."""
    try:
        with open(arquivo_da_lista(), encoding="utf-8") as fh:
            dados = json.load(fh) or {}
    except (OSError, ValueError):
        return []
    saida = []
    for d in dados.get("itens") or []:
        try:
            saida.append(Item(nome=str(d.get("nome") or ""),
                              gift_id=int(d.get("gift_id") or 0),
                              diamantes=int(d.get("diamantes") or 0),
                              effect_id=int(d.get("effect_id") or 0),
                              animacao=str(d.get("animacao") or ""),
                              icone_url=str(d.get("icone_url") or "")))
        except (TypeError, ValueError):
            continue
    return [i for i in saida if i.animacao]


def dias_da_lista() -> float:
    """Há quantos dias a lista guardada foi buscada. Sem lista, um número alto."""
    try:
        with open(arquivo_da_lista(), encoding="utf-8") as fh:
            quando = float((json.load(fh) or {}).get("quando") or 0)
    except (OSError, ValueError, TypeError):
        return 1e9
    return max(0.0, (time.time() - quando) / 86400)


def esta_velha() -> bool:
    return dias_da_lista() > DIAS_ATE_ENVELHECER


# ------------------------------------------------------------ ícones

def icone(item: Item, session=None) -> bytes:
    """O PNG do ícone: do pacote, do disco ou do CDN - nessa ordem.

    Fica guardado por presente. São 7 KB cada; baixar de novo a cada vez que a
    janela abre é o que fazia a lista aparecer sem ícone nenhum por segundos.
    """
    if item.icone_bytes:
        return item.icone_bytes
    if not item.icone_url:
        return b""
    destino = os.path.join(pasta_dos_icones(),
                           f"{item.gift_id or item.animacao}.png")
    try:
        with open(destino, "rb") as fh:
            item.icone_bytes = fh.read()
            return item.icone_bytes
    except OSError:
        pass
    import requests

    try:
        s = session or _sessao_web()
        r = s.get(item.icone_url, timeout=10)
        if r.status_code != 200 or not r.content:
            return b""
        item.icone_bytes = r.content
    except requests.RequestException:
        return b""
    try:
        with open(destino, "wb") as fh:
            fh.write(item.icone_bytes)
    except OSError:
        pass
    return item.icone_bytes


def arquivo_do_icone(item: Item) -> str:
    """O ícone em disco, para o contador poder desenhá-lo. "" se não deu."""
    dados = icone(item)
    if not dados:
        return ""
    destino = os.path.join(pasta_dos_icones(),
                           f"{item.gift_id or item.animacao}.png")
    if not os.path.exists(destino):
        try:
            with open(destino, "wb") as fh:
                fh.write(dados)
        except OSError:
            return ""
    return destino


# ------------------------------------------------------------ animação

def garantir_animacao(item: Item) -> str:
    """Deixa a animação desse presente em disco e devolve a pasta que a contém.

    Devolve a pasta-mãe (a que tem uma subpasta por animação), que é o formato
    que o `Presente.origem` espera. Um presente que veio de um pacote não passa
    por aqui: a animação dele já está dentro do .ttgifts.
    """
    if item.pacote:
        return item.pacote
    if not item.animacao:
        raise CatalogoError(f"{item.nome} não tem animação para baixar.")
    base = pasta_das_animacoes()
    if os.path.exists(os.path.join(base, item.animacao, "config.json")):
        return base
    if not item.effect_id:
        raise CatalogoError(f"Não sei de onde baixar a animação de {item.nome}.")
    try:
        efeitos = effects_api.resolve([item.effect_id])
        efeito = efeitos.get(item.effect_id)
        if efeito is None:
            raise effects_api.EffectError(
                f"O TikTok não conhece mais o efeito de {item.nome}.")
        effects_api.baixa(efeito, base)
    except effects_api.EffectError as e:
        raise CatalogoError(str(e)) from e
    if not os.path.exists(os.path.join(base, item.animacao, "config.json")):
        # O cache do LIVE Studio pode ter respondido de outra pasta.
        return os.path.dirname(efeito.pasta) or base
    return base


# ------------------------------------------------------- pacotes do disco

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


def _icones_do_pacote(caminho: str, idents: set[str]) -> dict[str, bytes]:
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
    """O que os pacotes dessas pastas já têm guardado.

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
            chave = (p.animacao,)
            if chave in achados or chave in novos:
                # Já temos essa animação; fica a primeira, que veio de um
                # pacote que já sabemos abrir.
                continue
            novos[chave] = Item(
                nome=p.nome, gift_id=p.gift_id, diamantes=p.diamantes,
                animacao=p.animacao, icone=p.icone, pacote=caminho)
        figuras = _icones_do_pacote(caminho,
                                    {i.icone for i in novos.values() if i.icone})
        for chave, item in novos.items():
            item.icone_bytes = figuras.get(item.icone, b"")
            achados[chave] = item
    return list(achados.values())


# ------------------------------------------------------------ juntar tudo

def juntar(*listas: list[Item]) -> list[Item]:
    """Uma lista só, sem repetir animação, com as variações numeradas.

    A ordem dos argumentos manda: o primeiro que trouxer uma animação fica com
    ela, e os seguintes só completam o que faltava - é assim que um presente da
    API que também está num pacote seu aparece uma vez só, e já sabendo que não
    precisa ser baixado.
    """
    achados: dict[tuple, Item] = {}
    for lista in listas:
        for item in lista:
            atual = achados.get(item.chave)
            if atual is None:
                achados[item.chave] = item
                continue
            atual.nome = atual.nome or item.nome
            atual.diamantes = atual.diamantes or item.diamantes
            atual.effect_id = atual.effect_id or item.effect_id
            atual.icone_url = atual.icone_url or item.icone_url
            atual.pacote = atual.pacote or item.pacote
            atual.icone = atual.icone or item.icone
            atual.icone_bytes = atual.icone_bytes or item.icone_bytes

    itens = sorted(achados.values(), key=lambda i: (i.nome.lower(), i.animacao))
    # Numera as variações só de quem tem mais de uma, para o nome do presente
    # continuar limpo no caso comum.
    por_nome: dict[str, list[Item]] = {}
    for item in itens:
        item.variacao = 0
        por_nome.setdefault(item.nome.lower(), []).append(item)
    for iguais in por_nome.values():
        if len(iguais) > 1:
            for n, item in enumerate(iguais, start=1):
                item.variacao = n
    return itens
