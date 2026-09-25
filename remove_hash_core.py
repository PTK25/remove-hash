#!/usr/bin/env python3
"""
Motor de limpeza — Removedor de Metadados & Alterador de Hash
==============================================================

Módulo compartilhado pela interface gráfica (remove_hash_gui.py) e pela
linha de comando (remove_hash.py).

Estratégia por tipo de arquivo
------------------------------
• JPEG / PNG / WebP / GIF  → limpeza SEM recodificar (pixels idênticos).
  Os metadados (EXIF, GPS, XMP, IPTC, comentários, datas) são removidos
  reescrevendo a estrutura do arquivo em Python puro. O hash muda por
  meio de "padding" permitido pela especificação de cada formato
  (bytes de preenchimento 0xFF no JPEG, refatiamento do IDAT no PNG,
  chunk JUNK no WebP, refatiamento dos sub-blocos no GIF), sem
  adicionar nenhum metadado novo.

• HEIC / HEIF / AVIF / RAW de câmera → convertidos para JPEG (via `sips`
  do macOS ou ffmpeg) e depois limpos como JPEG.
• TIFF / BMP → convertidos para PNG (sem perda) e limpos como PNG.

• Vídeo e áudio → ffmpeg remuxa os streams sem recodificar
  (`-map_metadata -1 -map_chapters -1 -fflags +bitexact`), descartando
  streams de dados (GPS/acelerômetro/rostos do iPhone), capítulos,
  capas e todas as tags. Se a cópia direta falhar, recodifica.
  O hash muda por padding válido: caixa `free` (MP4/MOV/M4A), elemento
  EBML Void (MKV/WebM), chunk JUNK (AVI/WAV), bloco PADDING (FLAC) ou
  padding ID3v2 (MP3).

Só depende do Python 3.9+ (biblioteca padrão) e do FFmpeg.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

__version__ = "2.0.0"

# ─────────────────────────────────────────────────────────
# FORMATOS SUPORTADOS
# ─────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mkv", ".webm", ".avi",
    ".wmv", ".asf", ".flv", ".f4v", ".mpg", ".mpeg", ".ts", ".mts",
    ".m2ts", ".vob", ".ogv", ".mxf",
}

# Imagens limpas sem recodificar (Python puro)
IMAGE_LOSSLESS_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".gif"}

# Imagens convertidas para JPEG antes da limpeza (fotos de celular/câmera)
IMAGE_TO_JPEG_EXTENSIONS = {
    ".heic", ".heif", ".hif", ".avif",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".srw",
}

# Imagens convertidas para PNG (sem perda) antes da limpeza
IMAGE_TO_PNG_EXTENSIONS = {".tif", ".tiff", ".bmp"}

IMAGE_EXTENSIONS = IMAGE_LOSSLESS_EXTENSIONS | IMAGE_TO_JPEG_EXTENSIONS | IMAGE_TO_PNG_EXTENSIONS

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".aif", ".aiff",
    ".ogg", ".oga", ".opus", ".wma",
}

ALL_SUPPORTED = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS

MP4_FAMILY = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".m4a"}
MATROSKA_FAMILY = {".mkv", ".webm"}
RIFF_FAMILY = {".avi", ".wav"}

OUTPUT_PREFIX = "LIMPO_"


# ─────────────────────────────────────────────────────────
# ESTRUTURAS
# ─────────────────────────────────────────────────────────

@dataclass
class Options:
    keep_icc: bool = True            # manter perfil de cor (não identifica ninguém; evita cores erradas)
    reencode_fallback: bool = True   # recodificar vídeo/áudio se a cópia direta falhar
    jpeg_quality: int = 92           # qualidade ao converter HEIC/RAW → JPEG


@dataclass
class FileResult:
    input: str
    output: str = ""
    kind: str = ""                 # video | image | audio
    method: str = ""               # lossless | convert | remux | reencode
    success: bool = False
    clean: bool = False            # verificação pós-processamento sem metadados restantes
    hash_before: str = ""
    hash_after: str = ""
    md5_before: str = ""
    md5_after: str = ""
    size_before: int = 0
    size_after: int = 0
    removed: list = field(default_factory=list)    # metadados removidos
    remaining: list = field(default_factory=list)  # tags técnicas restantes (benignas)
    notes: list = field(default_factory=list)
    error: Optional[str] = None
    seconds: float = 0.0


class Cancelled(Exception):
    """Processamento cancelado pelo usuário."""


# ─────────────────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────────────────

def file_hashes(path: str) -> tuple[str, str]:
    """SHA-256 e MD5 em uma única leitura."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def calculate_hash(path: str) -> str:
    return file_hashes(path)[0]


