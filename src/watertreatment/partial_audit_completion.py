"""Append missing dispositions to a completed, not-yet-admitted partial audit.

Pure scope/merge checks only. The caller must authenticate the original model
answer and replay the delivery progress and subsequent union proof separately.
Previously delivered sources are inherited explicitly, never counted as a new
read. Neither a report supplement nor delivery receipts validate the science.
"""
from __future__ import annotations

import copy
import json

from pydantic import Field

from .citation_audit import _parts, _prefix, _resolve
from .domain import ExtractionRecord, StrictModel
from .reading import AuditFinding, ReadingAudit
from .scientific_revision_audit import finding_paths, validate_scientific_revision_audit
from .storage import digest_json
from .word_review_links import AMENDMENT as LINKED_AMENDMENT, FIGURE, LINKED_POLICY


POLICY = "bounded-partial-audit-completion-v1"
MAX_TARGETS = 16
MAX_QUERY_PATHS = 32
MAX_NEW_SOURCE_BLOCKS = 6
_CHANGED = "Changed claim lacks explicit source review: "
_ENTITY = "Affected entity/dependency lacks explicit source disposition: "
_PRIOR = "Previous blocking claim was not reconsidered: "


class PartialAuditCompletionRejected(ValueError):
    """A recovery boundary; never permission for an automatic full-paper audit."""


class PartialAuditAddendum(StrictModel):
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_audit_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: list[AuditFinding] = Field(min_length=1, max_length=48)
    coverage_limitations: list[str] = Field(default_factory=list)
    scientific_concerns: list[str] = Field(default_factory=list)


def _pair(item):
    return item["source_id"], item["block_id"]


def _plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _blockers(audit):
    return [f for f in audit.findings if f.needs_revision or f.verdict in {"unsupported", "not_checked"}]


def _native_complete(requirement, structured, full_views):
    native = requirement.get("native_word_structure")
    if native is None:
        return None
    if native["representation"] == FIGURE:
        valid = (native["visual_status"] == "registered_occurrence_and_separate_caption"
                 and native["caption_block_id"] in native["block_ids"]
                 and native["full_view_block_ids"] == [native["occurrence_block_id"]])
    else:
        valid = (native["representation"] == "native_word_table_xml"
                 and native["visual_status"] == "native_table_has_no_registered_page_view"
                 and native["parent_table_block_id"] in native["block_ids"]
                 and set(native["full_view_block_ids"]).issubset(native["block_ids"]))
    if (not valid or requirement["requires_full_view"] is not False
            or not native["limitations"]
            or requirement["block_id"] not in native["block_ids"]
            or len(native["block_ids"]) != len(set(native["block_ids"]))):
        raise PartialAuditCompletionRejected("Malformed native Word source structure obligation")
    sid = requirement["source_id"]
    return (all((sid, bid) in structured for bid in native["block_ids"])
            and all((sid, bid) in full_views for bid in native["full_view_block_ids"]))


def _progress_partition(scope, progress):
    """Check the shape and exact partition of an externally replayed receipt."""
    if (progress.get("policy") != "bounded-source-delivery-progress-v1"
            or progress.get("review_scope_sha256") != scope["scope_sha256"]
            or progress.get("progress_sha256") != digest_json({k: v for k, v in progress.items()
                                                              if k != "progress_sha256"})):
        raise PartialAuditCompletionRejected("Source progress is not bound to the exact original scope")
    requirements = progress["requirements"]
    required = {_pair(r): r for r in requirements}
    if len(required) != len(requirements):
        raise PartialAuditCompletionRejected("Source progress repeats a requirement")
    anchor_ids = {}
    for anchor in scope["source_anchors"]:
        anchor_ids.setdefault(_pair(anchor), set()).add(anchor["evidence_id"])
    if set(required) != set(anchor_ids) or any(
            set(r["evidence_ids"]) != anchor_ids[p] or type(r["requires_full_view"]) is not bool
            for p, r in required.items()):
        raise PartialAuditCompletionRejected("Source progress omits or changes original source requirements")
    delivered = {_pair(x) for x in progress["delivered"]}
    full_views = {_pair(x) for x in progress["full_views"]}
    if not full_views.issubset(delivered):
        raise PartialAuditCompletionRejected("A claimed full view was not delivered")
    unread = {p for p in required if p not in delivered}
    unviewed = {p for p, r in required.items() if r["requires_full_view"] and p not in full_views}
    structured = {_pair(x) for x in progress.get("structured_blocks", [])}
    native_incomplete = {p for p, r in required.items() if _native_complete(r, structured, full_views) is False}
    if any("native_word_structure" in r for r in requirements) and (
            scope.get("source_requirement_policy") not in {"native-word-structure-v1", LINKED_POLICY}):
        raise PartialAuditCompletionRejected("Legacy source requirements cannot silently acquire native table substitutions")
    for field, expected in (("missing_blocks", unread), ("missing_full_views", unviewed)):
        entries = progress[field]
        if (len(entries) != len(expected) or {_pair(x) for x in entries} != expected
                or any(x != required[_pair(x)] for x in entries)):
            raise PartialAuditCompletionRejected("Source progress has an inconsistent missing-source partition")
    native_entries = progress.get("missing_native_structures", [])
    if (len(native_entries) != len(native_incomplete) or {_pair(x) for x in native_entries} != native_incomplete
            or any(x != required[_pair(x)] for x in native_entries)):
        raise PartialAuditCompletionRejected("Source progress has an inconsistent native-structure partition")
    if progress["complete"] is not (not unread and not unviewed and not native_incomplete):
        raise PartialAuditCompletionRejected("Source progress completeness contradicts its gaps")
    return unread | unviewed | native_incomplete, set(required) - unread - unviewed - native_incomplete


