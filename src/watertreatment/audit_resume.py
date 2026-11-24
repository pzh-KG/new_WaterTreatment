"""Authenticate a completed full source audit before a bounded resumed recheck.

No provider/graph calls. Frozen copies are navigation, never authority: admission
revalidates the original registered plan, source files and completed model call.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .domain import ExtractionRecord
from .reading import ReadingAudit
from .storage import digest_json, hash_file, write_new_json
from .validation import validate_record
from .word_review_links import LINKED_POLICY


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _identity(record):
    return (record.rule_version, record.schema_version, record.paper.id,
            record.paper.dataset_role, record.paper.source_ids, record.paper.role_history)


def authenticate_baseline(settings, proof, candidate, *, _seen=()):
    from .web_records import WebRecords
    from .web_model import _reading_checks

    records = WebRecords(settings)
    from .scientific_revision_audit import POLICY as SCIENTIFIC_POLICY
    if proof.get("policy") == SCIENTIFIC_POLICY:
        provenance = proof.get("seed_provenance", {"kind": "failed_revision_stage", "receipt": proof.get("staged_checkpoint")})
        if not isinstance(provenance, dict) or set(provenance) != {"kind", "receipt"}:
            raise ValueError("resumed scientific seed has malformed provenance")
        checkpoint = provenance["receipt"]
        if provenance["kind"] == "failed_revision_stage":
            from .staged_source_resume import authenticate_staged_candidate
            authenticated = authenticate_staged_candidate(settings, Path(checkpoint["stage_path"]))
            if "staged_checkpoint" in proof and proof["staged_checkpoint"] != checkpoint:
                raise ValueError("legacy staged checkpoint differs from its typed seed provenance")
        elif provenance["kind"] == "completed_continuation":
            from .completed_continuation_resume import authenticate_completed_continuation
            authenticated = authenticate_completed_continuation(settings, Path(checkpoint["candidate_path"]), _seen=_seen)
        elif provenance["kind"] == "web_preflight_revision":
            from .web_resume import authenticate_preflight_baseline
            authenticated = authenticate_preflight_baseline(settings, checkpoint["web_run"])
            if authenticated is None:
                raise ValueError("Web repair no longer has a preceding authenticated full audit")
        elif provenance["kind"] == "completed_revision":
            from .completed_native_resume import authenticate_completed_revision
            authenticated = authenticate_completed_revision(settings, Path(checkpoint["candidate_path"]))
        else:
            raise ValueError("unsupported scientific seed provenance")
        if (authenticated.receipt != checkpoint
                or _identity(authenticated.record) != _identity(candidate)
                or proof["parent_run"] != authenticated.parent_run.name
                or proof["parent_plan_sha256"] != checkpoint["parent_plan_sha256"]
                or proof["parent_sources_sha256"] != checkpoint["parent_sources_sha256"]
                or proof["original_candidate_path"] != str(authenticated.baseline_candidate_path)
                or proof["original_candidate_sha256"] != checkpoint["baseline_candidate_sha256"]
                or proof["original_audit_path"] != str(authenticated.baseline_audit_path)
                or proof["original_audit_sha256"] != checkpoint["baseline_audit_sha256"]):
            raise ValueError("staged continuation no longer matches its authenticated failed-turn lineage")
        return authenticated.baseline_record, authenticated.baseline_audit, authenticated.bundle
    prior = records._safe_data_path(settings.data_root / "runs" / proof["parent_run"])
    if prior.parent != (settings.data_root / "runs").resolve():
        raise ValueError("resumed audit is outside the run store")
    plan, bundle_path, bundle, documents = records._source(prior.name)
    if (plan.get("cohort_role") not in {"development", "unassigned"} or plan.get("rule_version") not in {"0.7", "0.8", "0.9"}
            or hash_file(prior / "plan.json") != proof["parent_plan_sha256"]
            or hash_file(bundle_path) != proof["parent_sources_sha256"]):
        raise ValueError("resumed full audit plan/source identity changed")
    paths = {}
    for kind in ("candidate", "audit"):
        path = records._safe_data_path(proof[f"original_{kind}_path"])
        if path.parent != prior or hash_file(path) != proof[f"original_{kind}_sha256"]:
            raise ValueError("resumed baseline artifact changed or escaped its run")
        paths[kind] = path
    if (not re.fullmatch(r"candidate\.v\d{2}\.json", paths["candidate"].name)
            or paths["audit"].name != paths["candidate"].name.replace("candidate.", "audit.")):
        raise ValueError("resumed baseline versions do not correspond")
    old = ExtractionRecord.model_validate(_read(paths["candidate"]))
    audit = ReadingAudit.model_validate(_read(paths["audit"]))
    if (_identity(old) != _identity(candidate) or audit.review_scope != "full"
            or audit.review_scope_sha256 is not None
            or not records._completed_audit_provenance(prior, old, audit, allow_citation=False)):
        raise ValueError("resumed baseline lacks a completed source-bound Sol MAX full audit")
    if any(i.severity == "error" for i in validate_record(old, source_documents=documents)):
        raise ValueError("resumed baseline contains machine errors")
    checks = _reading_checks(old, audit, documents)
    # A completed audit may have identified partial-series restrictions which
    # its delivered repair subsequently added.  The old audit remains unchanged;
    # the new local scope must review those additions and retain its followups.
    repaired_followups = (checks and all(x.startswith("QUANTITATIVE_FOLLOWUP_UNSCOPED:") for x in checks)
                          and not _reading_checks(candidate, audit, documents))
    if checks and not repaired_followups:
        raise ValueError("resumed baseline has unresolved reading consistency defects")
    return old, audit, bundle


def freeze_resume_audit(settings, run, candidate_path, candidate, bundle):
    """Explicit opt-in; use nearest eligible completed full audit, fail closed."""
    from .bounded_revision_audit import build_revision_audit_scope

    prior = candidate_path.resolve(strict=True).parent
    if (prior / candidate_path.name.replace("candidate.", "audit.")).exists():
        raise ValueError("selected candidate already has an audit; do not bypass its findings by inheriting an older audit")
    selected_version = int(candidate_path.stem.rsplit("v", 1)[1])
    for path in sorted(prior.glob("candidate.v[0-9][0-9].json"), reverse=True):
        if int(path.stem.rsplit("v", 1)[1]) >= selected_version:
            continue
        report = prior / path.name.replace("candidate.", "audit.")
        if not report.is_file():
            continue
        proof = {"parent_run": prior.name, "parent_plan_sha256": hash_file(prior / "plan.json"),
                 "parent_sources_sha256": hash_file(prior / "sources.json"),
                 "original_candidate_path": str(path), "original_candidate_sha256": hash_file(path),
                 "original_audit_path": str(report), "original_audit_sha256": hash_file(report)}
        # Do not fall back past a present but invalid baseline to hide tampering.
        old, audit, old_bundle = authenticate_baseline(settings, proof, candidate)
        if digest_json(old_bundle["documents"]) != digest_json(bundle["documents"]):
            raise ValueError("resumed audit source representations differ")
        scope = build_revision_audit_scope(old, candidate, audit, baseline_origin="fresh_model_audit",
                                           source_requirement_policy=LINKED_POLICY)
        if scope is None:
            # Native extraction revisions also have a completed source-bound
            # patch chain; they need not be mistaken for failed streamed drafts.
            from .completed_native_resume import authenticate_completed_revision
            authenticated = authenticate_completed_revision(settings, candidate_path)
            return freeze_seed_resume_audit(settings, run, authenticated, bundle, seed_kind="completed_revision")
        copied = []
        for kind, record in (("candidate", old), ("audit", audit)):
            target = run / f"resumed_baseline_{kind}.json"
            write_new_json(target, record.model_dump(mode="json"))
            proof[f"frozen_{kind}_path"] = str(target)
            proof[f"frozen_{kind}_sha256"] = hash_file(target)
            copied.append({"path": str(target), "sha256": hash_file(target)})
        proof["scope_sha256"] = scope["scope_sha256"]
        proof["source_requirement_policy"] = LINKED_POLICY
        proof["interpretation"] = "Inherited completed full-audit coverage; changed dependencies require a new source recheck. Not scientific validation."
        return proof, copied
    raise ValueError("no completed full source audit precedes this candidate")


def freeze_staged_resume_audit(settings, run, authenticated, bundle):
    return freeze_seed_resume_audit(settings, run, authenticated, bundle, seed_kind="failed_revision_stage")


def freeze_seed_resume_audit(settings, run, authenticated, bundle, *, seed_kind):
    """Freeze the actual staged checkpoint and its completed baseline separately."""
    from .scientific_revision_audit import POLICY, NEW_SCOPE_LIMITS, build_scientific_revision_scope

    if seed_kind not in {"failed_revision_stage", "completed_continuation", "completed_revision", "web_preflight_revision"}:
        raise ValueError("unsupported scientific continuation seed")
    if digest_json(authenticated.bundle["documents"]) != digest_json(bundle["documents"]):
        raise ValueError("staged continuation source representations differ")
    for artifact in authenticated.receipt["artifact_hashes"]:
        if hash_file(Path(artifact["path"])) != artifact["sha256"]:
            raise ValueError("authenticated staged input changed while preparing continuation")
    if hash_file(authenticated.parent_run / "app_server/events.jsonl") != authenticated.receipt["events_sha256"]:
        raise ValueError("authenticated staged protocol changed while preparing continuation")
    scope = build_scientific_revision_scope(authenticated.baseline_record, authenticated.record,
        authenticated.baseline_audit, baseline_origin="fresh_model_audit", **NEW_SCOPE_LIMITS)
    if scope is None:
        raise ValueError("staged changes or unresolved full-audit omissions require broader review")
    checkpoint = authenticated.receipt
    proof = {"policy": POLICY, "parent_run": authenticated.parent_run.name,
        "parent_plan_sha256": checkpoint["parent_plan_sha256"],
        "parent_sources_sha256": checkpoint["parent_sources_sha256"],
        "original_candidate_path": str(authenticated.baseline_candidate_path),
        "original_candidate_sha256": checkpoint["baseline_candidate_sha256"],
        "original_audit_path": str(authenticated.baseline_audit_path),
        "original_audit_sha256": checkpoint["baseline_audit_sha256"],
        "seed_provenance": {"kind": seed_kind, "receipt": checkpoint},
        "scope_limits": dict(NEW_SCOPE_LIMITS),
        "initial_scope_sha256": scope["scope_sha256"],
        "interpretation": "Authenticated scientific seed plus the original completed full baseline. Every cumulative changed claim requires a new source audit; no completed candidate or full audit is invented."}
    if seed_kind == "failed_revision_stage":
        proof["staged_checkpoint"] = checkpoint  # Backward-readable legacy lineage.
    artifacts = []
    for kind, data in (("candidate", authenticated.baseline_record.model_dump(mode="json")),
                       ("audit", authenticated.baseline_audit.model_dump(mode="json")),
                       ("stage", authenticated.record.model_dump(mode="json")),
                       ("checkpoint", checkpoint), ("initial_scope", scope)):
        path = run / f"resumed_baseline_{kind}.json"
        write_new_json(path, data)
        proof[f"frozen_{kind}_path"] = str(path)
        proof[f"frozen_{kind}_sha256"] = hash_file(path)
        artifacts.append({"path": str(path), "sha256": hash_file(path)})
    return proof, artifacts


def _scientific_frozen_inputs(proof, run, old, baseline):
    """Frozen navigation files cannot replace the original source authentication."""
    from .scientific_revision_audit import build_scientific_revision_scope

    frozen = {}
    for kind in ("candidate", "audit", "stage", "checkpoint", "initial_scope"):
        path = Path(proof[f"frozen_{kind}_path"]).resolve(strict=True)
        if path.parent != run.resolve() or hash_file(path) != proof[f"frozen_{kind}_sha256"]:
            raise ValueError("frozen staged continuation artifact changed or escaped its run")
        frozen[kind] = _read(path)
    staged = ExtractionRecord.model_validate(frozen["stage"])
    provenance = proof.get("seed_provenance", {"kind": "failed_revision_stage", "receipt": proof.get("staged_checkpoint")})
    if not isinstance(provenance, dict) or set(provenance) != {"kind", "receipt"}:
        raise ValueError("frozen scientific seed has malformed provenance")
    checkpoint = provenance["receipt"]
    hash_key = {"failed_revision_stage": "stage_content_hash", "completed_continuation": "candidate_content_hash",
                "completed_revision": "candidate_content_hash", "web_preflight_revision": "candidate_content_hash"}.get(provenance["kind"])
    if hash_key is None:
        raise ValueError("frozen scientific seed has unsupported provenance")
    expected = build_scientific_revision_scope(old, staged, baseline, baseline_origin="fresh_model_audit",
        **frozen["initial_scope"]["scope_limits"])
    if (frozen["candidate"] != old.model_dump(mode="json")
            or frozen["audit"] != baseline.model_dump(mode="json")
            or frozen["checkpoint"] != checkpoint
            or digest_json(staged.model_dump(mode="json")) != checkpoint[hash_key]
            or expected is None or expected != frozen["initial_scope"]
            or expected["scope_sha256"] != proof["initial_scope_sha256"]):
        raise ValueError("frozen initial checkpoint differs from its authenticated full-audit baseline")
    return staged


def _additional_audit_inputs(run, revision, receipts):
    """Bind retained prior local audit artifacts; provider provenance is verified at admission."""
    keys = {"candidate_path", "candidate_sha256", "audit_path", "audit_sha256"}
    if not isinstance(receipts, (list, tuple)) or len(receipts) != revision:
        raise ValueError("cumulative scientific review must retain every earlier local audit")
    audits = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict) or set(receipt) != keys:
            raise ValueError("additional audit must bind both exact candidate and audit files")
        for kind in ("candidate", "audit"):
            path = Path(receipt[f"{kind}_path"]).resolve(strict=True)
            if (path != (run / f"{kind}.v{index:02d}.json").resolve()
                    or hash_file(path) != receipt[f"{kind}_sha256"]):
                raise ValueError("additional audit changed or is not the preceding local version")
        audit = ReadingAudit.model_validate(_read(receipt["audit_path"]))
        if audit.review_scope != "scientific_delta" or not audit.review_scope_sha256:
            raise ValueError("additional continuation audits must retain scientific-delta scope")
        audits.append(audit)
    return audits


def prepare_resumed_review(settings, plan, run, candidate, *, revision=0, additional_audits=()):
    from .bounded_revision_audit import build_revision_audit_scope
    from .scientific_revision_audit import POLICY as SCIENTIFIC_POLICY, build_scientific_revision_scope

    proof = plan.get("resumed_full_audit")
    if not proof:
        return None
    old, baseline, bundle = authenticate_baseline(settings, proof, candidate)
    if digest_json(bundle["documents"]) != digest_json(_read(run / "sources.json")["documents"]):
        raise ValueError("resumed source scope changed")
    if proof.get("policy") == SCIENTIFIC_POLICY:
        _scientific_frozen_inputs(proof, run, old, baseline)
        if type(revision) is not int or not 0 <= revision <= 3:
            raise ValueError("scientific continuation revision is outside its bounded audit chain")
        prior = _additional_audit_inputs(run, revision, additional_audits)
        from .audit_execution import select_scientific_scope_limits
        previous_scopes = [_read(run / f"citation_scope.v{i:02d}.json") for i in range(revision)]
        limits = select_scientific_scope_limits(run, plan, previous_scopes=previous_scopes)
        scope = build_scientific_revision_scope(old, candidate, baseline,
            baseline_origin="fresh_model_audit", additional_audits=prior, **limits)
        if scope is None:
            raise ValueError("current cumulative repairs exceed the authenticated local review scope")
        current_path = run / f"candidate.v{revision:02d}.json"
        if ExtractionRecord.model_validate(_read(current_path)) != candidate:
            raise ValueError("current scientific audit candidate differs from its retained version")
        path = run / f"citation_scope.v{revision:02d}.json"
        write_new_json(path, scope)
        receipt = {"scope_path": str(path), "scope_file_sha256": hash_file(path),
            "baseline_candidate_path": proof["frozen_candidate_path"],
            "baseline_candidate_sha256": proof["frozen_candidate_sha256"],
            "baseline_audit_path": proof["frozen_audit_path"],
            "baseline_audit_sha256": proof["frozen_audit_sha256"],
            "current_candidate_path": str(current_path), "current_candidate_sha256": hash_file(current_path),
            "additional_audits": list(additional_audits)}
        return receipt, scope, baseline
    for kind, record in (("candidate", old), ("audit", baseline)):
        path = Path(proof[f"frozen_{kind}_path"]).resolve(strict=True)
        if (path.parent != run.resolve() or hash_file(path) != proof[f"frozen_{kind}_sha256"]
                or _read(path) != record.model_dump(mode="json")):
            raise ValueError("resumed frozen audit copy changed")
    scope = build_revision_audit_scope(old, candidate, baseline, baseline_origin="fresh_model_audit",
                                       source_requirement_policy=proof.get("source_requirement_policy"))
    if scope is None or scope["scope_sha256"] != proof["scope_sha256"]:
        raise ValueError("resumed revision scope no longer matches frozen plan")
    path = run / "citation_scope.v00.json"
    write_new_json(path, scope)
    receipt = {"scope_path": str(path), "scope_file_sha256": hash_file(path),
               "baseline_candidate_path": proof["frozen_candidate_path"],
               "baseline_candidate_sha256": proof["frozen_candidate_sha256"],
               "baseline_audit_path": proof["frozen_audit_path"],
               "baseline_audit_sha256": proof["frozen_audit_sha256"]}
    return receipt, scope, baseline


def verify_resumed_review(settings, run, record, old, baseline, receipt):
    """Admission-time proof; never trust inherited findings solely by their hash."""
    plan = _read(run / "plan.json")
    proof = plan.get("resumed_full_audit")
    if not proof:
        return False
    authenticated, completed, bundle = authenticate_baseline(settings, proof, record)
    if old != authenticated or baseline != completed:
        return False
    if digest_json(bundle["documents"]) != digest_json(_read(run / "sources.json")["documents"]):
        return False
    for kind in ("candidate", "audit"):
        if (receipt[f"baseline_{kind}_path"] != proof[f"frozen_{kind}_path"]
                or receipt[f"baseline_{kind}_sha256"] != proof[f"frozen_{kind}_sha256"]):
            return False
    history = _read(run / "result.json").get("history", [])
    from .scientific_revision_audit import POLICY as SCIENTIFIC_POLICY, build_scientific_revision_scope
    if proof.get("policy") == SCIENTIFIC_POLICY:
        _scientific_frozen_inputs(proof, run, old, baseline)
        current_path = Path(receipt["current_candidate_path"]).resolve(strict=True)
        if not re.fullmatch(r"candidate\.v[0-9]{2}\.json", current_path.name) or current_path.parent != run.resolve():
            return False
        revision = int(current_path.stem[-2:])
        if (revision > 3 or hash_file(current_path) != receipt["current_candidate_sha256"]
                or ExtractionRecord.model_validate(_read(current_path)) != record
                or not history or len(history) <= revision
                or history[0].get("inherited_full_audit") != proof
                or history[revision].get("audit_origin") != "scientific_delta_model_audit"
                or history[revision].get("candidate_sha256") != hash_file(current_path)):
            return False
        prior = _additional_audit_inputs(run, revision, receipt["additional_audits"])
        from .audit_execution import select_scientific_scope_limits
        previous_scopes = [_read(run / f"citation_scope.v{i:02d}.json") for i in range(revision)]
        limits = select_scientific_scope_limits(run, plan, previous_scopes=previous_scopes)
        scope_path = Path(receipt["scope_path"]).resolve(strict=True)
        if _read(scope_path).get("structural_followup"):
            from .ownership_followup import POLICY, build_followup_scope
            if plan.get("structural_followup_policy") != POLICY or len(prior) != 1:
                return False
            parent_candidate = ExtractionRecord.model_validate(_read(run / "candidate.v00.json"))
            expected = build_followup_scope(old, baseline, parent_candidate, record, prior[0], previous_scopes[0])
        else:
            expected = build_scientific_revision_scope(old, record, baseline,
                baseline_origin="fresh_model_audit", additional_audits=prior, **limits)
        return (scope_path == (run / f"citation_scope.v{revision:02d}.json").resolve()
                and hash_file(scope_path) == receipt["scope_file_sha256"]
                and expected is not None and expected == _read(scope_path))
    return (len(history) == 1 and history[0].get("audit_origin") == "citation_delta_model_audit"
            and history[0].get("inherited_full_audit") == proof
            and history[0].get("candidate_sha256") == hash_file(run / "candidate.json")
            and Path(receipt["scope_path"]).resolve() == (run / "citation_scope.v00.json").resolve())
