"""Subject-agnostic sheet geometry. Plans and diagnostics, never generated artwork."""
import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw


def plan(count, cell, cols=None, margin=0, gutter=0, safe_fraction=.08, background="isolated"):
    if type(count) is not int or not 1 <= count <= 240:
        raise ValueError("count must be 1..240")
    if len(cell) != 2 or any(type(v) is not int or v <= 0 for v in cell):
        raise ValueError("cell must contain two positive pixel dimensions")
    if min(margin, gutter) < 0 or not 0 <= safe_fraction < .45:
        raise ValueError("invalid spacing")
    if background not in ("isolated", "scene"):
        raise ValueError("background must be isolated or scene")
    if cols is None:
        cols = min(range(1, count+1), key=lambda c: abs(math.log(
            (c*cell[0]+(c-1)*gutter+2*margin) /
            (math.ceil(count/c)*cell[1]+(math.ceil(count/c)-1)*gutter+2*margin)))
            + .15 * (c*math.ceil(count/c)-count)/count)
    if not 1 <= cols <= count:
        raise ValueError("cols must be 1..count")
    rows = math.ceil(count/cols)
    width = cols*cell[0]+(cols-1)*gutter+2*margin
    height = rows*cell[1]+(rows-1)*gutter+2*margin
    if width*height > 100000000:
        raise ValueError("layout exceeds 100 megapixels")
    boxes = [[margin+i%cols*(cell[0]+gutter), margin+i//cols*(cell[1]+gutter),
              margin+i%cols*(cell[0]+gutter)+cell[0], margin+i//cols*(cell[1]+gutter)+cell[1]]
             for i in range(count)]
    return {"schema_version": 1, "count": count, "cols": cols, "rows": rows,
            "cell_size": list(cell), "sheet_size": [width, height], "boxes": boxes,
            "margin": margin, "gutter": gutter, "safe_fraction": safe_fraction,
            "background": background, "minimum_cell_size": list(cell),
            "note": "Geometry only. Fit the complete motion envelope; verify backend support before generation."}


def check(sheet, spec):
    # Reconstruct geometry rather than trusting manually changed boxes.
    expected = plan(spec["count"], spec["cell_size"], spec["cols"], spec["margin"],
                    spec["gutter"], spec["safe_fraction"], spec["background"])
    if spec["boxes"] != expected["boxes"] or spec["sheet_size"] != expected["sheet_size"]:
        raise ValueError("layout geometry inconsistent; regenerate the plan")
    with Image.open(sheet) as raw:
        if raw.width*raw.height > 100000000:
            raise ValueError("sheet exceeds pixel budget")
        im = raw.convert("RGBA")
    w, h = im.size
    pw, ph = spec["sheet_size"]
    issues = []
    if abs((w/h)/(pw/ph)-1) > .01:
        issues.append("aspect_mismatch")
    scaled = [[round(x*w/pw), round(y*h/ph), round(r*w/pw), round(b*h/ph)]
              for x,y,r,b in spec["boxes"]]
    sizes = {(r-x,b-y) for x,y,r,b in scaled}
    if len(sizes) != 1 or any(min(size) <= 0 for size in sizes):
        issues.append("unequal_or_empty_cells")
    minimum = spec.get("minimum_cell_size", spec["cell_size"])
    if any(cw < minimum[0] or ch < minimum[1] for cw,ch in sizes):
        issues.append("insufficient_cell_pixels")
    risks = []
    for i, box in enumerate(scaled):
        alpha = im.crop(box).getchannel("A")
        # Inspect faint alpha too; do not silently discard particles as noise.
        bbox = alpha.point([0]+[255]*255).getbbox()
        cw,ch = alpha.size
        if bbox and (bbox[0] == 0 or bbox[1] == 0 or bbox[2] == cw or bbox[3] == ch):
            risks.append(i+1)
    if spec["background"] == "isolated" and risks:
        issues.append("foreground_on_crop_boundary")
    return {"status": "blocked" if issues else "geometry_passed", "issues": issues,
            "actual_size": [w,h], "planned_size": [pw,ph], "boxes": scaled,
            "boundary_frames": risks, "visual_status": "pending",
            "next_step": "Inspect sheet and crops. Re-plan or regenerate ambiguous content; never trim it to pass."}


def diagnostic(image, boxes, safe=0):
    base = Image.new("RGBA", image.size, "#707070")
    base.alpha_composite(image.convert("RGBA"))
    draw = ImageDraw.Draw(base)
    for i,(x,y,r,b) in enumerate(boxes):
        draw.rectangle((x,y,r-1,b-1), outline="red", width=2)
        dx,dy = round((r-x)*safe),round((b-y)*safe)
        if safe and r-x > 2*dx+1 and b-y > 2*dy+1:
            draw.rectangle((x+dx,y+dy,r-dx-1,b-dy-1), outline="yellow")
        draw.text((x+3,y+3),str(i+1),fill="white")
    return base.convert("RGB")


def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="command",required=True)
    a=sub.add_parser("plan"); a.add_argument("--count",type=int,required=True)
    a.add_argument("--cell",type=int,nargs=2,required=True); a.add_argument("--cols",type=int)
    a.add_argument("--margin",type=int,default=0); a.add_argument("--gutter",type=int,default=0)
    a.add_argument("--safe-fraction",type=float,default=.08)
    a.add_argument("--background",choices=["isolated","scene"],default="isolated")
    a.add_argument("--out",type=Path,required=True)
    a=sub.add_parser("check"); a.add_argument("--sheet",type=Path,required=True)
    a.add_argument("--layout",type=Path,required=True); a.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    if args.out.exists(): raise FileExistsError(args.out)
    if args.command == "plan":
        result=plan(args.count,args.cell,args.cols,args.margin,args.gutter,args.safe_fraction,args.background)
        picture=diagnostic(Image.new("RGBA",result["sheet_size"]),result["boxes"],result["safe_fraction"])
    else:
        spec=json.loads(args.layout.read_text(encoding="utf-8-sig"))
        result=check(args.sheet,spec)
        with Image.open(args.sheet) as im: picture=diagnostic(im,result["boxes"],spec["safe_fraction"])
    args.out.mkdir(parents=True)
    (args.out/("layout.json" if args.command == "plan" else "check.json")).write_text(json.dumps(result,indent=2),encoding="utf-8")
    picture.save(args.out/"layout-guide.png")
    print(json.dumps(result))
    return 3 if result.get("status")=="blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
