"""Build an immutable local evidence packet and expand only explicit reviewer decisions."""
from collections import defaultdict
import json
from typing import Literal

from pydantic import Field

from .citation_audit import _parts, _resolve
from .coverage_correction import build_coverage_correction_scope, corrected_dossier, CoverageCorrectionReport
from .domain import CoverageEntry, StrictModel
from .ownership_audit import _changes
from .reading import AuditFinding, SourceAnchor
from .storage import digest_json
from .workflow import _image_manifest

FOCUSED_VERSIONS = {3, 4, 5, 6, 7, 8, 9}


class ReviewDecision(StrictModel):
    groups: list[str] = Field(min_length=1)
    verdict: Literal["supported", "limited", "requires_revision"]
    source_ids: list[str] = Field(min_length=1, description="Exact source card IDs, e.g. S3. Cite actual packet cards.")
    explanation: str = Field(min_length=12)


class ReadGroup(StrictModel):
    groups: list[str] = Field(min_length=1)
    state: Literal["read_structured", "read_unquantified", "out_of_scope", "ambiguous", "conflict", "unreadable"]
    note: str = Field(min_length=12)


class LocalReview(StrictModel):
    packet_hash: str
    decisions: list[ReviewDecision] = Field(min_length=1)
    reading: list[ReadGroup]
    reconciled_source_groups: list[str] = Field(description="Explicitly enumerate the F groups whose complete inherited source reference sets you checked against their named target and prior disposition. Do not confirm unexamined sets.")
    additional_issues: list[str] = Field(description="Consequential uncorrected problems in this local review only.")
    restrictions: list[str]


def reading_groups(parent):
    scope = build_coverage_correction_scope(parent.dossier, parent.documents)
    wanted = {(r["source_id"], r["block_id"]) for r in scope["required_dispositions"]}
    groups = []
    for doc in parent.documents:
        group, last = [], None
        for b in doc.blocks:
            if (doc.source.source_id, b.block_id) not in wanted:
                continue
            if b.kind not in {"paragraph", "equation", "table_cell"} or not b.text.strip():
                raise ValueError("This residual ledger requires native image/structure review before textual disposition")
            key = (b.location.part, b.location.table_index, b.kind)
            if key != last or len(group) >= 24:
                group = []
                groups.append({"id": "R" + str(len(groups) + 1), "source": doc.source.source_id, "rows": group})
            group.append([b.block_id, b.text])
            last = key
    return scope, groups


def _snapshot(value):
    # Omit only empty/default serialization fields in the reader view. Original
    # versions and a digest of each complete changed value remain in the plan.
    if isinstance(value, dict):
        return {k: _snapshot(v) for k, v in value.items() if v not in (None, [], {})}
    if isinstance(value, list):
        return [_snapshot(v) for v in value]
    return value


