"""Source-bound incremental reviews with exact baseline and dependency bindings."""
from __future__ import annotations

import json
import copy
from pathlib import Path

from .domain import ExtractionRecord
from .word_review_links import LINKED_POLICY
from .reading import ReadingAudit
from .reading_coverage_state import only_deferred_coverage_issues
from .storage import digest_json, hash_file, write_new_json
from .citation_audit import build_citation_audit_scope, validate_citation_audit
from .bounded_revision_audit import (
    POLICY as REVISION_POLICY, build_revision_audit_scope, validate_revision_audit,
)
from .scientific_revision_audit import (
    POLICY as SCIENTIFIC_POLICY, NEW_SCOPE_LIMITS, build_scientific_revision_scope, validate_scientific_revision_audit,
    scope_rejection,
)


class ScientificReviewScopeUnavailable(ValueError):
    """Pause a repaired candidate with a persisted reason instead of a full audit."""

    def __init__(self, diagnostics):
        self.diagnostics = copy.deepcopy(diagnostics)
        detail = "; ".join(f"{x['code']}: {x['message']}" for x in diagnostics)
        super().__init__("Localized source review is recoverably blocked; automatic full review is disabled. " + detail)


def _clean_history(item):
    # Pending dossier dispositions are repaired separately; they must not force
    # a repeat full scientific audit or disappear from final completion checks.
    return (only_deferred_coverage_issues(item.get("reading_check_issues"))
            and item.get("validation_error_count", item.get("validation_errors")) == 0)


def _receipt_failure(diagnostics, code, message):
    scope_rejection(diagnostics, code, message)
    return None


def _load_bound(path, sha256, run, model):
    path = Path(path).resolve(strict=True)
    if path.parent != Path(run).resolve(strict=True) or hash_file(path) != sha256:
        raise ValueError("scientific audit artifact changed or escaped its frozen run")
    return model.model_validate_json(path.read_bytes())


def _exact_scientific_limits(value):
    """Preserve the legacy two-field algorithm or the explicit split budgets."""
    legacy = {"max_entities", "max_changes"}
    if (not isinstance(value, dict) or not legacy.issubset(value)
            or set(value) - legacy - {"max_context_entities", "compiler_version", "source_requirement_policy"}
            or any(type(v) is not int or not 1 <= v <= 240 for k, v in value.items()
                   if k not in {"compiler_version", "source_requirement_policy"})
            or ("source_requirement_policy" in value
                and value["source_requirement_policy"] not in {"native-word-structure-v1", LINKED_POLICY})
            or ("compiler_version" in value and (type(value["compiler_version"]) is not int
                                                 or value["compiler_version"] not in {1, 2, 3, 4, 5, 6}))):
        raise ValueError("scientific scope limits must be explicit bounded integer budgets")
    return dict(value)


def select_scientific_scope_limits(run, plan=None, *, previous_scopes=()):
    """Choose explicit current-run budgets without changing historical scope hashes.

    A completed local chain cannot silently change its limits. A new run may
    explicitly freeze new budgets in its plan; its original checkpoint remains
    authenticated under the limits stored in that checkpoint's own scope.
    """
    plan = plan or {}
    planned = _exact_scientific_limits(plan["scientific_scope_limits"]) if "scientific_scope_limits" in plan else None
    prior = []
    for scope in previous_scopes:
        if (not isinstance(scope, dict) or scope.get("policy") != SCIENTIFIC_POLICY
                or scope.get("scope_sha256") != digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})):
            raise ValueError("previous scientific scope is not exactly frozen")
        prior.append(_exact_scientific_limits(scope["scope_limits"]))
    if prior:
        if any(item != prior[0] for item in prior) or (planned is not None and planned != prior[0]):
            raise ValueError("scientific scope budgets changed within the completed local audit chain")
        return prior[0]
    if planned is not None:
        return planned
    proof = plan.get("resumed_full_audit") or {}
    if "scope_limits" in proof:
        return _exact_scientific_limits(proof["scope_limits"])
    if "frozen_initial_scope_path" in proof:
        path = Path(proof["frozen_initial_scope_path"]).resolve(strict=True)
        if path.parent != Path(run).resolve(strict=True) or hash_file(path) != proof["frozen_initial_scope_sha256"]:
            raise ValueError("original scientific scope limits lost their frozen input binding")
        scope = json.loads(path.read_text(encoding="utf-8"))
        if (scope.get("policy") != SCIENTIFIC_POLICY
                or scope.get("scope_sha256") != proof["initial_scope_sha256"]
                or scope.get("scope_sha256") != digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})):
            raise ValueError("original scientific scope identity changed")
        return _exact_scientific_limits(scope["scope_limits"])
    # A retained plan without an explicit compiler predates v3. Installing new
    # code is not authority to change its interpretation boundary. Fresh plans
    # freeze NEW_SCOPE_LIMITS explicitly; a new continuation may select v3 there.
    if plan:
        return _exact_scientific_limits({"max_entities": 200, "max_context_entities": 240,
            "max_changes": 180, "compiler_version": 2,
            "source_requirement_policy": "native-word-structure-v1"})
    return _exact_scientific_limits(NEW_SCOPE_LIMITS)


