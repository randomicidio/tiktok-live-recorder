"""Monta `assets/emoji.zip`, o conjunto de figuras usado para desenhar o chat.

    python emoji_pack.py

São as imagens do Noto (as mesmas que o TikTok mostra no Android), uma por
sequência de codepoints. Vêm como imagem, e não de uma fonte, por três motivos:

  * o Segoe UI Emoji do Windows não tem bandeiras de país, então 🇧🇴 sairia
    como as letras "BO";
  * sem um motor de forma o Pillow não alcança tons de pele nem sequências
    ZWJ, que nessas fontes existem só como ligadura;
  * assim o resultado é o mesmo em qualquer máquina.

Ficam num zip só, e não em 3.786 arquivos soltos, porque o executável de um
arquivo só extrai tudo a cada abertura - um item pesa muito menos que milhares.

Não é versionado (13 MB); o build chama este script quando o arquivo falta.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DESTINO = os.path.join(BASE, "assets", "emoji.zip")
PACOTE = "emoji-datasource-google"      # arte do Noto, empacotada por tamanho
TAMANHO = "64"                          # px; o chat desenha entre 25 e 45


def _tarball() -> str:
    url = f"https://registry.npmjs.org/{PACOTE}"
    with urllib.request.urlopen(url, timeout=60) as r:
        dados = json.loads(r.read())
    versao = dados["dist-tags"]["latest"]
    print(f"  {PACOTE} {versao}")
    return dados["versions"][versao]["dist"]["tarball"]


def main() -> None:
    os.makedirs(os.path.dirname(DESTINO), exist_ok=True)
    print("Baixando o conjunto de emoji...")
    try:
        with urllib.request.urlopen(_tarball(), timeout=300) as r:
            bruto = io.BytesIO(r.read())
    except OSError as e:
        sys.exit(f"Não consegui baixar o conjunto de emoji: {e}")

    marca = f"/img/google/{TAMANHO}/"
    n = 0
    with tarfile.open(fileobj=bruto) as tf, \
            zipfile.ZipFile(DESTINO, "w", zipfile.ZIP_STORED) as z:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            if marca in m.name:
                # O nome do arquivo já é a sequência de codepoints, que é
                # exatamente a chave usada na hora de desenhar.
                z.writestr(os.path.basename(m.name), tf.extractfile(m).read())
                n += 1
            elif m.name.endswith("/LICENSE"):
                z.writestr("LICENSE.txt", tf.extractfile(m).read())

    if not n:
        os.remove(DESTINO)
        sys.exit(f"O pacote não trouxe imagens de {TAMANHO}px.")
    print(f"Pronto: {n} figuras em assets/emoji.zip "
          f"({os.path.getsize(DESTINO) / 1048576:.1f} MB)")


if __name__ == "__main__":
    main()
