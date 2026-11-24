"""Optional local rendering; a rendered image is still unread evidence."""

from __future__ import annotations

import importlib.metadata
import re
from fractions import Fraction
from pathlib import Path

from .storage import hash_file, write_new_json


def reading_canvas(original):
    """Composite alpha onto a documented white reading canvas, preserving media.

    Transparent scientific plots often contain black axes. Passing their alpha
    directly to an image consumer with a black canvas hides those axes. This is
    deterministic display composition, not reconstruction or generated content.
    It does not claim to reproduce Word's surrounding shapes or page layout.
    """
    from PIL import Image
    transparent = "A" in original.getbands() or "transparency" in original.info
    if not transparent:
        return original.convert("RGB"), {"alpha_composited": False, "background": "opaque source"}
    foreground = original.convert("RGBA")
    canvas = Image.new("RGBA", foreground.size, (255, 255, 255, 255))
    canvas.alpha_composite(foreground)
    return canvas.convert("RGB"), {"alpha_composited": True, "background": "#ffffff",
        "interpretation": "White reading canvas; original media unchanged, not a Word page-layout reconstruction."}


def prepare_reading_previews(image_manifest: list[dict], output_dir: Path) -> list[dict]:
    """Upgrade legacy transparent Word previews without mutating their bundle."""
    from PIL import Image
    result = []
    for entry in image_manifest:
        item = dict(entry)
        if item.get("block_id") and item.get("physical_page") is None:
            with Image.open(item["path"]) as original:
                if original.width * original.height > 40_000_000:
                    raise ValueError("Word reading preview exceeds pixel budget")
                if "A" in original.getbands() or "transparency" in original.info:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    image, adjustment = reading_canvas(original)
                    target = output_dir / f"attachment-{item['attachment_index']:04d}.png"
                    with target.open("xb") as stream:
                        image.save(stream, format="PNG")
                    item.update({"original_preview_path": item["path"], "path": str(target),
                                 "display_adjustment": adjustment, "sha256": hash_file(target)})
        result.append(item)
    return result


def render_pdf(path: Path, output_dir: Path, *, scale: float = 2.0) -> dict:
    try:
        import pypdfium2 as pdfium
    except ImportError as error:
        raise RuntimeError("PDF rendering requires the 'vision' optional dependencies") from error
    if not 0.5 <= scale <= 4:
        raise ValueError("scale must be between 0.5 and 4")
    output_dir.mkdir(parents=True, exist_ok=False)
    pages = []
    with pdfium.PdfDocument(str(path)) as document:
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
                if width * height * scale * scale > 40_000_000:
                    raise ValueError("page rendering exceeds pixel budget")
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    target = output_dir / f"page-{index + 1:04d}.png"
                    image.save(target)
                    pages.append({"physical_page": index + 1, "path": str(target), "sha256": hash_file(target), "width": image.width, "height": image.height, "status": "rendered_unread"})
                finally:
                    bitmap.close()
            finally:
                page.close()
    manifest = {"source_sha256": hash_file(path), "renderer": "pypdfium2", "version": importlib.metadata.version("pypdfium2"), "scale": scale, "pages": pages}
    write_new_json(output_dir / "render_manifest.json", manifest)
    return manifest


def word_occurrence_preview(media, crop):
    """Crop the exact embedded occurrence, preserving declared VML and DrawingML units."""
    from PIL import Image
    sides = ("left", "top", "right", "bottom")
    if crop.get("specified") and crop.get("unit") == "vml_raw":
        attrs = {k.rsplit("}", 1)[-1]: str(v).strip() for k, v in crop["raw"].items()}
        if not attrs or set(attrs) - {"crop" + side for side in sides}:
            raise ValueError("unsupported VML crop attributes")
        def fraction(side):
            raw = attrs.get("crop" + side, "0")
            if re.fullmatch(r"[+-]?\d+f", raw):
                return Fraction(int(raw[:-1]), 65536)
            if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)%?", raw):
                return Fraction(raw.rstrip("%")) / (100 if raw.endswith("%") else 1)
            raise ValueError("unsupported VML crop coordinate")
        fractions = [fraction(side) for side in sides]
    elif not crop.get("specified") or crop.get("unit") == "1/1000_percent":
        if crop.get("specified") and any(k in crop and crop[k] is None for k in sides):
            raise ValueError("incomplete DrawingML crop coordinate")
        fractions = [Fraction(int(crop.get(k) or 0), 100000) for k in sides]
    else:
        raise ValueError("unsupported Word crop coordinate system")
    left, top, right, bottom = fractions
    if left + right >= 1 or top + bottom >= 1:
        raise ValueError("crop yields an invalid source rectangle")
    if min(fractions) < 0 and (crop.get("unit") != "1/1000_percent" or crop.get("outset_fill") != "stretch"):
        raise ValueError("negative crop requires a verified non-tiled DrawingML stretch")

    with Image.open(media) as original:
        if original.width * original.height > 40_000_000:
            raise ValueError("Word image exceeds preview pixel budget")
        bounds = (round(original.width * left), round(original.height * top),
                  round(original.width * (1 - right)), round(original.height * (1 - bottom)))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise ValueError("rounded crop is empty")
        if not (max(0, bounds[0]) < min(original.width, bounds[2]) and
                max(0, bounds[1]) < min(original.height, bounds[3])):
            raise ValueError("crop contains no source pixels")
        if (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) > 40_000_000:
            raise ValueError("Word occurrence rectangle exceeds preview pixel budget")
        padded = (max(0, -bounds[0]), max(0, -bounds[1]),
                  max(0, bounds[2] - original.width), max(0, bounds[3] - original.height))
        # Pillow pads an RGBA crop with transparency. A documented white reading
        # canvas distinguishes the margin from original microscopy or plot pixels.
        source = original.convert("RGBA") if any(padded) else original
        preview, adjustment = reading_canvas(source.crop(bounds))
        if min(fractions) < 0:
            adjustment["source_outset_padding_ltrb_px"] = list(padded)
            adjustment["outset_fill"] = "stretch"
        return preview, list(bounds), adjustment


def render_word_images(document, output_dir: Path) -> dict:
    """Preview each DrawingML image occurrence, not just each shared media blob.

    The preview retains each occurrence's crop. Surrounding shape transforms and
    the complete Word page layout are outside this representation.
    """
    output_dir.mkdir(parents=True, exist_ok=False)
    images = []
    for block in document.blocks:
        media_path = block.metadata.get("image_output_path")
        if block.kind != "image" or not media_path:
            continue
        item = {"block_id": block.block_id, "source_id": document.source.source_id, "source_media_sha256": hash_file(Path(media_path)), "status": "unavailable", "limitations": ["Not a Word page render; surrounding layout and shape transforms are not reconstructed."]}
        try:
            preview, bounds, adjustment = word_occurrence_preview(media_path, block.metadata.get("crop", {}))
            target = output_dir / f"image-{block.order:06d}.png"
            preview.save(target)
            item.update({"path": str(target), "sha256": hash_file(target), "crop_pixels": bounds, "width": preview.width, "height": preview.height, "status": "rendered_unread", "display_adjustment": adjustment})
        except Exception as error:
            item["error"] = str(error)
        images.append(item)
    manifest = {"source_id": document.source.source_id, "renderer": "Pillow occurrence preview", "version": importlib.metadata.version("Pillow"), "images": images}
    write_new_json(output_dir / "render_manifest.json", manifest)
    return manifest