def _amended_requirements(original_scope, source_progress, amendment):
    """Bind a separately source-replayed native Word requirement correction.

    This does not rewrite or rehash historical progress. SourceDocument kinds,
    table children, source bytes and receipts must be checked by source_review.
    """
    requirements = copy.deepcopy(source_progress["requirements"])
    if amendment is None:
        return requirements
    if (amendment.get("policy") not in {"native-word-source-requirement-amendment-v1", LINKED_AMENDMENT}
            or amendment.get("review_scope_sha256") != original_scope["scope_sha256"]
            or amendment.get("source_bundle_sha256") != source_progress["source_bundle_sha256"]
            or amendment.get("amendment_sha256") != digest_json({k: v for k, v in amendment.items()
                                                               if k != "amendment_sha256"})):
        raise PartialAuditCompletionRejected("Native Word amendment is not bound to original scope and source bundle")
    originals = {_pair(r): r for r in source_progress["requirements"]}
    missing_views = {_pair(r) for r in source_progress["missing_full_views"]}
    replacements, seen = {}, set()
    for substitution in amendment["substitutions"]:
        original = substitution["original_requirement"]
        pair = _pair(original)
        native = substitution["native_word_structure"]
        if (pair in seen or pair not in missing_views or original != originals[pair]
                or original["requires_full_view"] is not True
                or substitution["replacement_requires_full_view"] is not False
                or (native["representation"] == FIGURE and amendment["policy"] != LINKED_AMENDMENT)):
            raise PartialAuditCompletionRejected("Native Word amendment drops or invents a source requirement")
        seen.add(pair)
        replacements[pair] = {**copy.deepcopy(original), "requires_full_view": False,
                              "native_word_structure": copy.deepcopy(native)}
        _native_complete(replacements[pair], set(), set())
    if not 1 <= len(seen) <= MAX_NEW_SOURCE_BLOCKS:
        raise PartialAuditCompletionRejected("Native Word amendment has an invalid bounded substitution set")
    return [replacements.get(_pair(r), r) for r in requirements]


