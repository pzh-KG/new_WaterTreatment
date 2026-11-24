"""Bind candidate observations to their own experiment and quoted source block."""
from __future__ import annotations

from .citation_audit import _evidence_ids


def _quote_location(quote, block):
    if not quote or not quote.strip():
        return {"quote_status": "no_quote"}
    start = block.text.find(quote)
    if start < 0:
        return {"quote_status": "no_literal_match"}
    if block.text.find(quote, start + 1) >= 0:
        return {"quote_status": "multiple_literal_matches"}
    end = start + len(quote)
    lo, hi = max(0, start - 120), min(len(block.text), end + 120)
    return {"quote_status": "unique_literal_match", "quote_span": [start, end],
            "excerpt_span": [lo, hi], "source_excerpt": block.text[lo:hi]}


def observation_context(record, items, documents, verify_source):
    """Only follow explicit IDs. Keep missing or ambiguous links local to that target."""
    owner_ids = {row["experiment_id"] for row in items}
    owners = [e.model_dump(mode="json", exclude_defaults=True) for e in record.experiments if e.id in owner_ids]
    refs = _evidence_ids(items) | _evidence_ids(owners)
    for owner in owners:
        for reagent in owner.get("reaction_inputs", []):
            refs.update(reagent.get("addition_evidence_ids", []))
    evidence = [e for e in record.evidence if e.id in refs]
    sources = {}
    for sid in dict.fromkeys(e.source_id for e in evidence):
        if sid in documents:
            doc = verify_source(sid)
            sources[sid] = doc.source.sha256, {block.block_id: block for block in doc.blocks}
    anchors = []
    block_texts = {}
    remaining_text_bytes = 24 * 1024
    for ref in evidence:
        item = {"candidate_evidence": ref.model_dump(mode="json", exclude_defaults=True)}
        if ref.source_id not in sources:
            item["quote_status"] = "unknown_source"
        else:
            source_hash, blocks = sources[ref.source_id]
            block = blocks.get(ref.block_id)
            item["source_sha256"] = source_hash
            if block is None:
                item["quote_status"] = "unknown_block"
            else:
                item.update(block_kind=block.kind, block_order=block.order,
                            source_location=block.location.model_dump(mode="json", exclude_none=True))
                item.update(_quote_location(ref.quote, block))
                if (item["quote_status"] != "unique_literal_match" and block.text.strip()
                        and block.block_id not in block_texts.get(ref.source_id, {})):
                    size = len(block.text.encode("utf-8"))
                    if size <= remaining_text_bytes:
                        block_texts.setdefault(ref.source_id, {})[block.block_id] = block.text
                        remaining_text_bytes -= size
        anchors.append(item)
    return {"candidate_experiments": owners, "source_anchors": anchors, "cited_block_texts": block_texts,
            "unresolved_experiment_ids": sorted(owner_ids - {e["id"] for e in owners}),
            "unresolved_evidence_ids": sorted(refs - {e.id for e in evidence}),
            "meaning": "Candidate ownership, panel labels and field citations are claims. Offsets are zero-based "
                       "Unicode character intervals [start,end) in the cited parsed block. An exact match locates "
                       "words only. Optional cited_block_texts are complete parsed text blocks, keyed by source and "
                       "block ID, for anchors without a unique quote. Other blocks remain available at their locators. "
                       "Inspect the original labelled group, graph, stream and conditions for support "
                       "and omissions. Visual values can be supported without a literal quotation."}
