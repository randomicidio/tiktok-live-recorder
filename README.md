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

O `.exe` é **um arquivo só que não depende de nada**: não precisa de Python,
nem de ffmpeg, nem de instalação. Copie para onde quiser — pendrive, outro PC,
área de trabalho — e ele funciona.

O ffmpeg, o ffprobe e a libmpv vão embutidos dentro dele, e é daí que vem quase
todo o tamanho: os dois binários do ffmpeg somam mais de 130 MB nas builds
atuais, então o executável sai em torno de 160 MB. Se quiser um arquivo menor,
empacote com uma build enxuta do ffmpeg no PATH — o script usa a que estiver lá.

Os dois são obrigatórios, e por motivos diferentes: o **ffmpeg** grava e monta
os vídeos, e o **ffprobe** informa duração e resolução. Sem o ffprobe o editor
não consegue nem abrir um arquivo, então o build recusa empacotar sem ele.

Testado num ambiente com o PATH limpo de Python e de ffmpeg: abriu e anotou no
registro que a prévia do editor e o registro de presentes estão disponíveis,
sem nenhum aviso de binário faltando — ou seja, achou tudo dentro do próprio
pacote.

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

### Armar sozinho ao abrir

A opção **"Armar o REC sozinho ao abrir o programa"** dispensa o primeiro
clique: com ela marcada, basta abrir o programa que ele já sai armado, com a
conta e a pasta que estavam salvas. Serve para não perder uma live por ter
esquecido de apertar REC.

Se faltar o @ ou a pasta não puder ser usada, o motivo vai para o registro e a
faixa fica vermelha — nenhuma janela de aviso aparece, justamente porque um
diálogo esperando OK seguraria a gravação até alguém voltar ao computador.

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

As mesmas linhas vão para um `registro.log` ao lado do `config.json`, que
sobrevive ao fechamento do programa. É lá que aparece o motivo quando algo dá
errado numa máquina que não é a sua — inclusive travamentos, que antes sumiam
sem deixar rastro (num `.exe` sem console não há para onde o erro ir). O arquivo
se limita a 2 MB e guarda uma geração anterior em `registro.log.1`.

## O computador não dorme com o REC armado

Enquanto o REC está armado — esperando a live ou gravando — o programa impede a
suspensão automática do Windows. Sem isso, armar e sair de casa terminava com o
PC dormindo em meia hora e a live perdida. Só o sono do sistema é barrado: a
tela apaga normalmente, e tudo volta ao normal assim que você desarma.

## Espaço em disco

Ao armar, o programa diz quanto cabe: uma live 1080x1920 rende por volta de
1,1 GB por hora. Abaixo de 5 GB livres ele avisa no registro, e abaixo de 1 GB
avisa de novo durante a gravação. São só avisos — nada é bloqueado, e a decisão
de gravar mesmo assim é sua.

## Versão nova

Na abertura, o programa consulta se saiu uma versão mais nova e, se saiu, põe um
cartão azul no registro com um atalho para a página. Não baixa nada nem altera
nada. Isso existe por causa da limitação lá embaixo: os endpoints do TikTok
podem mudar, e sem esse aviso quem tem o executável antigo ficaria preso numa
versão quebrada sem saber que já existe conserto.

Se não houver rede, ou nenhuma versão publicada, a consulta falha em silêncio.

## O cookie fica cifrado

O campo de cookie guarda a sessão da sua conta: quem o tem entra como você. Ele
é gravado no `config.json` cifrado pela DPAPI do Windows, que usa a sua conta de
usuário como chave — sem senha para inventar. Na prática isso significa que um
`config.json` copiado para outra máquina não entrega o cookie lá.

Configurações antigas, com o cookie em texto puro, continuam funcionando e
passam a ser gravadas cifradas na primeira vez que algo for salvo.

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

O programa **não captura a tela e não recodifica o vídeo**. Ele pede ao TikTok a
mesma URL de vídeo que o celular de quem te assiste recebe, e o ffmpeg copia
esses bytes de vídeo para o disco (`-c:v copy`).

O áudio é a única exceção: o TikTok transmite em HE-AAC, que editores como o
DaVinci Resolve não conseguem ler, então ele é convertido para AAC-LC durante a
gravação. É barato porque mexe só no áudio — o vídeo, que é a parte pesada,
continua sendo cópia crua.

Medido nesta máquina, gravando um stream 1080x1920 a 2,5 Mbps:

| | CPU |
|---|---|
| Este gravador (vídeo copiado, áudio convertido) | 2% de um núcleo — 12,3s de CPU por 10 min de live |
| O mesmo, se o áudio também fosse copiado | abaixo do medível — 0,4s por 10 min |
| Um gravador que recodifica o vídeo | 0,97s de CPU em 14s (~7% de um núcleo) |

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
Eles ficam soltos na pasta de saída, com o nome da live e o sufixo `_parte01`,
justamente para não sumirem da vista quando algo dá errado.