def _scientific_receipt(run, revision, candidate, history, plan, diagnostics=None):
    """Build cumulative changes from the nearest complete full baseline.

    Preparation is not provenance admission. The application must additionally
    authenticate every retained model call and original source receipt.
    """
    try:
        run = Path(run).resolve(strict=True)
        current_path = run / f"candidate.v{revision:02d}.json"
        if ExtractionRecord.model_validate_json(current_path.read_bytes()) != candidate:
            return _receipt_failure(diagnostics, "current_candidate_binding", "The in-memory candidate differs from its retained version file.")
        baseline_index = next((i for i in range(len(history)-1, -1, -1)
            if history[i].get("audit_origin") == "fresh_model_audit"), None)
        if baseline_index is not None:
            item = history[baseline_index]
            if not _clean_history(item) or item.get("version", baseline_index) != baseline_index:
                return _receipt_failure(diagnostics, "unclean_baseline_history", "The full baseline has unresolved validation/reading checks or a noncontiguous version.")
            baseline_path = run / f"candidate.v{baseline_index:02d}.json"
            old = _load_bound(baseline_path, item["candidate_sha256"], run, ExtractionRecord)
            baseline_audit_path = run / f"audit.v{baseline_index:02d}.json"
            baseline = ReadingAudit.model_validate_json(baseline_audit_path.read_bytes())
            first_additional = baseline_index + 1
        else:
            inherited = (plan or {}).get("resumed_full_audit") or (history[0].get("inherited_full_audit") if history else None)
            if not isinstance(inherited, dict):
                return _receipt_failure(diagnostics, "missing_full_baseline", "No retained complete full-audit provenance is available for the repair.")
            if history and history[0].get("inherited_full_audit") != inherited:
                return _receipt_failure(diagnostics, "inherited_baseline_mismatch", "The frozen inherited baseline disagrees with local history.")
            baseline_path, baseline_audit_path = Path(inherited["frozen_candidate_path"]), Path(inherited["frozen_audit_path"])
            old = _load_bound(baseline_path, inherited["frozen_candidate_sha256"], run, ExtractionRecord)
            baseline = _load_bound(baseline_audit_path, inherited["frozen_audit_sha256"], run, ReadingAudit)
            first_additional = 0
        additional, entries, previous_scopes = [], [], []
        for index in range(first_additional, len(history)):
            item = history[index]
            if (not _clean_history(item) or item.get("version", index) != index
                    or item.get("audit_origin") not in {"scientific_delta_model_audit", "citation_delta_model_audit"}):
                return _receipt_failure(diagnostics, "invalid_intermediate_history", "An intermediate repair has unresolved checks, unsupported origin or a history gap.")
            record_path, audit_path = run / f"candidate.v{index:02d}.json", run / f"audit.v{index:02d}.json"
            _load_bound(record_path, item["candidate_sha256"], run, ExtractionRecord)
            audit = ReadingAudit.model_validate_json(audit_path.read_bytes())
            if audit.review_scope != "scientific_delta":
                return _receipt_failure(diagnostics, "unsupported_intermediate_audit", "The retained intermediate audit is not a scientific delta; do not silently replace it with full review.")
            prior_scope = json.loads((run / f"citation_scope.v{index:02d}.json").read_text(encoding="utf-8"))
            if prior_scope.get("scope_sha256") != audit.review_scope_sha256:
                return _receipt_failure(diagnostics, "intermediate_scope_mismatch", "An intermediate audit does not bind its retained scope hash.")
            previous_scopes.append(prior_scope)
            entries.append({"candidate_path": str(record_path), "candidate_sha256": hash_file(record_path),
                            "audit_path": str(audit_path), "audit_sha256": hash_file(audit_path)})
            additional.append(audit)
        limits_plan = dict(plan or {})
        if "resumed_full_audit" not in limits_plan and baseline_index is None:
            limits_plan["resumed_full_audit"] = inherited
        limits = select_scientific_scope_limits(run, limits_plan, previous_scopes=previous_scopes)
        scope = None
        if len(previous_scopes) == 1 and (plan or {}).get("structural_followup_policy"):
            from .ownership_followup import POLICY, build_followup_scope
            if plan["structural_followup_policy"] == POLICY:
                preceding_record = ExtractionRecord.model_validate_json(Path(entries[0]["candidate_path"]).read_bytes())
                scope = build_followup_scope(old, baseline, preceding_record, candidate, additional[0], previous_scopes[0])
        if scope is None:
            scope = build_scientific_revision_scope(old, candidate, baseline,
                baseline_origin="fresh_model_audit", additional_audits=additional, diagnostics=diagnostics, **limits)
        if scope is None:
            return None
        path = run / f"citation_scope.v{revision:02d}.json"
        write_new_json(path, scope)
        receipt = {"scope_path": str(path), "scope_file_sha256": hash_file(path),
            "baseline_candidate_path": str(baseline_path), "baseline_candidate_sha256": hash_file(baseline_path),
            "baseline_audit_path": str(baseline_audit_path), "baseline_audit_sha256": hash_file(baseline_audit_path),
            "current_candidate_path": str(current_path), "current_candidate_sha256": hash_file(current_path),
            "additional_audits": entries}
        return receipt, scope, baseline
    except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
        scope_rejection(diagnostics, "frozen_audit_binding_unavailable", exc)
        return None


