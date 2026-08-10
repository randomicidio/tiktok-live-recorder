"""Gera o executável do Windows (.exe portátil, sem dependências).

    python build_windows.py

O resultado sai em `dist/`. O ffmpeg vai embutido, então o executável funciona
em qualquer PC com Windows — sem Python, sem instalar nada.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import resources  # noqa: E402

NOME = resources.APP_NAME
DIST = os.path.join(BASE, "dist")


def acha_ffmpeg() -> tuple[str, str]:
    """Os dois binários que o programa usa. Faltar um dos dois quebra o pacote.

    O ffprobe é tão obrigatório quanto o ffmpeg: é ele que informa duração e
    resolução do vídeo, e sem isso o editor não consegue nem abrir um arquivo.
    """
    achados = []
    for nome in ("ffmpeg", "ffprobe"):
        caminho = shutil.which(nome)
        if not caminho:
            sys.exit(f"{nome} não encontrado no PATH - instale antes de empacotar.")
        achados.append(caminho)
    return achados[0], achados[1]


def confere_conteudo(exe: str) -> None:
    """Confere que as peças dos recursos principais entraram no executável.

    Um --hidden-import que falha e um --collect-all que não acha nada só viram
    aviso no log do PyInstaller: o build "passa" e o programa sai mancando -
    editor sem prévia, ou sem registrar presente nenhum. Quem descobre é quem
    baixou. Melhor quebrar o build aqui.
    """
    from PyInstaller.archive.readers import CArchiveReader

    try:
        pacote = CArchiveReader(exe)
        # Os binarios e arquivos de dados estao no CArchive; os modulos Python
        # ficam no PYZ interno, que precisa ser aberto a parte.
        arquivos = {os.path.basename(n).lower() for n in pacote.toc}
        pyz = next(n for n in pacote.toc if n.lower().endswith(".pyz"))
        modulos = set(pacote.open_embedded_archive(pyz).toc)
    except Exception as e:                           # noqa: BLE001
        print(f"  aviso: não consegui inspecionar o pacote ({e}) - seguindo.")
        return

    def tem(prefixo: str) -> bool:
        return any(m == prefixo or m.startswith(prefixo + ".") for m in modulos)

    exigidos = {
        "mpv": "prévia do editor",
        "TikTokLive": "registro de presentes e chat",
        "PIL": "desenho do chat",
        "requests": "download das animações",
    }
    faltando = [f"{mod} ({para})" for mod, para in exigidos.items() if not tem(mod)]

    # O nome vem do arquivo de origem, e no Windows a extensao pode chegar em
    # maiuscula (ffmpeg.EXE) dependendo de onde o ffmpeg foi instalado.
    binarios = [b for b in ("ffmpeg.exe", "ffprobe.exe", "libmpv-2.dll")
                if b.lower() not in arquivos]

    if faltando or binarios:
        recado = ["O executável saiu incompleto:"]
        if faltando:
            recado.append("  módulos que não entraram: " + ", ".join(faltando))
        if binarios:
            recado.append("  binários que não entraram: " + ", ".join(binarios))
        sys.exit("\n".join(recado))

    print("  conferido: mpv, TikTokLive, PIL, requests, ffmpeg, ffprobe e libmpv dentro.")


def main() -> None:
    if os.name != "nt":
        sys.exit("Este script gera o executável do Windows. No Mac use build_mac.sh.")

    ffmpeg, ffprobe = acha_ffmpeg()
    libmpv = os.path.join(BASE, "mpvlib", "libmpv-2.dll")
    if not os.path.exists(libmpv):
        sys.exit("libmpv-2.dll não está em mpvlib/ - o editor não teria prévia.\n"
                 "Baixe de https://github.com/shinchiro/mpv-winbuild-cmake/releases "
                 "(pacote mpv-dev) e coloque a DLL nessa pasta.")
    icone = os.path.join(BASE, "assets", "icon.ico")
    if not os.path.exists(icone):
        print("Ícone ausente; gerando...")
        subprocess.run([sys.executable, os.path.join(BASE, "make_icon.py")], check=True)

    emoji = os.path.join(BASE, "assets", "emoji.zip")
    if not os.path.exists(emoji):
        print("Conjunto de emoji ausente; baixando...")
        subprocess.run([sys.executable, os.path.join(BASE, "emoji_pack.py")], check=True)

    # O python-mpv procura a DLL no PATH JA NO import, e o PyInstaller importa
    # de verdade o que vem em --hidden-import para analisar. Sem esta linha o
    # import levanta OSError, o PyInstaller registra um aviso no meio de mil
    # linhas de log e segue: o executavel sai completo, com a DLL dentro, mas
    # SEM o modulo mpv - e o editor abre sem previa. Falha silenciosa classica,
    # entao o resultado e conferido abaixo em vez de confiado.
    os.environ["PATH"] = os.path.dirname(libmpv) + os.pathsep + os.environ.get("PATH", "")

    print(f"Empacotando {NOME} v{resources.APP_VERSION}")
    for nome, caminho in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        print(f"  {nome}: {caminho} ({os.path.getsize(caminho) / 1024 / 1024:.0f} MB)")
    print()

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NOME,
        "--onefile",
        "--windowed",                       # sem janela de console
        "--icon", icone,
        "--add-data", f"{os.path.join(BASE, 'assets')}{os.pathsep}assets",
        "--add-binary", f"{ffmpeg}{os.pathsep}.",
        "--add-binary", f"{ffprobe}{os.pathsep}.",
        # O registro de presentes usa TikTokLive, que carrega submodulos por
        # nome - sem collect-all o PyInstaller nao os encontra.
        "--collect-all", "TikTokLive",
        # betterproto.plugin e um plugin do protoc: aborta ao ser importado e
        # nao serve para nada em tempo de execucao.
        "--exclude-module", "betterproto.plugin",
        # O editor usa a libmpv para a previa com as camadas.
        "--add-binary", f"{libmpv}{os.pathsep}mpvlib",
        "--hidden-import", "mpv",
        # PIL desenha o chat e recorta os avatares - agora e obrigatoria. O
        # ImageTk entra por nome (a linha do tempo do editor desenha a forma
        # de onda numa imagem), entao o PyInstaller precisa ser avisado.
        "--hidden-import", "PIL.ImageTk",
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
        os.path.join(BASE, "app.py"),
    ]

    inicio = time.time()
    res = subprocess.run(args, cwd=BASE)
    if res.returncode != 0:
        sys.exit(f"PyInstaller falhou (código {res.returncode}).")

    exe = os.path.join(DIST, f"{NOME}.exe")
    if not os.path.exists(exe):
        sys.exit("Build terminou mas o .exe não apareceu em dist/.")

    confere_conteudo(exe)

    print(f"\nPronto em {time.time() - inicio:.0f}s")
    print(f"  {exe}")
    print(f"  {os.path.getsize(exe) / 1024 / 1024:.1f} MB")
    print("\nÉ um arquivo só: copie para onde quiser e dê dois cliques.")


if __name__ == "__main__":
    main()
