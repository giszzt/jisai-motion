#!/usr/bin/env python3
"""Labnana GPT-Image-2.5 adapter for jisai-motion.

Fallback backend for agents without a built-in image tool. Transparency on this
route is produced by chroma keying, not by the model: Labnana's imageConfig is a
strict allowlist of imageSize / aspectRatio / quality, so OpenAI's
`background: "transparent"` and `output_format` never reach the model. Asked for
transparency in words alone, GPT-Image-2.5 paints a checkerboard into the pixels.
So this script forces a flat chroma backdrop into the prompt, keys it out locally
and verifies the resulting alpha. A painted checkerboard is detected and refused,
never relabelled as transparent.

Subcommands: precheck, generate, key.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_keys  # noqa: E402

BASE_URL_DEFAULT = "https://api.labnana.com"
IMAGE_ENDPOINT = "/openapi/v1/images/generation"
SUBSCRIPTION_ENDPOINT = "/openapi/v1/user/subscription"

# Verified live 2026-09-12. Labnana's public guide documents only gpt-image-2;
# both 2.5 variants answer on the same endpoint with provider "openai".
MODELS = {
    "flare": "gpt-image-2.5-flare",
    "sunburst": "gpt-image-2.5-sunburst",
    "gpt-image-2": "gpt-image-2",
}
DEFAULT_MODEL = "flare"

# Reported verbatim by the backend when an unsupported ratio is sent (code 26019).
ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
IMAGE_SIZES = ("1K", "2K", "4K")
QUALITIES = ("low", "medium", "high")  # OpenAI's xhigh/max are rejected by Labnana
MAX_REFERENCES = 4
MAX_BODY_BYTES = 20 * 1024 * 1024
REFERENCE_BUDGET = 17 * 1024 * 1024

RETRYABLE_CODES = frozenset({29998})          # rate limit, provider advises ~20s backoff
UNSTABLE_CODES = frozenset({91002})           # safety review; same prompt often passes on retry
FATAL_CODES = frozenset({21007, 26004})       # bad key, no credit
PARAMETER_CODES = frozenset({29003, 26019})   # bad params, unknown model, unsupported ratio
RETRY_BASE_SECONDS = 20

REFERENCE_ROLES = ("identity", "style", "motion", "composition", "edit_target", "supporting_insert")

# Saturated, far from common subject palettes. Order is the auto-pick preference.
KEY_CANDIDATES = (
    ("#00FF00", "pure green"),
    ("#FF00FF", "pure magenta"),
    ("#00FFFF", "pure cyan"),
    ("#0000FF", "pure blue"),
    ("#FFFF00", "pure yellow"),
)
KEY_ALPHA_LOW = 24     # chebyshev distance at or below this is fully transparent
KEY_ALPHA_HIGH = 48    # at or above this is fully opaque; between is a soft edge band
KEY_MIN_BACKGROUND = 0.08   # below this the "backdrop" is not a backdrop
KEY_MAX_BACKGROUND = 0.97   # above this the subject is missing
RING_WIDTH = 8


class ApiError(RuntimeError):
    def __init__(self, status: int | None, code: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- colour


def parse_hex(value: str) -> tuple[int, int, int]:
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise ValueError(f"{value!r} is not a #RRGGBB colour")
    return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % rgb


def chebyshev(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


def pixels_rgb(image: Image.Image) -> list[tuple[int, int, int]]:
    """Pixel list without Pillow's deprecated getdata()."""
    raw = image.convert("RGB").tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


def pixels_rgba(image: Image.Image) -> list[tuple[int, int, int, int]]:
    raw = image.convert("RGBA").tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2], raw[i + 3]) for i in range(0, len(raw), 4)]


