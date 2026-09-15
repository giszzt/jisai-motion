#!/usr/bin/env python3
"""Deterministic frame packaging. No network, art generation, or implicit alignment."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageStat, ImageDraw, features


FORMATS = {"gif", "apng", "webp", "sheet", "zip", "mp4"}


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def source(root, value):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (root / p).resolve()


def positive_int(value, label, minimum=1, maximum=60000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def load(manifest):
    manifest = Path(manifest).resolve()
    doc = read(manifest)
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise ValueError("expected schema_version 1 object")
    entries = doc.get("frames")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 240:
        raise ValueError("frames must contain 1..240 entries")
    formats = doc.get("formats", ["gif"])
    if not isinstance(formats, list) or not formats or any(f not in FORMATS for f in formats):
        raise ValueError(f"formats must be a nonempty subset of {sorted(FORMATS)}")
    for key, default in (("loop", True), ("require_transparency", False), ("allow_empty_frames", False)):
        if type(doc.get(key, default)) is not bool:
            raise ValueError(f"{key} must be boolean")
    positive_int(doc.get("sheet_columns", 4), "sheet_columns", 1, 240)
    positive_int(doc.get("gif_alpha_threshold", 128), "gif_alpha_threshold", 1, 255)
    bg = ImageColor.getrgb(doc.get("video_background", "#ffffff"))
    if len(bg) != 3:
        raise ValueError("video_background must be opaque RGB")
    images, times, facts, hashes = [], [], [], []
    size = None
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"frame {index + 1}: missing path")
        duration = positive_int(entry.get("duration_ms"), "duration_ms", 20)
        if duration % 10:
            raise ValueError("duration_ms must be a multiple of 10")
        p = source(manifest.parent, entry["path"])
        with Image.open(p) as raw:
            if raw.format != "PNG" or getattr(raw, "n_frames", 1) != 1:
                raise ValueError(f"frame {index + 1} must be a single PNG")
            if raw.width * raw.height * len(entries) > 100000000:
                raise ValueError("total frame pixel budget exceeds 100 megapixels")
            im = raw.convert("RGBA")
        if size is None:
            size = im.size
        if im.size != size:
            raise ValueError(f"frame {index + 1}: size mismatch; no implicit resizing")
        if size[0] * size[1] > 16777216:
            raise ValueError("frame exceeds 16 megapixels")
        alpha = im.getchannel("A")
        amin, amax = alpha.getextrema()
        if doc.get("require_transparency", False) and amin != 0:
            raise ValueError(f"frame {index + 1}: no fully transparent background pixels")
        if amax == 0 and not doc.get("allow_empty_frames", False):
            raise ValueError(f"frame {index + 1}: empty; allow_empty_frames required")
        bbox = alpha.getbbox()
        touches = bool(bbox and (bbox[0] == 0 or bbox[1] == 0 or bbox[2] == size[0] or bbox[3] == size[1]))
        facts.append({"index": index + 1, "alpha_min": amin, "alpha_max": amax,
                      "bbox": bbox, "touches_edge": touches})
        hashes.append(hashlib.sha256(im.tobytes()).hexdigest())
        provenance = entry.get("crop_provenance")
        if provenance:
            digest = hashes[-1]
            if provenance.get("frame_sha256") != digest or provenance.get("visual_layout_verified") is not True:
                raise ValueError(f"frame {index + 1}: crop review pending or stale; inspect layout overlay and approve the current frame")
        images.append(im)
        times.append(duration)
    if sum(times) > 60000:
        raise ValueError("motion exceeds 60 seconds")
    # Prevent accidentally constructing multi-gigabyte working sets/atlases.
    if size[0] * size[1] * len(images) > 100000000:
        raise ValueError("total frame pixel budget exceeds 100 megapixels")
    deltas = []
    for a, b in zip(images, images[1:]):
        deltas.append(round(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 4, 4))
    report = {"structural_status": "passed", "visual_status": "not_reviewed",
              "logical_frames": len(images), "unique_pixel_frames": len(set(hashes)),
              "size": size, "duration_ms": sum(times), "frames": facts,
              "adjacent_mean_pixel_differences": deltas,
              "warnings": [], "encoded": {}}
    report["review_binding"] = hashlib.sha256(json.dumps(
        {"frames": hashes, "times": times, "loop": doc.get("loop", True),
         "threshold": doc.get("gif_alpha_threshold", 128)}, sort_keys=True).encode()).hexdigest()
    report["average_frame_ms"] = round(sum(times) / len(times), 2)
    if len(images) >= 16 and sum(times) / len(times) < 90:
        report["warnings"].append("High pose count with short holds: review readability; do not automatically slow intentional fast motion.")
    if len(set(hashes)) < len(images):
        report["warnings"].append("Duplicate pixel frames: may be intentional holds; not extra poses.")
    if any(f["touches_edge"] for f in facts):
        report["warnings"].append("Visible alpha touches a canvas edge; inspect for unintended clipping.")
    if doc.get("audio"):
        if "mp4" not in formats:
            raise ValueError("audio requires mp4 in formats; GIF has no audio")
        if not source(manifest.parent, doc["audio"]).is_file():
            raise ValueError("audio file missing")
    return doc, images, times, report


def transaction(out, work):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError(f"output already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".motion-", dir=out.parent) as folder:
        staging = Path(folder) / "result"
        staging.mkdir()
        result = work(staging)
        staging.rename(out)
    return result


def split(sheet, cols, rows, out, margin=0, gutter=0, boxes=None, duration_ms=120, layout=None):
    layout_spec = None
    if layout is not None:
        module_spec = importlib.util.spec_from_file_location("motion_layout", Path(__file__).with_name("layout.py"))
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        layout_spec = read(layout)
        if boxes is not None or margin or gutter:
            raise ValueError("layout cannot be combined with boxes/margin/gutter")
        if (cols is not None and cols != layout_spec["cols"]) or (rows is not None and rows != layout_spec["rows"]):
            raise ValueError("grid conflicts with layout")
        checked = module.check(sheet, layout_spec)
        if checked["status"] == "blocked":
            raise ValueError("layout blocked: " + ", ".join(checked["issues"]) + "; run layout.py check for diagnostic image")
        cols, rows, boxes = layout_spec["cols"], layout_spec["rows"], checked["boxes"]
    positive_int(cols, "cols", 1, 240)
    positive_int(rows, "rows", 1, 240)
    if cols * rows > 240:
        raise ValueError("grid exceeds 240 cells")
    with Image.open(sheet) as raw:
        if raw.width * raw.height > 100000000:
            raise ValueError("sheet pixel budget exceeds 100 megapixels")
        im = raw.convert("RGBA")
    positive_int(duration_ms, "duration_ms", 20)
    if duration_ms % 10:
        raise ValueError("duration_ms must be a multiple of 10")
    if type(margin) is not int or type(gutter) is not int or min(margin, gutter) < 0:
        raise ValueError("margin and gutter must be nonnegative integers")
    if boxes is None:
        usable_w = im.width - 2 * margin - (cols - 1) * gutter
        usable_h = im.height - 2 * margin - (rows - 1) * gutter
        if min(usable_w, usable_h) <= 0 or usable_w % cols or usable_h % rows:
            raise ValueError("sheet dimensions minus margins/gutters must be exactly divisible by grid")
        w, h = usable_w // cols, usable_h // rows
        boxes = [[margin + i % cols * (w + gutter), margin + i // cols * (h + gutter),
                  margin + i % cols * (w + gutter) + w, margin + i // cols * (h + gutter) + h]
                 for i in range(cols * rows)]
    expected_count = layout_spec["count"] if layout_spec else cols * rows
    if not isinstance(boxes, list) or len(boxes) != expected_count:
        raise ValueError("boxes must contain one [left, top, right, bottom] per cell")
    sizes = set()
    for box in boxes:
        if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
            raise ValueError("crop boxes must contain four integers")
        l, t, r, b = box
        if not (0 <= l < r <= im.width and 0 <= t < b <= im.height):
            raise ValueError("crop box outside sheet")
        sizes.add((r-l, b-t))
    if len(sizes) != 1:
        raise ValueError("crop boxes must have equal size; no implicit scaling")
    for i, a in enumerate(boxes):
        for b in boxes[i+1:]:
            if max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3]):
                raise ValueError("crop boxes overlap; ambiguous frame ownership")
    w, h = next(iter(sizes))
    sheet_hash = hashlib.sha256(Path(sheet).read_bytes()).hexdigest()
    def work(dest):
        entries = []
        overlay = im.convert("RGB")
        draw = ImageDraw.Draw(overlay)
        for i, box in enumerate(boxes):
            name = f"frame-{i + 1:04d}.png"
            frame = im.crop(box)
            frame.save(dest / name)
            alpha = frame.getchannel("A")
            visible = alpha.point([255 if v >= 128 else 0 for v in range(256)]).getbbox()
            pad = max(1, round(min(w, h) * .08))
            risk = bool(visible and (visible[0] < pad or visible[1] < pad or visible[2] > w-pad or visible[3] > h-pad))
            x, y, r, b = box
            draw.rectangle((x, y, r-1, b-1), outline="red", width=2)
            if min(w, h) > 2 * pad:
                draw.rectangle((x+pad, y+pad, r-pad-1, b-pad-1), outline="yellow")
            draw.text((x+2, y+2), str(i+1), fill="red")
            entries.append({"path": name, "duration_ms": duration_ms,
                            "crop_provenance": {"sheet_sha256": sheet_hash, "box": box,
                            "frame_sha256": hashlib.sha256(frame.tobytes()).hexdigest(),
                            "safe_area_risk": risk, "visual_layout_verified": False}})
        overlay.save(dest / "layout-overlay.png")
        result = {"cols": cols, "rows": rows, "cell_size": [w, h], "frames": entries,
                  "visual_layout_verified": False, "timing_source": "provisional-uniform",
                  "duration_ms": duration_ms * len(entries), "overlay": "layout-overlay.png"}
        dump(dest / "grid.json", result)
        manifest = {"schema_version": 1, "name": Path(sheet).stem, "loop": True,
                    "require_transparency": (layout_spec["background"] == "isolated") if layout_spec else im.getchannel("A").getextrema()[0] == 0,
                    "formats": ["gif", "apng"], "sheet_columns": cols,
                    "frames": entries, "timing_source": "provisional-uniform"}
        dump(dest / "motion.json", manifest)
        preview(dest, manifest)
        return result
    return transaction(out, work)


def retime(manifest, out, factor=None, total_ms=None):
    doc = read(manifest)
    entries = doc.get("frames", [])
    if not entries:
        raise ValueError("no frames to retime")
    old = [positive_int(f.get("duration_ms"), "duration_ms", 20) for f in entries]
    if any(t % 10 for t in old):
        raise ValueError("duration_ms must be a multiple of 10")
    if factor is not None and (not math.isfinite(factor) or factor <= 0):
        raise ValueError("factor must be finite and positive")
    target = total_ms if total_ms is not None else round(sum(old) * factor / 10) * 10
    if type(target) is not int or target % 10 or not 20 * len(old) <= target <= 60000:
        raise ValueError("target must be a 10 ms multiple, at least 20 ms per frame, and at most 60000 ms")
    exact = [t * target / sum(old) / 10 for t in old]
    units = [math.floor(v) for v in exact]
    if min(units) < 2:
        raise ValueError("retime would make a frame shorter than 20 ms")
    for i in sorted(range(len(units)), key=lambda i: exact[i]-units[i], reverse=True)[:target//10-sum(units)]:
        units[i] += 1
    root = Path(manifest).resolve().parent
    for f, u in zip(entries, units):
        f["duration_ms"] = u * 10
        f["path"] = str(source(root, f["path"]))
    if doc.get("audio"):
        doc["audio"] = str(source(root, doc["audio"]))
    doc["timing"] = {"source": "retime", "previous_duration_ms": sum(old), "duration_ms": target,
                     "motion_and_timing": "not_reviewed", "encoded_playback": "not_reviewed"}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with Path(out).open("x", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return {"output": str(Path(out).resolve()), "duration_ms": target}


def gif_palette(images, threshold, reserve=32, far=32, min_pixels=4):
    # Histogram of every visible pixel at full resolution; transparent pixels get no weight.
    counts = {}
    for im in images:
        for n, (r, g, b, a) in im.getcolors(im.width * im.height):
            if a >= threshold:
                counts[(r, g, b)] = counts.get((r, g, b), 0) + n
    if len(counts) <= 255:
        return sorted(counts) or [(0, 0, 0)]
    scale = min(1, 1000000 / sum(counts.values()))
    sample = b"".join(bytes(c) * max(1, round(n * scale)) for c, n in counts.items())
    strip = Image.frombytes("RGB", (len(sample) // 3, 1), sample)

    def cut(k):
        p = strip.quantize(colors=k, method=Image.Quantize.MEDIANCUT).getpalette()[:3 * k]
        return [tuple(p[i:i + 3]) for i in range(0, len(p), 3)]

    def nearest(colors, entries):
        flat = [v for c in entries for v in c]
        ref = Image.new("P", (1, 1))
        ref.putpalette(flat + flat[:3] * (256 - len(entries)))
        src = Image.frombytes("RGB", (len(colors), 1), b"".join(bytes(c) for c in colors))
        got = src.quantize(palette=ref, dither=Image.Dither.NONE).convert("RGB").tobytes()
        return [tuple(got[i:i + 3]) for i in range(0, len(got), 3)]

    # MEDIANCUT follows pixel mass, so small but distinct regions (thin highlights, droplets)
    # lose their slots. Reserve slots for the best-covered colours far from every chosen entry.
    base, extra, candidates = cut(255 - reserve), [], list(counts)
    while candidates and len(extra) < reserve:
        mapped = nearest(candidates, base + extra)
        candidates = [c for c, m in zip(candidates, mapped)
                      if sum((x - y) ** 2 for x, y in zip(c, m)) > far * far]
        buckets = {}
        for c in candidates:
            b = buckets.setdefault((c[0] >> 4, c[1] >> 4, c[2] >> 4), [0, 0, c])
            b[0] += counts[c]
            if counts[c] > b[1]:
                b[1], b[2] = counts[c], c
        added = []
        for weight, _, colour in sorted(buckets.values(), reverse=True):
            if weight < min_pixels or len(extra) + len(added) >= reserve or len(added) >= 8:
                break
            if all(sum((x - y) ** 2 for x, y in zip(colour, a)) > far * far for a in added):
                added.append(colour)
        if not added:
            break
        extra += added
    return cut(255 - len(extra)) + extra


def gif_frames(images, threshold):
    # One shared palette avoids unrelated frame-by-frame palette flicker.
    colors = gif_palette(images, threshold)
    visible = [v for c in colors for v in c]
    # Unused slots and index 255 repeat entry 0; 255 is remapped to 0 below.
    pal = visible + visible[:3] * (256 - len(colors))
    palette = Image.new("P", (1, 1))
    palette.putpalette(pal)
    result = []
    for im in images:
        q = im.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE)
        # 255 is reserved even when the quantizer selected its duplicate RGB.
        q = q.point([*range(255), 0])
        q.putpalette(pal)
        q.paste(255, mask=im.getchannel("A").point(lambda v: 255 if v < threshold else 0))
        q.info["transparency"] = 255
        result.append(q)
    return result


def encoded_info(path):
    with Image.open(path) as im:
        count, duration = getattr(im, "n_frames", 1), 0
        for i in range(count):
            im.seek(i)
            im.load()
            duration += im.info.get("duration", 0)
        return {"encoded_frames": count, "duration_ms": duration, "size": list(im.size)}


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stdout


def video(dest, images, times, doc):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("mp4 requires ffmpeg and ffprobe on PATH")
    with tempfile.TemporaryDirectory(prefix="video-", dir=dest) as folder:
        work = Path(folder)
        lines = ["ffconcat version 1.0"]
        for i, (im, duration) in enumerate(zip(images, times)):
            name = f"v{i:04d}.png"
            bg = Image.new("RGBA", im.size, doc.get("video_background", "#ffffff"))
            bg.alpha_composite(im)
            bg.convert("RGB").save(work / name)
            lines.extend([f"file '{name}'", "option framerate 100", f"duration {duration / 1000:.3f}"])
        lines.extend([f"file 'v{len(images)-1:04d}.png'", "option framerate 100"])
        (work / "frames.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "concat", "-safe", "0", "-i", str(work / "frames.txt")]
        if doc.get("audio"):
            cmd += ["-i", str(dest / doc["audio"]), "-map", "0:v:0", "-map", "1:a:0", "-af", "apad", "-c:a", "aac"]
        cmd += ["-vf", "fps=100,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p", "-c:v", "libx264",
                "-threads", "2", "-t", f"{sum(times)/1000:.3f}", "-movflags", "+faststart", str(dest / "animation.mp4")]
        run(cmd)
    probe = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(dest / "animation.mp4")]))
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    actual = round(float(v["duration"]) * 1000)
    if abs(actual - sum(times)) > 11:
        raise ValueError("encoded video duration differs from timeline")
    return {"duration_ms": actual, "encoded_frames": int(v.get("nb_frames", 0)),
            "size": [v["width"], v["height"]], "audio": any(s["codec_type"] == "audio" for s in probe["streams"])}


def preview(dest, doc):
    # Only normalized local frame paths are embedded. Escape HTML script delimiters.
    data = json.dumps({"frames": doc["frames"], "loop": doc.get("loop", True)}, ensure_ascii=True).replace("<", "\\u003c")
    page = '''<!doctype html><meta charset="utf-8"><title>Motion preview</title>
<style>body{font:16px system-ui;margin:32px;background:#eee;color:#222}#stage{display:grid;place-items:center;min-height:360px;background:repeating-conic-gradient(#ddd 0% 25%,#fff 0% 50%) 0/24px 24px}img{max-width:100%;max-height:65vh}button,select,input{margin:8px}#label{font-variant-numeric:tabular-nums}</style>
<h1>Motion preview</h1><p>PNG timeline preview. Also check the exported animation in its target player.</p>
<button id="toggle">Pause</button><button id="prev">Previous</button><button id="next">Next</button>
<select id="speed"><option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option></select>
<select id="bg"><option value="checker">Checker</option><option value="white">White</option><option value="black">Black</option></select>
<div id="stage"><img id="picture"></div><input id="seek" type="range" min="0" value="0"><span id="label"></span>
<script>const data=DATA;const fs=data.frames;const pic=document.querySelector('#picture'),seek=document.querySelector('#seek'),label=document.querySelector('#label'),toggle=document.querySelector('#toggle');
fs.forEach(f=>{const i=new Image();i.src=f.path});let idx=0,playing=true,timer;seek.max=fs.length-1;
function draw(){pic.src=fs[idx].path;seek.value=idx;label.textContent=`${idx+1}/${fs.length} — ${fs[idx].duration_ms} ms — total ${fs.reduce((a,f)=>a+f.duration_ms,0)} ms — ${fs[idx].phase||''}`;toggle.textContent=playing?'Pause':'Play'}
function schedule(){clearTimeout(timer);draw();if(playing)timer=setTimeout(()=>{if(idx===fs.length-1&&!data.loop){playing=false;draw();return}idx=(idx+1)%fs.length;schedule()},fs[idx].duration_ms/Number(document.querySelector('#speed').value))}
toggle.onclick=()=>{playing=!playing;if(playing&&idx===fs.length-1)idx=0;schedule()};
function step(n){playing=false;idx=(idx+n+fs.length)%fs.length;schedule()}
document.querySelector('#prev').onclick=()=>step(-1);document.querySelector('#next').onclick=()=>step(1);seek.oninput=()=>{playing=false;idx=Number(seek.value);schedule()};
document.querySelector('#speed').onchange=schedule;document.querySelector('#bg').onchange=e=>{document.querySelector('#stage').style.background=e.target.value==='checker'?'':e.target.value};schedule();</script>'''
    (dest / "preview.html").write_text(page.replace("DATA", data), encoding="utf-8")


def build(manifest, out):
    doc, images, times, report = load(manifest)
    root = Path(manifest).resolve().parent
    def work(dest):
        (dest / "frames").mkdir()
        normalized = json.loads(json.dumps(doc))
        for i, im in enumerate(images):
            name = f"frames/frame-{i+1:04d}.png"
            im.save(dest / name)
            normalized["frames"][i]["path"] = name
        if doc.get("audio"):
            audio = source(root, doc["audio"])
            normalized["audio"] = "audio" + audio.suffix.lower()
            shutil.copy2(audio, dest / normalized["audio"])
        dump(dest / "motion.json", normalized)
        loop = {"loop": 0} if doc.get("loop", True) else {}
        for fmt in doc.get("formats", ["gif"]):
            if fmt == "gif":
                frames = gif_frames(images, doc.get("gif_alpha_threshold", 128))
                frames[0].save(dest / "animation.gif", save_all=True, append_images=frames[1:], duration=times,
                               transparency=255, background=255, disposal=2, optimize=False, **loop)
            elif fmt == "apng":
                images[0].save(dest / "animation.apng", format="PNG", save_all=True, append_images=images[1:],
                               duration=times, disposal=0, blend=0, loop=0 if doc.get("loop", True) else 1)
            elif fmt == "webp":
                if not features.check("webp"):
                    raise RuntimeError("Pillow build lacks WebP")
                images[0].save(dest / "animation.webp", save_all=True, append_images=images[1:], duration=times,
                               lossless=True, loop=0 if doc.get("loop", True) else 1)
            elif fmt == "sheet":
                cols = min(doc.get("sheet_columns", 4), len(images))
                atlas = Image.new("RGBA", (images[0].width * cols, images[0].height * math.ceil(len(images)/cols)))
                for i, im in enumerate(images):
                    atlas.paste(im, (i % cols * im.width, i // cols * im.height))
                atlas.save(dest / "sheet.png")
            elif fmt == "mp4":
                report["encoded"][fmt] = video(dest, images, times, normalized)
            if fmt in {"gif", "apng", "webp"}:
                info = encoded_info(dest / f"animation.{fmt}")
                # A single-image PNG/WebP has no animation duration; report it honestly.
                if info["encoded_frames"] == 1 and fmt in {"apng", "webp"} and info["duration_ms"] == 0:
                    report["warnings"].append(f"{fmt} encoded as a still; duration resides in manifest only.")
                elif abs(info["duration_ms"] - sum(times)) > 1:
                    raise ValueError(f"{fmt}: encoded duration differs from manifest")
                report["encoded"][fmt] = info
        preview(dest, normalized)
        dump(dest / "review.json", {"status": "pending", "identity": "not_reviewed", "motion_and_timing": "not_reviewed",
                                    "encoded_playback": "not_reviewed", "issues": [], "review_binding": report["review_binding"]})
        dump(dest / "report.json", report)
        if "zip" in doc.get("formats", []):
            with zipfile.ZipFile(dest / "frames.zip", "w", zipfile.ZIP_DEFLATED) as z:
                for p in sorted((dest / "frames").glob("*.png")):
                    z.write(p, p.relative_to(dest).as_posix())
                z.write(dest / "motion.json", "motion.json")
                if normalized.get("audio"):
                    z.write(dest / normalized["audio"], normalized["audio"])
        return report
    return transaction(out, work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    init = sub.add_parser("init"); init.add_argument("--out", type=Path, required=True)
    sp = sub.add_parser("split")
    sp.add_argument("--sheet", type=Path, required=True); sp.add_argument("--cols", type=int)
    sp.add_argument("--rows", type=int); sp.add_argument("--out", type=Path, required=True)
    sp.add_argument("--layout", type=Path)
    sp.add_argument("--margin", type=int, default=0); sp.add_argument("--gutter", type=int, default=0)
    sp.add_argument("--boxes", type=Path); sp.add_argument("--duration-ms", type=int, default=120)
    rt = sub.add_parser("retime")
    rt.add_argument("--manifest", type=Path, required=True); rt.add_argument("--out", type=Path, required=True)
    group = rt.add_mutually_exclusive_group(required=True)
    group.add_argument("--factor", type=float); group.add_argument("--total-ms", type=int)
    for name in ("inspect", "build"):
        p = sub.add_parser(name); p.add_argument("--manifest", type=Path, required=True)
        if name == "build": p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = {"python": sys.version.split()[0], "pillow": Image.__version__, "webp": features.check("webp"),
                      "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"), "generation": "host tool checked by agent"}
        elif args.command == "init":
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "name": "new-motion", "intent": "", "strategy": "sheet-repair",
                           "provider": {"tool": "built-in image_gen", "model": "host-managed/unknown"},
                           "references": [], "invariants": [], "motion_beats": [], "loop": True,
                           "require_transparency": True, "formats": ["gif", "sheet", "zip"],
                           "sheet_columns": 4, "frames": []}, f, indent=2)
            result = {"created": str(args.out), "state": "draft: populate frame paths and timing"}
        elif args.command == "split": result = split(args.sheet, args.cols, args.rows, args.out, args.margin, args.gutter, read(args.boxes) if args.boxes else None, args.duration_ms, args.layout)
        elif args.command == "retime": result = retime(args.manifest, args.out, args.factor, args.total_ms)
        elif args.command == "inspect": result = load(args.manifest)[3]
        else: result = build(args.manifest, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
