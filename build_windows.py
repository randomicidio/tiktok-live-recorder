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


def acha_ffmpeg() -> str:
    caminho = shutil.which("ffmpeg")
    if not caminho:
        sys.exit("ffmpeg não encontrado no PATH - instale antes de empacotar.")
    return caminho


def main() -> None:
    if os.name != "nt":
        sys.exit("Este script gera o executável do Windows. No Mac use build_mac.sh.")

    ffmpeg = acha_ffmpeg()
    icone = os.path.join(BASE, "assets", "icon.ico")
    if not os.path.exists(icone):
        print("Ícone ausente; gerando...")
        subprocess.run([sys.executable, os.path.join(BASE, "make_icon.py")], check=True)

    print(f"Empacotando {NOME} v{resources.APP_VERSION}")
    print(f"  ffmpeg: {ffmpeg} ({os.path.getsize(ffmpeg) / 1024 / 1024:.0f} MB)\n")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NOME,
        "--onefile",
        "--windowed",                       # sem janela de console
        "--icon", icone,
        "--add-data", f"{os.path.join(BASE, 'assets')}{os.pathsep}assets",
        "--add-binary", f"{ffmpeg}{os.pathsep}.",
        # O registro de presentes usa TikTokLive, que carrega submodulos por
        # nome - sem collect-all o PyInstaller nao os encontra.
        "--collect-all", "TikTokLive",
        # betterproto.plugin e um plugin do protoc: aborta ao ser importado e
        # nao serve para nada em tempo de execucao.
        "--exclude-module", "betterproto.plugin",
        # Nada disso e usado pelo programa; fora daqui o executavel incha a toa.
        "--exclude-module", "PIL",
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

    print(f"\nPronto em {time.time() - inicio:.0f}s")
    print(f"  {exe}")
    print(f"  {os.path.getsize(exe) / 1024 / 1024:.1f} MB")
    print("\nÉ um arquivo só: copie para onde quiser e dê dois cliques.")


if __name__ == "__main__":
    main()
