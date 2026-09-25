#!/usr/bin/env python3
"""
Testes do motor de limpeza.

Rodar:  python3 -m unittest discover -s tests -v

Fixtures são geradas na hora. Testes de imagem com EXIF usam Pillow se
disponível (pip install pillow) e são pulados caso contrário; testes de
vídeo/áudio usam o ffmpeg do sistema e são pulados se ele faltar.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import remove_hash_core as core  # noqa: E402

try:
    from PIL import Image, ImageChops, ImageOps
    from PIL.ExifTags import Base as ExifBase, GPS
    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

FFMPEG = core.find_ffmpeg()
PLANTED = [b"Secreto", b"secreta", b"Patrick", b"iPhone", b"2026:01:02", b"xmpmeta"]


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def sha256(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def planted_in(path):
    data = read_bytes(path)
    return [p.decode() for p in PLANTED if p in data]


def make_exif(orientation=1):
    exif = Image.Exif()
    exif[ExifBase.Make] = "Apple"
    exif[ExifBase.Model] = "iPhone 15 Pro"
    exif[ExifBase.Artist] = "Patrick Teste"
    exif[ExifBase.DateTime] = "2026:01:02 03:04:05"
    exif[ExifBase.ImageDescription] = "descricao secreta"
    exif[ExifBase.Orientation] = orientation
    gps = exif.get_ifd(ExifBase.GPSInfo)
    gps[GPS.GPSLatitudeRef] = "S"
    gps[GPS.GPSLatitude] = (23.0, 33.0, 0.0)
    gps[GPS.GPSLongitudeRef] = "W"
    gps[GPS.GPSLongitude] = (46.0, 38.0, 0.0)
    return exif.tobytes()


XMP = (b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
       b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description>'
       b'<dc:creator>Patrick</dc:creator></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def pixels_identical(a, b):
    ia, ib = Image.open(a), Image.open(b)
    ta, tb = ImageOps.exif_transpose(ia), ImageOps.exif_transpose(ib)
    if ta.size != tb.size:
        return False
    n = getattr(ia, "n_frames", 1)
    if n != getattr(ib, "n_frames", 1):
        return False
    for i in range(n):
        ia.seek(i)
        ib.seek(i)
        if ImageChops.difference(ia.convert("RGBA"), ib.convert("RGBA")).getbbox() is not None:
            return False
    return True


def ffmpeg_strict_ok(path):
    """Decodifica em modo estrito: qualquer aviso/erro reprova."""
    out = subprocess.run([FFMPEG, "-v", "warning", "-xerror", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True)
    stderr = "\n".join(l for l in out.stderr.splitlines() if "Guessed Channel Layout" not in l)
    return out.returncode == 0 and not stderr.strip()


class ImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAS_PIL:
            raise unittest.SkipTest("Pillow não instalado")
        cls.dir = tempfile.mkdtemp(prefix="rh_img_")
        cls.img = Image.effect_mandelbrot((160, 120), (-2.0, -1.5, 1.0, 1.5), 40).convert("RGB")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _run(self, name, **save_kwargs):
        src = os.path.join(self.dir, name)
        ext = os.path.splitext(name)[1].lower()
        img = save_kwargs.pop("img", self.img)
        img.save(src, **save_kwargs)
        self.assertTrue(planted_in(src), f"fixture {name} deveria conter metadados plantados")
        out1 = os.path.join(self.dir, "out1_" + name)
        out2 = os.path.join(self.dir, "out2_" + name)
        r1 = core.process_file(src, out1)
        r2 = core.process_file(src, out2)
        self.assertTrue(r1.success, r1.error)
        self.assertTrue(r1.clean, r1.remaining)
        self.assertEqual(planted_in(r1.output), [])
        self.assertNotEqual(r1.hash_before, r1.hash_after)
        self.assertNotEqual(r1.hash_after, r2.hash_after, "cada execução deve gerar hash diferente")
        self.assertEqual(os.path.splitext(r1.output)[1].lower(), ext)
        self.assertTrue(pixels_identical(src, r1.output), "pixels devem ser idênticos (sem recodificar)")
        return r1

    def test_jpeg(self):
        r = self._run("foto.jpg", quality=85, exif=make_exif(), comment="comentario secreta")
        self.assertIn("EXIF (com GPS)", r.removed)
        self.assertIn("comentário", r.removed)

    def test_jpeg_progressive_with_xmp(self):
        r = self._run("prog.jpg", quality=85, progressive=True, exif=make_exif(), xmp=XMP)
        self.assertIn("XMP", r.removed)

    def test_jpeg_orientation_preserved(self):
        r = self._run("orient.jpg", quality=85, exif=make_exif(orientation=6))
        self.assertEqual(Image.open(r.output).getexif().get(0x0112), 6)
        self.assertIn("EXIF (só orientação)", r.remaining)

    def test_png(self):
        from PIL import PngImagePlugin
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "Patrick")
        info.add_itxt("XML:com.adobe.xmp", XMP.decode())
        r = self._run("foto.png", pnginfo=info, exif=make_exif())
        self.assertIn("XMP", r.removed)
        self.assertIn("EXIF (com GPS)", r.removed)

    def test_webp_lossy_and_lossless(self):
        r = self._run("foto.webp", quality=80, exif=make_exif(), xmp=XMP)
        self.assertIn("XMP", r.removed)
        self._run("lossless.webp", lossless=True, exif=make_exif())

    def test_webp_animated(self):
        frames = [self.img, self.img.rotate(180)]
        src = os.path.join(self.dir, "anim.webp")
        frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0, exif=make_exif())
        r = core.process_file(src, os.path.join(self.dir, "out_anim.webp"))
        self.assertTrue(r.success and r.clean, r.error or r.remaining)
        self.assertEqual(planted_in(r.output), [])
        self.assertTrue(pixels_identical(src, r.output))

    def test_gif_animated_comment(self):
        frames = [self.img.convert("P"), self.img.rotate(180).convert("P")]
        src = os.path.join(self.dir, "anim.gif")
        frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0,
                       comment="comentario secreta Patrick")
        r = core.process_file(src, os.path.join(self.dir, "out_anim.gif"))
        self.assertTrue(r.success and r.clean, r.error or r.remaining)
        self.assertIn("comentário", r.removed)
        self.assertEqual(planted_in(r.output), [])
        self.assertTrue(pixels_identical(src, r.output))
        self.assertEqual(Image.open(r.output).info.get("loop"), 0)

    def test_icc_option(self):
        icc_path = "/System/Library/ColorSync/Profiles/Display P3.icc"
        icc = read_bytes(icc_path) if os.path.exists(icc_path) else None
        if not icc:
            self.skipTest("perfil ICC de teste indisponível")
        src = os.path.join(self.dir, "icc.jpg")
        self.img.save(src, quality=85, icc_profile=icc, exif=make_exif())
        keep = core.process_file(src, os.path.join(self.dir, "keep.jpg"), core.Options(keep_icc=True))
        drop = core.process_file(src, os.path.join(self.dir, "drop.jpg"), core.Options(keep_icc=False))
        self.assertIn("perfil ICC", keep.remaining)
        self.assertIn("perfil ICC", drop.removed)
        self.assertIsNotNone(Image.open(keep.output).info.get("icc_profile"))
        self.assertIsNone(Image.open(drop.output).info.get("icc_profile"))

    def test_trailing_hidden_data_removed(self):
        src = os.path.join(self.dir, "trailer.jpg")
        self.img.save(src, quality=85)
        with open(src, "ab") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42 video escondido Patrick")
        r = core.process_file(src, os.path.join(self.dir, "out_trailer.jpg"))
        self.assertTrue(r.success and r.clean)
        self.assertTrue(any("dados ocultos" in x for x in r.removed), r.removed)
        self.assertEqual(planted_in(r.output), [])

    def test_wrong_extension_is_corrected(self):
        src = os.path.join(self.dir, "na_verdade_png.jpg")
        self.img.save(src, format="PNG")
        r = core.process_file(src, os.path.join(self.dir, "out_wrong.jpg"))
        self.assertTrue(r.success, r.error)
        self.assertTrue(r.output.endswith(".png"))

    @unittest.skipUnless(sys.platform == "darwin", "conversão HEIC usa sips (macOS)")
    def test_heic_converted_to_clean_jpeg(self):
        jpg = os.path.join(self.dir, "src_heic.jpg")
        self.img.save(jpg, quality=90, exif=make_exif(orientation=6))
        heic = os.path.join(self.dir, "foto.heic")
        subprocess.run(["sips", "-s", "format", "heic", jpg, "--out", heic], check=True, capture_output=True)
        self.assertTrue(planted_in(heic))
        r = core.process_file(heic, os.path.join(self.dir, "out_heic.heic"))
        self.assertTrue(r.success and r.clean, r.error or r.remaining)
        self.assertTrue(r.output.endswith(".jpg"))
        self.assertEqual(r.method, "convert")
        self.assertEqual(planted_in(r.output), [])
        self.assertEqual(Image.open(r.output).getexif().get(0x0112), 6)


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FFMPEG:
            raise unittest.SkipTest("ffmpeg não encontrado")
        cls.dir = tempfile.mkdtemp(prefix="rh_media_")
        cls.meta = ["-metadata", "title=Titulo Secreto", "-metadata", "artist=Patrick",
                    "-metadata", "creation_time=2026-01-02T03:04:05Z", "-metadata", "location=-23.55-046.63/",
                    "-metadata", "model=iPhone 15 Pro", "-metadata:s:v", "handler_name=Core Media Secreto"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _make(self, name, video=True, extra=None):
        path = os.path.join(self.dir, name)
        cmd = [FFMPEG, "-y", "-v", "error", "-nostdin"]
        if video:
            cmd += ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=1"]
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        cmd += self.meta if video else self.meta[:6]
        cmd += extra or []
        cmd += [path]
        subprocess.run(cmd, check=True)
        self.assertTrue(planted_in(path), f"fixture {name} deveria conter metadados plantados")
        return path

    def _check(self, src, ext=None):
        out1 = os.path.join(self.dir, "out1_" + os.path.basename(src))
        out2 = os.path.join(self.dir, "out2_" + os.path.basename(src))
        r1 = core.process_file(src, out1)
        r2 = core.process_file(src, out2)
        self.assertTrue(r1.success, r1.error)
        self.assertTrue(r1.clean, r1.remaining)
        self.assertEqual([p for p in planted_in(r1.output) if p != "Patrick"], [])
        self.assertNotEqual(r1.hash_before, r1.hash_after)
        self.assertNotEqual(r1.hash_after, r2.hash_after)
        self.assertTrue(ffmpeg_strict_ok(r1.output), "arquivo de saída deve decodificar sem avisos")
        return r1

    def test_mp4(self):
        r = self._check(self._make("v.mp4", extra=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"]))
        self.assertEqual(r.method, "remux")
        self.assertTrue(any("free" in n for n in r.notes))

    def test_mov(self):
        self._check(self._make("v.mov", extra=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"]))

    def test_mkv_with_chapters_and_subs(self):
        srt = os.path.join(self.dir, "s.srt")
        write_text(srt, "1\n00:00:00,000 --> 00:00:01,000\nOla\n\n")
        chap = os.path.join(self.dir, "c.txt")
        write_text(chap, ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=Capitulo Secreto\n")
        path = os.path.join(self.dir, "v.mkv")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=1",
                        "-i", srt, "-i", chap, "-map_metadata", "2", "-map", "0:v", "-map", "1:s",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:s", "srt", path], check=True)
        r = self._check(path)
        self.assertTrue(any("capítulo" in x for x in r.removed), r.removed)
        info = core.ffprobe_json(r.output)
        self.assertEqual(info.get("chapters", []), [])
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video", "subtitle"])

    def test_webm(self):
        self._check(self._make("v.webm", extra=["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus"]))

    def test_avi(self):
        self._check(self._make("v.avi", extra=["-c:v", "mpeg4", "-c:a", "mp3"]))

    def test_reencode_fallback(self):
        # h264 dentro de .webm não pode ser copiado → recodifica para VP9
        path = self._make("fake.mp4", extra=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"])
        fake = os.path.join(self.dir, "fake.webm")
        os.rename(path, fake)
        r = core.process_file(fake, os.path.join(self.dir, "out_fake.webm"))
        self.assertTrue(r.success, r.error)
        self.assertEqual(r.method, "reencode")
        self.assertTrue(r.clean, r.remaining)
        r2 = core.process_file(fake, os.path.join(self.dir, "out_fake2.webm"), core.Options(reencode_fallback=False))
        self.assertFalse(r2.success)

    def test_audio_formats(self):
        for name, extra in [("a.mp3", ["-c:a", "libmp3lame"]), ("a.m4a", ["-c:a", "aac"]),
                            ("a.flac", ["-c:a", "flac"]), ("a.wav", ["-c:a", "pcm_s16le"])]:
            with self.subTest(name=name):
                self._check(self._make(name, video=False, extra=extra))

    def test_cover_art_dropped(self):
        if not HAS_PIL:
            self.skipTest("Pillow necessário para gerar a capa")
        cover = os.path.join(self.dir, "cover.jpg")
        Image.new("RGB", (64, 64), "red").save(cover)
        path = os.path.join(self.dir, "cover.mp3")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-i", cover,
                        "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-c:v", "copy", "-disposition:v", "attached_pic",
                        "-metadata", "title=Secreto", path], check=True)
        r = self._check(path)
        info = core.ffprobe_json(r.output)
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["audio"])

    def test_cancel(self):
        import threading
        path = self._make("long.mp4", extra=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"])
        ev = threading.Event()
        ev.set()
        r = core.process_file(path, os.path.join(self.dir, "cancel.mp4"), cancel=ev)
        self.assertFalse(r.success)
        self.assertEqual(r.error, "cancelado")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "cancel.mp4")))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rh_batch_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _png(self, path):
        # PNG mínimo 1x1 gerado à mão (sem Pillow)
        import struct, zlib
        raw = b"\x00\xff\x00\x00"
        chunks = [(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
                  (b"tEXt", b"Comment\x00secreto Patrick"),
                  (b"IDAT", zlib.compress(raw)), (b"IEND", b"")]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            for t, d in chunks:
                f.write(struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    def test_collect_ignores_hidden_and_previous_output(self):
        src = os.path.join(self.dir, "src")
        self._png(os.path.join(src, "a.png"))
        self._png(os.path.join(src, "sub", "b.png"))
        self._png(os.path.join(src, ".oculta", "c.png"))
        self._png(os.path.join(src, "LIMPO_2026", "d.png"))
        write_text(os.path.join(src, "nota.txt"), "x")
        files = core.collect_files([src])
        self.assertEqual([os.path.relpath(f, src) for f in files], ["a.png", os.path.join("sub", "b.png")])

    def test_batch_keeps_structure_and_writes_report(self):
        src = os.path.join(self.dir, "src")
        self._png(os.path.join(src, "a.png"))
        self._png(os.path.join(src, "sub", "b.png"))
        out_base = os.path.join(self.dir, "out")
        os.makedirs(out_base)
        seen = []
        output_dir, results = core.process_batch([src], out_base, on_done=lambda i, r: seen.append(i))
        self.assertEqual(seen, [0, 1])
        self.assertTrue(all(r.success and r.clean for r in results), [r.error for r in results])
        self.assertTrue(os.path.exists(os.path.join(output_dir, "a.png")))
        self.assertTrue(os.path.exists(os.path.join(output_dir, "sub", "b.png")))
        self.assertTrue(os.path.exists(os.path.join(output_dir, "relatorio.json")))
        self.assertTrue(os.path.exists(os.path.join(output_dir, "relatorio.txt")))
        self.assertEqual(planted_in(os.path.join(output_dir, "a.png")), [])

    def test_inspect(self):
        p = os.path.join(self.dir, "x.png")
        self._png(p)
        info = core.inspect_file(p)
        self.assertEqual(info["metadados"], ["texto (Comment)"])
        r = core.process_file(p, os.path.join(self.dir, "y.png"))
        self.assertEqual(core.inspect_file(r.output)["metadados"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
