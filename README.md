# 🛡️ Removedor de Metadados & Alterador de Hash

Ferramenta **100% offline** para macOS que remove **todos os metadados** (EXIF, GPS, XMP, IPTC, datas, título, câmera, software, capítulos, sensores do iPhone…) de **fotos, vídeos e áudios** e gera arquivos com **hash diferente** — sem perder qualidade.

- **Fotos** (JPG, PNG, WebP, GIF): limpeza *sem recodificar* — os pixels saem byte a byte idênticos.
- **HEIC / HEIF / AVIF / RAW** de câmera → convertidos para JPEG limpo. **TIFF / BMP** → PNG limpo.
- **Vídeos e áudios**: streams copiados para um container novo sem recodificar (via FFmpeg); recodifica só se a cópia direta for impossível.
- **Hash novo a cada execução**, usando preenchimento permitido pela especificação de cada formato — nenhum metadado novo é gravado.
- **Verificação automática**: cada arquivo de saída é reanalisado e marcado como *Limpo*; relatório JSON/TXT com os hashes SHA-256 e MD5 antes/depois.
- Interface gráfica nativa (Tkinter) e linha de comando. Sem dependências `pip`.

## Instalação (macOS)

Cole no Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/PTK25/remove-hash/main/install.sh | bash
```

O instalador:

1. instala o [Homebrew](https://brew.sh) se ainda não existir (pede confirmação);
2. instala o **FFmpeg** e o **Python com Tkinter** pelo Homebrew;
3. monta o app **"Removedor de Metadados.app"** em `/Applications`;
4. cria o comando de terminal `remove-hash`.

Como o app é montado no seu próprio Mac, o macOS **não** mostra o aviso de "desenvolvedor não identificado".

Alternativa com o repositório clonado:

```bash
git clone https://github.com/PTK25/remove-hash.git
cd remove-hash
./install.sh
```

Desinstalar (mantém FFmpeg/Python):

```bash
curl -fsSL https://raw.githubusercontent.com/PTK25/remove-hash/main/uninstall.sh | bash
```

> **Linux**: o motor funciona com `ffmpeg` + `python3-tk` instalados (`sudo apt install ffmpeg python3-tk`) — rode `python3 remove_hash_gui.py`. A conversão de HEIC/RAW usa o FFmpeg nesse caso. Windows não foi testado.

## Uso

### App

Abra **Removedor de Metadados** (Launchpad ou Spotlight), adicione arquivos ou uma pasta inteira (subpastas incluídas), escolha o destino e clique em **Processar**. Cada linha mostra o status e o novo hash; dê **duplo clique** em um arquivo para ver os detalhes (metadados removidos, hashes completos, tags técnicas restantes).

`Arquivo → Inspecionar metadados…` mostra o que um arquivo contém **sem modificá-lo** — útil para conferir o antes e o depois.

### Terminal

```bash
remove-hash video.mp4                     # um arquivo → ./LIMPO_<data>/
remove-hash ~/Fotos ~/Videos -o ~/Desktop # pastas inteiras, destino escolhido
remove-hash -i foto.jpg                   # só inspecionar: lista os metadados presentes
remove-hash --sem-icc ~/Fotos             # remover também o perfil de cor ICC
remove-hash --help
```

Sem instalar: `python3 remove_hash.py …` / `python3 remove_hash_gui.py`.

## O que é removido

| Tipo | Removido |
|------|----------|
| JPEG | EXIF (inclusive GPS, câmera, datas, miniatura), XMP, IPTC/Photoshop, comentários, MPF (imagens embutidas), APPs desconhecidos, **dados ocultos após o fim da imagem** (ex.: vídeo de "foto em movimento") |
| PNG | `tEXt`/`zTXt`/`iTXt` (inclui XMP), `eXIf`, `tIME`, chunks privados (Apple, etc.), dados após `IEND` |
| WebP | `EXIF`, `XMP`, chunks desconhecidos (estático e animado) |
| GIF | comentários, XMP, extensões de aplicação desconhecidas (o loop da animação é mantido) |
| HEIC/HEIF/AVIF/RAW/TIFF/BMP | tudo — o arquivo é convertido (JPEG ou PNG) e limpo como tal |
| Vídeo (MP4, MOV, MKV, WebM, AVI, …) | todas as tags globais e por stream (título, GPS, `creation_time`, câmera, software…), capítulos, streams de dados (GPS/acelerômetro/rostos detectados do iPhone, timecode), capas |
| Áudio (MP3, M4A, FLAC, WAV, …) | ID3 / tags do container, capa embutida |

**Mantido por padrão** (não identifica ninguém; evita cores erradas ou foto deitada):

- o **perfil de cor ICC** — desmarque "Manter perfil de cor" ou use `--sem-icc` para remover;
- a **orientação** da foto, gravada em uma tag EXIF única contendo só esse valor;
- tags técnicas genéricas do container: `major_brand`, `handler_name=VideoHandler`, `language=und`, `encoder=Lavf` (sem versão). Elas aparecem listadas nos detalhes e no relatório.

**O que a ferramenta não faz:**

- não altera o conteúdo visível (rostos, placas, textos na imagem);
- não remove assinaturas do codificador embutidas *dentro* do stream (ex.: SEI do x264, `Lavc` no AAC) quando copia sem recodificar — elas não contêm dados pessoais;
- não engana comparação **visual/perceptual**: o hash criptográfico muda, mas a imagem continua a mesma para sistemas de reconhecimento de conteúdo.

## Como o hash muda

Nenhum metadado é adicionado e nenhum byte "solto" é anexado ao arquivo. O preenchimento usado é o que a própria norma de cada formato define como ignorável:

| Formato | Técnica |
|---------|---------|
| JPEG | bytes de preenchimento `0xFF` antes de marcadores (ITU T.81 §B.1.1.2) |
| PNG | divisão do `IDAT` em chunks de tamanhos aleatórios |
| WebP | chunk `JUNK` no container RIFF |
| GIF | refatiamento dos sub-blocos de dados da imagem |
| MP4 / MOV / M4V / 3GP / M4A | caixa `free` (ISO BMFF) |
| MKV / WebM | elemento EBML `Void` dentro do Segment |
| AVI / WAV | chunk `JUNK` (RIFF) |
| FLAC | bloco `PADDING` redimensionado |
| MP3 | tag ID3v2 contendo só padding |

Todos os arquivos de saída decodificam sem nenhum aviso em `ffmpeg -xerror`, Pillow, ImageMagick, `sips` (macOS) e libwebp.

## Formatos suportados

| Tipo | Extensões |
|------|-----------|
| 🖼️ Foto (sem recodificar) | jpg, jpeg, jpe, jfif, png (inclui APNG), webp (inclui animado), gif (inclui animado) |
| 🖼️ Foto (convertida para JPEG) | heic, heif, hif, avif, cr2, cr3, nef, arw, dng, orf, rw2, raf, pef, srw |
| 🖼️ Foto (convertida para PNG) | tif, tiff, bmp |
| 🎬 Vídeo | mp4, m4v, mov, 3gp, 3g2, mkv, webm, avi, wmv, asf, flv, f4v, mpg, mpeg, ts, mts, m2ts, vob, ogv, mxf |
| 🎵 Áudio | mp3, m4a, aac, flac, wav, aif, aiff, ogg, oga, opus, wma |

## Saída

```
[destino]/LIMPO_2026-09-25_14-30-00/
├── IMG_0001.jpg        # limpo, pixels idênticos, hash novo
├── IMG_0002.jpg        # era IMG_0002.heic
├── ferias/clipe.mp4    # subpastas preservadas
├── relatorio.json      # hashes SHA-256/MD5 antes e depois, metadados removidos, verificação
└── relatorio.txt       # o mesmo, legível
```

Os originais nunca são modificados.

## Conferindo por conta própria

```bash
remove-hash -i original.jpg limpo.jpg          # inspeção interna
ffprobe -hide_banner limpo.mp4                 # tags do vídeo
exiftool limpo.jpg                             # se tiver o exiftool (brew install exiftool)
shasum -a 256 original.jpg limpo.jpg           # hashes
```

## Desenvolvimento

```
remove_hash_core.py   motor (parsers JPEG/PNG/WebP/GIF, FFmpeg, padding, verificação, relatório)
remove_hash_gui.py    interface Tkinter
remove_hash.py        linha de comando
install.sh            instalador macOS (monta o .app e o comando remove-hash)
uninstall.sh          desinstalador
tests/test_core.py    testes (python3 -m unittest discover -s tests -v)
assets/               ícone
```

Os testes geram as fixtures na hora; usam Pillow (opcional, `pip install pillow`) para as imagens com EXIF e o FFmpeg do sistema para vídeo/áudio.

## Licença

Código deste repositório: consulte o arquivo `LICENSE` (se ausente, todos os direitos reservados ao autor). O FFmpeg **não** é distribuído aqui — é instalado separadamente pelo Homebrew, sob sua própria licença (LGPL/GPL).
