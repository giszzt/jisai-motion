"""Offline tests for the Labnana adapter: keying, key choice, prompt, ratios.

No network. The live model ids and parameter allowlist are verified by
`generate_frames.py precheck` against the real account, not here.
"""
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageDraw

SPEC = importlib.util.spec_from_file_location(
    "generate_frames", Path(__file__).resolve().parents[1] / "scripts/generate_frames.py"
)
gf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gf)

GREEN = (0, 255, 0)


def flat_backdrop(size=(120, 120), key=(3, 249, 6), subject=(230, 20, 20), noise=True):
    """A subject on a near-flat chroma field, the shape the backend actually returns."""
    image = Image.new("RGB", size, key)
    if noise:
        for x in range(0, size[0], 7):
            for y in range(0, size[1], 5):
                image.putpixel((x, y), (key[0] + 2, key[1] - 2, key[2] + 3))
    ImageDraw.Draw(image).ellipse((30, 30, 90, 90), fill=subject)
    return image


def painted_checkerboard(size=(120, 120), cell=8):
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) % 2:
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(235, 235, 235))
    draw.ellipse((40, 40, 80, 80), fill=(230, 20, 20))
    return image


class KeyingTests(unittest.TestCase):
    def test_flat_backdrop_keys_to_real_alpha(self):
        keyed, report = gf.key_out(flat_backdrop(), GREEN)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["issues"], [])
        self.assertEqual(keyed.mode, "RGBA")
        alpha = keyed.getchannel("A")
        self.assertEqual(alpha.getextrema()[0], 0, "no pixel became transparent")
        self.assertEqual(alpha.getextrema()[1], 255, "no pixel stayed opaque")
        self.assertGreater(report["transparent_fraction"], 0.5)
        self.assertLess(report["key_spill_fraction"], 0.02)

    def test_noise_around_the_key_still_keys_out(self):
        _, report = gf.key_out(flat_backdrop(key=(8, 244, 11)), GREEN)
        self.assertEqual(report["status"], "ok")
        self.assertLessEqual(report["key_drift"], gf.KEY_ALPHA_HIGH * 2)

    def test_painted_checkerboard_is_refused_not_relabelled(self):
        _, report = gf.key_out(painted_checkerboard(), GREEN)
        self.assertTrue(report["painted_checkerboard"])
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["transparent_fraction"], 0.0)
        self.assertTrue(any("checkerboard" in issue for issue in report["issues"]))

    def test_key_colliding_with_the_subject_is_reported(self):
        # A green subject on a green field: keying would eat the subject.
        _, report = gf.key_out(flat_backdrop(subject=(20, 230, 30)), GREEN)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(
            any("keyed away" in i or "no flat backdrop" in i or "near the key" in i for i in report["issues"]),
            report["issues"],
        )

    def test_opaque_image_reports_no_backdrop(self):
        solid = Image.new("RGB", (60, 60), (120, 90, 60))
        _, report = gf.key_out(solid, GREEN)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["transparent_fraction"], 0.0)

    def test_thresholds_must_be_ordered(self):
        with self.assertRaises(ValueError):
            gf.key_out(flat_backdrop(), GREEN, low=60, high=60)

    def test_soft_edge_band_produces_partial_alpha(self):
        image = Image.new("RGB", (40, 40), (0, 255, 0))
        for x in range(40):
            image.putpixel((x, 20), (0, 255 - gf.KEY_ALPHA_LOW - 8, 0))  # inside the band
        ImageDraw.Draw(image).rectangle((10, 30, 30, 38), fill=(230, 20, 20))
        keyed, _ = gf.key_out(image, GREEN)
        values = {keyed.getchannel("A").getpixel((x, 20)) for x in range(40)}
        self.assertTrue(any(0 < v < 255 for v in values), values)


class CellTests(unittest.TestCase):
    def test_uniform_cells_have_small_spread(self):
        sheet = Image.new("RGB", (120, 120), (3, 249, 6))
        draw = ImageDraw.Draw(sheet)
        for cx, cy in ((30, 30), (90, 30), (30, 90), (90, 90)):
            draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), fill=(230, 20, 20))
        result = gf.cell_uniformity(sheet, GREEN, 2, 2)
        self.assertTrue(result["divisible"])
        self.assertEqual(len(result["cells"]), 4)
        self.assertLess(result["spread"], 0.02)

    def test_one_filled_cell_shows_a_large_spread(self):
        sheet = Image.new("RGB", (120, 120), (3, 249, 6))
        ImageDraw.Draw(sheet).rectangle((60, 0, 119, 59), fill=(230, 20, 20))
        result = gf.cell_uniformity(sheet, GREEN, 2, 2)
        self.assertGreater(result["spread"], 0.5)

    def test_indivisible_sheet_is_reported_not_guessed(self):
        result = gf.cell_uniformity(Image.new("RGB", (121, 120), GREEN), GREEN, 2, 2)
        self.assertFalse(result["divisible"])
        self.assertIsNone(result["spread"])