def _prepare_citation_review(run, revision, candidate, history, plan=None, diagnostics=None, settings=None):
    if candidate.rule_version not in {"0.7", "0.8", "0.9"} or type(revision) is not int or revision < 0:
        return _receipt_failure(diagnostics, "unsupported_rule_or_revision", "Only supported rule versions and nonnegative integer revision numbers are eligible.")
    if revision == 0:
        return _scientific_receipt(run, revision, candidate, history, plan, diagnostics) if not history and (plan or {}).get("resumed_full_audit") else None
    if len(history) != revision:
        return _receipt_failure(diagnostics, "audit_history_gap", "The number of completed prior audits differs from the current revision.")
    if (plan or {}).get("audit_guidance"):
        if settings is None:
            return _receipt_failure(diagnostics, "guidance_settings_required", "Authenticated guidance requires the configured source/run store.")
        from .audit_guidance_resume import prepare_guidance_review
        return prepare_guidance_review(settings, run, plan, revision, candidate, history)
    last = history[-1]
    if not _clean_history(last):
        return _receipt_failure(diagnostics, "unclean_prior_history", "The preceding audit has unresolved machine/reading checks; preserve and resolve them before local review.")
    if last.get("audit_origin") != "fresh_model_audit":
        return _scientific_receipt(run, revision, candidate, history, plan, diagnostics)
    previous = run / f"candidate.v{revision-1:02d}.json"
    report = run / f"audit.v{revision-1:02d}.json"
    if last.get("candidate_sha256") != hash_file(previous):
        return _receipt_failure(diagnostics, "prior_candidate_hash_mismatch", "The preceding candidate no longer matches its recorded hash.")
    old = ExtractionRecord.model_validate_json(previous.read_bytes())
    baseline = ReadingAudit.model_validate_json(report.read_bytes())
    if baseline.review_scope != "full" or baseline.review_scope_sha256 is not None:
        return _receipt_failure(diagnostics, "invalid_full_audit_binding", "The preceding audit is not an unscoped full baseline.")
    linked = (plan or {}).get("scientific_scope_limits", {}).get("source_requirement_policy") == LINKED_POLICY
    scope = (build_citation_audit_scope(old, candidate, baseline, baseline_origin=last["audit_origin"])
             or build_revision_audit_scope(old, candidate, baseline, baseline_origin=last["audit_origin"],
                                           source_requirement_policy=LINKED_POLICY if linked else None))
    if scope is None:
        return _scientific_receipt(run, revision, candidate, history, plan, diagnostics)
    path = run / f"citation_scope.v{revision:02d}.json"
    write_new_json(path, scope)
    receipt = {"scope_path": str(path), "scope_file_sha256": hash_file(path),
               "baseline_candidate_path": str(previous), "baseline_candidate_sha256": hash_file(previous),
               "baseline_audit_path": str(report), "baseline_audit_sha256": hash_file(report)}
    return receipt, scope, baseline


