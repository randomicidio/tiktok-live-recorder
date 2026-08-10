#!/usr/bin/env bash
#
# Gera o "Tiktok Live Recorder.app" para macOS.
#
# PRECISA RODAR NUM MAC. Nenhum empacotador Python faz compilação cruzada:
# o .app carrega o interpretador e as bibliotecas da máquina onde foi montado.
#
# Uso:
#     chmod +x build_mac.sh
#     ./build_mac.sh
#
# O resultado sai em dist/Tiktok Live Recorder.app

set -euo pipefail

cd "$(dirname "$0")"

APP="Tiktok Live Recorder"
BUNDLE_ID="com.tiktoklive.recorder"
VENV=".venv-build"

echo "=== $APP - build para macOS ==="
echo

if [ "$(uname)" != "Darwin" ]; then
    echo "ERRO: este script só funciona no macOS."
    echo "      No Windows use:  python build_windows.py"
    exit 1
fi

# --- dependências do sistema -------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERRO: Python 3 não encontrado."
    echo "      Instale com:  brew install python"
    exit 1
fi
echo "python3: $(python3 --version)"

FFMPEG="$(command -v ffmpeg || true)"
if [ -z "$FFMPEG" ]; then
    echo
    echo "ERRO: ffmpeg não encontrado - ele vai embutido no app."
    echo "      Instale com:  brew install ffmpeg"
    echo
    echo "      Para um app que rode em QUALQUER Mac, prefira uma versão"
    echo "      estática (sem dependências externas):"
    echo "        Apple Silicon: https://osxexperts.net"
    echo "        Intel:         https://evermeet.cx/ffmpeg/"
    echo "      Baixe, coloque o binário nesta pasta e rode de novo."
    exit 1
fi
echo "ffmpeg:  $FFMPEG"

# O ffprobe e tao obrigatorio quanto o ffmpeg: e ele que informa duracao e
# resolucao do video, e sem isso o editor nao abre arquivo nenhum.
FFPROBE="$(command -v ffprobe || true)"
if [ -z "$FFPROBE" ]; then
    echo
    echo "ERRO: ffprobe não encontrado - ele vai embutido junto com o ffmpeg."
    echo "      Vem no mesmo pacote; se instalou pelo brew, já deveria estar aí."
    exit 1
fi
echo "ffprobe: $FFPROBE"

# Um ffmpeg do Homebrew depende de .dylib que existem só na máquina que o
# instalou. Funciona aqui, mas quebra ao copiar o app para outro Mac.
EXTERNAS="$(otool -L "$FFMPEG" 2>/dev/null | tail -n +2 \
    | grep -v -E '/usr/lib/|/System/' | wc -l | tr -d ' ')"
if [ "$EXTERNAS" -gt 0 ]; then
    echo
    echo "AVISO: este ffmpeg depende de $EXTERNAS biblioteca(s) externa(s)."
    echo "       O app vai funcionar NESTE Mac, mas pode falhar em outro."
    echo "       Para distribuir, use um ffmpeg estático (links acima)."
    echo
    printf "Continuar assim mesmo? [s/N] "
    read -r resposta
    case "$resposta" in
        [sS]*) ;;
        *) echo "Cancelado."; exit 1 ;;
    esac
fi

ARCH="$(uname -m)"
echo "arquitetura: $ARCH  (o app gerado roda em Macs $ARCH)"
echo

# --- ambiente isolado ---------------------------------------------------
echo "=== Preparando ambiente isolado ==="
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet pyinstaller requests pillow TikTokLive
echo "dependências instaladas"
echo

# --- ícones -------------------------------------------------------------
echo "=== Gerando ícones ==="
python make_icon.py
echo

# --- empacotamento ------------------------------------------------------
echo "=== Empacotando ==="
pyinstaller --noconfirm --clean \
    --name "$APP" \
    --windowed \
    --icon assets/icon.icns \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --add-data "assets:assets" \
    --add-binary "$FFMPEG:." \
    --add-binary "$FFPROBE:." \
    --collect-all TikTokLive \
    --exclude-module betterproto.plugin \
    --hidden-import PIL.ImageTk \
    --exclude-module numpy \
    --exclude-module matplotlib \
    --exclude-module pytest \
    app.py

deactivate
rm -rf "$VENV"

DESTINO="dist/$APP.app"
if [ ! -d "$DESTINO" ]; then
    echo "ERRO: o build terminou mas $DESTINO não apareceu."
    exit 1
fi

TAMANHO="$(du -sh "$DESTINO" | cut -f1)"
echo
echo "======================================================"
echo " Pronto:  $DESTINO  ($TAMANHO)"
echo "======================================================"
echo
echo "Arraste para a pasta Aplicativos, ou use direto de onde está."
echo
echo "IMPORTANTE - primeira abertura:"
echo "  O app não tem assinatura da Apple, então o macOS vai barrar"
echo "  com 'não foi possível verificar o desenvolvedor'."
echo
echo "  Solução: clique com o BOTÃO DIREITO no app > Abrir > Abrir."
echo "  Só na primeira vez. Ou, pelo terminal:"
echo "      xattr -dr com.apple.quarantine \"$DESTINO\""
