import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageDraw

SPEC = importlib.util.spec_from_file_location("motion", Path(__file__).resolve().parents[1] / "scripts/motion.py")
motion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(motion)


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "\u52a8\u56fe space"
        self.root.mkdir()
        self.frames = []
        for i, x in enumerate((2, 10, 18)):
            im = Image.new("RGBA", (32, 24))
            ImageDraw.Draw(im).rectangle((x, 5, x+5, 10), fill=(240, 30, 20, 255))
            im.putpixel((x, 4), (20, 50, 240, 80))
            im.save(self.root / f"{i}.png")
            self.frames.append(im)
        self.doc = {"schema_version": 1, "loop": True, "require_transparency": True,
                    "formats": ["gif", "apng", "webp", "sheet", "zip"], "sheet_columns": 2,
                    "frames": [{"path": f"{i}.png", "duration_ms": d} for i, d in enumerate((120, 70, 230))]}
        self.manifest = self.root / "motion.json"
        self.save()

    def tearDown(self): self.tmp.cleanup()
    def save(self): motion.dump(self.manifest, self.doc)

    def test_roundtrip_timing_alpha_no_ghosts_and_zip(self):
        out = self.root / "export"
        report = motion.build(self.manifest, out)
        self.assertEqual(report["duration_ms"], 420)
        for fmt in ("gif", "apng", "webp"):
            self.assertEqual(report["encoded"][fmt]["duration_ms"], 420)
        with Image.open(out / "animation.gif") as gif:
            self.assertEqual(gif.info["loop"], 0)
            for i, x in enumerate((2, 10, 18)):
                gif.seek(i)
                frame = gif.convert("RGBA")
                self.assertEqual(frame.getpixel((x+2, 7))[3], 255)
                self.assertEqual(frame.getpixel((0, 0))[3], 0)
                if i: self.assertEqual(frame.getpixel(((2, 10)[i-1]+2, 7))[3], 0)
        with Image.open(out / "animation.apng") as apng:
            for i, x in enumerate((2, 10, 18)):
                apng.seek(i)
                self.assertEqual(apng.convert("RGBA").getpixel((x, 4))[3], 80)
        with zipfile.ZipFile(out / "frames.zip") as z:
            self.assertEqual(len([n for n in z.namelist() if n.endswith(".png")]), 3)
            z.extractall(self.root / "unpacked")
        rerun = motion.build(self.root / "unpacked/motion.json", self.root / "rebuilt")
        self.assertEqual(rerun["duration_ms"], 420)

    def test_single_play(self):
        self.doc["loop"] = False; self.save()
        out = self.root / "once"
        motion.build(self.manifest, out)
        with Image.open(out / "animation.gif") as gif: self.assertNotIn("loop", gif.info)
        with Image.open(out / "animation.apng") as apng: self.assertEqual(apng.info["loop"], 1)

    def test_transparency_and_size_rejection(self):
        Image.new("RGB", (32, 24), "white").save(self.root / "1.png")
        with self.assertRaisesRegex(ValueError, "transparent"): motion.load(self.manifest)
        Image.new("RGBA", (30, 24)).save(self.root / "1.png")
        with self.assertRaisesRegex(ValueError, "size mismatch"): motion.load(self.manifest)

    def test_invalid_timing_and_empty(self):
        self.doc["frames"][0]["duration_ms"] = 71; self.save()
        with self.assertRaisesRegex(ValueError, "multiple"): motion.load(self.manifest)
        self.doc["frames"][0]["duration_ms"] = 70; self.save()
        Image.new("RGBA", (32, 24)).save(self.root / "1.png")
        with self.assertRaisesRegex(ValueError, "empty"): motion.load(self.manifest)

    def test_split_order_and_nonoverwrite(self):
        atlas = Image.new("RGBA", (64, 48))
        for i in range(4): atlas.paste(self.frames[i % 3], (i%2*32, i//2*24))
        path = self.root / "sheet.png"; atlas.save(path)
        out = self.root / "split"
        motion.split(path, 2, 2, out)
        with Image.open(out / "frame-0002.png") as im: self.assertEqual(im.tobytes(), self.frames[1].tobytes())
        with self.assertRaises(FileExistsError): motion.split(path, 2, 2, out)
        with self.assertRaises(ValueError): motion.split(path, 3, 2, self.root / "invalid")

    def test_duplicate_holds_and_no_overwrite(self):
        self.doc["frames"][1]["path"] = "0.png"; self.save()
        out = self.root / "holds"
        report = motion.build(self.manifest, out)
        self.assertEqual(report["unique_pixel_frames"], 2)
        self.assertEqual(report["encoded"]["gif"]["duration_ms"], 420)
        with self.assertRaises(FileExistsError): motion.build(self.manifest, out)

    def test_crop_review_gate_and_changed_pixels(self):
        sheet = self.root / "sheet.png"
        self.frames[0].save(sheet)
        result = motion.split(sheet, 1, 1, self.root / "split")
        self.assertTrue((self.root / "split/layout-overlay.png").is_file())
        self.doc["frames"] = result["frames"]
        self.doc["frames"][0]["path"] = "split/frame-0001.png"
        self.save()
        with self.assertRaisesRegex(ValueError, "crop review pending"):
            motion.load(self.manifest)
        self.doc["frames"][0]["crop_provenance"]["visual_layout_verified"] = True
        self.save()
        motion.load(self.manifest)
        self.frames[1].save(self.root / "split/frame-0001.png")
        with self.assertRaisesRegex(ValueError, "stale"):
            motion.load(self.manifest)

    def test_margin_gutter_and_ambiguous_boxes(self):
        atlas = Image.new("RGBA", (70, 28))
        atlas.paste(self.frames[0], (2, 2)); atlas.paste(self.frames[1], (36, 2))
        sheet = self.root / "sheet.png"; atlas.save(sheet)
        result = motion.split(sheet, 2, 1, self.root / "split", margin=2, gutter=2)
        with Image.open(self.root / "split/frame-0002.png") as im:
            self.assertEqual(im.tobytes(), self.frames[1].tobytes())
        self.assertEqual(result["frames"][1]["crop_provenance"]["box"], [36, 2, 68, 26])
        with self.assertRaisesRegex(ValueError, "overlap"):
            motion.split(sheet, 2, 1, self.root / "bad", boxes=[[0,0,32,24],[10,0,42,24]])

    def test_retime_preserves_art_and_exact_total(self):
        out = self.root / "other/timing.json"
        motion.retime(self.manifest, out, total_ms=1000)
        updated = motion.read(out)
        self.assertEqual(sum(f["duration_ms"] for f in updated["frames"]), 1000)
        self.assertTrue(all(f["duration_ms"] % 10 == 0 for f in updated["frames"]))
        self.assertEqual(motion.read(self.manifest), self.doc)
        self.assertEqual(motion.load(out)[1][0].tobytes(), self.frames[0].tobytes())
        report = motion.build(out, self.root / "retimed")
        self.assertEqual(report["encoded"]["gif"]["duration_ms"], 1000)
        with self.assertRaises(FileExistsError): motion.retime(self.manifest, out, factor=2)
        with self.assertRaises(ValueError): motion.retime(self.manifest, self.root / "short.json", factor=.01)

    def test_failed_export_is_not_published(self):
        self.doc["formats"] = ["gif", "mp4"]; self.save()
        out = self.root / "failed"
        with patch.object(motion, "video", side_effect=RuntimeError("encoder unavailable")):
            with self.assertRaises(RuntimeError): motion.build(self.manifest, out)
        self.assertFalse(out.exists())
        self.assertTrue(self.manifest.exists())

    def test_palette_reserves_transparency_without_erasing_black(self):
        im = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        im.putpixel((2, 2), (0, 0, 0, 255))
        im.putpixel((3, 2), (255, 0, 0, 255))
        q = motion.gif_frames([im], 128)[0]
        self.assertNotEqual(q.getpixel((2, 2)), 255)
        self.assertEqual(q.convert("RGBA").getpixel((2, 2)), (0, 0, 0, 255))
        self.assertEqual(q.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_gif_palette_keeps_small_distinct_colours(self):
        # A large many-colour gradient must not crowd a small drop and a thin root out of the palette.
        drop, root = (135, 203, 242), (250, 250, 245)
        for i in range(3):
            im = Image.new("RGBA", (96, 80))
            for y in range(4, 76):
                for x in range(4, 92):
                    im.putpixel((x, y), (x + i * 3, 100 + y * 2, (x * y) % 60, 255))
            draw = ImageDraw.Draw(im)
            draw.rectangle((60 + i * 4, 20, 62 + i * 4, 22), fill=drop)
            draw.line((30 + i, 50, 30 + i, 56), fill=root)
            im.save(self.root / f"g{i}.png")
        self.doc.update(formats=["gif"], frames=[{"path": f"g{i}.png", "duration_ms": 100} for i in range(3)])
        self.save()
        out = self.root / "palette"
        motion.build(self.manifest, out)
        with Image.open(out / "animation.gif") as gif:
            self.assertEqual(gif.info["transparency"], 255)
            for i in range(3):
                gif.seek(i)
                frame = gif.convert("RGBA")
                self.assertEqual(frame.getpixel((0, 0))[3], 0)
                for xy, want in (((61 + i * 4, 21), drop), ((30 + i, 53), root)):
                    got = frame.getpixel(xy)
                    self.assertEqual(got[3], 255)
                    self.assertLessEqual(max(abs(a - b) for a, b in zip(got[:3], want)), 16, (i, xy, got))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_video_and_audio_timing(self):
        import wave
        with wave.open(str(self.root / "voice.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\0\0" * 800)
        self.doc["formats"] = ["mp4", "zip"]
        self.doc["audio"] = "voice.wav"; self.save()
        out = self.root / "video"
        report = motion.build(self.manifest, out)
        self.assertEqual(report["encoded"]["mp4"]["duration_ms"], 420)
        self.assertTrue(report["encoded"]["mp4"]["audio"])


if __name__ == "__main__": unittest.main()