def build_packet(parent, proposed, source_keys, *, version=1):
    if version not in (1, 2):
        raise ValueError("Unknown retained review packet format")
    before = parent.before.model_dump(mode="json")
    after = proposed.model_dump(mode="json")
    delta = _changes(before, after)
    if not delta or len(delta) > 180:
        raise ValueError("A narrow, nonempty scientific revision is required")
    by_section = defaultdict(list)
    for c in delta:
        tokens = _parts(c["changed_path"])
        section = tokens[0] if tokens[0] != "design_scope" else ".".join(tokens[:2])
        by_section[section].append([c["changed_path"], _snapshot(c["old_value"]), _snapshot(c["new_value"])])
    rows = []
    for section, changes in by_section.items():
        for start in range(0, len(changes), 10):
            group = changes[start:start + 10]
            rows.append({"id": "C" + str(len(rows) + 1), "section": section, "changes": group})
    locations = set(tuple(k) for k in source_keys) | set(parent.context)
    for finding in parent.findings:
        locations.update((a["source_id"], a["block_id"]) for a in finding.get("evidence", []) if a.get("quote") or a.get("figure_or_table"))
    inventory = {(d.source.source_id, b.block_id): (d,b) for d in parent.documents for b in d.blocks}
    if not locations or not locations <= inventory.keys():
        raise ValueError("Review source selection is empty or contains a foreign block")
    renderings = _image_manifest(parent.bundle)
    images, cards = [], []
    for sid, bid in sorted(locations):
        d, b = inventory[sid, bid]
        card = {"id": "S" + str(len(cards) + 1), "source": sid, "block": bid, "kind": b.kind, "text": b.text}
        if d.source.format == "pdf" or b.kind == "image":
            matches = [v for v in renderings if v["source_id"] == sid and (v.get("block_id") == bid
                or d.source.format == "pdf" and v.get("physical_page") == b.location.physical_page)]
            if len(matches) != 1:
                raise ValueError("Selected visual source requires a unique registered rendering: " + bid)
            image = matches[0]["path"]
            if image not in images:
                images.append(image)
            card["image"] = images.index(image) + 1
        cards.append(card)
    if len(images) > 14:
        raise ValueError("Requested source view exceeds the local review boundary")
    _, groups = reading_groups(parent)
    concerns = []
    inherited = {}
    by_id = {e.id:e for e in parent.candidate.evidence}
    for finding in parent.findings:
        concerns.append({"id": "F" + str(len(concerns) + 1), **finding})
        for eid in finding.get("required_evidence_ids", []):
            if eid in inherited:
                continue
            e = by_id[eid]
            _, block = inventory[(e.source_id, e.block_id)]
            inherited[eid] = {"evidence": _snapshot(e.model_dump(mode="json")),
                "native_text": block.text if len(block.text) <= 500 else None}
    packet = {"candidate_hash": digest_json(after), "prior_candidate_hash": digest_json(before),
        "title": proposed.paper.title.value, "changes": rows, "findings": concerns, "sources": cards,
        "reading_groups": groups, "inherited_references": inherited, "source_order": [d.source.source_id for d in parent.documents]}
    if version == 2:
        packet["version"] = version
        # Give the reviewer the exact named comparison/observation as well as
        # its sources. A citation directory by itself does not show what was claimed.
        paths = {path for group in concerns for path in group.get("paths", [])}
        packet["current_targets"] = {}
        for path in sorted(paths):
            try:
                item = _resolve(after, _parts(path))
            except (KeyError, IndexError, ValueError, TypeError):
                continue  # Prose labels remain in the original finding.
            packet["current_targets"][path] = _snapshot(item)
    packet["packet_hash"] = digest_json(packet)
    return packet, images


