"""Append-only reading-disposition corrections, separate from scientific facts.

Empty XML containers are not evidence of reading. Nonempty content, including
duplicate equation projections and external benchmark rows, needs a new model
declaration backed by current source-tool delivery. No candidate is accepted as
evidence that its source was read.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from .domain import CoverageEntry, CoverageState, StrictModel
from .reading import ReadingDossier, SourceAnchor
from .storage import digest_json

POLICY = "reading-coverage-correction-v1"
EQUATION_CONTEXT_POLICY = "reading-coverage-equation-context-v2"
VISUAL_CONTEXT_POLICY = "reading-coverage-linked-caption-v3"


class CoverageDisposition(StrictModel):
    source_id: str
    block_ids: list[str] = Field(min_length=1)
    state: CoverageState
    text_read: bool
    visual_checked: bool
    category: Literal["substantive", "bibliographic", "external_benchmark",
        "duplicate_representation", "equation_fragment", "source_limitation"]
    note: str = Field(min_length=12)
    evidence: list[SourceAnchor] = Field(min_length=1)


class CoverageScientificConcern(StrictModel):
    source_id: str
    block_ids: list[str] = Field(min_length=1)
    explanation: str = Field(min_length=12)


class CoverageCorrectionReport(StrictModel):
    scope_sha256: str
    dispositions: list[CoverageDisposition]
    scientific_concerns: list[CoverageScientificConcern] = Field(default_factory=list,
        description="New contradictions, omissions or materially unsupported claims requiring scientific review. Do not fix the candidate or conceal a concern to finish coverage.")
    source_limitations: list[str] = Field(default_factory=list)
    human_validated: Literal[False] = False


def _inventory(documents):
    result = {}
    for doc in documents:
        for block in doc.blocks:
            key = doc.source.source_id, block.block_id
            if key in result:
                raise ValueError("duplicate source/block identity")
            result[key] = block
    return result


def build_coverage_correction_scope(dossier, documents):
    blocks = _inventory(documents)
    seen, invalid = set(), []
    for index, entry in enumerate(dossier.coverage):
        for bid in entry.block_ids:
            key = entry.source_id, bid
            if key in blocks:
                seen.add(key)
            else:
                invalid.append({"entry_index": index, "source_id": entry.source_id, "block_id": bid,
                    "reason": "Identifier is absent from the hash-bound source inventory; removal does not assert reading."})
    structural, required = [], []
    for key in sorted(set(blocks) - seen):
        block = blocks[key]
        path = block.location.xml_path
        children = [b for (sid, _), b in blocks.items() if sid == key[0] and b.block_id != block.block_id
            and (b.metadata.get("parent_block_id") == block.block_id
                 or (path and b.location.part == block.location.part and b.location.xml_path
                     and b.location.xml_path.startswith(path + "/")))]
        blank = not block.text.strip() and block.phase == "parsed_unread"
        empty_paragraph = (block.kind == "paragraph" and not children
            and not block.metadata.get("has_omml") and not block.metadata.get("has_revisions")
            and block.metadata.get("is_current_text", True) is True)
        container = (block.kind in {"table", "table_cell", "paragraph"} and bool(children)
            and not block.metadata.get("has_revisions"))
        if blank and (empty_paragraph or container):
            structural.append({"source_id": key[0], "block_id": key[1],
                "block_sha256": digest_json(block.model_dump(mode="json")),
                "category": "structural_container" if container else "empty_paragraph",
                "child_block_ids": [b.block_id for b in children],
                "state": "out_of_scope", "text_read": False, "visual_checked": False,
                "note": ("Empty XML container only; its separately inventoried child content retains independent reading dispositions."
                    if container else "Empty parsed paragraph without equation, revision or registered child content; no scientific reading is asserted.")})
        else:
            required.append({"source_id": key[0], "block_id": key[1], "order": block.order,
                "kind": block.kind, "location": block.location.model_dump(mode="json"),
                "block_sha256": digest_json(block.model_dump(mode="json")),
                "parent_block_id": block.metadata.get("parent_block_id"),
                "has_text": bool(block.text.strip()),
                "hint": "equation_fragment" if block.kind == "equation" else "requires_source_disposition"})
    anchors = {(r["source_id"], r["block_id"]): {"source_id": r["source_id"], "block_id": r["block_id"],
        "evidence_id": "coverage:" + r["block_id"], "locator": {}} for r in required}
    for item in required:
        parent_key = item["source_id"], item["parent_block_id"]
        if item["kind"] == "equation" and parent_key in blocks:
            anchors.setdefault(parent_key, {"source_id": parent_key[0], "block_id": parent_key[1],
                "evidence_id": "coverage-context:" + parent_key[1], "locator": {}, "origin": "equation_parent_context"})
    scope = {"policy": POLICY, "paper_id": dossier.paper_id,
        "original_dossier_hash": digest_json(dossier.model_dump(mode="json")),
        "documents_hash": digest_json([d.model_dump(mode="json") for d in documents]),
        "source_hashes": {d.source.source_id: d.source.sha256 for d in documents},
        "invalid_identifiers": invalid, "structural_dispositions": structural,
        "required_dispositions": required,
        "source_anchors": list(anchors.values()),
        "scientific_validation": "not_established"}
    scope["scope_sha256"] = digest_json(scope)
    return scope


def coverage_correction_prompt(scope):
    navigation = {"policy": scope["policy"], "scope_sha256": scope["scope_sha256"],
        "paper_id": scope["paper_id"], "source_hashes": scope["source_hashes"],
        "required_dispositions": scope["required_dispositions"],
        "structural_disposition_count": len(scope["structural_dispositions"]),
        "structural_interpretation": "Already classified as empty containers/paragraphs with text_read=false; no model reading assertion.",
        "invalid_identifier_count": len(scope["invalid_identifiers"])}
    return ("Repair only the explicit source-reading disposition ledger. This is not extraction, a whole-paper audit, "
        "or scientific validation. The inherited scientific candidate and audit are immutable. "
        "Read every required block with the registered source tools IN THIS TURN, including table cells/paragraphs "
        "and equation fragments; inspect parent/context blocks as necessary. Initial delivery, the candidate and "
        "old dossier declarations are not proof of reading. Do not read unrelated full sources. "
        "For substantive methods, experiments or conditions, retrieve the relevant immutable candidate entities "
        "with wt_query_candidate in this turn and compare them against the newly read source. Do not request "
        "or reproduce the entire candidate. Pure bibliography, verified duplicate projections and empty "
        "containers do not require unrelated scientific queries. A substantive disposition without current "
        "candidate access is incomplete, and a discovered omission or contradiction belongs in scientific_concerns. "
        "Group adjacent blocks only when one honest disposition applies to all. Give an exact short quote "
        "from at least one named block per group and explain the actual content. Bibliography/external benchmarks "
        "can be out_of_scope after reading; do not import their scientific values. Formula fragments may be "
        "duplicate_representation only after checking the named parent equation/context; do not invent missing glyphs. "
        "Empty paragraphs/containers listed as structural are controller dispositions with text_read=false; do NOT "
        "include them in your report or call them read. Never infer not_reported from an unread block. "
        "If source content is unavailable/unreadable, retain that limitation honestly. If you encounter a consequential "
        "scientific contradiction, omission or an unsupported inherited claim, include scientific_concerns; "
        "do not silently fix the candidate or mark the issue solved. A concern stops admission for targeted review. "
        "Return only CoverageCorrectionReport, with every required block exactly once and no other blocks.\n" +
        json.dumps(navigation, ensure_ascii=False))


def coverage_correction_issues(scope, report, dossier, documents, *, validation_policy=POLICY, delivered=()):
    if validation_policy not in {POLICY, EQUATION_CONTEXT_POLICY, VISUAL_CONTEXT_POLICY}:
        return ["Unknown coverage-disposition validation contract."]
    expected = build_coverage_correction_scope(dossier, documents)
    if scope != expected or report.scope_sha256 != scope["scope_sha256"]:
        return ["Coverage correction does not bind the original dossier/source scope."]
    return disposition_issues(scope, report, documents, validation_policy=validation_policy, delivered=delivered)


def disposition_issues(scope, report, documents, *, validation_policy, require_all=True, delivered=()):
    """Check individual declarations even when the historical turn was only partly delivered."""
    blocks = _inventory(documents)
    required = {(x["source_id"], x["block_id"]) for x in scope["required_dispositions"]}
    seen, issues = set(), []
    for index, entry in enumerate(report.dispositions):
        keys = {(entry.source_id, bid) for bid in entry.block_ids}
        if len(keys) != len(entry.block_ids) or keys & seen or not keys.issubset(required):
            issues.append(f"Coverage disposition {index} contains duplicate or out-of-scope identifiers.")
        seen.update(keys)
        anchors = {(a.source_id, a.block_id) for a in entry.evidence}
        allowed = keys
        if validation_policy in {EQUATION_CONTEXT_POLICY, VISUAL_CONTEXT_POLICY}:
            registered_parents = {(a["source_id"], a["block_id"]) for a in scope["source_anchors"]
                if a.get("origin") == "equation_parent_context"}
            actual_parents = {(key[0], blocks[key].metadata.get("parent_block_id")) for key in keys
                if key in blocks and blocks[key].kind == "equation"}
            allowed = keys | (registered_parents & actual_parents)
        if validation_policy == VISUAL_CONTEXT_POLICY and entry.visual_checked and keys <= blocks.keys():
            from .word_review_links import figure_caption_for_occurrences
            doc = next((d for d in documents if d.source.source_id == entry.source_id), None)
            if doc:
                images = [blocks[key] for key in keys]
                allowed = allowed | {key for key in anchors - keys if key in delivered and key in blocks
                    and key[0] == entry.source_id and figure_caption_for_occurrences(doc, images, blocks[key])}
        if not (anchors & keys) or not anchors.issubset(allowed):
            issues.append(f"Coverage disposition {index} must quote its own registered block content.")
        for anchor in entry.evidence:
            block = blocks.get((anchor.source_id, anchor.block_id))
            visual_anchor = bool(block and not block.text.strip() and entry.visual_checked
                and block.kind in {"image", "pdf_page"} and anchor.figure_or_table)
            if not visual_anchor and (block is None or not anchor.quote or not anchor.quote.strip()
                    or anchor.quote not in block.text):
                issues.append(f"Coverage disposition {index} has a missing or nonmatching source quote.")
        if entry.state == CoverageState.PARSED_UNREAD:
            issues.append(f"Coverage disposition {index} still declares substantive content unread.")
        if any(blocks[k].text.strip() for k in keys if k in blocks) and not entry.text_read:
            issues.append(f"Coverage disposition {index} does not declare reading its nonempty text.")
        if entry.state in {CoverageState.UNREADABLE, CoverageState.UNAVAILABLE} and not report.source_limitations:
            issues.append(f"Coverage disposition {index} lacks an explicit source limitation.")
    if require_all and seen != required:
        issues.append("Coverage correction lacks explicit dispositions for: " + repr(sorted(required - seen)))
    for concern in report.scientific_concerns:
        if any((concern.source_id, bid) not in blocks for bid in concern.block_ids):
            issues.append("Scientific concern references an unknown source block.")
        issues.append("Coverage recheck found a scientific concern requiring separate review: " + concern.explanation)
    return issues


def corrected_dossier(scope, report, dossier, documents):
    """Derive a new version; never mutate the retained original dossier."""
    if scope != build_coverage_correction_scope(dossier, documents) or report.scope_sha256 != scope["scope_sha256"]:
        raise ValueError("coverage correction scope changed")
    blocks = _inventory(documents)
    entries = []
    for entry in dossier.coverage:
        kept = [bid for bid in entry.block_ids if (entry.source_id, bid) in blocks]
        if kept:
            entries.append(entry.model_copy(update={"block_ids": kept}))
    structural_groups = {}
    for item in scope["structural_dispositions"]:
        structural_groups.setdefault((item["source_id"], item["category"], item["note"]), []).append(item["block_id"])
    for (source_id, _, note), block_ids in structural_groups.items():
        entries.append(CoverageEntry(source_id=source_id, block_ids=block_ids,
            state="out_of_scope", text_read=False, visual_checked=False, note=note))
    for item in report.dispositions:
        entries.append(CoverageEntry(source_id=item.source_id, block_ids=item.block_ids,
            state=item.state, text_read=item.text_read, visual_checked=item.visual_checked, note=item.note))
    value = dossier.model_dump(mode="json")
    value["coverage"] = [e.model_dump(mode="json") for e in entries]
    return ReadingDossier.model_validate(value)


def apply_structural_correction(run, dossier, documents):
    """For new workflows, classify only provably empty structure, never text."""
    from .storage import hash_file, write_new_json
    scope = build_coverage_correction_scope(dossier, documents)
    if not scope["structural_dispositions"] and not scope["invalid_identifiers"]:
        return dossier, None
    report = CoverageCorrectionReport(scope_sha256=scope["scope_sha256"], dispositions=[])
    effective = corrected_dossier(scope, report, dossier, documents)
    write_new_json(run / "reading_structure_scope.v01.json", scope)
    write_new_json(run / "reading_dossier.structural.v01.json", effective.model_dump(mode="json"))
    proof = {"policy": "structural-reading-disposition-v1",
        "original_dossier_sha256": hash_file(run / "reading_dossier.json"),
        "source_bundle_sha256": hash_file(run / "sources.json"),
        "scope_sha256": scope["scope_sha256"],
        "scope_file_sha256": hash_file(run / "reading_structure_scope.v01.json"),
        "effective_dossier_sha256": hash_file(run / "reading_dossier.structural.v01.json"),
        "model_reading_asserted": False, "remaining_model_dispositions": len(scope["required_dispositions"])}
    write_new_json(run / "reading_structure_correction.v01.json", proof)
    return effective, proof


def verify_structural_correction(run, proof, documents):
    from pathlib import Path
    from .storage import hash_file
    try:
        run = Path(run)
        load = lambda name: json.loads((run / name).read_text(encoding="utf-8"))
        original = ReadingDossier.model_validate(load("reading_dossier.json"))
        scope = build_coverage_correction_scope(original, documents)
        effective = corrected_dossier(scope, CoverageCorrectionReport(scope_sha256=scope["scope_sha256"], dispositions=[]),
            original, documents)
        expected = {"policy": "structural-reading-disposition-v1",
            "original_dossier_sha256": hash_file(run / "reading_dossier.json"),
            "source_bundle_sha256": hash_file(run / "sources.json"), "scope_sha256": scope["scope_sha256"],
            "scope_file_sha256": hash_file(run / "reading_structure_scope.v01.json"),
            "effective_dossier_sha256": hash_file(run / "reading_dossier.structural.v01.json"),
            "model_reading_asserted": False, "remaining_model_dispositions": len(scope["required_dispositions"])}
        return (proof == expected and load("reading_structure_correction.v01.json") == expected
            and load("reading_structure_scope.v01.json") == scope
            and ReadingDossier.model_validate(load("reading_dossier.structural.v01.json")) == effective)
    except (ValueError, OSError, KeyError, TypeError):
        return False
