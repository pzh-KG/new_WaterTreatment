"""Lossless, literal sharing for repeated evidence-bearing fields in a local review prompt."""
from collections import Counter
import json


POLICY = "literal-local-review-v1"


def compact_review(packet):
    if packet.get("prompt_transport") != POLICY:
        return packet
    sections = ("changes", "current_targets")
    counts, reserved = Counter(), set()
    def literal(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    def inventory(value):
        if not isinstance(value, (dict, list)):
            return
        if isinstance(value, dict):
            reserved.update(value)
        key = literal(value)
        if 64 <= len(key) <= 8000:
            counts[key] += 1
        for child in value.values() if isinstance(value, dict) else value:
            inventory(child)
    for name in sections:
        inventory(packet[name])
    marker = "$same"
    while marker in reserved:
        marker = "_" + marker
    identifiers, exact = {}, {}
    def encode(value):
        if not isinstance(value, (dict, list)):
            return value
        key = literal(value)
        if counts[key] >= 2:
            if key not in identifiers:
                label = str(len(exact)+1)
                identifiers[key] = label
                exact[label] = value
            return {marker: identifiers[key]}
        if isinstance(value, dict):
            return {k: encode(v) for k, v in value.items()}
        return [encode(v) for v in value]
    result = dict(packet)
    for name in sections:
        result[name] = encode(packet[name])
    if exact:
        result["literal_reference_key"] = marker
        result["shared_exact_literals"] = exact
    return result
