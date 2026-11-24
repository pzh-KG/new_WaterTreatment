"""Resolve Word caption obligations from the source structure and exact figure witnesses."""
from __future__ import annotations

import re


LINKED_POLICY = "native-word-linked-objects-v2"
AMENDMENT = "native-word-linked-requirement-amendment-v2"
FIGURE = "native_word_figure_occurrence"
LABEL = re.compile(r"^\s*(figure|fig\.?|图|table|表)\s*([sS]?\s*\d+[a-zA-Z]?(?:[.-]\d+)*)(?![a-zA-Z0-9])", re.I)


def _label(text):
    match = LABEL.match(text)
    if not match:
        return None
    kind = "table" if match[1].lower() in {"table", "表"} else "figure"
    return kind, re.sub(r"\s+", "", match[2]).upper()


def _figure_id(text):
    value = re.sub(r"^(?:figure|fig\.?|图)\s*", "", str(text or "").strip(), flags=re.I)
    return re.sub(r"\s+", "", value).upper()


def figure_caption_for_occurrences(doc, occurrences, caption):
    """A separate literal caption may accompany viewed Word drawing occurrences."""
    if (doc.source.format != "docx" or caption.kind != "paragraph" or not occurrences
            or caption.location.table_index is not None or caption.metadata.get("is_current_text") is False):
        return False
    # Text boxes may precede the caption with panel letters in the same paragraph.
    text = re.sub(r"^(?:\s*\([a-zA-Z0-9]{1,3}\)\s*)+", "", caption.text.lstrip())
    label = _label(re.sub(r"^(figure|fig)\.\s*", r"\1 ", text, flags=re.I))
    if not label or label[0] != "figure":
        return False
    for image in occurrences:
        if (image.kind != "image" or image.location.part != caption.location.part
                or image.location.table_index is not None or image.metadata.get("is_current_text") is False):
            return False
        if image.metadata.get("parent_block_id") == caption.block_id:
            continue
        # The immediately following paragraph may caption an image-only drawing
        # paragraph. Other paragraphs, figures or tables break this connection.
        if caption.location.paragraph_index != (image.location.paragraph_index or 0) + 1:
            return False
        if image.order >= caption.order or any(
                b.kind != "image" or b.metadata.get("parent_block_id") != image.metadata.get("parent_block_id")
                for b in doc.blocks[image.order:caption.order - 1]):
            return False
    return True


def linked_structure(doc, target, anchors, registered, native_table, *, use_document_order=False):
    if doc.source.format != "docx" or target.kind != "paragraph" or target.location.table_index is not None:
        return None
    part = target.location.part
    if not part or target.metadata.get("is_current_text") is False:
        return None
    locators = [a.get("locator") or {} for a in anchors if a["block_id"] == target.block_id]
    figures = {_figure_id(loc["figure"]) for loc in locators if loc.get("figure")}
    label = _label(target.text)
    if label is None and len(figures) == 1:
        label = ("figure", next(iter(figures)))
    if not label or (figures and (label[0] != "figure" or figures != {label[1]})):
        return None
    blocks = doc.blocks
    captions = ([target] if _label(target.text) == label else
                [b for b in blocks if b.kind == "paragraph" and b.location.part == part
                 and b.location.table_index is None and b.metadata.get("is_current_text") is not False
                 and _label(b.text) == label])
    if len(captions) != 1:
        return None
    caption = captions[0]
    if target.block_id != caption.block_id:
        # A nearby paragraph may discuss this specific caption. Never traverse
        # intervening text, another figure or a table merely on a number match.
        lo, hi = sorted((target.order, caption.order))
        if any(b.kind != "paragraph" or b.text.strip() or b.location.part != part
               for b in blocks[lo:hi-1]):
            return None
    def blank(b):
        return (b.kind == "paragraph" and not b.text.strip() and b.location.part == part
                and b.location.table_index is None and not b.metadata.get("has_omml"))
    if label[0] == "table":
        if target.block_id != caption.block_id:
            return None
        table = next((b for b in blocks[caption.order:] if not blank(b)), None)
        if not table or table.kind != "table" or table.location.part != part:
            return None
        parent = native_table(table.block_id)
        if parent is None:
            return None
        return {**parent, "caption_block_id": caption.block_id,
                "block_ids": [caption.block_id, *parent["block_ids"]]}

    # Require independent agreement between an explicit figure anchor and the
    # actual adjacent registered image occurrence in this document part.
    witnesses = {a["block_id"] for a in anchors if _figure_id((a.get("locator") or {}).get("figure")) == label[1]}
    matching = [b for b in blocks if b.block_id in witnesses and b.kind == "image" and b.location.part == part]
    if not matching and use_document_order:
        if any(b.kind == "image" and b.block_id in witnesses for b in blocks):
            return None
        captions_in_part = [b for b in blocks if b.kind == "paragraph" and b.location.part == part
                            and b.metadata.get("is_current_text") is not False and _label(b.text) == label]
        if len(captions_in_part) != 1:
            return None
        parents = {b.block_id: b for b in blocks if b.kind == "paragraph"}
        adjacent = [b for b in blocks if b.kind == "image" and b.location.part == part
                    and figure_caption_for_occurrences(doc, [b], caption)]
        if len(adjacent) != 1:
            return None
        image = adjacent[0]
        parent = parents.get(image.metadata.get("parent_block_id"))
        if (parent is None or (parent != caption and parent.text.strip())
                or sum(b.kind == "image" and b.metadata.get("parent_block_id") == parent.block_id
                       for b in blocks) != 1):
            return None
        # A conflicting explicit image locator overrides typography or adjacency.
        if any(a["block_id"] == image.block_id and (a.get("locator") or {}).get("figure")
               and _figure_id(a["locator"]["figure"]) != label[1] for a in anchors):
            return None
        matching = [image]
    if len(matching) != 1:
        return None
    image = matching[0]
    lo, hi = sorted((image.order, caption.order))
    if (image.block_id not in registered or image.location.table_index is not None
            or image.metadata.get("is_current_text") is False
            or any(not blank(b) for b in blocks[lo:hi-1])):
        return None
    return {"representation": FIGURE, "part": part, "figure_label": label[1],
            "caption_block_id": caption.block_id, "occurrence_block_id": image.block_id,
            "block_ids": list(dict.fromkeys([target.block_id, caption.block_id])),
            "full_view_block_ids": [image.block_id], "visual_status": "registered_occurrence_and_separate_caption",
            "limitations": ["Word caption text and the full registered figure occurrence are separate source deliveries. No rendered Word page or panel-level reading is inferred."]}