def human_size(size_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def get_file_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "unknown"


def _app_bundle_bin_dir() -> Optional[str]:
    """Se rodando de dentro de um .app, retorna Contents/Resources/bin."""
    here = Path(__file__).resolve().parent
    for parent in [here] + list(here.parents):
        if parent.name == "Resources" and parent.parent.name == "Contents":
            cand = parent / "bin"
            if cand.is_dir():
                return str(cand)
    return None


def _find_tool(name: str) -> Optional[str]:
    env = os.environ.get(f"REMOVE_HASH_{name.upper()}")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    bundled = _app_bundle_bin_dir()
    if bundled:
        cand = os.path.join(bundled, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin"):
        cand = os.path.join(base, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def find_ffmpeg() -> Optional[str]:
    return _find_tool("ffmpeg")


def find_ffprobe() -> Optional[str]:
    return _find_tool("ffprobe")


def find_sips() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    return _find_tool("sips")


def ffmpeg_version() -> Optional[str]:
    ff = find_ffmpeg()
    if not ff:
        return None
    try:
        out = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"ffmpeg version (\S+)", out.stdout)
        return m.group(1) if m else "?"
    except Exception:
        return None


def check_ffmpeg() -> bool:
    return find_ffmpeg() is not None


def collect_files(inputs) -> list[str]:
    """Coleta arquivos suportados de um ou mais caminhos (arquivos ou pastas)."""
    if isinstance(inputs, (str, os.PathLike)):
        inputs = [inputs]
    files: list[str] = []
    seen = set()
    for inp in inputs:
        inp = os.path.abspath(str(inp))
        if os.path.isfile(inp):
            if Path(inp).suffix.lower() in ALL_SUPPORTED and inp not in seen:
                files.append(inp)
                seen.add(inp)
        elif os.path.isdir(inp):
            for root, dirs, filenames in os.walk(inp):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not d.startswith(OUTPUT_PREFIX))
                for fname in sorted(filenames):
                    if fname.startswith("."):
                        continue
                    if Path(fname).suffix.lower() in ALL_SUPPORTED:
                        full = os.path.join(root, fname)
                        if full not in seen:
                            files.append(full)
                            seen.add(full)
    return files


def _uniquify(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ─────────────────────────────────────────────────────────
# EXIF (mínimo): leitura de orientação/GPS e criação de EXIF só com orientação
# ─────────────────────────────────────────────────────────

def _exif_scan(tiff: bytes) -> dict:
    """Lê a orientação e detecta GPS em um bloco TIFF/EXIF. Tolerante a erros."""
    info = {"orientation": 1, "gps": False, "tags": 0}
    try:
        if tiff[:2] == b"II":
            e = "<"
        elif tiff[:2] == b"MM":
            e = ">"
        else:
            return info
        if struct.unpack(e + "H", tiff[2:4])[0] != 42:
            return info
        ifd = struct.unpack(e + "I", tiff[4:8])[0]
        if ifd + 2 > len(tiff):
            return info
        n = struct.unpack(e + "H", tiff[ifd:ifd + 2])[0]
        info["tags"] = n
        for i in range(n):
            off = ifd + 2 + i * 12
            if off + 12 > len(tiff):
                break
            tag, typ, cnt = struct.unpack(e + "HHI", tiff[off:off + 8])
            if tag == 0x0112 and typ == 3:
                info["orientation"] = struct.unpack(e + "H", tiff[off + 8:off + 10])[0] or 1
            elif tag == 0x8825:
                gps_off = struct.unpack(e + "I", tiff[off + 8:off + 12])[0]
                if gps_off + 2 <= len(tiff):
                    gn = struct.unpack(e + "H", tiff[gps_off:gps_off + 2])[0]
                    info["gps"] = gn > 0
    except Exception:
        pass
    if not (1 <= info["orientation"] <= 8):
        info["orientation"] = 1
    return info


def _exif_label(tiff: bytes) -> str:
    scan = _exif_scan(tiff)
    if scan["gps"]:
        return "EXIF (com GPS)"
    if scan["tags"] == 1 and scan["orientation"] != 1:
        return "EXIF (só orientação)"
    return "EXIF"


BENIGN_IMAGE_ITEMS = {"perfil ICC", "EXIF (só orientação)"}


def _minimal_exif_tiff(orientation: int) -> bytes:
    """Bloco TIFF contendo apenas a tag Orientation (necessária para a foto não ficar deitada)."""
    return (
        b"MM\x00\x2a\x00\x00\x00\x08"                       # cabeçalho TIFF big-endian, IFD0 @ 8
        + b"\x00\x01"                                        # 1 entrada
        + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0)  # Orientation, SHORT, count 1
        + b"\x00\x00\x00\x00"                                # sem próximo IFD
    )


# ─────────────────────────────────────────────────────────
# JPEG — limpeza sem recodificar
# ─────────────────────────────────────────────────────────

_JPEG_STANDALONE = {0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7}


def _jpeg_segment_name(marker: int, payload: bytes) -> tuple[str, bool]:
    """Retorna (nome legível, é_metadado)."""
    if marker == 0xE0:
        if payload.startswith(b"JFIF\x00"):
            return "JFIF", False
        if payload.startswith(b"JFXX\x00"):
            return "miniatura JFXX", True
        return "APP0", True
    if marker == 0xE1:
        if payload.startswith(b"Exif\x00\x00"):
            return _exif_label(payload[6:]), True
        if payload.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
            return "XMP", True
        if payload.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
            return "XMP estendido", True
        return "APP1", True
    if marker == 0xE2:
        if payload.startswith(b"ICC_PROFILE\x00"):
            return "perfil ICC", True
        if payload.startswith(b"MPF\x00"):
            return "MPF (imagens embutidas)", True
        return "APP2", True
    if marker == 0xED and payload.startswith(b"Photoshop 3.0\x00"):
        return "IPTC/Photoshop", True
    if marker == 0xEE and payload.startswith(b"Adobe"):
        return "Adobe (transformação de cor)", False
    if marker == 0xFE:
        return "comentário", True
    if 0xE0 <= marker <= 0xEF:
        ident = payload[:12].split(b"\x00")[0]
        try:
            ident_s = ident.decode("ascii")
            if not ident_s.isprintable():
                ident_s = ""
        except Exception:
            ident_s = ""
        return f"APP{marker - 0xE0}" + (f" ({ident_s})" if ident_s else ""), True
    return f"marcador 0x{marker:02X}", False


def _jpeg_parse(data: bytes) -> dict:
    """Divide um JPEG em segmentos. Retorna dict com 'segments' e 'trailing'."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("não é um arquivo JPEG válido")
    n = len(data)
    pos = 2
    segments = []  # dicts: marker, raw(bytes), payload, name, is_meta, entropy(bool)
    trailing = 0
    while pos < n:
        if data[pos] != 0xFF:
            raise ValueError("estrutura JPEG inválida (marcador esperado)")
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            trailing = n - pos
            break
        if marker in _JPEG_STANDALONE:
            segments.append({"marker": marker, "raw": bytes([0xFF, marker]), "payload": b"",
                             "name": f"marcador 0x{marker:02X}", "is_meta": False, "entropy": False})
            continue
        if pos + 2 > n:
            raise ValueError("segmento JPEG truncado")
        seglen = struct.unpack(">H", data[pos:pos + 2])[0]
        if seglen < 2 or pos + seglen > n:
            raise ValueError("comprimento de segmento JPEG inválido")
        seg_end = pos + seglen
        payload = data[pos + 2:seg_end]
        if marker == 0xDA:  # SOS + dados codificados até o próximo marcador real
            scan = seg_end
            while True:
                idx = data.find(b"\xff", scan)
                if idx < 0 or idx + 1 >= n:
                    idx = n
                    break
                nxt = data[idx + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    scan = idx + 2
                    continue
                if nxt == 0xFF:
                    scan = idx + 1
                    continue
                break
            segments.append({"marker": marker, "raw": data[pos - 2:idx], "payload": payload,
                             "name": "SOS", "is_meta": False, "entropy": True})
            pos = idx
            continue
        name, is_meta = _jpeg_segment_name(marker, payload)
        segments.append({"marker": marker, "raw": data[pos - 2:seg_end], "payload": payload,
                         "name": name, "is_meta": is_meta, "entropy": False})
        pos = seg_end
    if not any(s["entropy"] for s in segments):
        raise ValueError("JPEG sem dados de imagem (SOS)")
    return {"segments": segments, "trailing": trailing}


def strip_jpeg(data: bytes, keep_icc: bool = True, randomize: bool = True) -> tuple[bytes, list[str], list[str]]:
    """Remove metadados de um JPEG sem recodificar. Retorna (novo, removidos, notas)."""
    parsed = _jpeg_parse(data)
    removed: list[str] = []
    notes: list[str] = []
    orientation = 1
    kept: list[dict] = []
    for seg in parsed["segments"]:
        if seg["is_meta"]:
            if seg["name"] == "perfil ICC" and keep_icc:
                kept.append(seg)
                continue
            if seg["name"].startswith("EXIF"):
                orientation = _exif_scan(seg["payload"][6:])["orientation"]
            removed.append(seg["name"])
        else:
            kept.append(seg)
    if parsed["trailing"]:
        removed.append(f"dados ocultos após o fim da imagem ({human_size(parsed['trailing'])})")

    out = bytearray(b"\xff\xd8")
    header_marker_positions: list[int] = []
    first_sos_seen = False
    if orientation != 1:
        tiff = _minimal_exif_tiff(orientation)
        payload = b"Exif\x00\x00" + tiff
        seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        # coloca depois do APP0 JFIF, se houver
        if kept and kept[0]["marker"] == 0xE0:
            header_marker_positions.append(len(out))
            out += kept[0]["raw"]
            kept = kept[1:]
        header_marker_positions.append(len(out))
        out += seg
        notes.append(f"orientação EXIF {orientation} preservada (tag única)")
    for seg in kept:
        if not first_sos_seen:
            header_marker_positions.append(len(out))
        out += seg["raw"]
        if seg["entropy"]:
            first_sos_seen = True
    out += b"\xff\xd9"

    if randomize and header_marker_positions:
        # Bytes de preenchimento 0xFF antes de marcadores são permitidos pela
        # norma JPEG (ITU T.81 B.1.1.2) e ignorados por todo decodificador.
        positions = [p for p in header_marker_positions if p > 2]
        if positions:
            chosen = random.sample(positions, k=random.randint(1, len(positions)))
            for p in sorted(chosen, reverse=True):
                out[p:p] = b"\xff" * random.randint(1, 12)
    return bytes(out), removed, notes


def audit_jpeg(data: bytes) -> list[str]:
    parsed = _jpeg_parse(data)
    found = [s["name"] for s in parsed["segments"] if s["is_meta"]]
    if parsed["trailing"]:
        found.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")
    return found


# ─────────────────────────────────────────────────────────
# PNG — limpeza sem recodificar
# ─────────────────────────────────────────────────────────

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = {
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT",
    b"bKGD", b"pHYs", b"hIST", b"sPLT", b"acTL", b"fcTL", b"fdAT", b"cICP", b"mDCV", b"cLLI",
}


def _png_chunk(ctype: bytes, cdata: bytes) -> bytes:
    return struct.pack(">I", len(cdata)) + ctype + cdata + struct.pack(">I", zlib.crc32(ctype + cdata) & 0xFFFFFFFF)


def _png_parse(data: bytes) -> dict:
    if data[:8] != _PNG_SIG:
        raise ValueError("não é um arquivo PNG válido")
    chunks = []
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if pos + 12 + length > n:
            raise ValueError("chunk PNG truncado")
        cdata = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        chunks.append((ctype, cdata))
        if ctype == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR":
        raise ValueError("PNG sem IHDR")
    return {"chunks": chunks, "trailing": n - pos}


def _png_chunk_name(ctype: bytes, cdata: bytes) -> str:
    if ctype in (b"tEXt", b"zTXt", b"iTXt"):
        key = cdata.split(b"\x00", 1)[0][:40]
        try:
            k = key.decode("latin-1")
        except Exception:
            k = "?"
        if k == "XML:com.adobe.xmp":
            return "XMP"
        return f"texto ({k})"
    if ctype == b"eXIf":
        return _exif_label(cdata)
    if ctype == b"tIME":
        return "data de modificação"
    if ctype == b"iCCP":
        return "perfil ICC"
    return f"chunk {ctype.decode('latin-1')}"


def strip_png(data: bytes, keep_icc: bool = True, randomize: bool = True) -> tuple[bytes, list[str], list[str]]:
    parsed = _png_parse(data)
    removed: list[str] = []
    notes: list[str] = []
    orientation = 1
    kept: list[tuple[bytes, bytes]] = []
    for ctype, cdata in parsed["chunks"]:
        if ctype in _PNG_KEEP:
            kept.append((ctype, cdata))
        elif ctype == b"iCCP" and keep_icc:
            kept.append((ctype, cdata))
        elif ctype[0:1].isupper():  # chunk crítico desconhecido: manter
            kept.append((ctype, cdata))
        else:
            if ctype == b"eXIf":
                orientation = _exif_scan(cdata)["orientation"]
            removed.append(_png_chunk_name(ctype, cdata))
    if parsed["trailing"]:
        removed.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")

    out = bytearray(_PNG_SIG)
    idat_total = b"".join(c for t, c in kept if t == b"IDAT")
    idat_written = False
    for ctype, cdata in kept:
        if ctype == b"IDAT":
            if idat_written:
                continue
            idat_written = True
            pieces = [idat_total]
            if randomize and len(idat_total) >= 2:
                # Dividir o IDAT em vários chunks é permitido pela norma PNG
                # (os decodificadores concatenam) e não altera nenhum pixel.
                k = random.randint(2, min(4, len(idat_total)))
                cuts = sorted(random.sample(range(1, len(idat_total)), k - 1))
                pieces = [idat_total[i:j] for i, j in zip([0] + cuts, cuts + [len(idat_total)])]
            for piece in pieces:
                out += _png_chunk(b"IDAT", piece)
            continue
        if ctype == b"IEND" and orientation != 1:
            out += _png_chunk(b"eXIf", _minimal_exif_tiff(orientation))
            notes.append(f"orientação EXIF {orientation} preservada (tag única)")
        out += _png_chunk(ctype, cdata)
    return bytes(out), removed, notes


def audit_png(data: bytes) -> list[str]:
    parsed = _png_parse(data)
    found = []
    for ctype, cdata in parsed["chunks"]:
        if ctype in _PNG_KEEP or ctype[0:1].isupper():
            continue
        found.append(_png_chunk_name(ctype, cdata))
    if parsed["trailing"]:
        found.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")
    return found


# ─────────────────────────────────────────────────────────
# WebP — limpeza sem recodificar
# ─────────────────────────────────────────────────────────

def _webp_parse(data: bytes) -> dict:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("não é um arquivo WebP válido")
    riff_size = struct.unpack("<I", data[4:8])[0]
    end = min(len(data), 8 + riff_size)
    chunks = []
    pos = 12
    while pos + 8 <= end:
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if pos + 8 + size > end:
            raise ValueError("chunk WebP truncado")
        payload = data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
        chunks.append((fourcc, payload))
    return {"chunks": chunks, "trailing": max(0, len(data) - pos)}


def _webp_canvas(chunks: list) -> tuple[int, int, bool]:
    """Largura, altura e presença de alpha a partir do bitstream (formato simples)."""
    for fourcc, payload in chunks:
        if fourcc == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = struct.unpack("<I", payload[1:5])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            alpha = bool((bits >> 28) & 1)
            return w, h, alpha
        if fourcc == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            w = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
            h = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
            return w, h, False
    raise ValueError("WebP sem bitstream de imagem reconhecível")


def _webp_chunk_name(fourcc: bytes, payload: bytes) -> str:
    if fourcc == b"EXIF":
        tiff = payload[6:] if payload.startswith(b"Exif\x00\x00") else payload
        return _exif_label(tiff)
    if fourcc == b"XMP ":
        return "XMP"
    if fourcc == b"ICCP":
        return "perfil ICC"
    return f"chunk {fourcc.decode('latin-1').strip()}"


_WEBP_IMAGE_CHUNKS = {b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF"}


def strip_webp(data: bytes, keep_icc: bool = True, randomize: bool = True) -> tuple[bytes, list[str], list[str]]:
    parsed = _webp_parse(data)
    chunks = parsed["chunks"]
    removed: list[str] = []
    notes: list[str] = []
    orientation = 1
    vp8x = None
    kept: list[tuple[bytes, bytes]] = []
    icc = None
    for fourcc, payload in chunks:
        if fourcc == b"VP8X":
            vp8x = payload
        elif fourcc in _WEBP_IMAGE_CHUNKS:
            kept.append((fourcc, payload))
        elif fourcc == b"ICCP":
            if keep_icc:
                icc = payload
            else:
                removed.append("perfil ICC")
        elif fourcc == b"EXIF":
            tiff = payload[6:] if payload.startswith(b"Exif\x00\x00") else payload
            orientation = _exif_scan(tiff)["orientation"]
            removed.append(_webp_chunk_name(fourcc, payload))
        else:
            removed.append(_webp_chunk_name(fourcc, payload))
    if parsed["trailing"]:
        removed.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")
    if not any(f in (b"VP8 ", b"VP8L", b"ANMF") for f, _ in kept):
        raise ValueError("WebP sem dados de imagem")

    if vp8x is not None and len(vp8x) >= 10:
        flags = vp8x[0]
        w = int.from_bytes(vp8x[4:7], "little") + 1
        h = int.from_bytes(vp8x[7:10], "little") + 1
    else:
        w, h, alpha = _webp_canvas(kept)
        flags = 0x10 if alpha else 0
    flags &= ~(0x08 | 0x04)  # limpa bits EXIF e XMP
    if icc is None:
        flags &= ~0x20
    else:
        flags |= 0x20

    body = bytearray()

    def add(fourcc: bytes, payload: bytes):
        body.extend(fourcc)
        body.extend(struct.pack("<I", len(payload)))
        body.extend(payload)
        if len(payload) & 1:
            body.append(0)

    exif_payload = None
    if orientation != 1:
        exif_payload = _minimal_exif_tiff(orientation)
        flags |= 0x08
        notes.append(f"orientação EXIF {orientation} preservada (tag única)")

    add(b"VP8X", bytes([flags, 0, 0, 0]) + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))
    if icc is not None:
        add(b"ICCP", icc)
    for fourcc, payload in kept:
        add(fourcc, payload)
    if exif_payload is not None:
        add(b"EXIF", exif_payload)
    if randomize:
        # Chunks desconhecidos são permitidos pela especificação WebP
        # (formato estendido) e ignorados pelos decodificadores.
        add(b"JUNK", os.urandom(random.randint(8, 96)))
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + bytes(body), removed, notes


def audit_webp(data: bytes) -> list[str]:
    parsed = _webp_parse(data)
    found = []
    for fourcc, payload in parsed["chunks"]:
        if fourcc in _WEBP_IMAGE_CHUNKS or fourcc in (b"VP8X", b"JUNK"):
            continue
        found.append(_webp_chunk_name(fourcc, payload))
    if parsed["trailing"]:
        found.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")
    return found


# ─────────────────────────────────────────────────────────
# GIF — limpeza sem recodificar
# ─────────────────────────────────────────────────────────

def _gif_subblocks(data: bytes, pos: int) -> tuple[list[bytes], int]:
    blocks = []
    n = len(data)
    while True:
        if pos >= n:
            raise ValueError("GIF truncado")
        sz = data[pos]
        pos += 1
        if sz == 0:
            return blocks, pos
        if pos + sz > n:
            raise ValueError("GIF truncado")
        blocks.append(data[pos:pos + sz])
        pos += sz


def _gif_parse(data: bytes) -> dict:
    """Retorna lista de blocos: (kind, raw, extra). kind ∈ header|ext|image|trailer."""
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise ValueError("não é um arquivo GIF válido")
    n = len(data)
    pos = 13
    flags = data[10]
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 7) + 1))
    blocks = [("header", data[:pos], None)]
    trailing = 0
    while pos < n:
        b = data[pos]
        if b == 0x3B:
            blocks.append(("trailer", b"\x3b", None))
            pos += 1
            trailing = n - pos
            break
        if b == 0x21:
            if pos + 2 > n:
                raise ValueError("GIF truncado")
            label = data[pos + 1]
            sub, newpos = _gif_subblocks(data, pos + 2)
            blocks.append(("ext", data[pos:newpos], (label, sub)))
            pos = newpos
        elif b == 0x2C:
            if pos + 10 > n:
                raise ValueError("GIF truncado")
            desc = data[pos:pos + 10]
            p = pos + 10
            lflags = desc[9]
            if lflags & 0x80:
                p += 3 * (2 ** ((lflags & 7) + 1))
            head = data[pos:p + 1]  # descritor + tabela local + LZW min code size
            p += 1
            sub, newpos = _gif_subblocks(data, p)
            blocks.append(("image", head, b"".join(sub)))
            pos = newpos
        else:
            raise ValueError(f"bloco GIF desconhecido (0x{b:02X})")
    return {"blocks": blocks, "trailing": trailing}


def _gif_ext_name(label: int, sub: list[bytes]) -> tuple[str, bool]:
    """(nome, é_metadado)"""
    if label == 0xF9:
        return "controle gráfico", False
    if label == 0x01:
        return "texto simples", False
    if label == 0xFE:
        return "comentário", True
    if label == 0xFF:
        app = sub[0][:11] if sub else b""
        if app in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
            return "loop de animação", False
        if app == b"XMP DataXMP":
            return "XMP", True
        if app == b"ICCRGBG1012":
            return "perfil ICC", True
        return f"extensão de aplicação ({app.decode('latin-1', 'replace').strip()})", True
    return f"extensão 0x{label:02X}", True


def _gif_resplit(payload: bytes, randomize: bool) -> bytes:
    out = bytearray()
    pos = 0
    n = len(payload)
    while pos < n:
        sz = min(255, n - pos)
        if randomize and sz == 255 and n - pos > 255:
            sz = random.randint(160, 255)
        out.append(sz)
        out += payload[pos:pos + sz]
        pos += sz
    out.append(0)
    return bytes(out)


def strip_gif(data: bytes, keep_icc: bool = True, randomize: bool = True) -> tuple[bytes, list[str], list[str]]:
    parsed = _gif_parse(data)
    removed: list[str] = []
    notes: list[str] = []
    out = bytearray()
    for kind, raw, extra in parsed["blocks"]:
        if kind == "ext":
            label, sub = extra
            name, is_meta = _gif_ext_name(label, sub)
            if is_meta and not (name == "perfil ICC" and keep_icc):
                removed.append(name)
                continue
            out += raw
        elif kind == "image":
            out += raw
            out += _gif_resplit(extra, randomize)
        else:
            out += raw
    if parsed["trailing"]:
        removed.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")
    if not out.endswith(b"\x3b"):
        out.append(0x3B)
    return bytes(out), removed, notes


def audit_gif(data: bytes) -> list[str]:
    parsed = _gif_parse(data)
    found = []
    for kind, raw, extra in parsed["blocks"]:
        if kind == "ext":
            name, is_meta = _gif_ext_name(*extra)
            if is_meta:
                found.append(name)
    if parsed["trailing"]:
        found.append(f"dados após o fim da imagem ({human_size(parsed['trailing'])})")
    return found


# ─────────────────────────────────────────────────────────
# Despacho de imagens
# ─────────────────────────────────────────────────────────

_IMAGE_STRIPPERS = {
    ".jpg": strip_jpeg, ".jpeg": strip_jpeg, ".jpe": strip_jpeg, ".jfif": strip_jpeg,
    ".png": strip_png, ".webp": strip_webp, ".gif": strip_gif,
}
_IMAGE_AUDITORS = {
    ".jpg": audit_jpeg, ".jpeg": audit_jpeg, ".jpe": audit_jpeg, ".jfif": audit_jpeg,
    ".png": audit_png, ".webp": audit_webp, ".gif": audit_gif,
}


def _sniff_image(data: bytes) -> Optional[str]:
    """Detecta o formato real pelo conteúdo (extensão pode mentir)."""
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:8] == _PNG_SIG:
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def _convert_image(src: str, dst: str, target_ext: str, quality: int) -> str:
    """Converte HEIC/RAW/TIFF/BMP para JPEG ou PNG usando sips (macOS) ou ffmpeg."""
    errors = []
    sips = find_sips()
    if sips:
        fmt = "jpeg" if target_ext == ".jpg" else "png"
        cmd = [sips, "-s", "format", fmt]
        if fmt == "jpeg":
            cmd += ["-s", "formatOptions", str(quality)]
        cmd += [src, "--out", dst]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return "sips"
        errors.append("sips: " + (proc.stderr or proc.stdout).strip()[-200:])
    ff = find_ffmpeg()
    if ff:
        cmd = [ff, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src,
               "-map_metadata", "-1", "-frames:v", "1", "-fflags", "+bitexact", "-flags", "+bitexact"]
        if target_ext == ".jpg":
            q = max(1, min(31, round(31 - (quality / 100) * 30)))
            cmd += ["-q:v", str(q), "-pix_fmt", "yuvj420p"]
        cmd += [dst]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return "ffmpeg"
        errors.append("ffmpeg: " + proc.stderr.strip()[-200:])
    raise RuntimeError("conversão falhou — " + " | ".join(errors) if errors else "nenhum conversor disponível")


def process_image(input_path: str, output_path: str, options: Options, result: FileResult) -> str:
    """Processa uma imagem. Retorna o caminho final de saída (a extensão pode mudar)."""
    ext = Path(input_path).suffix.lower()
    data = _read_file(input_path)
    sniffed = _sniff_image(data)
    work_ext = ext
    if ext in IMAGE_LOSSLESS_EXTENSIONS:
        if sniffed and sniffed != {".jpeg": ".jpg", ".jpe": ".jpg", ".jfif": ".jpg"}.get(ext, ext):
            result.notes.append(f"conteúdo real é {sniffed[1:].upper()}, extensão corrigida")
            work_ext = sniffed
            output_path = os.path.splitext(output_path)[0] + sniffed
    elif sniffed:
        # ex.: um ".heic" que na verdade é JPEG
        work_ext = sniffed
        output_path = os.path.splitext(output_path)[0] + sniffed
        result.notes.append(f"conteúdo real é {sniffed[1:].upper()}, extensão corrigida")
    else:
        target = ".jpg" if ext in IMAGE_TO_JPEG_EXTENSIONS else ".png"
        output_path = os.path.splitext(output_path)[0] + target
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        tmp = output_path + ".conv.tmp" + target
        try:
            tool = _convert_image(input_path, tmp, target, options.jpeg_quality)
            data = _read_file(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        work_ext = target
        result.method = "convert"
        result.notes.append(f"convertido {ext[1:].upper()} → {target[1:].upper()} via {tool}")
        result.removed.append(f"container {ext[1:].upper()} (metadados do container)")

    stripper = _IMAGE_STRIPPERS[work_ext]
    new_data, removed, notes = stripper(data, keep_icc=options.keep_icc, randomize=True)
    result.removed.extend(removed)
    result.notes.extend(notes)
    if not result.method:
        result.method = "lossless"
    output_path = _uniquify(output_path)
    _write_file(output_path, new_data)

    # Verificação: reanalisa o arquivo gravado
    auditor = _IMAGE_AUDITORS[work_ext]
    remaining = auditor(new_data)
    suspicious = [item for item in remaining if item not in BENIGN_IMAGE_ITEMS]
    benign = [item for item in remaining if item in BENIGN_IMAGE_ITEMS]
    result.remaining.extend(suspicious + benign)
    result.clean = not suspicious
    return output_path


# ─────────────────────────────────────────────────────────
# Padding válido para alterar o hash de vídeo/áudio
# ─────────────────────────────────────────────────────────

def _pad_mp4(path: str) -> str:
    n = random.randint(16, 256)
    with open(path, "ab") as f:
        f.write(struct.pack(">I", 8 + n) + b"free" + os.urandom(n))
    return "caixa 'free' (ISO BMFF)"


def _ebml_vint(f) -> tuple[int, int, bool]:
    """Lê um vint EBML na posição atual. Retorna (valor, nº de bytes, tamanho_desconhecido)."""
    first = f.read(1)
    if not first:
        raise ValueError("EBML truncado")
    b = first[0]
    length = 1
    mask = 0x80
    while length <= 8 and not (b & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("vint EBML inválido")
    value = b & (mask - 1)
    rest = f.read(length - 1)
    for byte in rest:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown


def _pad_matroska(path: str) -> Optional[str]:
    """Acrescenta um elemento Void no FIM do Segment (dentro dele), ajustando o tamanho do Segment."""
    n = random.randint(16, 100)
    void = b"\xec" + bytes([0x80 | n]) + os.urandom(n)
    with open(path, "r+b") as f:
        if f.read(4) != b"\x1a\x45\xdf\xa3":
            raise ValueError("Matroska/WebM inválido")
        hdr_size, _, _ = _ebml_vint(f)
        f.seek(hdr_size, os.SEEK_CUR)
        if f.read(4) != b"\x18\x53\x80\x67":
            raise ValueError("Segment Matroska não encontrado")
        size_pos = f.tell()
        seg_size, length, unknown = _ebml_vint(f)
        file_size = os.path.getsize(path)
        if not unknown:
            if size_pos + length + seg_size != file_size:
                return None  # há dados após o Segment; não mexer
            new_size = seg_size + len(void)
            if new_size >= (1 << (7 * length)) - 1:
                return None
            encoded = new_size.to_bytes(length, "big")
            encoded = bytes([encoded[0] | (0x80 >> (length - 1))]) + encoded[1:]
            f.seek(size_pos)
            f.write(encoded)
        f.seek(0, os.SEEK_END)
        f.write(void)
    return "elemento EBML Void"


def _pad_riff(path: str) -> str:
    n = random.randint(16, 256)
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        # localiza o último chunk RIFF de nível superior (AVI > 1 GB tem vários)
        pos = 0
        last_riff = None
        while pos + 8 <= size:
            f.seek(pos)
            hdr = f.read(8)
            cid = hdr[:4]
            csize = struct.unpack("<I", hdr[4:8])[0]
            if cid == b"RIFF":
                last_riff = (pos, csize)
            pos += 8 + csize + (csize & 1)
        if last_riff is None:
            raise ValueError("RIFF inválido")
        f.seek(0, os.SEEK_END)
        f.write(b"JUNK" + struct.pack("<I", n) + os.urandom(n) + (b"\x00" if n & 1 else b""))
        rpos, rsize = last_riff
        f.seek(rpos + 4)
        f.write(struct.pack("<I", rsize + 8 + n + (n & 1)))
    return "chunk JUNK (RIFF)"


def _pad_flac(path: str) -> str:
    """Divide o bloco PADDING existente em dois de tamanhos aleatórios (in-place)."""
    with open(path, "r+b") as f:
        if f.read(4) != b"fLaC":
            raise ValueError("FLAC inválido")
        pos = 4
        while True:
            f.seek(pos)
            hdr = f.read(4)
            if len(hdr) < 4:
                raise ValueError("FLAC sem bloco PADDING")
            last = hdr[0] & 0x80
            btype = hdr[0] & 0x7F
            blen = int.from_bytes(hdr[1:4], "big")
            if btype == 1 and blen >= 16:
                split = random.randint(4, blen - 8)
                second = blen - 4 - split
                f.seek(pos)
                f.write(bytes([0x01]) + split.to_bytes(3, "big"))
                f.seek(pos + 4 + split)
                f.write(bytes([0x01 | last]) + second.to_bytes(3, "big"))
                return "bloco PADDING (FLAC)"
            if last:
                raise ValueError("FLAC sem bloco PADDING")
            pos += 4 + blen


def _pad_mp3(path: str) -> str:
    """Insere/redimensiona um tag ID3v2 contendo apenas padding (zeros)."""
    n = random.randint(64, 512)
    with open(path, "rb") as f:
        head = f.read(10)
    header = b"ID3\x04\x00\x00" + bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    tmp = path + ".pad.tmp"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        skip = 0
        if head[:3] == b"ID3" and len(head) == 10:
            old = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            skip = 10 + old + (10 if head[5] & 0x10 else 0)
        src.seek(skip)
        dst.write(header + b"\x00" * n)
        shutil.copyfileobj(src, dst, 1024 * 1024)
    os.replace(tmp, path)
    return "padding ID3v2 (vazio)"


def apply_padding(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    if ext in MP4_FAMILY:
        return _pad_mp4(path)
    if ext in MATROSKA_FAMILY:
        return _pad_matroska(path)
    if ext in RIFF_FAMILY:
        return _pad_riff(path)
    if ext == ".flac":
        return _pad_flac(path)
    if ext == ".mp3":
        return _pad_mp3(path)
    return None


# ─────────────────────────────────────────────────────────
# ffprobe: duração e verificação de tags
# ─────────────────────────────────────────────────────────

def ffprobe_json(path: str) -> Optional[dict]:
    fp = find_ffprobe()
    if not fp:
        return None
    try:
        out = subprocess.run(
            [fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", path],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def media_duration(path: str) -> float:
    info = ffprobe_json(path)
    try:
        return float(info["format"]["duration"])
    except Exception:
        return 0.0


_BENIGN_FORMAT_TAGS = {"major_brand", "minor_version", "compatible_brands"}
_BENIGN_STREAM_TAGS = {"language", "vendor_id", "duration"}
_GENERIC_HANDLERS = {"videohandler", "soundhandler", "subtitlehandler", "datahandler", "timecodehandler",
                     "core media video", "core media audio", "core media data", "core media metadata"}


def audit_media(path: str) -> tuple[list[str], list[str]]:
    """Retorna (tags pessoais/suspeitas restantes, tags técnicas benignas)."""
    info = ffprobe_json(path)
    if info is None:
        return [], ["ffprobe indisponível — verificação de vídeo/áudio pulada"]
    suspicious: list[str] = []
    benign: list[str] = []
    for k, v in info.get("format", {}).get("tags", {}).items():
        kl = k.lower()
        if kl in _BENIGN_FORMAT_TAGS:
            benign.append(f"{k}={v}")
        elif kl == "encoder" and _generic_encoder(v):
            benign.append(f"{k}={v} (genérico)")
        else:
            suspicious.append(f"{k}={str(v)[:60]}")
    for s in info.get("streams", []):
        for k, v in s.get("tags", {}).items():
            kl = k.lower()
            if kl in _BENIGN_STREAM_TAGS:
                benign.append(f"{k}={v}")
            elif kl == "handler_name" and str(v).strip().lower() in _GENERIC_HANDLERS:
                benign.append(f"{k}={v}")
            elif kl == "encoder" and _generic_encoder(v):
                benign.append(f"{k}={v} (genérico)")
            else:
                suspicious.append(f"stream {s.get('index', '?')}: {k}={str(v)[:60]}")
    if info.get("chapters"):
        suspicious.append(f"{len(info['chapters'])} capítulo(s)")
    for s in info.get("streams", []):
        if s.get("codec_type") == "data":
            suspicious.append(f"stream de dados ({s.get('codec_tag_string', '?')})")
    return suspicious, benign


def _generic_encoder(value) -> bool:
    """'Lavf' / 'Lavc libx264' sem versão: só diz qual biblioteca gravou o stream."""
    v = str(value).strip()
    return bool(re.fullmatch(r"Lav[cf]( [A-Za-z0-9_\-]+)?", v))


def describe_media_metadata(path: str) -> list[str]:
    """Lista legível dos metadados presentes em um vídeo/áudio (para inspeção)."""
    info = ffprobe_json(path)
    if info is None:
        return ["ffprobe indisponível"]
    found = []
    for k, v in info.get("format", {}).get("tags", {}).items():
        if k.lower() not in _BENIGN_FORMAT_TAGS:
            found.append(f"{k}: {str(v)[:80]}")
    for s in info.get("streams", []):
        st = s.get("codec_type", "?")
        if st == "data":
            found.append(f"stream de dados embutido ({s.get('codec_tag_string', '?').strip()}: "
                         "capítulos, sensores, GPS ou rostos)")
        for k, v in s.get("tags", {}).items():
            if k.lower() in _BENIGN_STREAM_TAGS:
                continue
            if k.lower() == "handler_name" and str(v).strip().lower() in _GENERIC_HANDLERS:
                continue
            found.append(f"[{st}] {k}: {str(v)[:80]}")
    if info.get("chapters"):
        found.append(f"{len(info['chapters'])} capítulo(s)")
    return found


# ─────────────────────────────────────────────────────────
# Vídeo / áudio via ffmpeg
# ─────────────────────────────────────────────────────────

def _ffmpeg_cmd(ffmpeg: str, inp: str, out: str, kind: str, ext: str, mode: str, subs: bool) -> list[str]:
    cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", inp,
           "-map_metadata", "-1", "-map_chapters", "-1",
           "-fflags", "+bitexact", "-flags", "+bitexact", "-dn"]
    if kind == "video":
        cmd += ["-map", "0:V", "-map", "0:a?"]
        cmd += ["-map", "0:s?"] if subs else ["-sn"]
    else:
        cmd += ["-map", "0:a", "-vn", "-sn"]
    if mode == "copy":
        cmd += ["-c", "copy"]
    elif kind == "video":
        if ext == ".webm":
            cmd += ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1",
                    "-c:a", "libopus", "-b:a", "128k"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k",
                    "-bsf:v", "filter_units=remove_types=6"]  # remove SEI com assinatura do x264
        if subs:
            cmd += ["-c:s", "copy"]
    else:
        codec = {".mp3": ["libmp3lame", "-q:a", "2"], ".flac": ["flac"], ".wav": ["pcm_s16le"],
                 ".aif": ["pcm_s16be"], ".aiff": ["pcm_s16be"], ".ogg": ["libvorbis", "-q:a", "6"],
                 ".oga": ["libvorbis", "-q:a", "6"], ".opus": ["libopus", "-b:a", "128k"],
                 ".wma": ["wmav2", "-b:a", "192k"]}.get(ext, ["aac", "-b:a", "192k"])
        cmd += ["-c:a"] + codec
    if ext in MP4_FAMILY:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out]
    return cmd


def _run_ffmpeg(cmd: list[str], duration: float, progress_cb: Optional[Callable[[float], None]],
                cancel: Optional[threading.Event]) -> tuple[int, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", bufsize=1)
    stderr_lines: list[str] = []

    def drain():
        for line in proc.stderr:
            stderr_lines.append(line)

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled()
            if progress_cb and duration > 0 and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1].strip())
                    progress_cb(min(1.0, max(0.0, us / 1_000_000 / duration)))
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
        t.join(timeout=2)
    return proc.returncode, "".join(stderr_lines)


def process_media(input_path: str, output_path: str, options: Options, result: FileResult,
                  progress_cb: Optional[Callable[[float], None]] = None,
                  cancel: Optional[threading.Event] = None) -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg não encontrado")
    kind = result.kind
    ext = Path(input_path).suffix.lower()
    output_path = _uniquify(output_path)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    duration = media_duration(input_path)

    before = describe_media_metadata(input_path)
    result.removed.extend(before)

    attempts = [("copy", True), ("copy", False)]
    if options.reencode_fallback:
        attempts.append(("reencode", False))
    errors = []
    for mode, subs in attempts:
        if kind != "video" and not subs:
            continue  # áudio: só uma tentativa de cópia
        cmd = _ffmpeg_cmd(ffmpeg, input_path, output_path, kind, ext, mode, subs)
        rc, err = _run_ffmpeg(cmd, duration, progress_cb, cancel)
        if rc == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            result.method = "remux" if mode == "copy" else "reencode"
            if mode == "copy" and not subs and kind == "video":
                result.notes.append("legendas descartadas (incompatíveis com a cópia direta)")
            if mode == "reencode":
                result.notes.append("recodificado (cópia direta dos streams não foi possível)")
            break
        errors.append(f"{mode}{'' if subs else ' sem legendas'}: {err.strip()[-300:]}")
        if os.path.exists(output_path):
            os.remove(output_path)
    else:
        raise RuntimeError("FFmpeg falhou — " + " || ".join(errors))

    pad = apply_padding(output_path)
    if pad:
        result.notes.append(f"hash alterado via {pad}")
    suspicious, benign = audit_media(output_path)
    result.remaining.extend(suspicious + benign)
    result.clean = not suspicious
    return output_path


# ─────────────────────────────────────────────────────────
# Processamento de um arquivo
# ─────────────────────────────────────────────────────────

def process_file(input_path: str, output_path: str, options: Optional[Options] = None,
                 progress_cb: Optional[Callable[[float], None]] = None,
                 cancel: Optional[threading.Event] = None) -> FileResult:
    options = options or Options()
    result = FileResult(input=input_path, output=output_path, kind=get_file_type(input_path))
    start = time.time()
    try:
        if result.kind == "unknown":
            raise ValueError(f"tipo não suportado: {Path(input_path).suffix}")
        result.hash_before, result.md5_before = file_hashes(input_path)
        result.size_before = os.path.getsize(input_path)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if result.kind == "image":
            final = process_image(input_path, output_path, options, result)
        else:
            final = process_media(input_path, output_path, options, result, progress_cb, cancel)
        result.output = final
        result.hash_after, result.md5_after = file_hashes(final)
        result.size_after = os.path.getsize(final)
        if result.hash_after == result.hash_before:
            # Praticamente impossível, mas garante o contrato "hash diferente".
            pad = apply_padding(final)
            if pad:
                result.hash_after, result.md5_after = file_hashes(final)
                result.size_after = os.path.getsize(final)
            if result.hash_after == result.hash_before:
                result.notes.append("AVISO: hash idêntico ao original (formato sem padding disponível)")
        result.success = True
    except Cancelled:
        result.error = "cancelado"
        if result.output and os.path.exists(result.output) and result.output != input_path:
            try:
                os.remove(result.output)
            except OSError:
                pass
    except subprocess.TimeoutExpired:
        result.error = "tempo esgotado"
    except Exception as e:  # noqa: BLE001
        result.error = str(e)[:400]
    result.seconds = round(time.time() - start, 2)
    return result


# ─────────────────────────────────────────────────────────
# Lote + relatório
# ─────────────────────────────────────────────────────────

def make_output_dir(base_dir: str) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out = os.path.join(base_dir, f"{OUTPUT_PREFIX}{timestamp}")
    os.makedirs(out, exist_ok=True)
    return out


def plan_outputs(inputs: list[str], files: list[str], output_dir: str) -> list[tuple[str, str]]:
    """Define o caminho de saída de cada arquivo, preservando subpastas."""
    inputs_abs = [os.path.abspath(i) for i in inputs]
    dirs = [i for i in inputs_abs if os.path.isdir(i)]
    pairs = []
    for f in files:
        rel = os.path.basename(f)
        for d in dirs:
            if f.startswith(d + os.sep):
                inner = os.path.relpath(f, d)
                rel = os.path.join(os.path.basename(d), inner) if len(dirs) > 1 else inner
                break
        pairs.append((f, os.path.join(output_dir, rel)))
    return pairs


def process_batch(inputs: list[str], output_base: str, options: Optional[Options] = None,
                  on_start: Optional[Callable[[int, int, str], None]] = None,
                  on_progress: Optional[Callable[[int, float], None]] = None,
                  on_done: Optional[Callable[[int, FileResult], None]] = None,
                  cancel: Optional[threading.Event] = None) -> tuple[str, list[FileResult]]:
    options = options or Options()
    files = collect_files(inputs)
    if not files:
        raise ValueError("nenhum arquivo suportado encontrado")
    output_dir = make_output_dir(output_base)
    results: list[FileResult] = []
    pairs = plan_outputs(inputs, files, output_dir)
    for i, (src, dst) in enumerate(pairs):
        if cancel is not None and cancel.is_set():
            break
        if on_start:
            on_start(i, len(pairs), src)
        r = process_file(src, dst, options,
                         progress_cb=(lambda p, i=i: on_progress(i, p)) if on_progress else None,
                         cancel=cancel)
        results.append(r)
        if on_done:
            on_done(i, r)
        if r.error == "cancelado":
            break
    save_report(output_dir, results)
    return output_dir, results


def save_report(output_dir: str, results: list[FileResult]) -> tuple[str, str]:
    report = {
        "ferramenta": f"Removedor de Metadados & Hash v{__version__}",
        "data_processamento": datetime.now().isoformat(timespec="seconds"),
        "total_arquivos": len(results),
        "sucesso": sum(1 for r in results if r.success),
        "falhas": sum(1 for r in results if not r.success),
        "arquivos": [],
    }
    for r in results:
        entry = {
            "arquivo_original": os.path.basename(r.input),
            "arquivo_saida": os.path.basename(r.output) if r.output else None,
            "tipo": r.kind,
            "metodo": r.method,
            "sucesso": r.success,
            "verificado_limpo": r.clean,
            "sha256_original": r.hash_before,
            "sha256_novo": r.hash_after or None,
            "md5_original": r.md5_before,
            "md5_novo": r.md5_after or None,
            "tamanho_original": human_size(r.size_before),
            "tamanho_novo": human_size(r.size_after) if r.size_after else None,
            "metadados_removidos": r.removed,
            "tags_tecnicas_restantes": r.remaining,
            "observacoes": r.notes,
            "segundos": r.seconds,
        }
        if r.error:
            entry["erro"] = r.error
        report["arquivos"].append(entry)

    json_path = os.path.join(output_dir, "relatorio.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    txt_path = os.path.join(output_dir, "relatorio.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 64 + "\n")
        f.write("  RELATÓRIO DE PROCESSAMENTO\n")
        f.write(f"  {report['data_processamento']}  ·  {report['ferramenta']}\n")
        f.write("=" * 64 + "\n\n")
        f.write(f"  Total: {report['total_arquivos']}  |  OK: {report['sucesso']}  |  Falhas: {report['falhas']}\n")
        f.write("-" * 64 + "\n")
        for e in report["arquivos"]:
            s = "✓" if e["sucesso"] else "✗"
            f.write(f"\n  [{s}] {e['arquivo_original']}")
            if e["arquivo_saida"] and e["arquivo_saida"] != e["arquivo_original"]:
                f.write(f"  →  {e['arquivo_saida']}")
            f.write("\n")
            if e["sucesso"]:
                f.write(f"      Método: {e['metodo']}  ·  Verificação: {'limpo' if e['verificado_limpo'] else 'ATENÇÃO'}\n")
                f.write(f"      SHA-256: {e['sha256_original']}\n")
                f.write(f"            → {e['sha256_novo']}\n")
                f.write(f"      Tamanho: {e['tamanho_original']} → {e['tamanho_novo']}\n")
                if e["metadados_removidos"]:
                    f.write(f"      Removido: {', '.join(e['metadados_removidos'])}\n")
                if e["tags_tecnicas_restantes"]:
                    f.write(f"      Restante (técnico): {', '.join(e['tags_tecnicas_restantes'])}\n")
                for n in e["observacoes"]:
                    f.write(f"      Obs: {n}\n")
            else:
                f.write(f"      Erro: {e.get('erro')}\n")
        f.write("\n" + "=" * 64 + "\n")
    return json_path, txt_path


# ─────────────────────────────────────────────────────────
# Inspeção (sem modificar nada)
# ─────────────────────────────────────────────────────────

def inspect_file(path: str) -> dict:
    """Lista os metadados presentes em um arquivo, sem alterá-lo."""
    kind = get_file_type(path)
    info = {"arquivo": path, "tipo": kind, "tamanho": human_size(os.path.getsize(path)),
            "sha256": calculate_hash(path), "metadados": [], "tecnicos": [], "erro": None}
    try:
        if kind == "image":
            data = _read_file(path)
            sniffed = _sniff_image(data)
            if sniffed and sniffed in _IMAGE_AUDITORS:
                found = _IMAGE_AUDITORS[sniffed](data)
                info["metadados"] = [f for f in found if f not in BENIGN_IMAGE_ITEMS]
                info["tecnicos"] = [f for f in found if f in BENIGN_IMAGE_ITEMS]
            else:
                info["metadados"] = describe_media_metadata(path)
                info["tecnicos"].append("formato de container (HEIC/RAW/TIFF): o EXIF interno "
                                        "é removido na conversão para JPEG/PNG")
        elif kind in ("video", "audio"):
            suspicious, benign = audit_media(path)
            info["metadados"] = describe_media_metadata(path)
            info["tecnicos"] = benign
        else:
            info["erro"] = "tipo não suportado"
    except Exception as e:  # noqa: BLE001
        info["erro"] = str(e)
    return info


def result_to_dict(r: FileResult) -> dict:
    return asdict(r)
