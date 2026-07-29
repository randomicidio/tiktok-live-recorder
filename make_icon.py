"""Gera os icones do programa (Windows .ico, macOS .icns e PNG).

Rode este script apenas quando quiser mudar o desenho do icone; os arquivos
gerados ficam em `assets/` e sao usados pelos scripts de build.

    python make_icon.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(BASE, "assets")

TAMANHO = 1024
FUNDO_TOPO = (23, 32, 52)      # ardosia escura
FUNDO_BASE = (12, 17, 29)
VERMELHO = (239, 68, 68)
VERMELHO_ANEL = (248, 113, 113)


def desenha(size: int = TAMANHO) -> Image.Image:
    """Um ponto de REC sobre um quadrado escuro arredondado."""
    # Desenha grande e reduz no fim: e o que da as bordas suaves.
    escala = 4
    s = size * escala
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Fundo com um degrade vertical discreto.
    fundo = Image.new("RGBA", (s, s), FUNDO_BASE + (255,))
    grad = ImageDraw.Draw(fundo)
    for y in range(s):
        t = y / s
        cor = tuple(
            int(FUNDO_TOPO[i] + (FUNDO_BASE[i] - FUNDO_TOPO[i]) * t) for i in range(3)
        )
        grad.line([(0, y), (s, y)], fill=cor + (255,))

    # Mascara arredondada, no estilo dos icones modernos.
    mascara = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mascara).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=int(s * 0.225), fill=255
    )
    img.paste(fundo, (0, 0), mascara)

    d = ImageDraw.Draw(img)
    centro = s / 2

    # Anel externo: sugere o botao de gravar.
    raio_anel = s * 0.335
    largura = int(s * 0.045)
    d.ellipse(
        [centro - raio_anel, centro - raio_anel, centro + raio_anel, centro + raio_anel],
        outline=VERMELHO_ANEL + (150,),
        width=largura,
    )

    # Ponto central preenchido.
    raio = s * 0.225
    d.ellipse(
        [centro - raio, centro - raio, centro + raio, centro + raio],
        fill=VERMELHO + (255,),
    )

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(ASSETS, exist_ok=True)
    mestre = desenha()

    png = os.path.join(ASSETS, "icon.png")
    mestre.save(png)
    print(f"{'icon.png':<12} {mestre.size}")

    ico = os.path.join(ASSETS, "icon.ico")
    mestre.save(ico, format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"{'icon.ico':<12} {os.path.getsize(ico) / 1024:.1f} KB")

    icns = os.path.join(ASSETS, "icon.icns")
    mestre.save(icns, format="ICNS")
    print(f"{'icon.icns':<12} {os.path.getsize(icns) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
