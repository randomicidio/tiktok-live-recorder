# Tiktok Live Recorder

Grava suas lives do TikTok em MP4, sem pesar no computador.

## Usando o programa

Dê dois cliques em **`Tiktok Live Recorder.exe`** (dentro de `dist/`).

1. Digite seu `@`
2. Clique em **● Iniciar Gravação**

Pronto. Pode armar antes de começar a transmitir: ele verifica a cada 30
segundos e começa a gravar sozinho assim que você entra ao vivo. Quando a live
acaba, o arquivo é fechado e ele volta a esperar a próxima.

Os vídeos vão para `Vídeos\TikTok Lives\` (ou a pasta que você escolher).

## O executável

O `.exe` é **um arquivo só, de 52,7 MB, que não depende de nada**: não precisa
de Python, nem de ffmpeg, nem de instalação. Copie para onde quiser — pendrive,
outro PC, área de trabalho — e ele funciona.

O ffmpeg vai embutido dentro dele. Testado num ambiente com o PATH limpo de
Python e ffmpeg: abriu e reconheceu tudo normalmente.

As configurações ficam num `config.json` ao lado do executável. Se a pasta for
somente leitura (Program Files, por exemplo), ele passa a usar
`%APPDATA%\TiktokLiveRecorder` sozinho.

## O botão REC

É um alternador:

| Clique | Estado |
|---|---|
| 1º | **REC armado** — botão afundado e escuro, esperando a live |
| 2º (antes da live) | Desarma e volta ao normal |

Durante a gravação o botão fica travado em **GRAVANDO**, de propósito: para
encerrar use **■ Parar e salvar**, e assim ninguém derruba uma gravação em
andamento com um clique distraído.

## O painel de estado

A faixa logo abaixo dos botões muda de cor conforme o que está acontecendo:

| Cor | Estado |
|---|---|
| Cinza | Parado |
| Âmbar | Esperando live iniciar |
| Laranja | Você está ao vivo |
| **Vermelho, bolinha piscando** | **Gravando** |
| Azul | Finalizando o arquivo |
| Verde | Gravação salva |
| Vermelho | Erro (o motivo aparece no registro) |

Durante a gravação, o tempo decorrido e o tamanho do arquivo aparecem à direita
da faixa. O estado também vai para o título da janela (`● GRAVANDO - ...`),
então dá para conferir pela barra de tarefas com a janela minimizada.

## O registro

Cada gravação concluída vira um cartão com miniatura, tamanho, duração e dois
atalhos: **▶ Abrir vídeo** e **Ver na pasta**.

## Qualidade e FPS: não há o que escolher

O programa sempre pega a variante `origin` do TikTok, que é o seu próprio vídeo
sem recompressão — a melhor coisa disponível. As outras são reduzidas:

| Variante | Resolução |
|---|---|
| **`origin` (a usada)** | **1080x1920** |
| `uhd` | 1280x720 |
| `hd` / `sd` | 960x540 |
| `ld` | 640x360 |

**Sobre FPS:** o TikTok não informa a taxa de quadros em lugar nenhum da API.
Mas não faz diferença: como o vídeo é copiado sem recodificar, o arquivo herda
exatamente o que vier — se você transmite a 60, o MP4 sai com 60. Gravar o
`origin` já significa resolução máxima e FPS original.

Por isso gravar ao vivo dá um arquivo **melhor** que baixar o replay depois, que
passa por recompressão.

## Por que isso não derruba os frames da sua live

O programa **não captura a tela e não recodifica nada**. Ele pede ao TikTok a
mesma URL de vídeo que o celular de quem te assiste recebe, e o ffmpeg copia
esses bytes para o disco (`-c copy`).

Medido nesta máquina, gravando um stream 1080x1920 a 2,5 Mbps:

| | CPU |
|---|---|
| Este gravador (`-c copy`) | abaixo do medível — 0,00s de CPU em 14s |
| Um gravador que recodifica | 0,97s de CPU em 14s |

O contador de tamanho na tela custa 0,14s de CPU por hora de live (0,004% de um
núcleo). A GPU não é tocada. O que ele usa é ~2-3 Mbps de **download**, enquanto
o OBS usa **upload** — vias separadas, não competem.

## Se a internet cair no meio da live

A gravação continua sozinha. O programa percebe a queda, verifica se você ainda
está ao vivo, pega URLs novas e abre uma parte nova. No final, todas as partes
são unidas num único MP4 automaticamente.

## Se faltar luz ou o PC desligar

Nada é perdido além do que ainda não tinha baixado. A gravação vai para arquivos
`.ts`, um formato que não corrompe com interrupção — o MP4 só é montado no fim.

---

# Gerando os executáveis

## Windows

```
python build_windows.py
```

Sai em `dist\Tiktok Live Recorder.exe`. Precisa de Python, PyInstaller
(`pip install pyinstaller`) e ffmpeg no PATH — só na máquina que empacota.

## macOS — pelo GitHub Actions (sem precisar de um Mac)

Este é o caminho recomendado. O GitHub tem máquinas macOS de verdade, e o
workflow `.github/workflows/build-mac.yml` monta o `.app` numa delas.

**Primeira vez** — envie o projeto para um repositório:

```
git init
git add .
git commit -m "Tiktok Live Recorder"
git branch -M main
git remote add origin https://github.com/SEU-USUARIO/tiktok-live-recorder.git
git push -u origin main
```

**Para gerar o app:** no GitHub, aba **Actions** > **Build macOS app** >
**Run workflow**. Em poucos minutos o `.zip` aparece em **Artifacts**, no rodapé
da execução. Também dispara sozinho ao publicar uma tag (`git tag v1.0 &&
git push --tags`).

Ele monta a versão **Apple Silicon** (M1/M2/M3/M4).

**Mac com Intel:** a GitHub aposentou as máquinas Intel — o runner `macos-13`
não recebe mais execução, fica na fila indefinidamente. Não há como gerar esse
binário pelo Actions. Se precisar dele, rode o `build_mac.sh` num Mac Intel.

O workflow **se verifica sozinho** antes de publicar: confere que o ffmpeg foi
mesmo embutido, que ele não aponta para nenhuma biblioteca de fora do pacote, e
executa o binário com o ambiente zerado. Se qualquer uma dessas falhar, o build
falha com o motivo — em vez de entregar um `.app` que só funcionaria naquele
runner.

## macOS — num Mac que você tenha

Alternativa ao Actions, se preferir. Copie a pasta do projeto para o Mac e rode:

```
chmod +x build_mac.sh
./build_mac.sh
```

O script confere o ambiente, instala o que falta num ambiente isolado, gera os
ícones e empacota. Sai em `dist/Tiktok Live Recorder.app`.

Três coisas para saber:

1. **ffmpeg estático.** O script embute o ffmpeg da máquina. O do Homebrew
   depende de bibliotecas que só existem lá — o app funciona nesse Mac, mas pode
   falhar em outro. O script detecta isso e avisa. Para distribuir, use um
   ffmpeg estático ([osxexperts.net](https://osxexperts.net) para Apple Silicon,
   [evermeet.cx](https://evermeet.cx/ffmpeg/) para Intel).

2. **Arquitetura.** O app gerado roda na arquitetura do Mac que o montou (Apple
   Silicon ou Intel). Para um binário universal seria preciso um Python
   universal2 e todas as dependências no mesmo formato.

3. **Gatekeeper.** Sem assinatura da Apple, o macOS barra na primeira abertura.
   Clique com o **botão direito no app > Abrir > Abrir** — só na primeira vez.

*Nota: o `build_mac.sh` e o workflow tiveram a sintaxe verificada, mas nenhum
dos dois foi executado num macOS de verdade. O workflow foi escrito para se
verificar sozinho justamente por isso: se algo estiver errado, ele acusa em vez
de entregar um app quebrado.*

## Arquivos

| Arquivo | O que faz |
|---|---|
| `app.py` | Interface gráfica |
| `tiktok_api.py` | Descobre se você está ao vivo e as URLs do vídeo |
| `recorder.py` | Controla o ffmpeg, reconexão, montagem do MP4 e miniaturas |
| `resources.py` | Acha arquivos e binários, empacotado ou não |
| `make_icon.py` | Gera os ícones (`.ico`, `.icns`, `.png`) |
| `build_windows.py` | Empacota o `.exe` |
| `build_mac.sh` | Empacota o `.app` (rodar num Mac) |
| `.github/workflows/build-mac.yml` | Monta o `.app` no GitHub Actions |

## Limitação conhecida

Os endpoints usados são internos do TikTok, não uma API oficial e documentada.
Eles funcionam hoje e são os mesmos que o site usa, mas o TikTok pode mudá-los
sem aviso. Se um dia parar de funcionar, o sintoma será "Falhou" ao clicar em
Verificar agora — o que precisará de ajuste é o `tiktok_api.py`.
