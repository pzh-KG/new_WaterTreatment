"""Explicit, source-grounded repairs on an authenticated existing candidate.

This is a workflow contract, not scientific rules or a source of paper answers.
It is frozen with each opted-in plan. It never changes facts automatically.
"""
from __future__ import annotations

import json

REPAIR_TARGETS = ("shared_references", "missingness")


def repair_contract(targets):
    if (not isinstance(targets, (list, tuple)) or not targets
            or len(set(targets)) != len(targets) or any(x not in REPAIR_TARGETS for x in targets)):
        raise ValueError("continuation repairs require distinct supported targets")
    instructions = [
        "Continue the exact controller-seeded candidate; do not re-extract or resubmit the complete paper. "
        "Use wt_revision_state, wt_query_candidate and bounded wt_patch_record. The older full audit and "
        "dossier are fallible history, not independent source evidence. Read the relevant original methods, "
        "figure captions and complete figure/table pages; enlarge only the relevant panels when needed. "
        "Preserve all existing observation IDs, numerical values, units, experiment ownership, pairing, "
        "quantitative coverage and unresolved source conflicts. No new observations or dense point digitization. "
        "Do not replace earlier explicit source limitations. If a requested repair cannot be supported, "
        "preserve uncertainty and explain the affected use, rather than inventing applicability. "
        "Stage small patches early and return the latest successful submission receipt. A separate source "
        "audit will review the cumulative changes and their direct scientific dependencies."
    ]
    if "shared_references" in targets:
        instructions.append(
            "SHARED REFERENCES: Inspect existing condition-sweep experiments, comparisons and series whose "
            "reasons/source_index mention a reference/control result owned elsewhere. Add a typed "
            "reference_associations entry only where the original text/figure supports that relationship. "
            "Retrieve the ReferenceAssociation schema when needed. Keep the result's sole primary owner; "
            "do not insert shared controls into another series.observation_ids or duplicate experiments or "
            "measurements. Do not imply independent replication, identical physical samples, paired maxima, "
            "or matched conditions from graphical similarity. Each relationship needs source support for "
            "both the referenced observation and the target, an explicit applicability state, and known "
            "matched/differing/unresolved conditions. Inspect all relevant existing groups, without forcing "
            "every group to have a reference. A textual note alone is not a queryable relationship."
        )
    if "missingness" in targets:
        instructions.append(
            "MISSINGNESS: Revisit machine-flagged unread facts with already-read source support, especially "
            "retained amounts/composition of final-material components. Distinguish recipe charge from "
            "retained amount, source not reported from unread, read but not quantified, unavailable source, "
            "ambiguity, explicit absence and conflicting alternatives. Only set not_reported after checking "
            "the relevant original preparation/characterization and SI, retaining a source locator and "
            "precise note. Keep missing values null; never use zero, a recipe mass or an inferred percentage. "
            "Do not globally convert null/unread fields, and do not demand unreported experimental conditions."
        )
    return "\n\n".join(instructions)


def source_navigation(bundle, audit=None):
    """Small navigation only; full original text/images stay available via tools."""
    from .workflow import source_acquisition_context
    anchors = {(a.source_id, a.block_id) for finding in audit.findings for a in finding.evidence} if audit else set()
    if audit:
        anchors.update((a.source_id, a.block_id) for followup in audit.quantitative_followups for a in followup.evidence)
    known = {(doc["source"]["source_id"], b["block_id"]) for doc in bundle["documents"] for b in doc["blocks"]}
    # A missing or unresolvable locator requires the complete navigation index.
    unlocated = audit and (any(not f.evidence and (f.needs_revision or f.verdict == "unsupported") for f in audit.findings)
                           or set(range(len(audit.potential_omissions))) -
                           {i for f in audit.findings if f.evidence for i in f.omission_indices})
    focus = bool(anchors) and anchors <= known and not unlocated
    inventory = []
    for doc in bundle["documents"]:
        source = doc["source"]
        inventory.append({"source_id": source["source_id"], "sha256": source["sha256"],
            "role": source.get("role"), "format": source["format"],
            "block_count": len(doc["blocks"]), "page_count": source.get("page_count"),
            "blocks": [{"block_id": b["block_id"], "kind": b["kind"], "order": b["order"],
                        "location": {k: v for k, v in b.get("location", {}).items() if v is not None}}
                       for b in doc["blocks"] if not focus or (source["source_id"], b["block_id"]) in anchors]})
    return ("\nREGISTERED SOURCE NAVIGATION (metadata, not newly read evidence):\n" +
            json.dumps({"sources": inventory, "block_index": "cited_by_audit" if focus else "all",
                        **source_acquisition_context(bundle["documents"])}, ensure_ascii=False, separators=(",", ":")) +
            "\nUse wt_inventory for the complete index, wt_search for other relevant evidence and wt_view for figures. "
            "The listed locations are starting points. Check other blocks, controls or SI whenever required. "
            "Retrieve complete relevant claims before editing; prior readings are inherited history, not new source delivery.\n")