def dominant_colours(path: Path, share: float = 0.04) -> list[tuple[int, int, int]]:
    """Colours covering at least `share` of a reference, quantised to 32 levels."""
    with Image.open(path) as opened:
        small = opened.convert("RGB").resize((64, 64))
    counts: dict[tuple[int, int, int], int] = {}
    for px in pixels_rgb(small):
        bucket = tuple(v // 32 * 32 + 16 for v in px)
        counts[bucket] = counts.get(bucket, 0) + 1  # type: ignore[index]
    total = 64 * 64
    return [c for c, n in counts.items() if n / total >= share]


def pick_key_colour(avoid: list[tuple[int, int, int]]) -> tuple[tuple[int, int, int], str, int]:
    """Choose the candidate furthest from every colour the subject is known to use.

    A key colour that collides with a subject colour silently eats part of the
    subject, so the margin is returned and reported rather than assumed safe.
    """
    best = None
    for hex_value, name in KEY_CANDIDATES:
        rgb = parse_hex(hex_value)
        margin = min((chebyshev(rgb, other) for other in avoid), default=255)
        if best is None or margin > best[2]:
            best = (rgb, name, margin)
    assert best is not None
    return best


# --------------------------------------------------------------------------- keying


def channel_distance(image: Image.Image, key: tuple[int, int, int]) -> Image.Image:
    """Per-pixel Chebyshev distance to `key` as an L band."""
    rgb = image.convert("RGB")
    solid = Image.new("RGB", rgb.size, key)
    diff = ImageChops.difference(rgb, solid)
    r, g, b = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def ring_pixels(image: Image.Image, width: int = RING_WIDTH) -> list[tuple[int, int, int]]:
    rgb = image.convert("RGB")
    w, h = rgb.size
    width = max(1, min(width, w // 2, h // 2))
    out: list[tuple[int, int, int]] = []
    for box in ((0, 0, w, width), (0, h - width, w, h), (0, 0, width, h), (w - width, 0, w, h)):
        out.extend(pixels_rgb(rgb.crop(box)))
    return out


def looks_like_painted_checkerboard(samples: list[tuple[int, int, int]]) -> bool:
    """Two near-neutral tones dominating the border ring is the fake-alpha pattern."""
    if not samples:
        return False
    counts: dict[tuple[int, int, int], int] = {}
    neutral = 0
    for px in samples:
        if max(px) - min(px) <= 24 and min(px) >= 170:
            neutral += 1
        bucket = tuple(v // 16 * 16 for v in px)
        counts[bucket] = counts.get(bucket, 0) + 1  # type: ignore[index]
    if neutral / len(samples) < 0.85:
        return False
    top = sorted(counts.values(), reverse=True)[:2]
    return len(top) == 2 and sum(top) / len(samples) >= 0.6


def key_out(
    image: Image.Image,
    key: tuple[int, int, int] | None,
    low: int = KEY_ALPHA_LOW,
    high: int = KEY_ALPHA_HIGH,
) -> tuple[Image.Image, dict[str, Any]]:
    """Replace the flat backdrop with real alpha and measure what happened."""
    if low >= high:
        raise ValueError("--key-low must be smaller than --key-high")
    rgb = image.convert("RGB")
    ring = ring_pixels(rgb)
    measured: tuple[int, int, int] = tuple(  # type: ignore[assignment]
        sorted(px[i] for px in ring)[len(ring) // 2] for i in range(3)
    )
    checkerboard = looks_like_painted_checkerboard(ring)
    target = key if key is not None else measured  # type: ignore[arg-type]

    distance = channel_distance(rgb, target)  # type: ignore[arg-type]
    lut = [0 if d <= low else 255 if d >= high else round(255 * (d - low) / (high - low)) for d in range(256)]
    alpha = distance.point(lut)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)

    hist = alpha.histogram()
    total = rgb.width * rgb.height
    transparent = hist[0] / total
    opaque = hist[255] / total
    hard = alpha.point([255 if v == 255 else 0 for v in range(256)])
    bbox = hard.getbbox()
    # Measure over everything that survived keying, partial alpha included: a key
    # colliding with the subject dissolves it into the soft band rather than
    # leaving fully opaque pixels behind.
    kept = rgb.convert("RGBA")
    kept.putalpha(alpha)
    spill = 0.0
    small = kept.resize((min(rgb.width, 256), min(rgb.height, 256)), Image.NEAREST)
    pixels = [px for px in pixels_rgba(small) if px[3] > 0]
    if pixels:
        near = sum(1 for px in pixels if chebyshev(px[:3], target) < high * 2)  # type: ignore[arg-type]
        spill = near / len(pixels)

    report = {
        "requested_key": to_hex(key) if key else None,
        "measured_border_key": to_hex(measured),  # type: ignore[arg-type]
        "key_drift": chebyshev(measured, key) if key else 0,  # type: ignore[arg-type]
        "thresholds": {"low": low, "high": high},
        "transparent_fraction": round(transparent, 4),
        "opaque_fraction": round(opaque, 4),
        "soft_edge_fraction": round(1 - transparent - opaque, 4),
        "subject_bbox": list(bbox) if bbox else None,
        "key_spill_fraction": round(spill, 4),
        "painted_checkerboard": checkerboard,
        "status": "ok",
        "issues": [],
    }
    issues: list[str] = report["issues"]  # type: ignore[assignment]
    if checkerboard:
        issues.append(
            "border ring is a two-tone near-neutral pattern: the model painted a fake "
            "transparency checkerboard instead of the requested flat chroma backdrop. "
            "Regenerate with the chroma clause, do not key this image."
        )
    if key is not None and report["key_drift"] > high * 2:
        issues.append(
            f"border colour {report['measured_border_key']} is {report['key_drift']} away from the "
            f"requested {report['requested_key']}; the backdrop is not the key colour that was asked for."
        )
    if transparent < KEY_MIN_BACKGROUND:
        issues.append(
            f"only {transparent:.1%} of pixels keyed out; there is no flat backdrop to remove."
        )
    if transparent > KEY_MAX_BACKGROUND:
        issues.append(f"{transparent:.1%} of pixels keyed out; the subject was keyed away too.")
    if report["soft_edge_fraction"] > 0.05:
        issues.append(
            f"{report['soft_edge_fraction']:.1%} of pixels came out partially transparent; a soft band "
            "that large means the key is dissolving the subject, not tracing its edge."
        )
    if spill > 0.02:
        issues.append(
            f"{spill:.1%} of subject pixels sit near the key colour; the key collides with the "
            "subject palette. Regenerate with a different --key-color."
        )
    if issues:
        report["status"] = "fail"
    return out, report


def cell_uniformity(image: Image.Image, key: tuple[int, int, int], cols: int, rows: int) -> dict[str, Any]:
    """Per-cell keyed fraction. A sheet whose cells key unevenly splits badly."""
    w, h = image.size
    if cols < 1 or rows < 1:
        raise ValueError("--cells must be COLSxROWS with positive integers")
    if w % cols or h % rows:
        return {"divisible": False, "cells": [], "spread": None}
    distance = channel_distance(image, key)
    mask = distance.point([255 if d > KEY_ALPHA_HIGH else 0 for d in range(256)])
    fractions = []
    cw, ch = w // cols, h // rows
    for r in range(rows):
        for c in range(cols):
            cell = mask.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
            hist = cell.histogram()
            fractions.append(round(hist[0] / (cw * ch), 4))
    return {
        "divisible": True,
        "cells": fractions,
        "spread": round(max(fractions) - min(fractions), 4),
    }


# --------------------------------------------------------------------------- prompt


def chroma_clause(key: tuple[int, int, int], name: str, cells: tuple[int, int] | None) -> str:
    same_cell = (
        f" The backdrop must be the identical colour in all {cells[0] * cells[1]} cells, with no cell "
        "dividers, borders, numbers or text."
        if cells
        else ""
    )
    return (
        f"Background: every pixel outside the subject is one completely flat solid chroma-key "
        f"backdrop, {name} hex {to_hex(key)}, uniform across the whole canvas.{same_cell} "
        "No checkerboard pattern, no simulated transparency, no gradient, no vignette, no cast "
        "shadow, no ground plane, no texture, no white or grey backdrop. "
        f"The subject itself must contain no {name} and must not blend into the backdrop; keep its "
        "own materials, colours and shading unchanged."
    )


def build_prompt(text: str, transparent: bool, key, name, cells) -> str:
    body = text.strip()
    if not body:
        raise ValueError("prompt is empty")
    if cells:
        body += (f"\nLayout: exactly {cells[0]} columns and {cells[1]} rows, equal cells in row-major time order. "
                 "Keep the complete motion envelope, limbs, hair, props, detached objects and effects "
                 "inside each cell with at least 8 percent clear space on all four sides. "
                 "No content may cross or touch a cell boundary. Keep one shared camera and subject scale; "
                 "do not individually fit or resize each pose. No grid lines or labels.")
    if not transparent:
        return body
    return body + "\n" + chroma_clause(key, name, cells)


# --------------------------------------------------------------------------- request


def snap_aspect(value: str) -> tuple[str, bool]:
    text = str(value).strip().replace("：", ":").lower().replace("x", ":").replace("/", ":")
    if text in {r.lower() for r in ASPECT_RATIOS}:
        return next(r for r in ASPECT_RATIOS if r.lower() == text), False
    try:
        left, _, right = text.partition(":")
        wanted = float(left) / float(right)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"{value!r} is not a ratio; supported: {', '.join(ASPECT_RATIOS)}") from None
    import math

    best = min(
        ASPECT_RATIOS,
        key=lambda r: abs(math.log(wanted) - math.log(int(r.split(":")[0]) / int(r.split(":")[1]))),
    )
    left_best, right_best = (int(v) for v in best.split(":"))
    # "1024x1024" is exactly 1:1; nothing was given up, so do not report a snap.
    return best, abs(wanted - left_best / right_best) > 1e-6


def inline_reference(path: Path, budget: int) -> tuple[dict[str, Any], str | None]:
    raw = path.read_bytes()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/png"
    )
    note = None
    if len(raw) * 4 // 3 > budget:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        width, height = image.size
        for _ in range(6):
            width, height = max(640, int(width * 0.75)), max(640, int(height * 0.75))
            buffer = io.BytesIO()
            image.resize((width, height)).save(buffer, format="JPEG", quality=90)
            raw = buffer.getvalue()
            if len(raw) * 4 // 3 <= budget:
                mime = "image/jpeg"
                note = f"{path.name} downscaled to {width}x{height} JPEG to fit the 20 MB request body."
                break
        else:
            raise RuntimeError(f"{path.name} stays above the request body budget after six passes.")
    return {"inlineData": {"data": base64.b64encode(raw).decode("ascii"), "mimeType": mime}}, note


def post(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['LABNANA_API_KEY']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        code, message = None, raw[:400]
        try:
            parsed = json.loads(raw)
            code = parsed.get("code")
            message = parsed.get("message") or message
        except json.JSONDecodeError:
            pass
        raise ApiError(exc.code, code, message) from None


def extract_image(body: dict[str, Any]) -> tuple[bytes, str]:
    for candidate in body.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            data = part.get("inlineData")
            if data and data.get("data"):
                return base64.b64decode(data["data"]), data.get("mimeType", "image/png")
    raise RuntimeError(f"response carried no image: {json.dumps(body)[:400]}")


def call_labnana(prompt: str, references, args) -> tuple[Image.Image, dict[str, Any]]:
    model = MODELS[args.model]
    if len(references) > MAX_REFERENCES:
        raise RuntimeError(
            f"{model} accepts at most {MAX_REFERENCES} reference images but {len(references)} were "
            "supplied. Drop the lowest-value ones; keep the identity anchor."
        )
    aspect, snapped = snap_aspect(args.aspect_ratio)
    if snapped and getattr(args, "cells", None):
        raise ValueError("sheet aspect ratio is unsupported; re-plan rows/columns before generation instead of snapping the layout")
    notes: list[str] = []
    if snapped:
        notes.append(
            f"aspectRatio {args.aspect_ratio} is outside the GPT route's table; snapped to {aspect}. "
            "Re-plan the sheet layout for the ratio that was actually used."
        )

    payload_references = []
    for path, _role in references:
        item, note = inline_reference(path, REFERENCE_BUDGET // max(1, len(references)))
        payload_references.append(item)
        if note:
            notes.append(note)

    image_config = {"aspectRatio": aspect, "imageSize": args.image_size, "quality": args.quality}
    payload: dict[str, Any] = {
        "provider": "openai",
        "model": model,
        "prompt": prompt,
        "imageConfig": image_config,
    }
    if payload_references:
        payload["referenceImages"] = payload_references
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise RuntimeError(f"request body is {size // 1048576} MB; Labnana caps it at 20 MB.")

    base = os.environ.get("LABNANA_BASE_URL", BASE_URL_DEFAULT).rstrip("/")
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, args.max_retries + 1):
        started = time.time()
        try:
            body = post(base + IMAGE_ENDPOINT, payload, args.timeout)
        except ApiError as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "http_status": exc.status,
                    "code": exc.code,
                    "message": exc.message,
                    "seconds": round(time.time() - started, 1),
                }
            )
            if exc.code in FATAL_CODES or exc.status in (401, 403):
                raise RuntimeError(
                    f"Labnana account failure (code {exc.code}): {exc.message}. Fix the key or credit "
                    "before retrying; do not fall through to another paid backend silently."
                ) from None
            if exc.code in PARAMETER_CODES and attempt >= 1:
                hint = ""
                if exc.code == 29003 and "generation failed" in exc.message.lower():
                    hint = (
                        f" A generic 'AI content generation failed' within a second usually means the "
                        f"model id is unknown to Labnana. Known ids: {', '.join(sorted(MODELS.values()))}."
                    )
                raise RuntimeError(f"Labnana rejected the request (code {exc.code}): {exc.message}.{hint}") from None
            if exc.code in RETRYABLE_CODES or exc.code in UNSTABLE_CODES or (exc.status or 0) >= 500:
                if attempt < args.max_retries:
                    time.sleep(min(60, RETRY_BASE_SECONDS * attempt) if exc.code in RETRYABLE_CODES else 3)
                    continue
            raise RuntimeError(f"Labnana failed after {attempt} attempts: {exc.message}") from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "http_status": None,
                    "code": None,
                    "message": repr(exc)[:300],
                    "seconds": round(time.time() - started, 1),
                }
            )
            if attempt < args.max_retries:
                time.sleep(3)
                continue
            raise RuntimeError(f"Labnana transport failure after {attempt} attempts: {exc!r}") from None

        raw, mime = extract_image(body)
        attempts.append(
            {
                "attempt": attempt,
                "http_status": 200,
                "code": 0,
                "message": "ok",
                "seconds": round(time.time() - started, 1),
            }
        )
        image = Image.open(io.BytesIO(raw))
        image.load()
        meta = {
            "backend": "labnana",
            "model": model,
            "provider_mode": "external-image-api",
            "requested_aspect_ratio": args.aspect_ratio,
            "sent_aspect_ratio": aspect,
            "aspect_source": "snapped" if snapped else "exact",
            "image_size": args.image_size,
            "quality": args.quality,
            "returned_mime": mime,
            "returned_size": list(image.size),
            "returned_mode": image.mode,
            "usage": body.get("usageMetadata", {}),
            "attempts": attempts,
            "notes": notes,
        }
        return image, meta
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- commands


