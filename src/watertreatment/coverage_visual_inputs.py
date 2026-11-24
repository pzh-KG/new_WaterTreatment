"""Bind the exact image occurrences required by a reading-ledger correction."""
import copy
import hashlib
import io
from pathlib import Path
from zipfile import ZipFile

from .rendering import word_occurrence_preview
from .storage import hash_file

POLICY = "coverage-registered-visual-inputs-v1"


def coverage_tool_limit(scope):
    entries = scope["required_dispositions"]
    views = sum(r["kind"] in {"image", "pdf_page"} for r in entries)
    return min(160, max(24, 24 + len(entries) // 10 + views))


def coverage_visual_bundle(parent, run, *, create):
    """Recover supported occurrence crops from hashed original ZIP parts. Recompute at admission."""
    required = {(x["source_id"], x["block_id"]) for x in parent.scope["required_dispositions"]
                if x["kind"] == "image"}
    return recover_word_occurrences(parent.bundle, parent.documents, required, run, create=create)


def recover_word_occurrences(bundle, documents, required, run, *, create, allow_unread=False):
    """Build a separate view of verified occurrences. The registered source bundle stays immutable."""
    from PIL import Image

    bundle = copy.deepcopy(bundle)
    if not required:
        return bundle
    data_root = Path(run).resolve().parent.parent
    available = set()
    parsed_parts = {}
    documents = {d.source.source_id: d for d in documents}
    for rendered in bundle.get("renderings", []):
        sid = rendered["source_id"]
        if not any(s == sid for s, _ in required):
            continue
        doc = documents[sid]
        blocks = {b.block_id: b for b in doc.blocks}
        for item in rendered.get("images", []):
            bid = item["block_id"]
            if (sid, bid) not in required:
                continue
            if item.get("path"):
                image = Path(item["path"]).resolve(strict=True)
                if not image.is_relative_to(data_root) or (item.get("sha256") and hash_file(image) != item["sha256"]):
                    raise ValueError("registered Word occurrence changed before reading-ledger correction")
                available.add((sid, bid))
                continue
            block = blocks[bid]
            meta = block.metadata
            crop = meta.get("crop", {})
            outset = (crop.get("unit") == "1/1000_percent" and any((crop.get(side) or 0) < 0
                      for side in ("left", "top", "right", "bottom")))
            if block.kind != "image" or not crop.get("specified") or (crop.get("unit") != "vml_raw" and not outset):
                continue
            original = Path(doc.source.path).resolve(strict=True)
            if not original.is_relative_to(data_root) or hash_file(original) != doc.source.sha256:
                raise ValueError("Word source for occurrence recovery has changed")
            with ZipFile(original) as archive:
                if outset:
                    from .sources import A, R, _paths, _read_member, _xml, drawingml_outset_fill
                    key = (sid, block.location.part)
                    if key not in parsed_parts:
                        paths, parents = _paths(_xml(_read_member(archive, block.location.part)))
                        parsed_parts[key] = ({path:node for node,path in paths.items()}, parents)
                    nodes, parents = parsed_parts[key]
                    ref = nodes.get(block.location.xml_path)
                    fill = parents.get(ref)
                    rect = fill.find(f"{{{A}}}srcRect") if fill is not None else None
                    if (ref is None or ref.tag != f"{{{A}}}blip" or ref.get(f"{{{R}}}embed") != meta["relationship_id"]
                            or rect is None or dict(rect.attrib) != crop["raw"]):
                        raise ValueError("signed Word crop does not match its exact original occurrence")
                    crop = {**crop, "outset_fill": drawingml_outset_fill(fill, crop)}
                part = archive.getinfo(meta["media_path"])
                if not 0 < part.file_size <= 64_000_000:
                    if allow_unread:
                        item["recovery_limitation"] = "Word media exceeds the registered size limit"
                        continue
                    raise ValueError("Word media exceeds the registered size limit")
                raw = archive.read(part)
            media_hash = hashlib.sha256(raw).hexdigest()
            if media_hash != meta["media_sha256"] or media_hash != item.get("source_media_sha256"):
                raise ValueError("Word occurrence and original media hash disagree")
            try:
                preview, bounds, adjustment = word_occurrence_preview(io.BytesIO(raw), crop)
            except (ValueError, OSError, NotImplementedError, Image.DecompressionBombError) as exc:
                if not allow_unread:
                    raise
                # Decoder errors can embed the address of the transient BytesIO stream.
                # Only stable source-derived facts belong to the replayed occurrence manifest.
                item["recovery_limitation"] = (f"Word raster decoder unavailable or failed ({type(exc).__name__})"
                                               if isinstance(exc, OSError) else str(exc))
                continue
            encoded = io.BytesIO()
            preview.save(encoded, format="PNG")
            content = encoded.getvalue()
            sha = hashlib.sha256(content).hexdigest()
            target = Path(run) / "coverage_visuals" / f"{doc.source.sha256}-{block.order}.png"
            if create:
                target.parent.mkdir(exist_ok=True)
                with target.open("xb") as f:
                    f.write(content)
            elif target.resolve() != target or hash_file(target) != sha:
                raise ValueError("recovered Word occurrence preview changed")
            item.pop("error", None)
            item.update(path=str(target), sha256=sha, crop_pixels=bounds, width=preview.width,
                        height=preview.height, status="rendered_unread", display_adjustment=adjustment)
            available.add((sid, bid))
    if required - available and not allow_unread:
        raise ValueError("阅读补正仍缺少可核验的 Word 图像预览，未启动模型：" + str(sorted(required - available)))
    return bundle