def prepare_citation_review(run, revision, candidate, history, plan=None, *, diagnostics=None, settings=None):
    """Compile a local scope, exposing rejection without silently scheduling full review.

    The None return is retained for frozen/legacy callers. Runtime workflows must
    persist diagnostics and stop recoverably after a repair if no scope exists.
    Initial extraction (no prior full baseline) still requires its first full audit.
    """
    collected = diagnostics if diagnostics is not None else []
    start = len(collected)
    try:
        result = _prepare_citation_review(Path(run), revision, candidate, history, plan, collected, settings)
    except ScientificReviewScopeUnavailable as exc:
        collected.extend(exc.diagnostics)
        return None
    except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
        scope_rejection(collected, "frozen_audit_binding_unavailable", exc)
        return None
    if result is None and len(collected) == start:
        initial = revision == 0 and not history and not (plan or {}).get("resumed_full_audit")
        scope_rejection(collected, "initial_full_audit_required" if initial else "incomplete_or_unbound_audit_history",
            "No full scientific audit exists yet." if initial else
            "A complete authenticated full baseline and contiguous checked local audit history are required; inspect retained candidates/audits before resuming.")
    return result


def citation_review_prompt(prepared, candidate):
    receipt, scope, baseline = prepared
    design_context = ""
    if candidate.design_scope is not None:
        # Scope/eligibility are interpretation dependencies of the changed
        # claims. Keep them queryable without re-sending the full manuscript.
        design_context = (
            "\nTHREE-LAYER INTERPRETATION DEPENDENCIES: Use wt_query_candidate paths under design_scope "
            "to inspect observation_assignments, modules, source_index and eligibility that refer to the "
            "affected entity IDs/source blocks listed below. Check implications for retained purpose, "
            "deferral and allowed comparisons. Unchanged scope labels are not renewed approval. If their "
            "claims/eligibility need revision, report a blocking finding; a scope-contract change requires "
            "broader review. Do not reclassify an adverse result or design control as optional detail.\n")
    if scope.get("policy") == SCIENTIFIC_POLICY:
        _, _, additional = _verified_scientific_inputs(prepared)
        return scientific_review_prompt(scope, baseline, additional)
    if scope.get("policy") == REVISION_POLICY:
        return revision_review_prompt(scope, baseline) + design_context
    data = candidate.model_dump(mode="json", exclude_defaults=True)
    entities = [{"record_path": ref["record_path"], "scope_role": ref["role"],
                 "entity": data[ref["section"]][ref["index"]]} for ref in scope["entity_refs"]]
    return ("CITATION-ONLY SOURCE RECHECK. This is not a fresh full-paper audit. The frozen baseline covered all nine "
        "dimensions; only specified Fact.evidence_ids changed. Inspect every affected/context entity and its exact "
        "source claims, close all previous blockers, and look for same-family citation errors in the declared dependency scope. "
        "Use wt_query_candidate for full entities/fields (at least one complete section/path read), and wt_ source tools. "
        "Keep unaffected baseline findings, uncertainties, coverage limitations and followups; do not certify them as newly checked. "
        "If scope is insufficient, emit a blocking finding explaining the required broader review. Return a complete current "
        "ReadingAudit with review_scope=citation_delta, review_scope_sha256 exactly as specified, all nine cumulative "
        "checked_dimensions and dimension_notes explicitly distinguishing inherited coverage from this recheck. "
        "Do not reread unrelated entities or redigitize curves. Do not change the candidate.\nSCOPE:\n"
        + json.dumps(scope, ensure_ascii=False) + "\nBASELINE AUDIT (fallible, immutable):\n"
        + baseline.model_dump_json() + "\nEXACT CURRENT SCOPE ENTITIES (omitted defaults mean unread, not absent):\n"
        + json.dumps(entities, ensure_ascii=False) + design_context)


