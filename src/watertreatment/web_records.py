"""Versioned workbench records, scoped human reviews and explicit graph publication.

The record layer remains authoritative. A review is scoped to fields in a named
version, never an assertion that a whole paper has been human validated. Original
model outputs and source plans are only read, never edited by this service.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .model_policy import manifest_matches_plan, plan_model, receipt_matches_plan
from .domain import ExtractionRecord, Fact, FactStatus
from .reading import AUDIT_DIMENSIONS, ReadingAudit
from .reading_coverage_state import only_deferred_coverage_issues
from .audit_followups import quantitative_followup_issues
from .review_packet import select_observations
from .settings import Settings
from .storage import DataStore, cohort_at_receipt, digest_json, hash_file, safe_name, utc_now, write_new_json
from .validation import validate_record
from .workflow import _read_bundle, validate_plan_metadata


COMPLETED = {"candidate_model_audited", "candidate_audited_with_limitations"}
LOCAL_REVIEW_MODES = {"retained-issue-source-review-v1", "source-reviewer-verbatim-text-v1"}


def _receipt_assessment_version(result):
    # Re-evaluate an old negative cache entry when field-level source replay improves.
    return 6 if "source_review_final_corrections" in result else 1


def _recheck_cached_receipt(metadata):
    result = metadata.get("result", {})
    assessment = metadata.get("assessment", {})
    reasons = set(assessment.get("reasons", []))
    cached_version = metadata.get("receipt_assessment_version")
    return (metadata.get("origin") == "model" and result.get("review_mode") in LOCAL_REVIEW_MODES
            and result.get("status") in COMPLETED and result.get("stop_reason") == "audit_completed"
            and reasons and reasons <= {"audit_provenance_unverified", "machine_validation_errors"}
            and all(issue.get("code") == "ESTIMATE_ORIGIN_CONFLICT" for issue in assessment.get("errors", []))
            and (cached_version is None or type(cached_version) is int and cached_version < _receipt_assessment_version(result)))


PATCH_FIELDS = {"value", "unit", "time_window", "experiment_id", "material_id"}
REVIEW_SCOPES = PATCH_FIELDS | {"metric", "evidence", "conditions", "pairing", "stability"}
ACTIONS = {"confirm", "correct", "not_reported", "conflict", "uncertain", "omission"}


class ReviewConflict(ValueError):
    """The client is reviewing an outdated record or draft version."""


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _value(fact):
    return fact.value if fact.value is not None else fact.raw


class WebRecords:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = DataStore(settings.data_root)
        self.root = settings.data_root / "workbench"
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions = self.root / "record_versions"
        self.versions.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                  id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, run_id TEXT NOT NULL,
                  parent_id TEXT, created_at TEXT NOT NULL, record_path TEXT NOT NULL,
                  record_hash TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS current_records (
                  paper_id TEXT PRIMARY KEY, record_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publications (
                  paper_id TEXT PRIMARY KEY, record_id TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL, published_at TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS publication_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
                  status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                  id TEXT PRIMARY KEY, record_id TEXT NOT NULL, parent_record_id TEXT NOT NULL,
                  reviewer TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts (
                  record_id TEXT NOT NULL, reviewer TEXT NOT NULL, base_hash TEXT NOT NULL,
                  payload TEXT NOT NULL, saved_at TEXT NOT NULL,
                  PRIMARY KEY(record_id, reviewer));
                CREATE TABLE IF NOT EXISTS source_changes (
                  paper_id TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.root / "records.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _safe_data_path(self, path: str | Path) -> Path:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_relative_to(self.settings.data_root):
            raise ValueError("record or evidence path is outside the configured data store")
        return resolved

    def _source(self, run_id: str):
        safe_name(run_id)
        run = self.settings.data_root / "runs" / run_id
        plan_path = self._safe_data_path(run / "plan.json")
        event = self.store.last_event(run_id, "prepared")
        if not event or event.get("plan_sha256") != hash_file(plan_path):
            raise ValueError("source plan is missing, unregistered or changed")
        plan = _json(plan_path)
        if plan.get("run_id") != run_id:
            raise ValueError("source plan belongs to another run")
        bundle_path = self._safe_data_path(run / "sources.json")
        expected = [item for item in plan.get("input_paths", [])
                    if Path(item["path"]).resolve() == bundle_path]
        if len(expected) != 1 or expected[0]["sha256"] != hash_file(bundle_path):
            raise ValueError("source bundle does not match the frozen plan")
        for item in [*plan.get("input_paths", []), *plan.get("reading_artifacts", [])]:
            if hash_file(self._safe_data_path(item["path"])) != item["sha256"]:
                raise ValueError("frozen reading input has changed")
        # Validate every source location before the parser opens it.
        for document in _json(bundle_path).get("documents", []):
            self._safe_data_path(document["source"]["path"])
        bundle, documents = _read_bundle(bundle_path)
        if plan.get("review_visual_recovery"):
            from .review_visuals import review_visuals
            _, recovered, _ = review_visuals(run, bundle, create=False, proof=plan["review_visual_recovery"])
            frozen = {Path(i["path"]).resolve(): i["sha256"] for i in plan["input_paths"]}
            if any(frozen.get(path) != hash_file(path) for path in recovered):
                raise ValueError("Recovered Word review inputs are missing from the registered plan")
        for document in documents:
            self._safe_data_path(document.source.path)
        return plan, bundle_path, bundle, documents

    def _queried_candidate_snapshot(self, run: Path, record: ExtractionRecord, request: dict, sources: dict) -> bool:
        """Verify bounded candidate access, without treating delivery as understanding."""
        binding = request.get("candidate_binding")
        if (not isinstance(binding, dict) or set(binding) != {"path", "sha256", "content_hash", "scope"}
                or any(not isinstance(value, str) or not value.strip() for value in binding.values())):
            return False
        try:
            run_root = self._safe_data_path(run)
            snapshot = self._safe_data_path(binding["path"])
            if (not snapshot.is_relative_to(run_root) or hash_file(snapshot) != binding["sha256"]
                    or sources.get(str(snapshot)) != binding["sha256"]
                    or binding["content_hash"] != digest_json(record.model_dump(mode="json"))
                    or "CANDIDATE_SNAPSHOT " + binding["content_hash"] not in request.get("prompt", "")
                    or ExtractionRecord.model_validate(_json(snapshot)) != record):
                return False
            for request_path in (run / "reading_tools").glob("*/request.json"):
                result_path = request_path.parent / "result.json"
                if not result_path.is_file():
                    continue
                try:
                    safe_request = self._safe_data_path(request_path)
                    safe_result = self._safe_data_path(result_path)
                    if not all(path.is_relative_to(run_root) for path in (safe_request, safe_result)):
                        continue
                    tool_request, completed = _json(safe_request), _json(safe_result)
                    data = completed.get("data", {})
                    if (tool_request.get("tool") == "wt_query_candidate" and completed.get("success") is True
                            and isinstance(data, dict) and data.get("candidate_binding") == binding
                            and type(data.get("returned_count")) is int and data["returned_count"] > 0):
                        if data.get("mode") in {"section", "path"}:
                            return True
                        if data.get("mode") == "paths":
                            from .reading_tools import candidate_path_batch_matches
                            if candidate_path_batch_matches(record, binding, tool_request.get("arguments", {}), data):
                                return True
                except (ValueError, OSError, TypeError, AttributeError):
                    continue
        except (ValueError, OSError, TypeError):
            return False
        return False

    def _audit_provenance(self, run: Path, record: ExtractionRecord, result: dict, audit: dict) -> bool:
        """An audit must match a completed Sol MAX call for these exact facts."""
        plan = _json(run / "plan.json")
        identity = plan.get("scientific_plan", plan)
        if not isinstance(identity, dict):
            return False
        version = identity.get("rule_version")
        if version is not None and version != record.rule_version:
            return False
        verifier = self
        if version is not None and version != self.settings.rules_version:
            # Historical receipt replay needs its frozen scientific contract. Leave the live
            # worker settings and the shared store/lock untouched, including concurrent reads.
            verifier = copy.copy(self)
            verifier.settings = self.settings.model_copy(update={"rules_version": version})
        return verifier._verify_frozen_audit(run, record, result, audit, plan)

    def _verify_frozen_audit(self, run, record, result, audit, plan):
        if plan.get("interrupted_first_audit"):
            from .interrupted_reading import verify_first_audit_progress
            try:
                verify_first_audit_progress(self.settings,run,plan)
            except (ValueError, OSError, KeyError, TypeError):
                return False
        if plan.get("initial_recovery"):
            from .web_initial_recovery import verify_initial_recovery
            try:
                verify_initial_recovery(self.settings, run, plan)
            except (ValueError, OSError, KeyError, TypeError):
                return False
        if plan.get("budget_continuation"):
            from .budget_extension_workflow import verify_extension_plan
            try:
                verify_extension_plan(self.settings, run, plan)
            except (ValueError, OSError, KeyError, TypeError):
                return False
        if plan.get("audit_guidance"):
            from .audit_guidance_resume import verify_guidance
            try:
                verify_guidance(self.settings, run)
            except (ValueError, OSError, KeyError, TypeError):
                return False
        if plan.get("identity_correction"):
            from .identity_resume import verify_identity_resume
            try:
                verify_identity_resume(self.settings, run)
            except (ValueError, OSError, KeyError, TypeError):
                return False
        if (run / "dossier_identity_binding.json").exists():
            from .identity_resume import verify_dossier_binding
            try:
                verify_dossier_binding(run, _json(run / "plan.json"))
            except (ValueError, OSError, KeyError, TypeError):
                return False
        parsed = ReadingAudit.model_validate(audit)
        if set(parsed.checked_dimensions) != set(AUDIT_DIMENSIONS):
            return False
        history = result.get("history", [])
        if not history or history[-1].get("candidate_sha256") != hash_file(run / "candidate.json"):
            return False
        if history[-1].get("audit_origin") == "authenticated_retained_issue_review":
            from .retained_review import verify
            return verify(self.settings, run, record, parsed)
        if history[-1].get("audit_origin") == "verified_source_reviewer_text_directive":
            from .reviewer_text import verify
            return verify(self.settings, run, record, parsed)
        if result.get("structural_coverage_correction") or (run / "reading_structure_correction.v01.json").exists():
            from .coverage_correction import verify_structural_correction
            _, _, _, documents = self._source(run.name)
            if not verify_structural_correction(run, result.get("structural_coverage_correction"), documents):
                return False
        if history[-1].get("audit_origin") == "audit_addendum_composite":
            from .audit_addendum_workflow import verify_audit_addendum_run
            return verify_audit_addendum_run(self.settings, run, record, parsed)
        if history[-1].get("audit_origin") == "inherited_scientific_audit_with_context_revalidation":
            from .coverage_context_revalidation import verify_context_revalidation_run
            return verify_context_revalidation_run(self.settings, run, record, parsed)
        if history[-1].get("audit_origin") == "inherited_scientific_audit_with_coverage_correction":
            from .coverage_correction_workflow import verify_coverage_correction_run
            return verify_coverage_correction_run(self.settings, run, record, parsed)
        if history[-1].get("audit_origin") == "partial_scientific_audit_composite":
            from .partial_audit_completion_workflow import verify_partial_audit_completion_run
            return verify_partial_audit_completion_run(self.settings, run, record, parsed)
        if history[-1].get("audit_origin") == "partial_scientific_audit_remaining_composite":
            from .partial_audit_remaining_resume import verify_remaining_completion_run
            return verify_remaining_completion_run(self.settings, run, record, parsed)
        if parsed.review_scope == "citation_delta" and history[-1].get("audit_origin") != "citation_delta_model_audit":
            return False
        if parsed.review_scope == "scientific_delta" and history[-1].get("audit_origin") != "scientific_delta_model_audit":
            return False
        return self._completed_audit_provenance(run, record, parsed)

    def _completed_audit_provenance(self, run, record, parsed, *, allow_citation=True):
        """Authenticate one completed audit, with at most one inherited full baseline."""
        plan = _json(run / "plan.json")
        planned = plan.get("input_paths", [])
        compact = record.model_dump_json(exclude_defaults=True)
        for request_path in (run / "calls").glob("*/request.json"):
            call = request_path.parent
            if not (call / "result.json").is_file() or not (call / "answer.json").is_file():
                continue
            envelope, completed = _json(request_path), _json(call / "result.json")
            request = envelope.get("request", {})
            if envelope.get("fingerprint") != digest_json(request):
                continue
            sources = {str(Path(item["path"]).resolve()): item["sha256"] for item in request.get("sources", [])}
            if not planned or any(sources.get(str(Path(item["path"]).resolve())) != item["sha256"] for item in planned):
                continue
            if (completed.get("status") != "schema_valid_candidate"
                    or not receipt_matches_plan(plan, request, completed, legacy_sparse=True)
                    or not request.get("stage", "").startswith("audit_")):
                continue
            candidate_matched = (self._queried_candidate_snapshot(run, record, request, sources)
                                 if "candidate_binding" in request else compact in request.get("prompt", ""))
            if not candidate_matched:
                continue
            # Match the exact audit before traversing inherited provenance. A
            # no-op repair can reuse candidate bytes across versions; matching
            # only those bytes could otherwise recurse into a later audit.
            try:
                if ReadingAudit.model_validate(_json(call / "answer.json")) != parsed:
                    continue
            except ValueError:
                continue
            if request.get("citation_review"):
                scope_file = Path(request["citation_review"].get("scope_path", ""))
                try:
                    scope = _json(self._safe_data_path(scope_file))
                    if parsed.review_scope_sha256 != scope.get("scope_sha256"):
                        continue
                    if scope.get("policy") in {"bounded-revision-audit-v1", "bounded-scientific-revision-v1"}:
                        from .source_review import verify_source_review
                        if (request.get("source_review_call") != call.name
                                or request.get("source_review_scope_sha256") != scope.get("scope_sha256")
                                or not verify_source_review(run, scope, completed.get("source_review_proof"), call.name)):
                            continue
                except (ValueError, OSError, TypeError, KeyError):
                    continue
                if not allow_citation or not self._citation_provenance(run, record, parsed, request, sources):
                    continue
            elif parsed.review_scope != "full" or parsed.review_scope_sha256 is not None:
                continue
            try:
                if ReadingAudit.model_validate(_json(call / "answer.json")) == parsed:
                    return True
            except ValueError:
                continue
        return False

    def _citation_provenance(self, run, record, parsed, request, sources):
        from .citation_audit import build_citation_audit_scope, validate_citation_audit
        from .bounded_revision_audit import build_revision_audit_scope, validate_revision_audit, POLICY as REVISION_POLICY
        try:
            if parsed.review_scope == "scientific_delta":
                return self._scientific_revision_provenance(run, record, parsed, request, sources)
            if record.rule_version not in {"0.7", "0.8", "0.9"} or parsed.review_scope != "citation_delta":
                return False
            receipt = request["citation_review"]
            pairs = (("scope_path", "scope_file_sha256"), ("baseline_candidate_path", "baseline_candidate_sha256"),
                     ("baseline_audit_path", "baseline_audit_sha256"))
            if not isinstance(receipt, dict) or set(receipt) != {key for pair in pairs for key in pair}:
                return False
            resolved = {}
            for path_key, hash_key in pairs:
                path = self._safe_data_path(Path(receipt[path_key]))
                if (not path.is_relative_to(run.resolve()) or hash_file(path) != receipt[hash_key]
                        or sources.get(str(path)) != receipt[hash_key]):
                    return False
                resolved[path_key] = path
            old = ExtractionRecord.model_validate(_json(resolved["baseline_candidate_path"]))
            baseline = ReadingAudit.model_validate(_json(resolved["baseline_audit_path"]))
            if baseline.review_scope != "full" or baseline.review_scope_sha256 is not None:
                return False
            scope = _json(resolved["scope_path"])
            build_scope, validate_scope = ((build_revision_audit_scope, validate_revision_audit)
                if scope.get("policy") == REVISION_POLICY else (build_citation_audit_scope, validate_citation_audit))
            from .word_review_links import LINKED_POLICY
            frozen_plan = _json(run / "plan.json")
            policy = (frozen_plan.get("resumed_full_audit", {}).get("source_requirement_policy")
                      if resolved["scope_path"].name == "citation_scope.v00.json"
                      else frozen_plan.get("scientific_scope_limits", {}).get("source_requirement_policy"))
            options = ({"source_requirement_policy": LINKED_POLICY}
                       if scope.get("policy") == REVISION_POLICY and policy == LINKED_POLICY else {})
            if _json(run / "plan.json").get("resumed_full_audit") and resolved["scope_path"].name == "citation_scope.v00.json":
                from .audit_resume import verify_resumed_review
                if not verify_resumed_review(self.settings, run, record, old, baseline, receipt):
                    return False
                expected = build_scope(old, record, baseline, baseline_origin="fresh_model_audit", **options)
                proof = _json(run / "plan.json")["resumed_full_audit"]
                return (expected is not None and expected == scope
                        and scope["scope_sha256"] == proof["scope_sha256"]
                        and parsed.review_scope_sha256 == scope["scope_sha256"]
                        and not validate_scope(scope, baseline, parsed))
            history = _json(run / "result.json").get("history", [])
            if not isinstance(history, list) or len(history) < 2:
                return False
            previous, current = history[-2:]
            baseline_index = len(history) - 2
            if (previous.get("audit_origin") != "fresh_model_audit"
                    or previous.get("candidate_sha256") != receipt["baseline_candidate_sha256"]
                    or previous.get("validation_error_count", previous.get("validation_errors")) != 0
                    or not only_deferred_coverage_issues(previous.get("reading_check_issues"))
                    or current.get("audit_origin") != "citation_delta_model_audit"
                    or current.get("candidate_sha256") != hash_file(run / "candidate.json")
                    or resolved["baseline_candidate_path"] != (run / f"candidate.v{baseline_index:02d}.json").resolve()
                    or resolved["baseline_audit_path"] != (run / f"audit.v{baseline_index:02d}.json").resolve()
                    or resolved["scope_path"] != (run / f"citation_scope.v{baseline_index + 1:02d}.json").resolve()):
                return False
            if not self._completed_audit_provenance(run, old, baseline, allow_citation=False):
                return False
            expected = build_scope(old, record, baseline, baseline_origin="fresh_model_audit", **options)
            return (expected is not None and expected == scope
                    and parsed.review_scope_sha256 == scope["scope_sha256"]
                    and not validate_scope(scope, baseline, parsed))
        except (ValueError, OSError, TypeError, KeyError, AttributeError):
            return False

    def _scientific_revision_provenance(self, run, record, parsed, request, sources):
        """Authenticate a bounded cumulative review, including every earlier delta.

        Earlier deltas add obligations, not full-paper authority. Version numbers
        strictly decrease during recursion, preventing circular inheritance.
        """
        from .scientific_revision_audit import POLICY, build_scientific_revision_scope, validate_scientific_revision_audit
        import re
        try:
            if _json(run / "plan.json").get("audit_guidance"):
                from .audit_guidance_resume import verify_guidance_review
                return verify_guidance_review(self.settings, run, record, parsed, request, sources)
            if record.rule_version not in {"0.7", "0.8", "0.9"}:
                return False
            receipt = request["citation_review"]
            pairs = (("scope_path", "scope_file_sha256"),
                     ("baseline_candidate_path", "baseline_candidate_sha256"),
                     ("baseline_audit_path", "baseline_audit_sha256"),
                     ("current_candidate_path", "current_candidate_sha256"))
            if (not isinstance(receipt, dict)
                    or set(receipt) != {key for pair in pairs for key in pair} | {"additional_audits"}):
                return False
            def checked_path(value, expected):
                path = self._safe_data_path(value)
                if (not path.is_relative_to(run.resolve()) or hash_file(path) != expected
                        or sources.get(str(path)) != expected):
                    raise ValueError("scientific review artifact is not an immutable delivered input")
                return path
            paths = {key: checked_path(receipt[key], receipt[checksum]) for key, checksum in pairs}
            version = re.fullmatch(r"candidate\.v(\d{2})\.json", paths["current_candidate_path"].name)
            if not version or paths["current_candidate_path"].parent != run.resolve():
                return False
            version = int(version[1])
            if (version > 3 or paths["scope_path"] != (run / f"citation_scope.v{version:02d}.json").resolve()
                    or request.get("stage") not in {f"audit_{version}", f"audit_web_{version}"}
                    or ExtractionRecord.model_validate(_json(paths["current_candidate_path"])) != record):
                return False
            scope = _json(paths["scope_path"])
            if scope.get("policy") != POLICY or parsed.review_scope_sha256 != scope.get("scope_sha256"):
                return False
            old = ExtractionRecord.model_validate(_json(paths["baseline_candidate_path"]))
            baseline = ReadingAudit.model_validate(_json(paths["baseline_audit_path"]))
            plan = _json(run / "plan.json")
            history = _json(run / "result.json").get("history", [])
            if (not isinstance(history, list) or len(history) <= version
                    or history[version].get("version") != version
                    or history[version].get("audit_origin") != "scientific_delta_model_audit"
                    or history[version].get("candidate_sha256") != receipt["current_candidate_sha256"]):
                return False
            proof = plan.get("resumed_full_audit")
            if proof and paths["baseline_candidate_path"] == Path(proof["frozen_candidate_path"]).resolve():
                from .audit_resume import verify_resumed_review
                if not verify_resumed_review(self.settings, run, record, old, baseline, receipt):
                    return False
                baseline_index = -1
            else:
                baseline_match = re.fullmatch(r"candidate\.v(\d{2})\.json", paths["baseline_candidate_path"].name)
                if not baseline_match:
                    return False
                baseline_index = int(baseline_match[1])
                if (not 0 <= baseline_index < version
                        or paths["baseline_candidate_path"].parent != run.resolve()
                        or paths["baseline_audit_path"] != (run / f"audit.v{baseline_index:02d}.json").resolve()):
                    return False
                h = history[baseline_index]
                if (h.get("audit_origin") != "fresh_model_audit"
                        or h.get("validation_error_count", h.get("validation_errors")) != 0
                        or not only_deferred_coverage_issues(h.get("reading_check_issues"))
                        or h.get("candidate_sha256") != receipt["baseline_candidate_sha256"]
                        or not self._completed_audit_provenance(run, old, baseline, allow_citation=False)):
                    return False
            additional = receipt["additional_audits"]
            expected_versions = list(range(baseline_index + 1, version))
            if not isinstance(additional, list) or len(additional) != len(expected_versions) or len(additional) > 3:
                return False
            previous_audits, previous_scopes = [], []
            for index, item in zip(expected_versions, additional):
                if not isinstance(item, dict) or set(item) != {"candidate_path", "candidate_sha256", "audit_path", "audit_sha256"}:
                    return False
                candidate_path = checked_path(item["candidate_path"], item["candidate_sha256"])
                audit_path = checked_path(item["audit_path"], item["audit_sha256"])
                if (candidate_path != (run / f"candidate.v{index:02d}.json").resolve()
                        or audit_path != (run / f"audit.v{index:02d}.json").resolve()
                        or history[index].get("audit_origin") != "scientific_delta_model_audit"
                        or history[index].get("validation_error_count", history[index].get("validation_errors")) != 0
                        or not only_deferred_coverage_issues(history[index].get("reading_check_issues"))
                        or history[index].get("candidate_sha256") != item["candidate_sha256"]):
                    return False
                previous_record = ExtractionRecord.model_validate(_json(candidate_path))
                previous = ReadingAudit.model_validate(_json(audit_path))
                if (previous.review_scope != "scientific_delta"
                        or not self._completed_audit_provenance(run, previous_record, previous)):
                    return False
                previous_scope = _json(run / f"citation_scope.v{index:02d}.json")
                if (previous_scope.get("baseline_audit_hash") != scope.get("baseline_audit_hash")
                        or previous_scope.get("old_record_hash") != scope.get("old_record_hash")):
                    return False
                previous_audits.append(previous)
                previous_scopes.append(previous_scope)
            from .audit_execution import select_scientific_scope_limits
            # Existing unversioned plans are replayed using their frozen scopes,
            # never today's defaults. A v3 report requires an explicit v3 plan.
            version = scope.get("scope_limits", {}).get("compiler_version")
            if (version in {3, 4, 6}
                    and plan.get("scientific_scope_limits", {}).get("compiler_version") != version):
                return False
            if select_scientific_scope_limits(run,
                    plan if "scientific_scope_limits" in plan else None,
                    previous_scopes=[scope, *previous_scopes]) != scope["scope_limits"]:
                return False
            previous_step = None
            if scope.get("structural_followup"):
                from .ownership_followup import POLICY as FOLLOWUP_POLICY, build_followup_scope
                if plan.get("structural_followup_policy") != FOLLOWUP_POLICY or len(previous_audits) != 1:
                    return False
                previous_step = (previous_scopes[0], previous_record)
                expected = build_followup_scope(old, baseline, previous_record, record, previous_audits[0], previous_scopes[0])
            else:
                expected = build_scientific_revision_scope(old, record, baseline,
                    baseline_origin="fresh_model_audit", additional_audits=previous_audits, **scope["scope_limits"])
            return (expected is not None and expected == scope
                    and not validate_scientific_revision_audit(scope, baseline, parsed,
                        old_record=old, new_record=record, additional_audits=previous_audits, previous_step=previous_step))
        except (ValueError, OSError, TypeError, KeyError, IndexError, AttributeError):
            return False

    @staticmethod
    def _scientific_plan(original_plan: dict, audit_plan: dict, manifest: dict) -> dict:
        """A rule upgrade changes versions, never the source/cohort identity."""
        scientific = audit_plan.get("scientific_plan")
        targets = {"rule_version": "target_rule_version", "schema_version": "target_schema_version"}
        if scientific is None:
            if any(key in manifest for key in targets.values()):
                raise ValueError("re-audit target versions require a frozen scientific_plan")
            return original_plan
        fields = {"paper_id", "cohort_role", "rule_version", "schema_version"}
        if not isinstance(scientific, dict) or set(scientific) != fields:
            raise ValueError("re-audit scientific_plan must contain exactly the scientific identity and versions")
        if any(scientific[key] != original_plan[key] for key in ("paper_id", "cohort_role")):
            raise ValueError("re-audit scientific_plan cannot change paper or cohort identity")
        if any(not isinstance(scientific[key], str) or not scientific[key]
               or scientific[key] != manifest.get(target) for key, target in targets.items()):
            raise ValueError("re-audit scientific_plan versions differ from its target manifest")
        effective = {**original_plan, **scientific}
        if scientific["rule_version"] in {"0.7", "0.8", "0.9"}:
            if not isinstance(audit_plan.get("extraction_policy"), dict):
                raise ValueError("scoped re-audit requires frozen extraction scope")
            effective["extraction_policy"] = audit_plan["extraction_policy"]
        return effective

    def _effective_plan(self, row: dict, original_plan: dict) -> dict:
        """Resolve registered versions from the frozen audit, including human descendants.

        Database metadata is a pointer/check, not authority to replace a source
        plan. Old source plans and candidate files remain byte-for-byte intact.
        """
        meta = row["metadata"]
        stored = meta.get("scientific_plan")
        audit_id = meta.get("audit_run_id")
        if not audit_id:
            if stored is not None:
                raise ValueError("scientific_plan lacks a registered audit run")
            return original_plan
        registered = next((r for r in self.store.runs()
                           if r["run_id"] == audit_id and r["kind"] == "web_reaudit"), None)
        if registered is None:
            raise ValueError("scientific_plan audit run is not registered")
        audit_plan, bundle_path, _, _ = self._source(audit_id)
        manifest = _json(self._safe_data_path(registered["manifest_path"]))
        if (manifest.get("parent_run") != row["run_id"]
                or manifest.get("source_bundle_sha256") != meta["source_hash"]
                or hash_file(bundle_path) != meta["source_hash"]
                or audit_plan.get("record_id") != manifest.get("record_id")
                or audit_plan.get("record_hash") != manifest.get("record_hash")):
            raise ValueError("scientific_plan audit does not bind the registered source lineage")
        effective = self._scientific_plan(original_plan, audit_plan, manifest)
        if stored != audit_plan.get("scientific_plan"):
            raise ValueError("registered scientific_plan differs from its frozen audit plan")
        return effective

    def _assess(self, record: ExtractionRecord, run_id: str, result: dict, audit: dict,
                *, provenance: bool = True):
        plan, bundle_path, bundle, documents = self._source(run_id)
        issues = validate_record(record, source_documents=documents)
        issues += validate_plan_metadata(record, plan, documents, store=self.store)
        errors = [i.model_dump(mode="json") for i in issues if i.severity == "error"]
        reasons = []
        if errors:
            reasons.append("machine_validation_errors")
        if (result.get("status") not in COMPLETED or result.get("stop_reason") != "audit_completed"
                or result.get("reading_check_issues") or result.get("validation_error_count", 0)):
            reasons.append("model_audit_incomplete")
        if (any(f.get("needs_revision") or f.get("verdict") == "unsupported" for f in audit.get("findings", []))
                or audit.get("potential_omissions")):
            reasons.append("model_revision_required")
        if not self._audit_consistency(record, audit, documents):
            reasons.append("reading_coverage_or_anchor_incomplete")
        if provenance:
            try:
                if not self._audit_provenance(self.settings.data_root / "runs" / run_id, record, result, audit):
                    reasons.append("audit_provenance_unverified")
            except (ValueError, OSError, KeyError):
                reasons.append("audit_provenance_unverified")
        return {"errors": errors, "issues": [i.model_dump(mode="json") for i in issues],
                "reasons": reasons, "admissible": not reasons}, bundle_path, bundle, documents

    @staticmethod
    def _audit_consistency(record, audit, documents):
        if quantitative_followup_issues(record, audit, documents):
            return False
        known = {d.source.source_id: {b.block_id for b in d.blocks} for d in documents}
        seen = {source_id: set() for source_id in known}
        for coverage in record.coverage:
            if coverage.source_id not in known or not set(coverage.block_ids).issubset(known.get(coverage.source_id, set())):
                return False
            seen[coverage.source_id].update(coverage.block_ids)
        if any(blocks - seen[source_id] for source_id, blocks in known.items()):
            return False
        return all(anchor.get("block_id") in known.get(anchor.get("source_id"), set())
                   for finding in audit.get("findings", []) for anchor in finding.get("evidence", []))

    def refresh(self) -> list[dict]:
        """Index only catalog-registered extraction runs; never scan arbitrary JSON."""
        with self.lock:
            for run_meta in sorted(self.store.runs(), key=lambda r: r["created_at"]):
                if run_meta["kind"] != "extraction":
                    continue
                run_id = safe_name(run_meta["run_id"])
                run = self.settings.data_root / "runs" / run_id
                if not (run / "candidate.json").is_file() or not (run / "result.json").is_file():
                    continue
                with self._db() as db:
                    cached = db.execute("SELECT record_hash, metadata FROM records WHERE id=?", (run_id,)).fetchone()
                previous = json.loads(cached["metadata"]) if cached else None
                if previous and not _recheck_cached_receipt(previous):
                    continue
                try:
                    record = ExtractionRecord.model_validate(_json(run / "candidate.json"))
                    result = _json(run / "result.json")
                    audit = _json(run / "model_review.json") if (run / "model_review.json").is_file() else {}
                    assessment, bundle_path, _, _ = self._assess(record, run_id, result, audit)
                    metadata = {"result": result, "audit": audit, "assessment": assessment,
                        "sources_bundle": str(bundle_path), "title": _value(record.paper.title),
                        "doi": _value(record.paper.doi), "observation_count": len(record.observations),
                        "origin": "model", "source_hash": hash_file(bundle_path),
                        "result_hash": hash_file(run / "result.json"),
                        "audit_hash": hash_file(run / "model_review.json") if audit else None}
                    if result.get("review_mode") in LOCAL_REVIEW_MODES:
                        metadata["receipt_assessment_version"] = _receipt_assessment_version(result)
                    if previous:
                        if (cached["record_hash"] != hash_file(run/"candidate.json")
                                or any(previous.get(k) != metadata[k] for k in ("source_hash","result_hash","audit_hash"))):
                            continue
                        metadata["previous_assessments"] = [*previous.get("previous_assessments", []), previous["assessment"]]
                    with self._db() as db:
                        if previous:
                            updated = db.execute("UPDATE records SET metadata=? WHERE id=? AND metadata=?",
                                (json.dumps(metadata, ensure_ascii=False), run_id, cached["metadata"]))
                            if updated.rowcount != 1:
                                continue
                        else:
                            db.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?)", (
                                run_id, record.paper.id, run_id, None, run_meta["created_at"],
                                str(run / "candidate.json"), hash_file(run / "candidate.json"), json.dumps(metadata, ensure_ascii=False)))
                        old = db.execute("SELECT r.created_at,r.metadata FROM current_records c JOIN records r ON r.id=c.record_id WHERE c.paper_id=?", (record.paper.id,)).fetchone()
                        if old is None or (assessment["admissible"] and run_meta["created_at"] > old["created_at"]):
                            db.execute("INSERT INTO current_records VALUES (?,?) ON CONFLICT(paper_id) DO UPDATE SET record_id=excluded.record_id", (record.paper.id, run_id))
                except (ValueError, OSError, KeyError):
                    # Invalid runs remain in the original run catalog for diagnostics.
                    # They must never become externally supplied publication records.
                    continue
        return self.list_records()

    def _row(self, record_id: str) -> dict:
        safe_name(record_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError("record not found")
        value = dict(row)
        value["metadata"] = json.loads(value["metadata"])
        return value

    def _summary(self, row: dict) -> dict:
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        with self._db() as db:
            current = db.execute("SELECT record_id FROM current_records WHERE paper_id=?", (row["paper_id"],)).fetchone()
            pub = db.execute("SELECT * FROM publications WHERE paper_id=?", (row["paper_id"],)).fetchone()
            reviews = db.execute("SELECT COUNT(*) FROM reviews WHERE record_id=?", (row["id"],)).fetchone()[0]
            latest_event = db.execute("SELECT status FROM publication_events WHERE record_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            changed = db.execute("SELECT reason FROM source_changes WHERE paper_id=?", (row["paper_id"],)).fetchone()
        state = meta["result"].get("status", "unknown")
        published_current = bool(pub and pub["record_id"] == row["id"])
        publish_status = ("published" if published_current else "superseded" if pub and latest_event and latest_event["status"] == "published"
                          else latest_event["status"] if latest_event and latest_event["status"] in {"failed", "publishing"} else "not_published")
        return {"id": row["id"], "paper_id": row["paper_id"], "title": meta.get("title") or row["paper_id"],
            "doi": meta.get("doi"), "run_id": row["run_id"], "status": state,
            "observation_count": meta.get("observation_count", 0),
            "review_status": "partial_reviewed" if reviews else "not_reviewed",
            "publish_status": publish_status, "published_current": published_current,
            "snapshot_id": pub["snapshot_id"] if published_current else None,
            "created_at": row["created_at"], "parent_id": row["parent_id"],
            "current": bool(current and current["record_id"] == row["id"]), "record_hash": row["record_hash"],
            "validation_errors": len(meta["assessment"]["errors"]),
            "admissible": bool(meta["assessment"]["admissible"] and not changed),
            "admission_reasons": meta["assessment"]["reasons"] + (["source_changed"] if changed else []),
            "source_change": changed["reason"] if changed else None,
            "human_validated": False, "review_scope": "selected_items", "origin": meta.get("origin", "model")}

    def list_records(self) -> list[dict]:
        with self._db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM records ORDER BY created_at DESC")]
        return [self._summary(r) for r in rows]

    def get(self, record_id: str) -> dict:
        row = self._row(record_id)
        path = self._safe_data_path(row["record_path"])
        if hash_file(path) != row["record_hash"]:
            raise ValueError("record integrity failure; existing versions cannot be overwritten")
        record = ExtractionRecord.model_validate(_json(path))
        meta = row["metadata"]
        plan, bundle_path, bundle, documents = self._source(row["run_id"])
        plan = self._effective_plan(row, plan)
        if hash_file(bundle_path) != meta["source_hash"]:
            raise ValueError("source version changed")
        issues = validate_record(record, source_documents=documents)
        issues += validate_plan_metadata(record, plan, documents, store=self.store)
        errors = [i.model_dump(mode="json") for i in issues if i.severity == "error"]
        if meta["origin"] in {"model", "model_reaudit", "model_revision"}:
            run = self.settings.data_root / "runs" / meta.get("audit_run_id", row["run_id"])
            if meta.get("audit_run_id"):
                _, audited_sources, _, _ = self._source(meta["audit_run_id"])
                if hash_file(audited_sources) != meta["source_hash"]:
                    raise ValueError("re-audit source bundle changed")
            if (hash_file(run / "result.json") != meta["result_hash"] or
                    (meta.get("audit_hash") and hash_file(run / "model_review.json") != meta["audit_hash"])):
                raise ValueError("model result or audit changed after registration")
            if meta["assessment"]["admissible"]:
                # A later development assignment blocks publication today. Authenticate the
                # historical review against the cohort ledger available when it was registered.
                stale_cohort = any(i.code == "PLAN_COHORT_STALE" for i in issues)
                with cohort_at_receipt(row["created_at"]) if stale_cohort else nullcontext():
                    if not self._audit_provenance(run, record, meta["result"], meta["audit"]):
                        raise ValueError("registered audit no longer matches its completed model call")
        with self._db() as db:
            review_rows = [dict(r) for r in db.execute("SELECT * FROM reviews WHERE record_id=? ORDER BY created_at", (record_id,))]
            history = [dict(r) for r in db.execute("SELECT id,parent_id,created_at,record_hash FROM records WHERE paper_id=? ORDER BY created_at", (row["paper_id"],))]
        reviews = [{**r, "payload": json.loads(r["payload"])} for r in review_rows]
        summary = self._summary(row)
        if errors:
            summary.update(admissible=False, validation_errors=len(errors))
            summary["admission_reasons"] = list(set(summary["admission_reasons"] + ["machine_validation_errors"]))
        from .quantitative import quantitative_summary
        return {"id": record_id, "record": record.model_dump(mode="json"), "record_path": str(path), "summary": summary,
            "quantitative_coverage_summary": quantitative_summary(record),
            "result": meta["result"], "audit": meta["audit"], "sources_bundle": str(bundle_path),
            "selected_observation_ids": [o.id for o in select_observations(record)],
            "reviews": reviews, "history": history, "validation_issues": [i.model_dump(mode="json") for i in issues]}

    def current_records(self, published_only: bool = False) -> list[dict]:
        table = "publications" if published_only else "current_records"
        with self._db() as db:
            ids = [r["record_id"] for r in db.execute(f"SELECT record_id FROM {table} ORDER BY paper_id")]
        return [self.get(identifier) for identifier in ids]

    def core_view(self, record_id: str, view_id: str | None = None) -> dict:
        """Read a separately versioned research view, never replace its candidate.

        This endpoint does not enqueue work, classify scientific facts, publish
        a graph, or write an artifact. A registered mapping is bound to the exact
        immutable source bytes and reproduced before it can be displayed.
        """
        detail = self.get(record_id)
        return self._core_view_from_detail(detail, view_id)

    def _core_view_from_detail(self, detail: dict, view_id: str | None = None) -> dict:
        """Use an already verified detail within one HTTP request, without re-auditing it."""
        from .core_view import build_core_view, build_native_core_view

        record_id = detail["id"]
        raw_hash = detail["summary"]["record_hash"]
        fingerprint = digest_json(detail["record"])
        if view_id is not None:
            safe_name(view_id)
        if detail["record"].get("rule_version") in {"0.8", "0.9"}:
            view = build_native_core_view(detail["record"], source_sha256=raw_hash,
                                          source_path=detail["record_path"])
            if view_id is not None and view_id != view["view_id"]:
                raise KeyError("core-view version not found for this candidate")
            return {"available": True, "record_id": record_id, "view": view,
                    "versions": [{"id": view["view_id"], "policy_version": detail["record"]["rule_version"]}],
                    "record_hash": raw_hash, "download_scope": "extracted_research_scope",
                    "source_download_scope": "complete_original_candidate"}
        registry = self.settings.data_root / "workbench" / "core_views" / raw_hash
        if not registry.exists():
            return {"available": False, "record_id": record_id, "versions": [],
                    "reason": "此版本尚无三层研究视图；完整抽取结果保持原样。"}
        registry = self._safe_data_path(registry)
        versions = []
        for manifest_path in sorted(registry.glob("*/core-view-manifest.json")):
            manifest_path = self._safe_data_path(manifest_path)
            if not manifest_path.is_relative_to(registry):
                raise ValueError("core-view manifest is outside its source registry")
            manifest = _json(manifest_path)
            if (manifest.get("version") != "research-core-view-manifest-v1"
                    or manifest.get("artifact_type") != "research_core_view"
                    or manifest.get("source_record_sha256") != raw_hash
                    or manifest.get("source_record_fingerprint") != fingerprint
                    or manifest.get("status") != "derived_not_reaudited"):
                raise ValueError("core-view manifest does not bind this immutable candidate")
            identifier = safe_name(manifest.get("view_id", ""))
            versions.append({"id": identifier, "created_at": manifest.get("created_at", ""),
                             "policy_version": manifest.get("policy_version"),
                             "view_sha256": manifest.get("view_sha256"),
                             "manifest": manifest, "manifest_path": manifest_path})
        if not versions:
            return {"available": False, "record_id": record_id, "versions": [],
                    "reason": "此版本尚无三层研究视图；完整抽取结果保持原样。"}
        if len({item["id"] for item in versions}) != len(versions):
            raise ValueError("core-view registry contains duplicate view versions")
        versions.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        if view_id is None and (registry / "current.json").exists():
            pointer = _json(self._safe_data_path(registry / "current.json"))
            if (pointer.get("version") != "research-core-view-selection-v1"
                    or pointer.get("source_record_sha256") != raw_hash):
                raise ValueError("core-view selection does not bind this source candidate")
            view_id = safe_name(pointer.get("view_id", ""))
            selected = next((item for item in versions if item["id"] == view_id), None)
            if selected is None or hash_file(selected["manifest_path"]) != pointer.get("manifest_sha256"):
                raise ValueError("selected core-view manifest is missing or changed")
        if view_id is None and len(versions) > 1:
            raise ValueError("multiple research views require an explicit bound selection or view_id")
        chosen = next((item for item in versions if item["id"] == view_id), None) if view_id else versions[0]
        if chosen is None:
            raise KeyError("core-view version not found for this candidate")
        manifest, folder = chosen["manifest"], chosen["manifest_path"].parent

        def artifact(path_key, hash_key):
            value = manifest.get(path_key)
            if not isinstance(value, str) or not value or Path(value).is_absolute():
                raise ValueError("core-view artifact paths must be relative to their manifest")
            path = self._safe_data_path(folder / value)
            if not path.is_relative_to(folder) or hash_file(path) != manifest.get(hash_key):
                raise ValueError("core-view artifact is changed or outside its immutable version")
            return _json(path)

        mapping = artifact("mapping_path", "mapping_sha256")
        view = artifact("view_path", "view_sha256")
        source_path = self._safe_data_path(manifest["source_record_path"])
        if hash_file(source_path) != raw_hash:
            raise ValueError("core-view original candidate has changed")
        expected = build_core_view(detail["record"], mapping,
                                   source_sha256=raw_hash, source_path=str(source_path), view_version=view.get("version"))
        if expected != view or view.get("view_id") != chosen["id"]:
            raise ValueError("core-view artifact differs from its bound mapping and candidate")
        public_versions = [{key: value for key, value in item.items() if key not in {"manifest", "manifest_path"}}
                           for item in versions]
        return {"available": True, "record_id": record_id, "view": view,
                "versions": public_versions, "record_hash": raw_hash,
                "download_scope": "derived_research_view", "source_download_scope": "complete_original_candidate"}

    def publication_scope(self) -> list[dict]:
        """Only these immutable snapshot IDs belong to the current workbench view."""
        with self._db() as db:
            rows = [dict(r) for r in db.execute("SELECT p.*,c.record_id AS latest_record_id,s.reason AS source_change FROM publications p LEFT JOIN current_records c USING(paper_id) LEFT JOIN source_changes s USING(paper_id) ORDER BY p.paper_id")]
        return [{"paper_id": r["paper_id"], "record_id": r["record_id"], "snapshot_id": r["snapshot_id"],
                 "latest_record_id": r["latest_record_id"], "source_change": r["source_change"],
                 "stale": r["latest_record_id"] != r["record_id"] or bool(r["source_change"])} for r in rows]

    def mark_source_changed(self, paper_id: str, reason: str):
        with self._db() as db:
            db.execute("INSERT INTO source_changes VALUES (?,?,?) ON CONFLICT(paper_id) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at", (paper_id, reason, utc_now()))

    def mark_source_stale(self, paper_id: str, reason: str):
        self.mark_source_changed(paper_id, reason)

    def clear_source_changed(self, paper_id: str, record_id: str):
        row = self._row(record_id)
        if row["paper_id"] != paper_id:
            raise ValueError("record belongs to another paper")
        detail = self.get(record_id)
        if (not detail["summary"]["current"] or detail["summary"]["validation_errors"]
                or not row["metadata"]["assessment"]["admissible"]):
            raise ValueError("only a fully source-verified current candidate can clear changed-source admission")
        with self._db() as db:
            db.execute("DELETE FROM source_changes WHERE paper_id=?", (paper_id,))

    def _review_base(self, record_id: str, reviewer: str, base_hash: str) -> dict:
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
            raise ValueError("reviewer must be a nonempty name of at most 120 characters")
        result = self.get(record_id)
        if base_hash != result["summary"]["record_hash"] or not result["summary"]["current"]:
            raise ReviewConflict("record changed; reload and compare the latest version before submitting")
        return result

    def _entries(self, record: ExtractionRecord, entries: list, *, submitting: bool):
        if not isinstance(entries, list) or len(entries) > 100 or (submitting and not entries):
            raise ValueError("review entries must contain 1 to 100 items when submitting")
        observations = {o.id for o in record.observations}
        evidence = {e.id for e in record.evidence}
        cleaned = []
        seen = set()
        for item in entries:
            if not isinstance(item, dict) or set(item) - {"observation_id", "action", "scope", "patch", "note", "evidence_ids"}:
                raise ValueError("unsupported review entry")
            entry = {"observation_id": item.get("observation_id"), "action": item.get("action"),
                "scope": item.get("scope", []), "patch": item.get("patch", {}),
                "note": item.get("note", ""), "evidence_ids": item.get("evidence_ids", [])}
            if (not isinstance(entry["action"], str) or entry["action"] not in ACTIONS
                    or not isinstance(entry["observation_id"], str) or entry["observation_id"] not in observations):
                raise ValueError("unknown review action or observation")
            if (not isinstance(entry["scope"], list) or not entry["scope"] or
                    any(not isinstance(s, str) or s not in REVIEW_SCOPES for s in entry["scope"])):
                raise ValueError("review must declare its exact supported field scope")
            if (not isinstance(entry["patch"], dict) or set(entry["patch"]) - PATCH_FIELDS or
                    not set(entry["patch"]).issubset(entry["scope"])):
                raise ValueError("patch changes fields outside the explicitly reviewed scope")
            if (not isinstance(entry["note"], str) or len(entry["note"]) > 6000 or
                    not isinstance(entry["evidence_ids"], list) or any(not isinstance(e, str) or e not in evidence for e in entry["evidence_ids"])):
                raise ValueError("invalid note or unknown source evidence")
            if submitting and entry["action"] != "confirm" and not entry["note"].strip():
                raise ValueError("correction or unresolved judgment requires a source-based explanation")
            if entry["action"] in {"confirm", "uncertain", "omission"} and entry["patch"]:
                raise ValueError("this action records a judgment, not a factual patch")
            if entry["action"] == "correct" and submitting and not entry["patch"]:
                raise ValueError("correct requires a concrete patch")
            for field in entry["scope"]:
                key = (entry["observation_id"], field)
                if key in seen:
                    raise ValueError("overlapping judgments in one submission must be resolved explicitly")
                seen.add(key)
            cleaned.append(entry)
        return cleaned

    def save_draft(self, record_id: str, reviewer: str, entries: list, base_hash: str) -> dict:
        detail = self._review_base(record_id, reviewer, base_hash)
        entries = self._entries(ExtractionRecord.model_validate(detail["record"]), entries, submitting=False)
        saved = utc_now()
        with self._db() as db:
            db.execute("INSERT INTO drafts VALUES (?,?,?,?,?) ON CONFLICT(record_id,reviewer) DO UPDATE SET base_hash=excluded.base_hash,payload=excluded.payload,saved_at=excluded.saved_at", (record_id, reviewer.strip(), base_hash, json.dumps(entries, ensure_ascii=False), saved))
        return {"record_id": record_id, "saved_at": saved, "status": "draft_saved", "human_validated": False}

    def get_draft(self, record_id: str, reviewer: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM drafts WHERE record_id=? AND reviewer=?", (record_id, reviewer.strip())).fetchone()
        return {**dict(row), "entries": json.loads(row["payload"])} if row else None

    def submit_review(self, record_id: str, reviewer: str, entries: list, base_hash: str) -> dict:
        with self.lock:
            detail = self._review_base(record_id, reviewer, base_hash)
            before = ExtractionRecord.model_validate(detail["record"])
            entries = self._entries(before, entries, submitting=True)
            raw = copy.deepcopy(detail["record"])
            indexed = {o["id"]: o for o in raw["observations"]}
            changed_ids, changed_experiments = set(), set()
            needs_reaudit = False
            for entry in entries:
                observation = indexed[entry["observation_id"]]
                patch = copy.deepcopy(entry["patch"])
                if entry["action"] == "not_reported":
                    if any(s not in {"value", "unit"} for s in entry["scope"]):
                        raise ValueError("not_reported applies to selected value/unit facts only")
                    patch = {s: Fact(status=FactStatus.NOT_REPORTED, note=entry["note"],
                                   evidence_ids=entry["evidence_ids"]).model_dump(mode="json") for s in entry["scope"]}
                for field, value in patch.items():
                    if field in {"value", "unit"}:
                        fact = Fact.model_validate(value)
                        if entry["action"] == "conflict" and fact.status != FactStatus.CONFLICT:
                            raise ValueError("a conflict judgment must preserve conflicting source alternatives")
                        if fact.status in {FactStatus.REPORTED, FactStatus.INFERRED, FactStatus.CONFLICT, FactStatus.ESTIMATED} and not fact.evidence_ids:
                            fact.evidence_ids = entry["evidence_ids"]
                        if fact.status in {FactStatus.REPORTED, FactStatus.INFERRED, FactStatus.CONFLICT, FactStatus.ESTIMATED} and not fact.evidence_ids:
                            raise ValueError("a corrected scientific value requires explicit source evidence")
                        if (field == "value" and observation.get("origin") == "plot_estimate"
                                and fact.value is not None and fact.status == FactStatus.REPORTED):
                            raise ValueError("a plot estimate must remain estimated, not an explicitly reported value")
                        if fact.status == FactStatus.CONFLICT and len(fact.alternatives) < 2:
                            raise ValueError("conflict must preserve both source alternatives")
                        if (fact.value == 0 or fact.normalized == 0) and observation[field]["status"] in {"not_reported", "not_available", "unread", "read_unquantified"}:
                            raise ValueError("missing facts cannot be converted into zero in a review patch; request source re-extraction")
                        patch[field] = fact.model_dump(mode="json")
                    if value != observation.get(field):
                        changed_ids.add(observation["id"])
                        changed_experiments.add(observation["experiment_id"])
                        if field in {"experiment_id", "material_id", "time_window"}:
                            needs_reaudit = True
                observation.update(patch)
                if patch:
                    changed_experiments.add(observation["experiment_id"])
                if entry["action"] in {"omission", "uncertain", "conflict"} and not patch:
                    needs_reaudit = True
            after = ExtractionRecord.model_validate(raw)
            row = self._row(record_id)
            plan, bundle_path, _, documents = self._source(row["run_id"])
            plan = self._effective_plan(row, plan)
            issues = validate_record(after, source_documents=documents)
            issues += validate_plan_metadata(after, plan, documents, store=self.store)
            errors = [i.model_dump(mode="json") for i in issues if i.severity == "error"]
            if errors:
                raise ValueError("review patch violates record consistency: " + json.dumps(errors[:8], ensure_ascii=False))
            changed = after != before
            created = utc_now()
            review_id = "review-" + uuid.uuid4().hex
            target_id = record_id
            review_payload = {"entries": entries, "scope": "selected_items", "base_record_hash": base_hash,
                "changed_observation_ids": sorted(changed_ids), "affected_experiment_ids": sorted(changed_experiments),
                "human_validated": False, "scientific_accuracy": "not_established", "reviewer_kind": "human"}
            if changed or needs_reaudit:
                target_id = "rev-" + uuid.uuid4().hex
                folder = self.versions / target_id
                path = folder / "record.json"
                write_new_json(path, after.model_dump(mode="json"))
                meta = copy.deepcopy(row["metadata"])
                meta["origin"] = "human_revision"
                meta["result"] = {**meta["result"], "status": "needs_reaudit" if needs_reaudit else "human_revised_candidate",
                    "human_validated": False, "human_review_scope": "selected_items", "parent_record_id": record_id,
                    "inherited_model_audit_applies_to": "unchanged_content_only", "graph_published": False}
                meta["assessment"] = {"issues": [i.model_dump(mode="json") for i in issues], "errors": [],
                    "admissible": bool(detail["summary"]["admissible"] and not needs_reaudit),
                    "reasons": ["needs_reaudit"] if needs_reaudit else list(detail["summary"]["admission_reasons"])}
                meta["audit_scope"] = "parent_unchanged_fields_plus_explicit_human_corrections"
                meta["review_id"] = review_id
                meta["invalidated_review_observations"] = sorted(changed_ids)
                if needs_reaudit:
                    questioned = {e["observation_id"] for e in entries if e["action"] in {"uncertain", "omission", "conflict"}}
                    meta["invalidated_review_observations"] = sorted(questioned | changed_ids | {
                        o.id for o in before.observations if o.experiment_id in changed_experiments})
                write_new_json(folder / "metadata.json", meta)
                write_new_json(folder / "review.json", review_payload)
                with self._db() as db:
                    # Concurrent submitters must not branch invisibly.
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT record_id FROM current_records WHERE paper_id=?", (row["paper_id"],)).fetchone()
                    if not current or current["record_id"] != record_id:
                        raise ReviewConflict("another revision was submitted; reload the latest result")
                    db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)", (target_id, row["paper_id"], row["run_id"], record_id, created, str(path), hash_file(path), json.dumps(meta, ensure_ascii=False)))
                    db.execute("UPDATE current_records SET record_id=? WHERE paper_id=?", (target_id, row["paper_id"]))
                    # Only field judgments whose complete context is unchanged survive.
                    old_reviews = db.execute("SELECT * FROM reviews WHERE record_id=?", (record_id,)).fetchall()
                    affected = set(meta["invalidated_review_observations"])
                    for old in old_reviews:
                        old_payload = json.loads(old["payload"])
                        retained = [e for e in old_payload["entries"] if e["observation_id"] not in affected]
                        if retained:
                            inherited = {**old_payload, "entries": retained, "inherited_from": old["id"], "base_record_hash": hash_file(path)}
                            db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)", ("review-" + uuid.uuid4().hex, target_id, old["parent_record_id"], old["reviewer"], created, json.dumps(inherited, ensure_ascii=False)))
            review_payload["result_record_hash"] = self._row(target_id)["record_hash"]
            with self._db() as db:
                db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)", (review_id, target_id, record_id, reviewer.strip(), created, json.dumps(review_payload, ensure_ascii=False)))
                db.execute("DELETE FROM drafts WHERE record_id=? AND reviewer=?", (record_id, reviewer.strip()))
            return {"record_id": target_id, "parent_record_id": record_id, "review_id": review_id,
                "changed": changed, "needs_reaudit": needs_reaudit, "human_validated": False,
                "summary": self.get(target_id)["summary"]}

    @staticmethod
    def _reviewed_content(record: dict, observation_id: str, field: str):
        observation = next((o for o in record["observations"] if o["id"] == observation_id), None)
        if observation is None:
            return None
        experiment = next((e for e in record["experiments"] if e["id"] == observation["experiment_id"]), None)
        if field == "conditions":
            return experiment
        if field == "pairing":
            return {"experiment_id": observation["experiment_id"], "pairs": experiment.get("pairs", []) if experiment else []}
        if field == "stability":
            return {"observation": observation, "experiment": experiment}
        if field == "evidence":
            referenced = set(observation.get("evidence_ids", []))
            for fact in (observation.get("value", {}), observation.get("unit", {})):
                referenced.update(fact.get("evidence_ids", []))
            return [e for e in record["evidence"] if e["id"] in referenced]
        return observation.get(field)

    def _human_conflicts(self, before: dict, after: dict, reviews: list[dict]) -> list[dict]:
        conflicts = []
        for review in reviews:
            for entry in review["payload"]["entries"]:
                if entry["action"] not in {"confirm", "correct", "not_reported", "conflict"}:
                    continue
                changed = [field for field in entry["scope"] if self._reviewed_content(before, entry["observation_id"], field)
                           != self._reviewed_content(after, entry["observation_id"], field)]
                if changed:
                    conflicts.append({"review_id": review["id"], "observation_id": entry["observation_id"], "scope": changed})
        return conflicts

    def register_reaudit(self, record_id: str, run_id: str) -> dict:
        """Register a complete fresh audit of the exact current record version."""
        return self._register_model_result(record_id, run_id, allow_revision=False)

    def accept_model_revision(self, record_id: str, run_id: str) -> dict:
        """Accept a model revision only after a new audit and scoped human guards."""
        return self._register_model_result(record_id, run_id, allow_revision=True)

    def _register_model_result(self, record_id: str, run_id: str, *, allow_revision: bool) -> dict:
        with self.lock:
            safe_name(run_id)
            detail = self.get(record_id)
            if not detail["summary"]["current"]:
                raise ReviewConflict("re-audit belongs to an outdated version")
            row = self._row(record_id)
            registered = next((r for r in self.store.runs() if r["run_id"] == run_id and r["kind"] == "web_reaudit"), None)
            if not registered:
                raise ValueError("re-audit must be a registered web_reaudit run")
            run = self.settings.data_root / "runs" / run_id
            manifest = _json(self._safe_data_path(registered["manifest_path"]))
            if (manifest.get("record_id") != record_id or manifest.get("record_hash") != row["record_hash"]
                    or manifest.get("parent_run") != row["run_id"]
                    or manifest.get("source_bundle_sha256") != row["metadata"]["source_hash"]
                    or plan_model(manifest) is None):
                raise ValueError("re-audit manifest does not bind the current record, sources and Sol MAX")
            reaudit_plan, source_path, _, documents = self._source(run_id)
            if (not manifest_matches_plan(reaudit_plan, manifest)
                    or reaudit_plan.get("record_id") != record_id or reaudit_plan.get("record_hash") != row["record_hash"]
                    or hash_file(source_path) != row["metadata"]["source_hash"]):
                raise ValueError("re-audit plan/source version differs from the requested record")
            frozen = self._safe_data_path(run / "input_candidate.json")
            if (not any(Path(p["path"]).resolve() == frozen and p["sha256"] == hash_file(frozen)
                        for p in reaudit_plan.get("input_paths", []))
                    or ExtractionRecord.model_validate(_json(frozen)).model_dump(mode="json") != detail["record"]):
                raise ValueError("re-audit input candidate is not the frozen original version")
            candidate = ExtractionRecord.model_validate(_json(self._safe_data_path(run / "candidate.json")))
            result, audit = _json(run / "result.json"), _json(run / "model_review.json")
            changed = candidate.model_dump(mode="json") != detail["record"]
            if changed and not allow_revision:
                raise ValueError("audit-only registration cannot change scientific facts")
            original_plan, _, _, _ = self._source(row["run_id"])
            scientific_plan = self._scientific_plan(original_plan, reaudit_plan, manifest)
            if scientific_plan["rule_version"] in {"0.7", "0.8", "0.9"}:
                from .scope_policy import validate_scope_baseline
                validate_scope_baseline(scientific_plan["extraction_policy"], ExtractionRecord.model_validate(_json(frozen)))
            issues = validate_record(candidate, source_documents=documents)
            issues += validate_plan_metadata(candidate, scientific_plan, documents, store=self.store)
            errors = [i.model_dump(mode="json") for i in issues if i.severity == "error"]
            parsed_audit = ReadingAudit.model_validate(audit)
            if (errors or result.get("status") not in COMPLETED or result.get("stop_reason") != "audit_completed"
                    or result.get("validation_error_count", 0) or result.get("reading_check_issues")
                    or any(f.needs_revision or f.verdict == "unsupported" for f in parsed_audit.findings)
                    or parsed_audit.potential_omissions
                    or not self._audit_consistency(candidate, audit, documents)
                    or not self._audit_provenance(run, candidate, result, audit)):
                raise ValueError("re-audit has unresolved revisions, machine errors or unverified model provenance")
            if changed:
                delivered = False
                for request_path in (run / "calls").glob("*/request.json"):
                    call = request_path.parent
                    if not (call / "record.json").is_file() or not (call / "result.json").is_file():
                        continue
                    envelope, complete = _json(request_path), _json(call / "result.json")
                    request = envelope.get("request", {})
                    if (envelope.get("fingerprint") == digest_json(request)
                            and complete.get("status") == "schema_valid_candidate"
                            and receipt_matches_plan(reaudit_plan, request, complete, legacy_sparse=True)
                            and (request.get("stage") == "extraction" or request.get("stage", "").startswith("revision_"))
                            and ExtractionRecord.model_validate(_json(call / "record.json")) == candidate):
                        delivered = True
                        break
                if not delivered and reaudit_plan.get("resumed_staged_candidate"):
                    # A failed turn's staged draft is not completed model output.
                    # It can become a candidate only after protocol verification
                    # AND the exact fresh audit checked above. Never silently
                    # change the failed turn's status or effective metadata.
                    receipt = reaudit_plan["resumed_staged_candidate"]
                    if manifest.get("resumed_staged_candidate") != receipt or result.get("resumed_staged_candidate") != receipt:
                        raise ValueError("resumed checkpoint provenance differs across frozen run metadata")
                    from .web_resume import staged_revision
                    recovery_settings = self.settings.model_copy(update={"rules_version": scientific_plan["rule_version"]})
                    staged, _, verified = staged_revision(recovery_settings, self, detail, receipt["run_id"])
                    if verified != receipt or staged != candidate:
                        raise ValueError("revised candidate differs from the verified resumed checkpoint")
                    delivered = True
                if not delivered:
                    raise ValueError("revised candidate lacks a completed Sol MAX delivery or a verified checkpoint with fresh audit")
                conflicts = self._human_conflicts(detail["record"], candidate.model_dump(mode="json"), detail["reviews"])
                if conflicts:
                    return {"status": "pending_human_conflict", "accepted": False, "record_id": record_id,
                        "run_id": run_id, "conflicts": conflicts, "human_validated": False}
            identifier, created = "rev-" + uuid.uuid4().hex, utc_now()
            folder = self.versions / identifier
            path = folder / "record.json"
            write_new_json(path, candidate.model_dump(mode="json"))
            meta = copy.deepcopy(row["metadata"])
            meta.update(origin="model_revision" if changed else "model_reaudit", result=result, audit=audit,
                audit_run_id=run_id, result_hash=hash_file(run / "result.json"), audit_hash=hash_file(run / "model_review.json"),
                observation_count=len(candidate.observations), title=_value(candidate.paper.title), doi=_value(candidate.paper.doi),
                assessment={"errors": [], "issues": [i.model_dump(mode="json") for i in issues], "reasons": [], "admissible": True})
            if reaudit_plan.get("scientific_plan") is not None:
                meta["scientific_plan"] = copy.deepcopy(reaudit_plan["scientific_plan"])
            else:
                meta.pop("scientific_plan", None)
            write_new_json(folder / "metadata.json", meta)
            write_new_json(folder / "model_review.json", audit)
            changed_context = set()
            if changed:
                before_obs = {o["id"]: o for o in detail["record"]["observations"]}
                after_obs = {o.id: o.model_dump(mode="json") for o in candidate.observations}
                before_exp = {e["id"]: e for e in detail["record"]["experiments"]}
                after_exp = {e.id: e.model_dump(mode="json") for e in candidate.experiments}
                for observation_id, previous in before_obs.items():
                    current = after_obs.get(observation_id)
                    if previous != current or before_exp.get(previous["experiment_id"]) != after_exp.get(previous["experiment_id"]):
                        changed_context.add(observation_id)
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT record_id FROM current_records WHERE paper_id=?", (row["paper_id"],)).fetchone()
                if not current or current["record_id"] != record_id:
                    raise ReviewConflict("record changed while registering re-audit")
                db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)", (identifier, row["paper_id"], row["run_id"], record_id, created, str(path), hash_file(path), json.dumps(meta, ensure_ascii=False)))
                db.execute("UPDATE current_records SET record_id=? WHERE paper_id=?", (identifier, row["paper_id"]))
                for review in detail["reviews"]:
                    payload = review["payload"]
                    retained = [e for e in payload["entries"] if e["observation_id"] not in changed_context]
                    if retained:
                        inherited = {**payload, "entries": retained, "inherited_from": review["id"], "base_record_hash": hash_file(path)}
                        db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)", ("review-" + uuid.uuid4().hex, identifier, review["parent_record_id"], review["reviewer"], created, json.dumps(inherited, ensure_ascii=False)))
            return {"status": "model_reaudit_registered", "accepted": True, "record_id": identifier,
                "parent_record_id": record_id, "run_id": run_id, "changed": changed, "summary": self.get(identifier)["summary"]}

    def publish(self, record_id: str, live: bool = False, loader=None) -> dict:
        if not live:
            raise ValueError("publication requires explicit live mode")
        with self.lock:
            detail = self.get(record_id)
            summary = detail["summary"]
            if not summary["current"] or not summary["admissible"]:
                raise ValueError("only the current, source-verified and completed result can be published: " + ", ".join(summary["admission_reasons"]))
            with self._db() as db:
                existing = db.execute("SELECT * FROM publications WHERE paper_id=?", (summary["paper_id"],)).fetchone()
            if existing and existing["record_id"] == record_id:
                return {**json.loads(existing["result"]), "status": "already_published", "record_id": record_id}
            record = ExtractionRecord.model_validate(detail["record"])
            _, _, _, documents = self._source(summary["run_id"])
            from .projection import project_record
            from .graph_store import load_graph
            graph = project_record(record, source_documents=documents)
            snapshot = digest_json(graph.model_dump(mode="json"))
            export = self.root / "publications" / ("publication-" + uuid.uuid4().hex)
            write_new_json(export / "record.json", record.model_dump(mode="json"))
            write_new_json(export / "graph.json", graph.model_dump(mode="json"))
            with self._db() as db:
                db.execute("INSERT INTO publication_events(record_id,status,payload,created_at) VALUES (?,?,?,?)", (record_id, "publishing", json.dumps({"snapshot_id": snapshot, "export": str(export)}), utc_now()))
            try:
                result = (loader or load_graph)(self.settings.neo4j, graph, source_record=export / "record.json", live=True)
                if not result.get("persistent_graph_write") or result.get("snapshot_id") != snapshot:
                    raise ValueError("graph loader did not confirm this exact persistent snapshot")
                # Recheck before switching the local authoritative query pointer.
                refreshed = self.get(record_id)
                if not refreshed["summary"]["current"] or not refreshed["summary"]["admissible"]:
                    raise ReviewConflict("record changed during graph publication; current pointer was not switched")
                result = {**result, "record_id": record_id, "published_at": utc_now(), "human_validated": False}
                write_new_json(export / "result.json", result)
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT record_id FROM current_records WHERE paper_id=?", (record.paper.id,)).fetchone()
                    stale = db.execute("SELECT 1 FROM source_changes WHERE paper_id=?", (record.paper.id,)).fetchone()
                    if not current or current["record_id"] != record_id or stale:
                        raise ReviewConflict("current version or source changed before publication commit")
                    db.execute("INSERT INTO publications VALUES (?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET record_id=excluded.record_id,snapshot_id=excluded.snapshot_id,published_at=excluded.published_at,result=excluded.result", (record.paper.id, record_id, snapshot, result["published_at"], json.dumps(result, ensure_ascii=False)))
                    db.execute("INSERT INTO publication_events(record_id,status,payload,created_at) VALUES (?,?,?,?)", (record_id, "published", json.dumps(result, ensure_ascii=False), utc_now()))
                return result
            except Exception as error:
                failure = {"error_type": type(error).__name__, "message": "graph publication failed; previous active snapshot retained", "snapshot_id": snapshot}
                write_new_json(export / "failure.json", failure)
                with self._db() as db:
                    db.execute("INSERT INTO publication_events(record_id,status,payload,created_at) VALUES (?,?,?,?)", (record_id, "failed", json.dumps(failure), utc_now()))
                raise