class KeyChoiceTests(unittest.TestCase):
    def test_avoids_a_colour_the_subject_uses(self):
        key, _name, margin = gf.pick_key_colour([gf.parse_hex("#00FF00")])
        self.assertNotEqual(key, GREEN)
        self.assertGreater(margin, 100)

    def test_no_constraints_keeps_the_preferred_candidate(self):
        key, _name, margin = gf.pick_key_colour([])
        self.assertEqual(key, gf.parse_hex(gf.KEY_CANDIDATES[0][0]))
        self.assertEqual(margin, 255)

    def test_dominant_colours_finds_the_subject_palette(self, ):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ref.png"
            Image.new("RGB", (40, 40), (240, 130, 30)).save(path)
            colours = gf.dominant_colours(path)
        self.assertTrue(any(abs(c[0] - 240) < 40 and c[2] < 80 for c in colours), colours)

    def test_hex_parsing_and_rejection(self):
        self.assertEqual(gf.parse_hex("#0f0"), (0, 255, 0))
        self.assertEqual(gf.to_hex((0, 255, 0)), "#00FF00")
        with self.assertRaises(ValueError):
            gf.parse_hex("#12345")


class PromptTests(unittest.TestCase):
    def test_sheet_ratio_rejected_before_network(self):
        args = SimpleNamespace(model="flare", aspect_ratio="8:1", cells="8x1")
        with patch.object(gf, "post") as post:
            with self.assertRaisesRegex(ValueError, "re-plan"):
                gf.call_labnana("motion", [], args)
            post.assert_not_called()
    def test_frame_count_resolution_and_explicit_override(self):
        self.assertEqual(gf.resolve_image_size("auto", (4, 2)), "2K")
        self.assertEqual(gf.resolve_image_size("auto", (4, 4)), "4K")
        self.assertEqual(gf.resolve_image_size("auto", (6, 6)), "4K")
        self.assertEqual(gf.resolve_image_size("2K", (4, 4)), "2K")
        self.assertEqual(gf.resolve_image_size("auto", None), "2K")

    def test_opaque_sheet_also_has_containment_rules(self):
        text = gf.build_prompt("a cat", False, None, "", (4, 4))
        self.assertIn("detached objects", text)
        self.assertIn("8 percent", text)
        self.assertNotIn("chroma-key", text)

    def test_transparent_prompt_gets_the_chroma_clause(self):
        out = gf.build_prompt("a cat waves", True, GREEN, "pure green", None)
        self.assertIn("a cat waves", out)
        self.assertIn("#00FF00", out)
        self.assertIn("No checkerboard", out)
        self.assertNotIn("cells", out)

    def test_sheet_prompt_demands_one_backdrop_across_cells(self):
        out = gf.build_prompt("a cat waves", True, GREEN, "pure green", (4, 4))
        self.assertIn("all 16 cells", out)

    def test_opaque_prompt_is_left_alone(self):
        self.assertEqual(gf.build_prompt("a cat in a kitchen", False, None, "", None), "a cat in a kitchen")

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(ValueError):
            gf.build_prompt("   ", True, GREEN, "pure green", None)


class AspectTests(unittest.TestCase):
    def test_supported_ratio_is_sent_as_is(self):
        self.assertEqual(gf.snap_aspect("16:9"), ("16:9", False))
        self.assertEqual(gf.snap_aspect("1:1"), ("1:1", False))

    def test_unsupported_ratio_snaps_to_the_nearest(self):
        # 4:1 is on Gemini's table but not the GPT route's; 21:9 is the closest legal one.
        self.assertEqual(gf.snap_aspect("4:1"), ("21:9", True))
        self.assertEqual(gf.snap_aspect("1.91:1"), ("16:9", True))

    def test_alternate_separators(self):
        self.assertEqual(gf.snap_aspect("1024x1024"), ("1:1", False))

    def test_nonsense_ratio_raises(self):
        with self.assertRaises(ValueError):
            gf.snap_aspect("wide")

    def test_quality_table_excludes_openai_only_values(self):
        self.assertNotIn("xhigh", gf.QUALITIES)
        self.assertNotIn("max", gf.QUALITIES)

    def test_model_ids_are_the_verified_ones(self):
        self.assertEqual(gf.MODELS["flare"], "gpt-image-2.5-flare")
        self.assertEqual(gf.MODELS["sunburst"], "gpt-image-2.5-sunburst")
        self.assertEqual(gf.DEFAULT_MODEL, "flare")


class ReferenceTests(unittest.TestCase):
    def test_role_defaults_to_identity(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            Image.new("RGB", (8, 8)).save(path)
            self.assertEqual(gf.parse_reference(str(path))[1], "identity")
            self.assertEqual(gf.parse_reference(f"{path}::style")[1], "style")
            with self.assertRaises(ValueError):
                gf.parse_reference(f"{path}::whatever")

    def test_missing_reference_raises(self):
        with self.assertRaises(ValueError):
            gf.parse_reference("no-such-file-here.png")

    def test_reference_is_inlined_as_base64(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
            item, note = gf.inline_reference(path, gf.REFERENCE_BUDGET)
            self.assertIn("inlineData", item)
            self.assertEqual(item["inlineData"]["mimeType"], "image/png")
            self.assertIsNone(note)

    def test_oversized_reference_is_downscaled_with_a_note(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.png"
            noisy = Image.effect_noise((2000, 2000), 90).convert("RGB")
            noisy.save(path)
            _item, note = gf.inline_reference(path, 1_500_000)
            self.assertIsNotNone(note)
            self.assertIn("downscaled", note)


if __name__ == "__main__":
    unittest.main()