def parse_reference(value: str) -> tuple[Path, str]:
    path_text, _, role = value.partition("::")
    role = role or "identity"
    if role not in REFERENCE_ROLES:
        raise ValueError(f"role {role!r} must be one of {', '.join(REFERENCE_ROLES)}")
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise ValueError(f"reference {path} does not exist")
    return path.resolve(), role


def cmd_precheck(args) -> dict[str, Any]:
    api_keys.load()
    available = bool(os.environ.get("LABNANA_API_KEY"))
    account: Any = "not checked"
    if available and not args.offline:
        base = os.environ.get("LABNANA_BASE_URL", BASE_URL_DEFAULT).rstrip("/")
        request = urllib.request.Request(
            base + SUBSCRIPTION_ENDPOINT,
            headers={"Authorization": f"Bearer {os.environ['LABNANA_API_KEY']}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8")).get("data", {})
            account = {
                "total_available_credits": data.get("totalAvailableCredits"),
                "plan": (data.get("subscriptionPlan") or {}).get("name"),
                "free_usages": {k: v.get("remaining") for k, v in (data.get("freeUsages") or {}).items()},
            }
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            account = {"error": repr(exc)[:200]}
    return {
        "backend": "labnana",
        "available": available,
        "env_files_checked": [str(p) for p in api_keys.CANDIDATES],
        "models": MODELS,
        "default_model": DEFAULT_MODEL,
        "aspect_ratios": list(ASPECT_RATIOS),
        "image_sizes": list(IMAGE_SIZES),
        "qualities": list(QUALITIES),
        "max_references": MAX_REFERENCES,
        "transparency": "chroma-key only; Labnana exposes no background/output_format parameter",
        "key_candidates": [h for h, _ in KEY_CANDIDATES],
        "account": account,
        "note": "A built-in image tool, when the host has one, still outranks this backend.",
    }