def build_partial_audit_completion_scope(original_scope, completed_audit, *, old_record, new_record,
                                         baseline_audit, source_progress, additional_audits=(),
                                         requirement_amendment=None):
    """Select only exact missing reports and still-undelivered source blocks.

    Changed children may be covered by an already-required affected parent. The
    exact child paths remain required candidate queries and named obligations.
    Other malformed proofs, scientific blockers or missing inherited limits do
    not qualify. Source-progress byte authenticity remains the caller's duty.
    """
    try:
        parent = ReadingAudit.model_validate(completed_audit)
        record = ExtractionRecord.model_validate(new_record)
        if parent.potential_omissions or _blockers(parent):
            raise PartialAuditCompletionRejected("Completed partial audit contains scientific blockers or omissions")
        issues = validate_scientific_revision_audit(original_scope, baseline_audit, parent,
            old_record=old_record, new_record=record, additional_audits=additional_audits)
        linked = (original_scope.get("source_requirement_policy") == LINKED_POLICY
                  or (requirement_amendment or {}).get("policy") == LINKED_AMENDMENT)
        supported = (_CHANGED, _ENTITY, _PRIOR) if linked else (_CHANGED, _ENTITY)
        if any(not issue.startswith(supported) for issue in issues):
            raise PartialAuditCompletionRejected("Original scientific contract has unsupported recovery issues: " + "; ".join(issues))
        missing, inherited = _progress_partition(original_scope, source_progress)
        requirements = _amended_requirements(original_scope, source_progress, requirement_amendment)
        inherited_native = []
        structured = {_pair(x) for x in source_progress.get("structured_blocks", [])}
        full_views = {_pair(x) for x in source_progress["full_views"]}
        for requirement in requirements:
            pair = _pair(requirement)
            if _native_complete(requirement, structured, full_views) is True and pair in missing:
                # Replayed earlier XML delivery can already satisfy an explicit
                # source-kind correction. It is never described as a new read.
                missing.remove(pair)
                inherited.add(pair)
                inherited_native.append(copy.deepcopy(requirement))
        if not issues and not missing:
            if not linked or not inherited_native:
                raise PartialAuditCompletionRejected("There is no missing partial-audit work to complete")
            # A corrected caption/object association still needs a scientist's
            # model disposition, even if the old call received both artifacts.
            missing = {_pair(r) for r in inherited_native}
            inherited -= missing
            inherited_native = []
        if len(missing) > MAX_NEW_SOURCE_BLOCKS:
            raise PartialAuditCompletionRejected("Missing source delivery exceeds the bounded recovery budget")
        anchors = {a["evidence_id"]: a for a in original_scope["source_anchors"]}
        changes = {c["changed_path"]: c for c in original_scope["changes"]}
        refs = {r["record_path"]: r for r in original_scope["entity_refs"] if r["role"] == "affected"}
        required_queries, raw_targets = set(), {}
        data = record.model_dump(mode="json")

        def add(path, reason, ids):
            _resolve(data, _parts(path))
            if not ids or not set(ids).issubset(anchors):
                raise PartialAuditCompletionRejected("Completion target lacks exact source obligations: " + path)
            required_queries.add(path)
            item = raw_targets.setdefault(path, {"record_path": path, "reasons": set(),
                                                "required_evidence_ids": set(), "covered_paths": set()})
            item["reasons"].add(reason)
            item["required_evidence_ids"].update(ids)
            item["covered_paths"].add(path)

        for issue in issues:
            if issue.startswith(_CHANGED):
                path = issue[len(_CHANGED):]
                add(path, "missing_changed_claim_disposition", changes[path]["required_evidence_ids"])
            elif issue.startswith(_PRIOR):
                path = issue[len(_PRIOR):]
                required_blocks = {tuple(pair) for row in original_scope["blocking_obligations"]
                                   if path in row["paths"] for pair in row["source_blocks"]}
                ids = [eid for eid, a in anchors.items() if _pair(a) in required_blocks]
                if not required_blocks or {_pair(anchors[eid]) for eid in ids} != required_blocks:
                    raise PartialAuditCompletionRejected("Unbound prior blocking claim: " + path)
                add(path, "missing_prior_blocker_disposition", ids)
            else:
                path = issue[len(_ENTITY):]
                add(path, "missing_affected_entity_disposition", refs[path]["required_evidence_ids"])
        # A missing view must be reconsidered against its consumers, even when
        # the unfinished answer already cited it as if it had been viewed.
        mapped_missing = set()
        for path, ref in refs.items():
            touched = {_pair(anchors[eid]) for eid in ref["required_evidence_ids"]} & missing
            if touched:
                add(path, "missing_source_delivery", ref["required_evidence_ids"])
                mapped_missing.update(touched)
        if mapped_missing != missing:
            raise PartialAuditCompletionRejected("A missing source has no exact affected consumer; broader recovery is required")
        # Parent coverage is explicit, not an inferred blanket disposition.
        for path in sorted(list(raw_targets), key=lambda p: len(_parts(p)), reverse=True):
            parents = [p for p in raw_targets if p != path and _prefix(_parts(p), _parts(path))]
            if parents:
                parent_path = max(parents, key=lambda p: len(_parts(p)))
                child = raw_targets.pop(path)
                for field in ("reasons", "required_evidence_ids", "covered_paths"):
                    raw_targets[parent_path][field].update(child[field])
        batch_paths = []
        if not 1 <= len(raw_targets) <= MAX_TARGETS or len(required_queries) > MAX_QUERY_PATHS:
            # Only a report with authenticated complete source delivery can
            # be paginated. Every consumer still needs a query and a source
            # disposition. This adds neither a model turn nor a time grant.
            if (not linked or missing or not raw_targets or not issues
                    or any(not issue.startswith(_ENTITY) for issue in issues)
                    or set(raw_targets) != required_queries or len(raw_targets) > 3 * MAX_TARGETS):
                raise PartialAuditCompletionRejected("Completion target/query set exceeds the bounded recovery budget")
            paths = sorted(raw_targets)
            batch_paths = [paths[i:i + MAX_TARGETS] for i in range(0, len(paths), MAX_TARGETS)]
        targets, selected_ids = [], set()
        for path, item in sorted(raw_targets.items()):
            target = {k: sorted(v) if isinstance(v, set) else v for k, v in item.items()}
            target["value_hash"] = digest_json(_resolve(data, _parts(path)))
            targets.append(target)
            selected_ids.update(target["required_evidence_ids"])
        new_anchors = [copy.deepcopy(a) for a in original_scope["source_anchors"] if _pair(a) in missing]
        inherited_anchors = [copy.deepcopy(anchors[eid]) for eid in sorted(selected_ids)
                             if _pair(anchors[eid]) in inherited]
        selected_blocks = {_pair(anchors[eid]) for eid in selected_ids}
        scope = {"policy": POLICY, "parent_scope_sha256": original_scope["scope_sha256"],
            "parent_audit_hash": digest_json(parent.model_dump(mode="json")),
            "candidate_hash": digest_json(data), "old_record_hash": original_scope["old_record_hash"],
            "baseline_audit_hash": original_scope["baseline_audit_hash"],
            "additional_audit_hashes": list(original_scope["additional_audit_hashes"]),
            "source_progress_sha256": source_progress["progress_sha256"],
            "targets": targets, "required_query_paths": sorted(required_queries),
            "changed_claims": [copy.deepcopy(changes[p]) for p in sorted(required_queries) if p in changes],
            "source_anchors": new_anchors, "inherited_source_anchors": inherited_anchors,
            "source_delivery_requirements": [copy.deepcopy(r) for r in requirements
                if _pair(r) in selected_blocks],
            "source_requirement_amendment": copy.deepcopy(requirement_amendment),
            "inherited_native_source_dispositions": inherited_native,
            "required_coverage_limitations": sorted({limit for r in requirements
                if _pair(r) in selected_blocks
                for limit in r.get("native_word_structure", {}).get("limitations", [])}),
            "parent_protocol_issues": list(issues),
            "scope_limits": {"max_targets": MAX_TARGETS, "max_query_paths": MAX_QUERY_PATHS,
                             "max_new_source_blocks": MAX_NEW_SOURCE_BLOCKS, "compiler_version": 2},
            "interpretation": "Only source_anchors require new source delivery. inherited_source_anchors were delivered in the authenticated earlier partial call and remain fallible inherited evidence, not a new read. Query every required_query_path. Append dispositions without editing candidate or deleting earlier findings/limits. Original full scientific scope and union delivery must both pass before admission."}
        if linked:
            scope["source_requirement_policy"] = LINKED_POLICY
            if batch_paths:
                scope["report_query_batches"] = batch_paths
                scope["scope_limits"]["max_report_only_batches"] = 3
            occurrences = {(r["source_id"], bid) for r in requirements
                           for bid in r.get("native_word_structure", {}).get("full_view_block_ids", [])}
            scope["word_figure_witnesses"] = [copy.deepcopy(a) for a in original_scope["source_anchors"]
                if _pair(a) in occurrences and (a.get("locator") or {}).get("figure")]
        scope["scope_sha256"] = digest_json(scope)
        return scope
    except PartialAuditCompletionRejected:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise PartialAuditCompletionRejected("Malformed or unbound partial-audit recovery inputs") from exc