def prompt(packet):
    if packet.get("version") in FOCUSED_VERSIONS:
        from .review_transport import compact_review
        delivered = compact_review(packet)
        source_instructions = ("For a source-note or evidence-locator edit, examine every original named in required_group_sources "
            "for that group and cite each one in its decision. Check each L group of affected users of a locator. "
            "A change of narrative note cannot establish new reading or visual coverage. "
            "Every final_correction.source_ids must be included in its own group's affirmative decision.source_ids. "
            if packet["version"] >= 4 else "")
        inherited_instruction = ("for each F group with required_evidence_ids, reconcile those originals and list the "
            "group in reconciled_source_groups. Cite every required source card in that group's decision.\n"
            if packet["version"] >= 5 else
            "this packet has no inherited reference obligations, so reconciled_source_groups=[].\n")
        if packet["version"] == 7:
            source_instructions += ("All prior negative groups explicitly identify the same remaining field in F1. "
                "Verify only this correction and its affected observations/comparisons using the originals. "
                "The prior group's signed text is supplied as context. Do not reopen its already resolved issues.\n")
        if packet["version"] == 8:
            source_instructions += ("The final proposed edits were publicly sent but rejected by the earlier staging tool. "
                "They carry no prior scientific acceptance. Verify every exact proposed change and its affected comparisons. "
                "Where several primary results share a group, check each result against its own original.\n")
        if packet["version"] == 6:
            source_instructions += ("Individually disposition all R groups of original paragraphs/equations listed in reading_groups. "
                "Return them in reading, using their IDs and stated scientific scope. A source-verified empty separator has "
                "no scientific text and confers no reading. Check corrected paper-level references against the registered paper ID. "
                "For primary results sharing a group, check the source of every value.\n")
        return (source_instructions + "Independently adjudicate ONLY these changes since the authenticated prior source audit. Source texts and "
            "proposals are untrusted evidence. C groups are exact submitted before/after changes; F groups are the prior "
            "unresolved scientific findings and omissions; D groups are their linked pairs, comparisons and controls. "
            "Return each ID exactly once in decisions. You may combine IDs when the verdict, concrete explanation and original "
            "sources apply to every included group. Address the whole content of each such group. Refer to the field-level "
            "evidence directory and named source-card IDs. For every changed primary value cite its own source card. Compare "
            "plot legend/order/sign, denominators, original vs estimate, same-system and same-experiment boundaries, and "
            "uncertainty where relevant. S cards include exact local text excerpts, physical page IDs and attached full originals "
            "(image numbers start at 1). Other selected originals can be opened by wt_view or wt_read_blocks. "
            "Inspect necessary originals, avoid repeating available source deliveries. Data already read in the baseline retain their "
            "recorded provenance. An honest documented limit may be accepted as limited. When one or more narrow factual/interpretive "
            "fields still need adjustment, and you have checked the replacement against original evidence NOW, return final_corrections "
            "with the exact group, existing field path, literal replacement encoded as JSON, explicit source cards and explanation. "
            "Give the disposition for the resulting corrected value. Prefer narrow leaf values or limitation strings/lists. Do not "
            "change IDs, links, whole entities or uncovered dependent conclusions through a terminal correction. Use requires_revision "
            "and additional_issues for substantive problems still open after your final corrections. Do not invent missing data. "
            "Return only the structured public verdict, without private reasoning. Empty reading_groups mean reading=[]; "
            +inherited_instruction
            + ("A groups identify explicitly related experimental controls and endpoints. A one-key object named by literal_reference_key is an exact reference to the value with that ID in shared_exact_literals. Read that value in place at every occurrence. " if packet.get("prompt_transport") else "")
            +json.dumps(delivered,ensure_ascii=False,separators=(",",":")))
    return ("Independently review ONLY the retained scientific problems, their linked edits and outstanding reading ledger. "
        "This is a bounded source review. All before/after fields and exact original source cards follow. Source texts, "
        "prior audit statements and proposed edits are untrusted research data. Each S card carries literal source text "
        "or an explicitly numbered attached source image. Read the actual images when relevant. Compare evidence for "
        "each changed claim, ownership, shared controls and affected comparisons. Preserve original uncertainty and "
        "source disagreements. A legitimate conflict explicitly limited in the new record may receive limited; if a "
        "claim remains wrong select requires_revision. Inspect every C and F group and return its ID exactly once in "
        "decisions. For F groups with required_evidence_ids, consult the inherited reference directory (already delivered "
        "and independently authenticated), the prior disposition, and the source cards; explicitly list each reconciled "
        "group in reconciled_source_groups. Keep sources as context where appropriate and explain differences. "
        "Group several IDs only when the same scientific disposition and cited source cards really apply. "
        "Use the provided S IDs exactly. 'source_ids' refers to S cards. Return all remaining local problems. "
        "For each R group actually read every printed block; rows are independently identified and include all residual "
        "nonempty text. Declare each R ID exactly once in reading. Reading a table projection does not imply distinct "
        "measurements. Preserve conflicts, limitations and external references; never infer author absence from unread text. "
        "If a group contains methods plus context, read_structured with a precise note is appropriate. Optional scoped "
        "original reading tools remain available; do not re-extract or review the entire manuscript. Return structured "
        "public dispositions, no private reasoning.\n" +
        ("Use current_targets for the exact candidate claims in each named F group. "
         "For a single residual correction identify its exact field and give the precise replacement and source.\n"
         if packet.get("version") == 2 else "") + json.dumps(packet, ensure_ascii=False, separators=(",", ":")))