Na próxima vez que o programa abrir, ele percebe sozinho que aquela gravação não
chegou a virar MP4 e põe no registro um cartão amarelo com a data, o tamanho e um
botão **Recuperar**. Não aparece nenhuma janela pedindo resposta: se você ignorar,
o cartão simplesmente volta na abertura seguinte.

Um clique junta as partes, monta o MP4 e refaz o `.ttgifts` a partir do registro
de presentes e chat. O resultado é indistinguível de uma gravação que terminou
bem — o editor acha o pacote sozinho.

**Vale correr com esse.** Remontar o vídeo pode esperar o tempo que for, mas o
`.ttgifts` guarda as animações dos presentes baixando-as do TikTok na hora em que
é fechado, e essas URLs expiram em algumas horas. Quanto antes recuperar, mais
animação se salva. O texto do chat e a lista de presentes não têm esse prazo:
estão no disco desde o momento em que aconteceram.

A varredura não pesa na abertura: são décimos de milissegundo, e ela roda em
segundo plano — a janela abre no mesmo tempo de sempre e o cartão chega logo
depois.

---

# Gerando os executáveis

## Windows

```
python build_windows.py
```

Sai em `dist\Tiktok Live Recorder.exe`. Precisa de Python, PyInstaller
(`pip install pyinstaller`) e ffmpeg no PATH — só na máquina que empacota.

Na primeira vez o build baixa `assets/emoji.zip` (13 MB, as figuras usadas
para desenhar o chat). Dá para adiantar com `python emoji_pack.py`.

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

## A aba Editar

É onde o vídeo gravado ganha as camadas: as animações oficiais dos presentes, o
chat e o cartão de presente. A prévia mostra tudo isso ao vivo, no lugar e no
instante em que vai sair no arquivo.

**A barra de navegação.** Clicar leva direto ao ponto clicado. Ao fundo vai a
forma de onda do áudio, e por cima dela as marcas de **Início** (verde) e
**Fim** (vermelho), que podem ser arrastadas. A roda do mouse aproxima a vista
em volta do ponteiro — dá para chegar a menos de dois segundos de janela, o que
é precisão de quadro; o botão direito arrasta a vista, e o **⤢** volta a mostrar
o vídeo inteiro.

**Sincronia do áudio.** Quando a gravação sai com o som deslocado do vídeo, o
campo em milissegundos acerta os dois: positivo atrasa o áudio, negativo o
adianta. Vale ao mesmo tempo na prévia, na forma de onda e no vídeo exportado, e
volta a zero a cada vídeo aberto — um acerto esquecido de outro arquivo
estragaria o corte seguinte sem ninguém perceber. O espaço nesse campo toca o
vídeo em vez de digitar, e devolve o teclado à barra do tempo.
Quem se desloca é o áudio da live: as animações, o chat e o contador
são posicionados pelo relógio do vídeo, e o som das animações continua onde
estava. Na exportação o arquivo entra duas vezes, com o corte do áudio
deslocado, para o som seguir inteiro nas duas pontas do trecho em vez de abrir
um silêncio no começo.

**Pôr uma animação à mão.** Embaixo da lista de presentes, **+ Adicionar
animação...** abre o catálogo do TikTok — todos os presentes que têm animação de
tela cheia, com o ícone oficial e o nome em português. Escolhido o presente, a
animação entra na posição em que o vídeo está parado e se comporta como se
tivesse acontecido na live: espera a anterior acabar, aparece na prévia e sai no
MP4. É o que salva um replay baixado de outro lugar, que não tem `.ttgifts` e,
portanto, não tem presente nenhum para compor.

O campo **De @** é opcional: com ele, o nome e a foto de quem mandou vêm do
TikTok e o cartão do contador fica igual ao de um presente de verdade. Sem ele,
o cartão diz apenas "Alguém".

A lista é buscada uma vez e fica guardada (`presentes.json`, ao lado do
`config.json`), então a segunda abertura é instantânea; ela é renovada sozinha
depois de uma semana, e o botão **Atualizar do TikTok** força na hora. Os
ícones ficam em `icones/` e cada animação é baixada só quando alguém a escolhe,
para `animacoes/`. Os `.ttgifts` das suas gravações entram na mesma lista: são
eles que respondem sem internet, e são a única fonte de um presente que já saiu
de catálogo.

Na lista, o que foi posto à mão vem com **`*`** e pode ser removido (botão
**Remover** ou a tecla Delete) — o que veio da live, não: o pacote é o registro
do que aconteceu, e o editor não reescreve isso. O que você adicionar fica
guardado por vídeo no `editor.json`, então reabrir o mesmo arquivo devolve o
trabalho.

**A prévia é a mesma coisa que o arquivo.** O chat e o contador são desenhados
pelo mesmo código que a exportação usa, então o que aparece na tela é o que sai
no MP4 — não é uma aproximação.

**Abrir e exportar lembram pastas diferentes.** O replay da live costuma ficar
num lugar e os cortes em outro, então cada diálogo volta para onde esteve pela
última vez, sem um arrastar o outro. Ficam no `editor.json`, ao lado do
`config.json`.

