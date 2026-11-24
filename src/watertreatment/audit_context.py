"""Source navigation for bounded rechecks; originals remain available via tools."""
from __future__ import annotations

import json


def bounded_source_context(run, scope):
    from .workflow import source_acquisition_context
    bundle = json.loads((run / "sources.json").read_text(encoding="utf-8"))
    required = {(a["source_id"], a["block_id"]) for a in scope["source_anchors"]}
    inventory, blocks = [], []
    for doc in bundle["documents"]:
        source = doc["source"]
        inventory.append({key: source.get(key) for key in
                          ("source_id", "sha256", "role", "format", "page_count")}
                         | {"block_count": len(doc["blocks"])})
        for block in doc["blocks"]:
            if (source["source_id"], block["block_id"]) in required:
                blocks.append({"source_id": source["source_id"], "block_id": block["block_id"],
                               "order": block["order"], "kind": block["kind"],
                               "location": block.get("location", {})})
    if {(b["source_id"], b["block_id"]) for b in blocks} != required:
        raise ValueError("bounded review references unknown original source blocks")
    return ("\nREGISTERED SOURCE NAVIGATION (metadata, not evidence or proof of reading):\n"
            + json.dumps({"sources": inventory, "required_blocks": blocks,
                          **source_acquisition_context(bundle["documents"])}, ensure_ascii=False)
            + "\nAll original documents and their hashes remain frozen. Read each required block with wt_read_blocks or "
              "wt_page_text. For figure/table claims, view the complete corresponding page/Word occurrence and enlarge "
              "relevant regions as needed. Other source blocks remain accessible for dependency questions. Unchanged "
              "full-paper coverage is inherited, not newly read; do not silently approve a scope that needs broader review.\n")
