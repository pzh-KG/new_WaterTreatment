"""Row-addressed views of parsed Word tables with original block anchors."""
from __future__ import annotations

from itertools import islice
from xml.etree import ElementTree

from .sources import W


def _cell_layout(block):
    raw = block.metadata.get("properties_xml")
    if not raw:
        return {}
    try:
        properties = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return {"unparsed_properties": True}
    layout = {}
    for name in ("gridSpan", "hMerge", "vMerge"):
        element = properties.find(f"{{{W}}}{name}")
        if element is not None:
            layout[name] = element.get(f"{{{W}}}val")
    return layout


def _context(blocks, part):
    context = []
    for block in islice(blocks, 6):
        if block.location.part != part or block.location.table_index is not None:
            break
        if block.kind == "paragraph" and block.text.strip():
            context.append({"block_id": block.block_id, "text": block.text})
            if len(context) == 2:
                break
    return context


def table_rows(doc, table_id, first_row, row_count):
    table = next((b for b in doc.blocks if b.block_id == table_id and b.kind == "table"), None)
    if doc.source.format != "docx" or table is None:
        raise ValueError("Use a native Word table block_id from the selected source.")
    total = table.metadata.get("row_count")
    if type(total) is not int or total < 0 or first_row > max(1, total):
        raise ValueError("Row selection exceeds the original table.")
    last = min(total, first_row + row_count - 1)
    rows = {row: [] for row in range(first_row, last + 1)}
    if total:
        rows.setdefault(1, [])  # Keep the first physical row as labelled context on later pages.
    start = table.order - 1
    end = start + 1
    cells = {}
    nested = []
    prefix = (table.location.xml_path or "") + "/"
    for block in islice(doc.blocks, start + 1, None):
        loc = block.location
        if loc.part != table.location.part or loc.table_index is None:
            break
        if loc.table_index != table.location.table_index:
            if loc.xml_path and table.location.xml_path and loc.xml_path.startswith(prefix):
                if block.kind == "table":
                    nested.append(block.block_id)
                end = block.order
                continue
            break
        end = block.order
        if loc.row_index not in rows:
            continue
        key = (loc.row_index, loc.cell_index)
        if block.kind == "table_cell":
            cell = {"cell": loc.cell_index, "block_id": block.block_id, "content": []}
            layout = _cell_layout(block)
            if layout:
                cell["layout"] = layout
            cells[key] = cell
            rows[loc.row_index].append(cell)
        elif key in cells:
            item = {"block_id": block.block_id, "kind": block.kind, "text": block.text}
            for field in ("text_is_lossy_projection", "is_current_text", "revision_states", "content_role"):
                if field in block.metadata:
                    item[field] = block.metadata[field]
            cells[key]["content"].append(item)
        else:
            raise ValueError("Unresolved cell ownership. Retrieve the original ordered blocks.")
    return {
        "source_id": doc.source.source_id, "source_sha256": doc.source.sha256,
        "table_block_id": table_id, "part": table.location.part, "table_index": table.location.table_index,
        "row_count": total, "first_row": first_row, "last_row": last,
        "next_row": last + 1 if last < total else None,
        "rows": [{"row": row, "cells": rows[row]} for row in sorted(rows)],
        "nearby_before": list(reversed(_context(reversed(doc.blocks[:start]), table.location.part))),
        "nearby_after": _context(islice(doc.blocks, end, None), table.location.part),
        "nested_table_block_ids": nested,
        "interpretation": "Physical XML rows and cells. The first row is also included on later pages. "
                          "Inspect additional header rows when applicable. Nearby paragraphs may be captions or unrelated. "
                          "Merged and empty cells have no automatically inherited value. Equation text is a projection; "
                          "check its raw block XML. Inspect image occurrences with wt_view. Distinguish cited benchmarks "
                          "from this paper's experiments. This table view does not certify reading or scientific support.",
    }