def revision_review_prompt(scope, baseline):
    """Freeze exact changes/dependencies without repeating whole candidate rows."""
    return ("BOUNDED REVISION SOURCE RECHECK. This is not a new full-paper audit. The authenticated full baseline "
        "covered all nine dimensions. Main values, units, material/experiment ownership, source definitions and source "
        "coverage are unchanged. Only the explicit metadata/support repairs below are eligible. The baseline remains "
        "fallible and is not scientific validation.\n"
        "Inspect EVERY changed path against its old AND new values and the required original source anchors. Read the "
        "necessary source blocks and enlarge relevant figures. Locate the changed entities with wt_query_candidate, "
        "including at least one complete changed-entity path read to verify the bound candidate. For other dependencies, "
        "query the listed IDs with ONLY the necessary fields, grouped in batches; do not retrieve an entire section or "
        "repeat already delivered rows/schema. A dependency directory does not require re-auditing every unchanged value. "
        "Check the implications that changed: source support ranges, condition/time ambiguity, pair compatibility, series "
        "ownership and downstream interpretation. All unrelated facts retain explicitly inherited baseline coverage.\n"
        "Return an explicit source-grounded finding covering every changed path (an entity-level finding may cover several "
        "paths, with every required source block cited). Reconsider every baseline blocking path, including comma-separated "
        "paths. Preserve ALL baseline unresolved findings verbatim, coverage limitations and optional followups; add "
        "qualified new limitations if needed. Conflict alternatives are not a resolved answer. If changes or dependencies "
        "need broader inspection, emit a blocking finding; never approve by hash or prior model agreement.\n"
        "Return the complete current ReadingAudit with review_scope=citation_delta, review_scope_sha256 exactly as below, "
        "all nine cumulative checked_dimensions, and dimension_notes distinguishing inherited coverage from newly reviewed "
        "claims. No new extraction or digitization; do not modify the candidate.\nFROZEN REPAIR SCOPE:\n"
        + json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
        + "\nAUTHENTICATED FULL BASELINE AUDIT (fallible, unchanged):\n" + baseline.model_dump_json())


def citation_review_issues(prepared, audit):
    if prepared is None:
        return [] if audit.review_scope == "full" and audit.review_scope_sha256 is None else ["Unplanned incremental audit scope"]
    _, scope, baseline = prepared
    if scope.get("policy") == SCIENTIFIC_POLICY:
        try:
            old, current, additional = _verified_scientific_inputs(prepared)
            previous_step = None
            if scope.get("structural_followup"):
                entry = prepared[0]["additional_audits"][0]
                preceding = Path(entry["candidate_path"])
                previous_step = (json.loads(preceding.with_name(preceding.name.replace("candidate.", "citation_scope.")).read_bytes()),
                                 ExtractionRecord.model_validate_json(preceding.read_bytes()))
            return validate_scientific_revision_audit(scope, baseline, audit,
                old_record=old, new_record=current, additional_audits=additional, previous_step=previous_step)
        except (ValueError, OSError, TypeError, KeyError, IndexError):
            return ["Scientific recheck lost an exact frozen candidate, baseline or intermediate audit binding"]
    issues = (validate_revision_audit(scope, baseline, audit) if scope.get("policy") == REVISION_POLICY
              else validate_citation_audit(scope, baseline, audit))
    if audit.review_scope != "citation_delta" or audit.review_scope_sha256 != scope["scope_sha256"]:
        issues.append("Citation recheck does not bind its frozen scope")
    return issues