def merge_partial_audit_addendum(scope, addendum, completed_audit, *, original_scope, old_record,
                                 new_record, baseline_audit, source_progress, additional_audits=(),
                                 requirement_amendment=None, contextual_citations=()):
    """Append only; scientific blockers are preserved, never silently resolved.

    A merged object is not approval. validate_partial_audit_addendum and source
    union/candidate-query receipt checks must succeed before it is admitted.
    """
    expected = build_partial_audit_completion_scope(original_scope, completed_audit,
        old_record=old_record, new_record=new_record, baseline_audit=baseline_audit,
        source_progress=source_progress, additional_audits=additional_audits,
        requirement_amendment=requirement_amendment)
    if scope != expected:
        raise PartialAuditCompletionRejected("Completion scope differs from the exact frozen inputs")
    supplement = PartialAuditAddendum.model_validate(addendum)
    for key in ("scope_sha256", "parent_audit_hash", "candidate_hash"):
        if getattr(supplement, key) != scope[key]:
            raise PartialAuditCompletionRejected("Addendum has an incorrect " + key)
    data = ExtractionRecord.model_validate(new_record).model_dump(mode="json")
    allowed = {_parts(t["record_path"]) for t in scope["targets"]}
    allowed.update(_parts(p) for p in scope["required_query_paths"])
    known_sources = {_pair(a) for a in original_scope["source_anchors"]}
    for finding in supplement.findings:
        if finding.omission_indices:
            raise PartialAuditCompletionRejected("Addendum findings cannot invent parent omission indices")
        if finding.needs_revision or finding.verdict in {"unsupported", "not_checked"}:
            continue  # Preserve a new concern even outside the successful local scope.
        if any(p not in allowed for p in finding_paths(finding, data)):
            raise PartialAuditCompletionRejected("Nonblocking addendum finding exceeds exact permitted paths")
        if any(_pair(_plain(e)) not in known_sources and _pair(_plain(e)) not in contextual_citations for e in finding.evidence):
            raise PartialAuditCompletionRejected("Nonblocking addendum cites a source outside the original scientific scope")
    merged = ReadingAudit.model_validate(completed_audit).model_copy(deep=True)
    merged.findings.extend(f.model_copy(deep=True) for f in supplement.findings)
    merged.coverage_limitations.extend(x for x in supplement.coverage_limitations
                                       if x not in merged.coverage_limitations)
    merged.potential_omissions.extend(supplement.scientific_concerns)
    return merged