**A exportação mostra o progresso** das duas etapas (desenhar o chat e montar o
vídeo) e pode ser cancelada. Ela continua normalmente se você trocar de aba ou
minimizar a janela; enquanto ela roda, a prévia para de desenhar para não
disputar processador com a exportação.

## Como o chat e o contador foram medidos

Não há arte oficial dessas camadas, então elas são reconstruídas. As medidas não
foram estimadas a olho: saíram de uma gravação de tela do aplicativo a 1080 px
de largura, medindo pixel a pixel a foto, a coluna do texto, a entrelinha, a
altura de caixa alta das fontes, a opacidade da pílula do cartão e a duração de
cada animação. Tudo fica guardado em **fração da largura do vídeo**, e é isso
que faz o resultado bater com o original em qualquer resolução.

O que foi copiado da referência:

| Detalhe | Como é |
|---|---|
| Fonte | TikTok Sans em Medium (500); os números do cartão em Bold |
| Chat | apelido apagado em cima, mensagem em branco embaixo, sem retângulo de fundo — a legibilidade vem de uma sombra suave |
| Topo do chat | a mensagem que sai por cima se dissolve numa faixa curta, não de uma vez |
| Mensagem nova | a pilha desliza para cima em vez de saltar |
| Cartão de presente | entra pela esquerda, sai em dissolução; texto comprido termina em "…" |
| Contagem | o `x` sai bem menor que os dígitos; a cada unidade nova o número apaga por um instante e volta grande, encolhendo até o tamanho normal |
| Presente único | não mostra contagem nenhuma, igual ao app |

## Arquivos

| Arquivo | O que faz |
|---|---|
| `app.py` | Interface gráfica |
| `tiktok_api.py` | Descobre se você está ao vivo e as URLs do vídeo |
| `recorder.py` | Controla o ffmpeg, reconexão, montagem do MP4 e miniaturas |
| `gift_log.py` | Registra presentes, chat e emotes durante a live |
| `pacote.py` | Lê e escreve o `.ttgifts` que acompanha o vídeo |
| `catalogo.py` | Lista de presentes do TikTok e dos pacotes, para adicionar à mão |
| `effects_api.py` | Resolve e baixa as animações oficiais dos presentes |
| `editor.py` | Aba do editor: prévia com as camadas e exportação |
| `compositor.py` | Monta o vídeo final com as animações e o chat |
| `tipografia.py` | Desenha texto de qualquer idioma e os emoji |
| `resources.py` | Acha arquivos e binários, empacotado ou não |
| `diario.py` | Registro em arquivo e captura de travamentos |
| `segredo.py` | Cifra o cookie com a DPAPI do Windows |
| `atualizacao.py` | Consulta se saiu versão nova |
| `testes.py` | Testes de regressão (`python testes.py`) |
| `make_icon.py` | Gera os ícones (`.ico`, `.icns`, `.png`) |
| `emoji_pack.py` | Monta `assets/emoji.zip` (chamado pelo build) |
| `build_windows.py` | Empacota o `.exe` |
| `build_mac.sh` | Empacota o `.app` (rodar num Mac) |
| `.github/workflows/build-mac.yml` | Monta o `.app` no GitHub Actions |

## Limitações conhecidas

Os endpoints usados são internos do TikTok, não uma API oficial e documentada.
Eles funcionam hoje e são os mesmos que o site usa, mas o TikTok pode mudá-los
sem aviso. Se um dia parar de funcionar, o sintoma será "Falhou" ao clicar em
Verificar agora — o que precisará de ajuste é o `tiktok_api.py`.

O chat desenhado é uma reconstrução: fica muito parecido, não idêntico.

Emotes próprios da live e selos do apelido (nível, clube de fãs, ranking) só
aparecem em **gravações novas** — são dados que antes não eram registrados, e
não dá para recuperar depois. Quase todos vêm prontos do TikTok e são copiados
como estão; a exceção é o selo do clube de fãs, que chega partido em ícone mais
nome e é remontado aqui — esse fica próximo, não igual.

Escritas que precisam de ligadura para serem lidas direito — árabe, hebraico,
algumas da Índia — aparecem com as letras soltas e na ordem errada, porque a
biblioteca de desenho vem sem motor de forma. Latino, cirílico, grego, japonês,
chinês, coreano, tailandês e os emoji saem certos.

## Créditos

A fonte do chat é a [TikTok Sans](https://fonts.google.com/specimen/TikTok+Sans),
publicada no Google Fonts sob a licença OFL — é a mesma que o app usa
(`assets/fonts`, com o texto da licença ao lado).

As figuras de emoji são do projeto [Noto Emoji](https://github.com/googlefonts/noto-emoji)
do Google, sob a licença Apache 2.0 (o texto vai dentro de `assets/emoji.zip`).
São as mesmas que o TikTok mostra no Android.