def citation_paths(prepared):
    if not prepared:
        return []
    receipt, scope, _ = prepared
    paths = [Path(receipt[key]) for key in ("scope_path", "baseline_candidate_path", "baseline_audit_path")]
    if scope.get("policy") == SCIENTIFIC_POLICY:
        paths.append(Path(receipt["current_candidate_path"]))
        paths.extend(Path(entry[key]) for entry in receipt["additional_audits"] for key in ("candidate_path", "audit_path"))
    return list(dict.fromkeys(paths))


def _verified_scientific_inputs(prepared):
    receipt, scope, baseline = prepared
    scope_path = Path(receipt["scope_path"]).resolve(strict=True)
    run = scope_path.parent
    if hash_file(scope_path) != receipt["scope_file_sha256"] or json.loads(scope_path.read_text(encoding="utf-8")) != scope:
        raise ValueError("scientific scope artifact changed")
    old = _load_bound(receipt["baseline_candidate_path"], receipt["baseline_candidate_sha256"], run, ExtractionRecord)
    actual = _load_bound(receipt["baseline_audit_path"], receipt["baseline_audit_sha256"], run, ReadingAudit)
    if actual != baseline:
        raise ValueError("scientific baseline differs from its frozen audit")
    current = _load_bound(receipt["current_candidate_path"], receipt["current_candidate_sha256"], run, ExtractionRecord)
    entries = receipt["additional_audits"]
    if not isinstance(entries, list) or len(entries) > 3:
        raise ValueError("invalid intermediate scientific audit chain")
    additional = []
    seen = set()
    for entry in entries:
        if set(entry) != {"candidate_path", "candidate_sha256", "audit_path", "audit_sha256"}:
            raise ValueError("malformed intermediate scientific audit receipt")
        record_path, audit_path = Path(entry["candidate_path"]).resolve(strict=True), Path(entry["audit_path"]).resolve(strict=True)
        if (record_path.name.replace("candidate.", "audit.") != audit_path.name
                or str(audit_path) in seen):
            raise ValueError("repeated or mismatched intermediate scientific audit")
        seen.add(str(audit_path))
        _load_bound(record_path, entry["candidate_sha256"], run, ExtractionRecord)
        additional.append(_load_bound(audit_path, entry["audit_sha256"], run, ReadingAudit))
    return old, current, additional


def scientific_scope_display(scope):
    """Display only: every scientific field and the full manifest remain intact."""
    display = copy.deepcopy(scope)
    for change in display.get("changes", []):
        change.pop("old_value_hash", None)
        change.pop("new_value_hash", None)
    for entity in display.get("entity_refs", []):
        entity.pop("old_hash", None)
        entity.pop("new_hash", None)
    for anchor in display.get("source_anchors", []):
        if isinstance(anchor.get("locator"), dict):
            anchor["locator"] = {key: value for key, value in anchor["locator"].items() if value is not None}
    if scope.get("structural_review"):
        # The same page can have many material/figure-specific evidence IDs.
        # This index removes repeated source lookups; it supplies no verdict.
        anchors = {a["evidence_id"]: (a["source_id"], a["block_id"]) for a in scope["source_anchors"]}
        pages = sorted(set(anchors.values()))
        page_number = {pair: i for i, pair in enumerate(pages)}
        groups = {}
        for ref in scope["entity_refs"]:
            if ref["role"] == "affected":
                indices = tuple(sorted({page_number[anchors[e]] for e in ref["required_evidence_ids"]}))
                groups.setdefault(indices, []).append(ref["record_path"])
        display["disposition_source_index"] = {"source_blocks": pages,
            "groups": [{"block_indices": ids, "affected_paths": paths} for ids, paths in groups.items()]}
    return display