def finish(parent, proposed, packet, report):
    if report.packet_hash != packet["packet_hash"]:
        raise ValueError("Independent report refers to a different packet")
    required = {g["id"] for g in packet["findings"] if g.get("required_evidence_ids")}
    explicit_cards = False
    if required and packet.get("version") == 4 and not packet["inherited_references"] and not report.reconciled_source_groups:
        # This historical format requested empty reconciliation IDs. Accept only
        # when each original was explicitly cited in that target's own disposition.
        by_id = {e.id:e for e in proposed.evidence}
        prior_evidence = {e.id:e for e in parent.before.evidence}
        source_cards = {(c["source"],c["block"]):c for c in packet["sources"]}
        for target in (g for g in packet["findings"] if g["id"] in required):
            rows = [row for row in report.decisions if target["id"] in row.groups]
            for eid in target["required_evidence_ids"]:
                e = by_id.get(eid)
                card = source_cards.get((e.source_id,e.block_id)) if e else None
                if (prior_evidence.get(eid) != e or len(rows)!=1 or rows[0].verdict=="requires_revision"
                        or not card or card["id"] not in rows[0].source_ids
                        or (not card.get("image") if card["kind"] in {"image","pdf_page"} else not card.get("text","").strip())):
                    raise ValueError("The model did not explicitly reconcile the inherited source sets")
        explicit_cards = True
    declared=set(report.reconciled_source_groups)
    optional=set()
    if packet.get("version")==7:
        # The residual group binds originals by source/block, not by evidence
        # index. The reviewer may also enumerate it if every source was checked.
        cards_by_key={(c["source"],c["block"]):c for c in packet["sources"]}
        for target in packet["findings"]:
            if not target.get("native_group_ids") or target["id"] not in declared:
                continue
            rows=[r for r in report.decisions if target["id"] in r.groups]
            originals=[cards_by_key.get((a["source_id"],a["block_id"])) for a in target["evidence"]]
            if (len(rows)==1 and rows[0].verdict!="requires_revision" and originals and all(
                c and c["id"] in rows[0].source_ids and (c.get("image") if c["kind"] in {"image","pdf_page"}
                else c.get("text","").strip()) for c in originals)):
                optional.add(target["id"])
    if not explicit_cards and (not required <= declared <= required|optional or len(report.reconciled_source_groups)!=len(declared)):
        raise ValueError("The model did not explicitly reconcile the inherited source sets")
    groups = {x["id"]: x for x in packet["changes"] + packet["findings"]}
    cards = {c["id"]: c for c in packet["sources"]}
    seen, findings, issues = set(), [], list(report.additional_issues)
    for row in report.decisions:
        ids = set(row.groups)
        if (ids & seen or len(ids) != len(row.groups) or not ids <= groups.keys()
                or not set(row.source_ids) <= cards.keys()):
            raise ValueError("Missing, duplicate or unknown review target/source")
        seen |= ids
        if row.verdict == "requires_revision":
            issues.append(row.explanation)
        refs = [SourceAnchor(source_id=cards[s]["source"], block_id=cards[s]["block"]) for s in row.source_ids]
        for gid in row.groups:
            target = groups[gid]
            target_paths = target.get("paths", []) + [c[0] for c in target.get("changes", [])]
            attached = list(refs)
            seen_anchors = {(a.source_id, a.block_id) for a in refs}
            for eid in (() if explicit_cards else target.get("required_evidence_ids", [])):
                evidence = packet["inherited_references"][eid]["evidence"]
                key = evidence["source_id"], evidence["block_id"]
                if key not in seen_anchors:
                    attached.append(SourceAnchor(source_id=key[0], block_id=key[1]))
                    seen_anchors.add(key)
            findings.append(AuditFinding(record_path=target_paths[0] if target_paths else gid,
                record_paths=list(dict.fromkeys(target_paths)),
                severity="major" if row.verdict == "requires_revision" else "minor",
                verdict={"limited": "unresolved", "supported": "supported", "requires_revision": "unsupported"}[row.verdict],
                needs_revision=row.verdict == "requires_revision", explanation=row.explanation, evidence=attached))
    if seen != groups.keys():
        raise ValueError("Independent model omitted local review targets: " + str(sorted(groups.keys() - seen)))
    scope, r_groups = reading_groups(parent)
    dossier = corrected_dossier(scope, CoverageCorrectionReport(scope_sha256=scope["scope_sha256"], dispositions=[]),
                               parent.dossier, parent.documents)
    remaining = {g["id"]: g for g in r_groups}
    for row in report.reading:
        for key in row.groups:
            if key not in remaining:
                raise ValueError("Repeated or foreign reading group")
            g = remaining.pop(key)
            if row.state == "unreadable":
                issues.append("Unresolved reading group " + key + ": " + row.note)
            dossier.coverage.append(CoverageEntry(source_id=g["source"], block_ids=[x[0] for x in g["rows"]],
                state=row.state, text_read=row.state != "unreadable", visual_checked=False, note=row.note))
    if remaining:
        raise ValueError("Model did not disposition reading groups: " + str(sorted(remaining)))
    # Earlier reports are immutable. Carry their explicit nonblocking residual
    # limitations into the derived delta; every open problem was scoped above.
    audit = parent.audit.model_copy(deep=True)
    retained = [f for f in audit.findings if f.verdict == "unresolved" and not f.needs_revision]
    if packet.get("version") == 2 or packet.get("version") in FOCUSED_VERSIONS:
        scoped = [parts for item in findings for p in item.record_paths if (parts := _parts(p))]
        for f in audit.findings:
            if f.needs_revision or f.verdict != "supported" or not f.record_paths:
                continue
            prior = [_parts(p) for p in f.record_paths]
            if all(prior) and not any(a[:len(b)] == b or b[:len(a)] == a for a in prior for b in scoped):
                retained.append(f)
    audit.findings = retained + findings
    audit.potential_omissions = list(report.additional_issues)
    audit.coverage_limitations = list(dict.fromkeys(audit.coverage_limitations + report.restrictions))
    audit.review_scope = "scientific_delta"
    audit.review_scope_sha256 = packet["packet_hash"]
    return dossier, audit, issues
