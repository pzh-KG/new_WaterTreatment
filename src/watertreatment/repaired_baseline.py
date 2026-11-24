"""Compile previously invalid controller references and structural ledger edits for source review."""
from .coverage_correction import build_coverage_correction_scope
from .domain import CoverageEntry
from .ownership_audit import _changes
from .scientific_revision_audit import _change


def canonical_proposal(parent):
    """Split a genuinely empty block out of a previously read bibliography/contents row."""
    before, proposed = parent.before, parent.candidate.model_copy(deep=True)
    if len(before.coverage) != len(proposed.coverage):
        raise ValueError("Unreconciled changes to reading ledger membership")
    ledger = parent.dossier.model_copy(update={"coverage":before.coverage})
    scope = build_coverage_correction_scope(ledger,parent.documents)
    structural = {(v["source_id"],v["block_id"]):v for v in scope["structural_dispositions"]}
    for previous,current in zip(before.coverage,list(proposed.coverage)):
        if previous.block_ids == current.block_ids:
            continue
        added = [bid for bid in current.block_ids if bid not in previous.block_ids]
        if (not added or [b for b in current.block_ids if b not in added] != previous.block_ids
                or current.source_id != previous.source_id or current.state != "out_of_scope"
                or any(getattr(previous,k)!=getattr(current,k) for k in ("state","text_read","visual_checked"))):
            raise ValueError("Changed substantive reading membership requires source adjudication")
        current.block_ids = list(previous.block_ids)
        for bid in added:
            item = structural.pop((current.source_id,bid),None)
            if not item or item["category"] != "empty_paragraph":
                raise ValueError("A substantive, repeated or unverified block cannot be classified as a blank separator")
            proposed.coverage.append(CoverageEntry(source_id=current.source_id,block_ids=[bid],state="out_of_scope",
                text_read=False,visual_checked=False,note=item["note"]))
    return proposed


def scoped_changes(parent, proposed, *, review_quote_extensions=False):
    baseline = parent.before.model_dump(mode="json")
    current = proposed.model_dump(mode="json")
    if proposed.coverage != canonical_proposal(parent).coverage:
        raise ValueError("The structural projection differs from its original source blocks")
    n = len(baseline["coverage"])
    structural = [_change(("coverage",i),"empty_source_block",None,row) for i,row in enumerate(current["coverage"][n:],start=n)]
    current = {**current,"coverage":current["coverage"][:n]}
    repaired = []
    paper_id = parent.before.paper.id
    doi = parent.before.paper.doi.value
    previous_rows=(baseline.get("design_scope") or {}).get("eligibility",[])
    current_rows=(current.get("design_scope") or {}).get("eligibility",[])
    for i,(old,new) in enumerate(zip(previous_rows,current_rows)):
        if (old["target_kind"] == new["target_kind"] == "paper" and old["use"] == new["use"]
                and old["target_id"] != paper_id and isinstance(doi,str)
                and old["target_id"].casefold()==doi.casefold() and new["target_id"]==paper_id):
            repaired.append(_change(("design_scope","eligibility",i,"target_id"),"controller_reference",old["target_id"],paper_id))
            old["target_id"] = paper_id
    changes = _changes(baseline,current,adjudicate_outcomes=True,review_source_edits=True,
                       review_quote_extensions=review_quote_extensions)
    return [*changes,*repaired,*structural]
