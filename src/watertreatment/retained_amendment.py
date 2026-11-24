"""Source-located follow-up on an independently completed local review."""
from copy import deepcopy

from .citation_audit import _parts, _resolve
from .storage import digest_json, hash_file


def declared_findings(run, documents, statement):
    if (not isinstance(statement, dict) or statement.get("parent_run") != run.name
            or statement.get("record_sha256") != hash_file(run / "candidate.json")
            or statement.get("audit_sha256") != hash_file(run / "model_review.json")):
        raise ValueError("Follow-up does not identify the completed source review and candidate")
    inventory = {(doc.source.source_id, block.block_id) for doc in documents for block in doc.blocks}
    findings = statement.get("findings")
    if not isinstance(findings, list) or not 1 <= len(findings) <= 40:
        raise ValueError("Expected a bounded set of original-source questions")
    anchors = set()
    for item in findings:
        if (not isinstance(item, dict) or set(item) != {"text", "paths", "evidence"}
                or not isinstance(item["text"], str) or not 1 <= len(item["text"].strip()) <= 3200
                or not isinstance(item["paths"], list) or not 1 <= len(item["paths"]) <= 60
                or not isinstance(item["evidence"], list) or not 1 <= len(item["evidence"]) <= 30):
            raise ValueError("Each source question must identify existing fields and original evidence")
        for ref in item["evidence"]:
            if not isinstance(ref, dict) or set(ref) != {"source_id", "block_id"} or (ref["source_id"], ref["block_id"]) not in inventory:
                raise ValueError("Follow-up evidence is outside the signed source bundle")
            anchors.add((ref["source_id"], ref["block_id"]))
    return findings, sorted(anchors)


def check_changes(old, new, findings):
    # Recreate the full proposal using only the expressly named fields. The
    # focused packet subsequently discovers their control arms and dependencies.
    rebuilt = deepcopy(old)
    covered = set()
    for question in findings:
        for path in question["paths"]:
            parts = _parts(path)
            if len(parts) < 3 or not isinstance(parts[1], int) or parts[0] not in ("observations", "experiments", "materials", "comparisons", "configurations"):
                raise ValueError("Expected a located scientific field on an existing registered entity")
            location = tuple(parts)
            if location in covered:
                continue
            if any(location[:len(item)] == item or item[:len(location)] == location for item in covered):
                raise ValueError("Overlapping field locations in the new source questions")
            prior, replacement = _resolve(old, parts), _resolve(new, parts)
            if prior == replacement:
                raise ValueError("A named amendment has no proposed field change")
            _resolve(rebuilt, parts[:-1])[parts[-1]] = deepcopy(replacement)
            covered.add(location)
    if not covered or rebuilt != new:
        raise ValueError("Source amendment contains edits outside its declared fields")


def parent_for_proposal(authenticate, settings, run, proposal):
    statement = proposal.get("source_amendment") if isinstance(proposal, dict) else None
    if statement is None:
        return authenticate(settings, run)
    parent = authenticate(settings, run, source_amendment=statement)
    if ("proposal_origin" in proposal or parent.receipt.get("source_amendment") != digest_json(statement)
            or not set(parent.context).issubset({tuple(x) for x in proposal.get("source_blocks", [])})):
        raise ValueError("Source amendment lacks its registered original references")
    check_changes(parent.candidate.model_dump(mode="json"), proposal["candidate"], parent.findings)
    return parent
