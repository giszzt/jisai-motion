import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name+".py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod
layout, motion = module("layout"), module("motion")

class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def save(self, spec, full=False):
        im = Image.new("RGBA", spec["sheet_size"], "blue" if full else (0,0,0,0))
        draw = ImageDraw.Draw(im)
        for x,y,r,b in spec["boxes"]:
            draw.rectangle((x+4,y+4,r-5,b-5), fill="orange")
        path=self.root/"sheet.png"; im.save(path)
        plan=self.root/"layout.json"; plan.write_text(json.dumps(spec))
        return path,plan
    def test_non_character_rectangular_units_and_unused_slot(self):
        spec=layout.plan(5,[32,24],cols=3,gutter=4,margin=2)
        sheet,plan=self.save(spec)
        self.assertEqual(layout.check(sheet,spec)["status"],"geometry_passed")
        out=self.root/"split"
        result=motion.split(sheet,None,None,out,layout=plan)
        self.assertEqual(len(result["frames"]),5)
        self.assertTrue((out/"preview.html").exists())
        manifest=out/"motion.json"; doc=motion.read(manifest)
        with self.assertRaises(ValueError): motion.build(manifest,self.root/"blocked")
        for frame in doc["frames"]: frame["crop_provenance"]["visual_layout_verified"]=True
        motion.dump(manifest,doc)
        self.assertEqual(motion.build(manifest,self.root/"export")["duration_ms"],600)
    def test_wrong_aspect_and_no_output(self):
        spec=layout.plan(8,[32,32],cols=8)
        sheet,plan=self.save(spec)
        Image.new("RGBA",(96,32)).save(sheet)
        self.assertIn("aspect_mismatch",layout.check(sheet,spec)["issues"])
        with self.assertRaisesRegex(ValueError,"layout blocked"):
            motion.split(sheet,None,None,self.root/"split",layout=plan)
        self.assertFalse((self.root/"split").exists())
    def test_boundary_particles_and_full_scene(self):
        spec=layout.plan(2,[32,24],cols=2)
        sheet,_=self.save(spec)
        with Image.open(sheet) as raw: im=raw.copy()
        im.putpixel((31,10),(255,255,255,1)); im.save(sheet)
        self.assertIn("foreground_on_crop_boundary",layout.check(sheet,spec)["issues"])
        spec["background"]="scene"; sheet,_=self.save(spec,full=True)
        self.assertEqual(layout.check(sheet,spec)["status"],"geometry_passed")
        self.assertEqual(layout.check(sheet,spec)["boundary_frames"],[1,2])
    def test_scaled_sheet_and_resolution(self):
        spec=layout.plan(2,[32,24],cols=2); sheet,_=self.save(spec)
        with Image.open(sheet) as raw: im=raw.resize((128,48))
        im.save(sheet)
        self.assertEqual(layout.check(sheet,spec)["status"],"geometry_passed")
        im.resize((32,12)).save(sheet)
        self.assertIn("insufficient_cell_pixels",layout.check(sheet,spec)["issues"])
    def test_arbitrary_count_auto_and_tampered_geometry(self):
        spec=layout.plan(7,[48,24]); sheet,_=self.save(spec)
        self.assertEqual(len(spec["boxes"]),7)
        spec["boxes"][0][0]+=1
        with self.assertRaises(ValueError): layout.check(sheet,spec)

if __name__ == "__main__": unittest.main()