def validate_partial_audit_addendum(scope, addendum, completed_audit, *, original_scope, old_record,
                                    new_record, baseline_audit, source_progress, additional_audits=(),
                                    requirement_amendment=None, contextual_citations=()):
    """Validate new exact dispositions and then the entire original SCI scope."""
    try:
        merged = merge_partial_audit_addendum(scope, addendum, completed_audit,
            original_scope=original_scope, old_record=old_record, new_record=new_record,
            baseline_audit=baseline_audit, source_progress=source_progress, additional_audits=additional_audits,
            requirement_amendment=requirement_amendment, contextual_citations=contextual_citations)
        supplement = PartialAuditAddendum.model_validate(addendum)
        issues = validate_scientific_revision_audit(original_scope, baseline_audit, merged,
            old_record=old_record, new_record=new_record, additional_audits=additional_audits)
        data = ExtractionRecord.model_validate(new_record).model_dump(mode="json")
        anchors = {a["evidence_id"]: _pair(a) for a in original_scope["source_anchors"]}
        parsed = [(finding_paths(f, data), f) for f in supplement.findings
                  if not (f.needs_revision or f.verdict in {"unsupported", "not_checked"})]
        for target in scope["targets"]:
            path = _parts(target["record_path"])
            actual = {_pair(_plain(e)) for paths, f in parsed if path in paths for e in f.evidence}
            required = {anchors[eid] for eid in target["required_evidence_ids"]}
            if not required.issubset(actual):
                issues.append("Completion addendum lacks an exact source disposition: " + target["record_path"])
        if supplement.scientific_concerns or merged.potential_omissions:
            issues.append("Completion addendum retains scientific concerns or omissions")
        if not set(scope["required_coverage_limitations"]).issubset(merged.coverage_limitations):
            issues.append("Completion addendum drops required native-source representation limitations")
        inherited_unresolved = {digest_json(f.model_dump(mode="json")) for f in
                                ReadingAudit.model_validate(completed_audit).findings if f.verdict == "unresolved"}
        for finding in supplement.findings:
            if (finding.needs_revision or finding.verdict in {"unsupported", "not_checked"}
                    or (finding.verdict == "unresolved" and digest_json(finding.model_dump(mode="json"))
                        not in inherited_unresolved)):
                issues.append("Completion addendum reports a new scientific concern: " + finding.record_path)
        return issues
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return ["Malformed or unbound partial-audit addendum; completion is not permitted"]


