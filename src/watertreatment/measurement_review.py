"""Source-first navigation for independent review. Literal matches never approve a fact."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json

from .storage import digest_json


POLICY = "printed-endpoint-navigation-v1"

READING_GUIDE = """
During the full scientific reading, use printed_endpoints as the handoff for decisive printed
outcomes: performance under relevant waters/conditions, controls, durability losses and trade-offs.
Record the author's actual number, unit/comparator, specimen, stream, condition and source block.
Prioritize facts that could change the material, design trade-off or usable operating range.
For each decisive comparison, include the named matched controls and adverse or failing endpoint.
Separate the observed treatment/control role from unknown catalyst identity, sampling stream or mechanism.
Record which observations share a run and which are only same-material measurements. For dense series,
keep the baseline, decisive endpoint, turning/failure point and any reported limit. Index the rest for retrieval.
A figure label or table cell can be printed; an axis estimate is not. When numeric evidence is
graphical only, keep the family/conditions and exact locator in experiment_map. List a printed
claim once. Use short experiment_map and scope_outline entries to express grouping and limits,
without copying these figures into multiple narrative fields. Optional characterization and
diagnostic families may be indexed together when they do not discriminate a design or limit a use.
"""

EXTRACTION_GUIDE = """
Stage decision-relevant outcomes first, including controls, adverse results and limitations.
For each printed endpoint from the reader check the original and its numerical basis. A
graphical estimate, a number attached to the wrong arm and a general mention cannot stand for
the printed result. Capture ranges, inequalities and conflicting reports faithfully. For one
unchanged arm and assay retain the reported endpoint, baseline and consequential transitions;
put routine interior samples in the sourced partial-series index. When the text and plot repeat
the same quantity in the same arm, retain one observation with its supporting sources. Keep
different sampling streams, assays, cycles and genuinely distinct conditions separate. Use
component measurements, preparation, experiment conditions or a comparison for supporting
facts which already have a natural owner. Do not duplicate them as performance observations.
Read each series along its original axis. An operating or sampling day is a time axis;
a repeated cycle requires evidence of a repeated operation. Capture any explicit relationship
between the axes without treating an ordinal day as elapsed irradiation time. Identify both
the measured scale (such as residual concentration) and any complementary quantity the
authors report. Preserve them as distinct metrics with their original evidence. Keep a supplied
calculation constant with its method, and report whether its own derivation is given.
For auxiliary mechanism/characterization evidence keep the result needed for the design
claim, contrary findings and limitations. Index the other inspected detail for later retrieval.
"""

AUDIT_GUIDE = """
Start with the source-side shortlist below and check the original results independently, including
results outside the shortlist. Check every flagged missing or competing match, the actual arm,
stream, numerical origin, units, inequality, and any pairing. An unregistered anchor is a fallible reading lead: locate the original and qualify the lead if unconfirmed. A literal numeric match on the same
page is not ownership or evidence of agreement. Resolve the actual defects together in this audit.
A false first-reading lead, an equivalent supported unit conversion, or a properly deferred
noncritical family is not a defect. Report genuine remaining defects with the exact record paths
and source anchors. Preserve the ordinary independent full-source audit and its coverage.
"""


def _numeric_key(value):
    if isinstance(value, bool) or not isinstance(value, (float, int, str)):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def measurement_navigation(dossier, record, documents):
    """Compare literal numbers and cited locations without inferring unit or owner equivalence."""
    sources = {(doc.source.source_id, b.block_id) for doc in documents for b in doc.blocks}
    counts, local = defaultdict(int), defaultdict(list)
    evidence = {item.id: (item.source_id, item.block_id) for item in record.evidence}
    for row in record.observations:
        number = _numeric_key(row.value.value)
        if number is None:
            continue
        counts[number] += 1
        ids = set(row.value.evidence_ids)
        ids.update(link.evidence_id for link in row.evidence_links
                   if link.support_type == "direct" and "value" in link.supported_fields)
        for location in {evidence[key] for key in ids if key in evidence}:
            local[number, location].append(row)
    flags = []
    for claim in dossier.printed_endpoints:
        location = (claim.evidence.source_id, claim.evidence.block_id)
        number = _numeric_key(claim.number)
        rows = local[number, location]
        flags.append({"reader_flag": claim.model_dump(mode="json", exclude_none=True),
                      "source_anchor_registered": location in sources,
                      "same_value_same_block_count": len(rows), "same_value_other_block_count": counts[number] - len(rows),
                      "candidate_examples": [{"id": o.id, "experiment_id": o.experiment_id, "material_id": o.material_id,
                                              "stream_id": o.stream_id, "kind": o.endpoint_kind, "origin": o.origin}
                                             for o in rows[:5]]})
    return {"policy": POLICY, "candidate_hash": digest_json(record.model_dump(mode="json")),
            "flags": flags, "scientific_validation": "not_established"}


def audit_navigation_text(report, byte_limit=24000):
    # The complete navigation is retained. Prefer gaps before common-number matches.
    ordered = sorted(report["flags"], key=lambda f: bool(f["same_value_same_block_count"] and f["source_anchor_registered"]))
    lines = []
    notice = ("Navigation shows {shown} of {total} reader flags. "
              "Unlisted flags/rows still require normal source coverage.\n")
    header = AUDIT_GUIDE + notice.format(shown=len(ordered), total=len(ordered))
    used = len(header.encode("utf-8"))
    if used > byte_limit:
        return ""

    for flag in ordered:
        line = json.dumps(flag, ensure_ascii=False, separators=(",", ":"))
        size = len(line.encode("utf-8")) + 1
        if size + used <= byte_limit:
            lines.append(line)
            used += size
    return AUDIT_GUIDE + notice.format(shown=len(lines), total=len(ordered)) + "\n".join(lines)