def resolve_image_size(requested, cells):
    if requested != "auto":
        if requested not in IMAGE_SIZES:
            raise ValueError("unsupported image size")
        return requested
    return "4K" if cells and cells[0] * cells[1] >= 16 else "2K"


def cmd_generate(args) -> dict[str, Any]:
    api_keys.load()
    if not os.environ.get("LABNANA_API_KEY"):
        raise RuntimeError(
            "LABNANA_API_KEY is not set. Put it in one of: "
            + ", ".join(str(p) for p in api_keys.CANDIDATES)
        )
    if args.out.exists():
        raise RuntimeError(f"{args.out} already exists; pick a new path instead of overwriting frames.")
    text = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else (args.prompt or "")
    references = [parse_reference(v) for v in (args.reference or [])]

    cells = None
    if args.cells:
        cols, _, rows = args.cells.lower().partition("x")
        cells = (int(cols), int(rows))
        if min(cells) < 1 or cells[0] * cells[1] > 240:
            raise ValueError("cells must contain 1..240 frames")
    requested_size = args.image_size
    args.image_size = resolve_image_size(requested_size, cells)

    transparent = not args.opaque
    key_rgb: tuple[int, int, int] | None = None
    key_name = ""
    key_margin = None
    if transparent:
        if args.key_color and args.key_color != "auto":
            key_rgb = parse_hex(args.key_color)
            key_name = next((n for h, n in KEY_CANDIDATES if parse_hex(h) == key_rgb), "chroma-key")
            avoid = [parse_hex(c) for c in (args.avoid_color or [])]
            for path, _role in references:
                avoid.extend(dominant_colours(path))
            key_margin = min((chebyshev(key_rgb, other) for other in avoid), default=255)
        else:
            avoid = [parse_hex(c) for c in (args.avoid_color or [])]
            for path, _role in references:
                avoid.extend(dominant_colours(path))
            key_rgb, key_name, key_margin = pick_key_colour(avoid)

    prompt = build_prompt(text, transparent, key_rgb, key_name, cells)
    image, meta = call_labnana(prompt, references, args)
    meta["image_size_policy"] = "automatic-frame-count" if requested_size == "auto" else "explicit"
    meta["resolution_review"] = "pending"
    if cells:
        meta["nominal_cell_size"] = [image.width / cells[0], image.height / cells[1]]
        meta["estimated_safe_cell_size"] = [image.width / cells[0] * .84, image.height / cells[1] * .84]
        meta["layout_review"] = "pending"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    keying: dict[str, Any] | None = None
    grid: dict[str, Any] | None = None
    if transparent:
        keyed, keying = key_out(image, key_rgb, args.key_low, args.key_high)
        if keying["status"] == "fail" and not args.save_failed:
            raw_path = args.out.with_name(args.out.stem + "-raw" + args.out.suffix)
            image.convert("RGB").save(raw_path)
            return {
                "ok": False,
                "status": "keying_failed",
                "raw_image": str(raw_path),
                "keyed_image": None,
                "provider": meta,
                "keying": keying,
                "key_margin": key_margin,
                "next_step": "Regenerate; do not treat the raw image as a transparent frame.",
            }
        if cells and key_rgb:
            grid = cell_uniformity(image, key_rgb, *cells)
        keyed.save(args.out)
        if args.keep_raw:
            image.convert("RGB").save(args.out.with_name(args.out.stem + "-raw" + args.out.suffix))
    else:
        image.convert("RGB").save(args.out)

    record = {
        "ok": True,
        "status": "ok" if not keying or keying["status"] == "ok" else "keying_failed",
        "output": str(args.out.resolve()),
        "transparency": {
            "requested": transparent,
            "mechanism": "chroma-key + local alpha extraction" if transparent else "opaque",
            "key_color": to_hex(key_rgb) if key_rgb else None,
            "key_margin_to_subject_palette": key_margin,
            "verified": bool(keying and keying["status"] == "ok"),
        },
        "keying": keying,
        "grid": grid,
        "provider": meta,
        "prompt": {
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            "chars": len(prompt),
            "chroma_clause_appended": transparent,
            "source": str(args.prompt_file.resolve()) if args.prompt_file else "inline",
        },
        "references": [
            {"path": str(p), "role": r, "binding_mechanism": "reference_images_payload", "bound": True}
            for p, r in references
        ],
    }
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def cmd_key(args) -> dict[str, Any]:
    if args.out.exists():
        raise RuntimeError(f"{args.out} already exists; pick a new path.")
    with Image.open(args.image) as opened:
        image = opened.convert("RGB")
    key_rgb = None if args.key_color in (None, "auto") else parse_hex(args.key_color)
    keyed, keying = key_out(image, key_rgb, args.key_low, args.key_high)
    grid = None
    if args.cells:
        cols, _, rows = args.cells.lower().partition("x")
        measured = key_rgb or parse_hex(keying["measured_border_key"])
        grid = cell_uniformity(image, measured, int(cols), int(rows))
    if keying["status"] == "fail" and not args.save_failed:
        return {"ok": False, "status": "keying_failed", "keying": keying, "grid": grid, "output": None}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    keyed.save(args.out)
    return {
        "ok": True,
        "status": keying["status"],
        "output": str(args.out.resolve()),
        "keying": keying,
        "grid": grid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("precheck", help="Report backend availability, models and limits.")
    pre.add_argument("--offline", action="store_true", help="Skip the account/credit lookup.")

    gen = sub.add_parser("generate", help="Generate one image, chroma-keyed to real alpha by default.")
    gen.add_argument("--prompt", help="Prompt text.")
    gen.add_argument("--prompt-file", type=Path, help="Prompt file (preferred; avoids shell quoting).")
    gen.add_argument("--out", type=Path, required=True, help="Output PNG path; must not exist.")
    gen.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL,
                     help="flare is the default; sunburst for finer detail and edit precision.")
    gen.add_argument("--reference", action="append", metavar="PATH[::ROLE]",
                     help=f"Reference image; ROLE one of {', '.join(REFERENCE_ROLES)} (default identity).")
    gen.add_argument("--aspect-ratio", default="1:1")
    gen.add_argument("--image-size", choices=("auto",) + IMAGE_SIZES, default="auto")
    gen.add_argument("--quality", choices=QUALITIES, default="high")
    gen.add_argument("--cells", metavar="COLSxROWS", help="Sheet layout; adds the same-backdrop clause and checks cells.")
    gen.add_argument("--opaque", action="store_true", help="Keep the generated background; skip keying.")
    gen.add_argument("--key-color", default="auto", help="auto, or #RRGGBB.")
    gen.add_argument("--avoid-color", action="append", metavar="#RRGGBB", help="Subject colour the key must avoid.")
    gen.add_argument("--key-low", type=int, default=KEY_ALPHA_LOW)
    gen.add_argument("--key-high", type=int, default=KEY_ALPHA_HIGH)
    gen.add_argument("--keep-raw", action="store_true", help="Also save the pre-keying image.")
    gen.add_argument("--save-failed", action="store_true", help="Write the keyed PNG even when checks fail.")
    gen.add_argument("--record", type=Path, help="Where to write the generation record.")
    gen.add_argument("--timeout", type=int, default=420)
    gen.add_argument("--max-retries", type=int, default=3)

    key = sub.add_parser("key", help="Key an already-generated flat-backdrop image to real alpha.")
    key.add_argument("--image", type=Path, required=True)
    key.add_argument("--out", type=Path, required=True)
    key.add_argument("--key-color", default="auto")
    key.add_argument("--key-low", type=int, default=KEY_ALPHA_LOW)
    key.add_argument("--key-high", type=int, default=KEY_ALPHA_HIGH)
    key.add_argument("--cells", metavar="COLSxROWS")
    key.add_argument("--save-failed", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "precheck":
            result = cmd_precheck(args)
        elif args.command == "generate":
            result = cmd_generate(args)
        else:
            result = cmd_key(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 3
    except (ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