def partial_audit_completion_prompt(scope, completed_audit, *, version=1, documents=(), drafts=None):
    if type(version) is not int or version not in {1, 2, 3}:
        raise ValueError("unknown partial-completion prompt version")
    parent = ReadingAudit.model_validate(completed_audit)
    if version == 3:
        from .partial_report_transport import report_prompt
        return report_prompt(scope, parent, documents, drafts or {})
    if version == 2:
        legacy = partial_audit_completion_prompt(scope, parent, version=1)
        if scope.get("source_anchors") == []:
            focus = (
                "REPORT-DISPOSITION COMPLETION ONLY. The authenticated scope contains ZERO new source "
                "delivery obligations; the source union is checked by the controller separately. Start with "
                "wt_query_candidate mode=paths for required_query_paths, in batches of at most 20 exact paths "
                "and within the tool's response-size limit. Query each required path; do not scan the whole "
                "candidate. Use the retained parent findings, exact before/after targets and inherited source "
                "dispositions to write ONLY the missing explicit target dispositions. Do not routinely reopen "
                "all cited figures or repeat the whole audit. Inherited delivery is NOT scientific approval. "
                "If inherited information is insufficient to judge a target, say so as a blocking finding or "
                "scientific_concerns; do not manufacture a supported verdict to finish within budget. Necessary "
                "targeted source checks remain available, but do not start another general reading pass. "
                "Preserve inherited uncertainties and required coverage limitations. Finish with the bounded "
                "PartialAuditAddendum; a completed query or source receipt is not the required final answer.\n"
            )
        else:
            focus = (
                "MISSING-SOURCE AND REPORT COMPLETION. Query exact required_query_paths in bounded batches, "
                "read only the specified missing source anchors and necessary context, then submit the "
                "PartialAuditAddendum. Preserve inherited findings and limits; new scientific concerns must "
                "block completion rather than trigger an unbounded whole-paper audit.\n"
            )
        return focus + legacy
    return ("BOUNDED COMPLETION OF AN UNADMITTED PARTIAL SCIENTIFIC AUDIT. No extraction or candidate edit. "
        "The earlier answer completed but is NOT approved: it missed source delivery/report obligations. "
        "Use wt_query_candidate for EVERY exact required_query_path. A named parent target may disposition its "
        "listed children, but do not skip the child's query or before/after comparison. Query only necessary context. "
        "Read every source_anchors block now using its source_delivery_requirements. View a COMPLETE page/image "
        "occurrence only when actually visual, before any crop. Native Word table containers/cells require the "
        "registered native text/structure disposition and relevant actual table content, never a fabricated image "
        "view; retain the delivery-mode limitations explicitly. inherited_source_anchors were byte-authenticated as delivered in the earlier call; their "
        "findings are fallible inherited context, NOT newly read sources. Do not unnecessarily repeat these reads. "
        "For every target, check the missing sources and their consequences against its exact claims and all "
        "required_evidence_ids; append an explicit source-grounded disposition listing the exact target path. "
        "Cite both inherited and newly read required anchors with an honest explanation of what was newly checked. "
        "Return ONLY PartialAuditAddendum, bound to scope_sha256, parent_audit_hash and candidate_hash. "
        "Never return an entire new ReadingAudit. Existing findings, uncertainties, quantitative followups and "
        "coverage limitations will be retained verbatim. Include EVERY required_coverage_limitations string "
        "verbatim in addendum.coverage_limitations, plus any other relevant restriction. If any new scientific problem, "
        "unresolved question or out-of-scope dependency prevents approval, return a blocking finding and/or "
        "scientific_concerns; do not force a supported verdict merely to repair the report protocol. "
        "No candidate values or conditions can change in this completion.\nCOMPLETION SCOPE:\n"
        + json.dumps(_prompt_scope(scope), ensure_ascii=False, separators=(",", ":"))
        + "\nCOMPLETED BUT UNADMITTED PARENT AUDIT (retained, fallible):\n"
        + json.dumps(parent.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")))


def _prompt_scope(scope):
    if scope.get("source_requirement_policy") != LINKED_POLICY:
        return scope
    from .source_review import review_navigation
    projected = dict(scope)
    for key in ("source_delivery_requirements", "inherited_native_source_dispositions"):
        projected[key] = review_navigation(scope[key])
    return projected
