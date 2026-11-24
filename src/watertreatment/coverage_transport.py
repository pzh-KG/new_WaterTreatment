"""Literal ordinal ranges for model-authored dossier dispositions."""
from __future__ import annotations

import json
import re
from collections import Counter

from .reading import ReadingDossier
from .storage import digest_json


POLICY = "explicit-dossier-block-orders-v1"
_RANGE = re.compile(r"@orders:([1-9][0-9]{0,6})-([1-9][0-9]{0,6})\Z")


def order_gaps(total, spans):
    for span in spans:
        if (len(span) != 2 or any(type(n) is not int for n in span)
                or not 1 <= span[0] <= span[1] <= total):
            raise ValueError("coverage intervals must contain two in-range inclusive block orders")
    missing = []
    next_order = 1
    for start, end in sorted(spans):
        if start > next_order:
            missing.append([next_order, start - 1])
        next_order = max(next_order, end + 1)
    if next_order <= total:
        missing.append([next_order, total])
    return missing


def expand_dossier(raw, bundle):
    dossier = ReadingDossier.model_validate(raw)
    inventory = {d["source"]["source_id"]: d["blocks"] for d in bundle["documents"]}
    count = 0
    claimed = {}
    ranged_sources = {e.source_id for e in dossier.coverage if any(b.startswith("@orders:") for b in e.block_ids)}
    for entry in dossier.coverage:
        if entry.source_id not in ranged_sources:
            continue
        blocks = inventory.get(entry.source_id)
        if blocks is None:
            raise ValueError("explicit reading orders name an unknown source")
        ids = []
        for key in entry.block_ids:
            if not key.startswith("@orders:"):
                ids.append(key)
                continue
            match = _RANGE.fullmatch(key)
            if match is None:
                raise ValueError("invalid explicit reading order interval")
            start, end = map(int, match.groups())
            if not 1 <= start <= end <= len(blocks) or len(ids) + end - start + 1 > len(blocks):
                raise ValueError("explicit reading orders outside the frozen source or repeated")
            for order in range(start, end + 1):
                block = blocks[order-1]
                if block["order"] != order or block.get("source_id", entry.source_id) != entry.source_id:
                    raise ValueError("source block order or ownership changed")
                ids.append(block["block_id"])
            count += 1
        seen = claimed.setdefault(entry.source_id, set())
        if len(set(ids)) != len(ids) or not set(ids).isdisjoint(seen) or not set(ids).issubset({b["block_id"] for b in blocks}):
            raise ValueError("duplicate or unknown block within an explicit reading disposition")
        seen.update(ids)
        entry.block_ids = ids
    return dossier, {"policy": POLICY, "ranges": count, "answer_hash": digest_json(raw),
                     "dossier_hash": digest_json(dossier.model_dump(mode="json"))}


def verified_dossier(raw, bundle, request, result):
    if "dossier_coverage_transport" not in request and "dossier_coverage_expansion" not in result:
        return ReadingDossier.model_validate(raw)
    if request.get("dossier_coverage_transport") != POLICY:
        raise ValueError("unsupported dossier coverage transport")
    dossier, proof = expand_dossier(raw, bundle)
    if result.get("dossier_coverage_expansion") != proof:
        raise ValueError("dossier coverage expansion differs from the retained answer and source")
    return dossier


def dossier_for_prompt(dossier, bundle):
    """Lossless presentation of the same dispositions in extraction and repairs."""
    data = dossier.model_dump(mode="json")
    orders = {}
    for doc in bundle["documents"]:
        source = doc["source"]["source_id"]
        blocks = doc["blocks"]
        by_id = {b["block_id"]: i for i, b in enumerate(blocks, 1)
                 if b.get("order") == i and b.get("source_id", source) == source}
        if len(by_id) == len(blocks):
            orders[source] = by_id
    counts = Counter((e.source_id, key) for e in dossier.coverage for key in e.block_ids)
    # expand_dossier rejects ambiguous/unknown orders across an entire source.
    # Keep its original literals if even one disposition is ambiguous.
    for (source, key), count in counts.items():
        if count != 1 or key not in orders.get(source, {}) or key.startswith("@orders:"):
            orders.pop(source, None)
    for entry in data["coverage"]:
        by_id = orders.get(entry["source_id"])
        if not by_id:
            continue
        ids = entry["block_ids"]
        compact = []
        i = 0
        while i < len(ids):
            start = i
            while i + 1 < len(ids) and by_id[ids[i + 1]] == by_id[ids[i]] + 1:
                i += 1
            compact.append(ids[i] if i == start else f"@orders:{by_id[ids[start]]}-{by_id[ids[i]]}")
            i += 1
        entry["block_ids"] = compact
    numbered = [s for s, mapping in orders.items() if mapping and
                all(key == f"{s}:b{order:06d}" for key, order in mapping.items())]
    literal_ids = (" For " + ", ".join(numbered) + ", literal IDs are source_id + ':b' + six-digit order."
                   if numbered else "")
    return ("Coverage @orders:a-b denotes the exact inclusive block orders in that entry's frozen source. "
            "It preserves the recorded disposition and flags. Full locators are available via wt_inventory." + literal_ids + "\n"
            + json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