def scientific_review_prompt(scope, baseline, additional=()):
    """Only changed claims, dependency navigation and inherited audit obligations."""
    if scope.get("structural_followup"):
        if len(additional) != 1:
            raise ValueError("missing preceding structural audit")
        prior = ReadingAudit.model_validate(additional[0])
        carry = {"findings":[f.model_dump(mode="json") for f in prior.findings if f.needs_revision or f.verdict in {"unsupported","unresolved"}],
                 "coverage_limitations":prior.coverage_limitations,
                 "quantitative_followups":[f.model_dump(mode="json") for f in prior.quantitative_followups]}
        return ("TARGETED FOLLOW-UP TO A COMPLETED OWNERSHIP SOURCE AUDIT. Use only the affected rows, changed "
            "fields, earlier blockers and registered sources below. Prior supported findings outside these changes "
            "remain inherited. Read all required original blocks and full visuals. Query each affected entity via "
            "wt_query_candidate paths. Decide independently if the repaired wording, signed quantities, references, "
            "attribution and pair qualifications are supported by their actual sources. Check other context only "
            "as needed to explain those changes. Group exact paths in findings only for the same scientific disposition. "
            "Cite actual source_id and block_id pairs for all changes and prior blockers. Source delivery does not "
            "imply a favorable verdict. Return ReadingAudit with scientific_delta, the exact scope_sha256, nine "
            "cumulative checked_dimensions and dimension_notes identifying what was inherited. Retain prior nonblocking "
            "unresolved findings verbatim, coverage_limitations and quantitative_followups. Locate any actual omissions. "
            "Be concise and deliver the report within the bound turn.\nCURRENT SCOPE:\n" +
            json.dumps(scientific_scope_display(scope), ensure_ascii=False, separators=(",", ":")) +
            "\nPRECEDING AUDIT DEFECTS AND RETAINED LIMITATIONS:\n" + json.dumps(carry, ensure_ascii=False, separators=(",", ":")))
    audits = []
    for index, audit in enumerate((baseline, *additional)):
        audits.append({"audit_index": index, "review_scope": audit.review_scope,
            "checked_dimensions": audit.checked_dimensions, "dimension_notes": audit.dimension_notes,
            "findings": [{"finding_index": i, **finding.model_dump(mode="json")}
                         for i, finding in enumerate(audit.findings)
                         if finding.needs_revision or finding.verdict in {"unsupported", "unresolved", "not_checked"}],
            "potential_omissions": audit.potential_omissions,
            "coverage_limitations": audit.coverage_limitations,
            "quantitative_followups": [item.model_dump(mode="json") for item in audit.quantitative_followups]})
    boundary = ("Only the explicitly listed condition/unit and typed condition-array repairs may change scientific "
        "quantities. Review them against both original and repaired claims and every required downstream consumer. "
        "Primary numeric outcomes, entity/ownership identities, source definitions and reading coverage remain protected.\n"
        if scope.get("scope_limits", {}).get("compiler_version", 1) >= 2 else
        "Every numerical quantity, experiment/material identity and source coverage remains protected by the controller.\n")
    source_actions = ("Read every required original source block now. For native Word XML tables/cells, read the "
        "complete registered parent table and all child blocks (including empty cells/equations), and retain the "
        "explicit limitation that no rendered Word table/page layout was viewed. Embedded images still require "
        "their own complete registered occurrence view. View complete PDF figure/table pages or Word images before "
        "enlarging details; do not request a nonexistent page view for a native XML table. "
        if scope.get("source_requirement_policy") in {"native-word-structure-v1", LINKED_POLICY} else
        "retrieve each required source block now, and view the complete page/Word image occurrence for figure/table "
        "anchors before enlarging details. ")
    evidence_boundary = ("New evidence entries are added source references, never new authority by themselves. "
        "Explicitly source-check each evidence_addition, its exact original source/block/locator and the "
        "changed claims which cite it. Preserve every prior evidence entry unchanged. A time-window kind "
        "change is a scientific interpretation: compare the entire old/new window, its time reference, "
        "conditions, pairing consequences and missingness. Do not treat an associated test interval as an "
        "independently reported calculation window. Reading new anchors does not repair or waive any "
        "unrelated coverage gaps.\n" if scope.get("scope_limits", {}).get("compiler_version", 1) >= 3 else "")
    if scope.get("structural_review"):
        boundary = ("Newly registered experiments, materials or observations and explicit ownership edits require "
            "a fresh check of their complete values, former and current owners, source conditions and control arms. "
            "Check both ends of every changed shared-reference relationship, even when the previous target no longer "
            "contains the observation. Verify condition comparability, sampling phase, streams, uncertainty, all "
            "affected pairings and design comparisons. Preserve reported/estimated/inferred/conflict distinctions. "
            "Do not infer shared conditions from the same formula or the same material. "
            "Use disposition_source_index to batch navigation: each group lists exact affected entity paths requiring "
            "the same source blocks. Compare the scientific fields and old/new changes individually even when pages "
            "overlap. You may put several exact entity paths into ONE finding only when you have reviewed each and "
            "the same disposition actually applies. Separate defects. Entity-level findings may cover their own "
            "nested fields when the explanation checks those changes and all their required sources are cited. "
            "Cite actual source_id/block_id pairs from the indexed blocks; resolve the index before submission.\n")
    current_review = ("This is a current-source check of exact cumulative changes and explicitly affected entities, "
        "not a new extraction. Context entries support navigation and inherited baseline understanding; do not "
        "claim that each context entry received an independent source review in this call. "
        if scope.get("scope_limits", {}).get("compiler_version", 1) >= 3 else
        "This is a current-source check of exact cumulative changes and their direct consumers, not a new extraction. ")
    return ("BOUNDED SCIENTIFIC REVISION SOURCE RECHECK. Use the authenticated complete full audit as the fallible "
        "baseline; intermediate scientific audits add obligations and cannot erase previous blockers or source limits. "
        + current_review + boundary + evidence_boundary +
        "For EVERY entry in changes, compare the old AND new values, " + source_actions + "Review the intended "
        "scientific disposition: explicit absence, author-not-reported, unread, read-but-unquantified, inference and "
        "conflict are distinct; no null conversion or condition inheritance. Conflict alternatives remain limitations. "
        "Use wt_query_candidate for exact changed paths and at least one complete changed-entity/path read. Then batch "
        "only the fields needed from each affected consumer and context entity. Do not repeat whole sections, unchanged "
        "scientific rows, source pages already delivered in this call, or the entire schema. Refresh only if genuinely needed.\n"
        "Return source-grounded findings whose record_paths explicitly enumerate EVERY changed path, EVERY previous "
        "blocking_obligation path, and EVERY entity_refs entry with role=affected. Group equal dispositions only by "
        "listing each exact resolvable path separately; do not use prose selectors, wildcards, shortened index lists or "
        "generic collection paths. Cite all source blocks required for the covered changes/blockers. For affected "
        "consumers, explicitly address the change's consequences for shared controls, primary ownership, simultaneous "
        "pairing, conditions, series completeness, design comparisons and eligibility. Context entities supply context "
        "rather than requiring every unchanged value to be checked again. If the bounded scope is insufficient, return "
        "an unsupported/needs_revision finding identifying why broader review is needed; do not approve by agreement.\n"
        "Return the complete current ReadingAudit with review_scope=scientific_delta, review_scope_sha256 exactly as "
        "below and all NINE cumulative checked_dimensions. dimension_notes must distinguish newly reviewed changes "
        "from inherited coverage for each dimension. Preserve inherited unresolved nonblocking findings verbatim, ALL "
        "coverage_limitations and optional quantitative_followups. Reconsider each earlier blocker explicitly; locate "
        "any remaining potential_omissions with omission_indices on its blocking finding. Never treat an incomplete "
        "review, a retained limitation or missing conditions as an invented resolved value. Do not alter the candidate.\n"
        "FROZEN CUMULATIVE CHANGE/DEPENDENCY SCOPE — DISPLAY COPY ONLY:\n"
        "Per-item integrity hashes and null source-locator fields are omitted from this display only; the complete "
        "immutable manifest is verified by the controller. All scientific old/new values, paths, required evidence "
        "and obligations remain. The root scope_sha256 binds that COMPLETE original manifest, not this display.\n"
        + json.dumps(scientific_scope_display(scope), ensure_ascii=False, separators=(",", ":"))
        + "\nINHERITED AUDIT OBLIGATIONS (complete retained files remain frozen inputs):\n"
        + json.dumps(audits, ensure_ascii=False, separators=(",", ":")))
