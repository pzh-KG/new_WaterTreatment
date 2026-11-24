"""Frozen research scope, independent of source completeness and model truth."""
from __future__ import annotations

from hashlib import sha256
import re

from .model_policy import ACTIVE_MODEL
from .domain import ExtractionRecord
from .storage import safe_name, digest_json


def scientific_prompt(settings, stem, version=None, *, reader_model=None):
    version = safe_name(version or settings.rules_version)
    text = (settings.project_root / "prompts" / f"{stem}.v{version}.txt").read_text(encoding="utf-8")
    if stem == "understand" and version in {"0.5", "0.6"}:
        model = reader_model or settings.codex.model or ACTIVE_MODEL
        text = text.replace("{reader_model}", model.removeprefix("gpt-").replace("-sol", " Sol") + " MAX")
    # Audit/revision prompts are paired with extraction rules by the controller.
    # Keep the shared policy once in each full request, not in every template.
    if version in {"0.7", "0.8", "0.9"} and stem in {"extract", "understand", "verify"}:
        text += "\n\nSHARED SCIENTIFIC POLICY:\n" + (settings.project_root / "prompts" / f"quantitative-policy.v{version}.txt").read_text(encoding="utf-8")
    return text


# Version 0.8 gained explicit controls for duration, specimen attribution and
# panel-specific evidence. These are template revision hashes, independent of
# document, model output or candidate. An unregistered edit remains ineligible.
_FIRST_AUDIT_PROMPT_TRANSITIONS = {
    ("0.8", "understand"): (
        "81c40640e925cefc53cdb1d54e1c5aa02269eb2092b42f1dca40cfa51568b825",
        "7b37731a198a2c3216e486adf646bfce30fc37678e36edd58835adc90d86ff4d"),
    ("0.8", "extract"): (
        "cc1b0ad26648744c8ff66d650fddfeef50dcda9d857476bc6fefa2557f4b4486",
        "e93086c0a7cede2a1ee63c6073463ad75584e007d9c1d140674ded3b47c5f6d5"),
}


def first_audit_prompt_transition(settings, stem, original, reader_model):
    """Record a known clarification before first audit, retaining original stage provenance."""
    active = scientific_prompt(settings, stem, reader_model=reader_model)
    if active == original:
        return None
    hashes = tuple(sha256(text.encode("utf-8")).hexdigest() for text in (original, active))
    if _FIRST_AUDIT_PROMPT_TRANSITIONS.get((settings.rules_version, stem)) != hashes:
        raise ValueError("Scientific instructions changed outside a registered first-audit transition")
    return {"stage": stem, "original_text_sha256": hashes[0], "current_text_sha256": hashes[1],
            "first_audit_uses_current_rules": True}


_POLICY_KEYS = {"version", "mode", "digitization_targets", "allow_new_dense",
                "preserved_observation_ids", "baseline_record_fingerprint"}


def _identifiers(value, name, *, allow_tuple=False):
    allowed = (list, tuple) if allow_tuple else (list,)
    if not isinstance(value, allowed):
        raise ValueError(f"{name} must be a list of unique nonempty string IDs")
    if any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in value):
        raise ValueError(f"{name} must contain only nonempty string IDs without surrounding whitespace")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicate IDs")
    return list(value)


def _policy_structure(policy):
    """Validate shape and local invariants, not a missing baseline's contents."""
    if not isinstance(policy, dict) or set(policy) != _POLICY_KEYS:
        raise ValueError("scope policy must contain exactly the frozen research-scope-v1 fields")
    if policy["version"] != "research-scope-v1":
        raise ValueError("unsupported scope policy version")
    mode = policy["mode"]
    if not isinstance(mode, str) or mode not in {"research_core", "series_digitization"}:
        raise ValueError("unknown research extraction scope")
    targets = _identifiers(policy["digitization_targets"], "digitization_targets")
    preserved = _identifiers(policy["preserved_observation_ids"], "preserved_observation_ids")
    if type(policy["allow_new_dense"]) is not bool:
        raise ValueError("allow_new_dense must be a boolean, not a coerced number or string")
    if ((mode == "research_core" and (targets or policy["allow_new_dense"]))
            or (mode == "series_digitization" and not targets)):
        raise ValueError("digitization requires explicit unique target series; core scope has no targets")
    fingerprint = policy["baseline_record_fingerprint"]
    if fingerprint is None:
        if preserved or mode != "research_core":
            raise ValueError("a fresh scope without a baseline must be core with no preserved observations")
    elif not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
        raise ValueError("baseline_record_fingerprint must be a lowercase SHA-256 string or null")


def frozen_scope(mode="research_core", targets=(), baseline=None, *, allow_new_dense=None):
    """Build a scope from typed inputs without converting strings to authority."""
    if not isinstance(mode, str) or mode not in {"research_core", "series_digitization"}:
        raise ValueError("unknown research extraction scope")
    targets = _identifiers(targets, "digitization_targets", allow_tuple=True)
    if allow_new_dense is not None and type(allow_new_dense) is not bool:
        raise ValueError("allow_new_dense must be a boolean or null")
    if baseline is not None and not isinstance(baseline, ExtractionRecord):
        raise ValueError("scope baseline must be an ExtractionRecord or null")
    policy = {"version": "research-scope-v1", "mode": mode, "digitization_targets": targets,
              "allow_new_dense": mode == "series_digitization" if allow_new_dense is None else allow_new_dense,
              "preserved_observation_ids": [o.id for o in baseline.observations] if baseline is not None else [],
              "baseline_record_fingerprint": digest_json(baseline.model_dump(mode="json")) if baseline is not None else None}
    _policy_structure(policy)
    if mode == "series_digitization":
        available = {s.id for s in baseline.quantitative_coverage if s.data_kind == "time_series"
                     and s.scope != "external_benchmark" and s.state != "out_of_scope"} if baseline is not None else set()
        if not set(targets) <= available:
            raise ValueError("digitization targets must be existing time-series IDs in the selected record")
    return policy


def validate_scope_baseline(policy, baseline):
    """Return None on exact binding; raise ValueError on malformed/mismatched policy.

    Admission must supply the actual frozen baseline (or None for a fresh
    extraction). Structural policy validation alone cannot prove retained IDs.
    An existing baseline with zero observations still has its real content hash.
    """
    _policy_structure(policy)
    expected = frozen_scope(policy["mode"], policy["digitization_targets"], baseline,
                            allow_new_dense=policy["allow_new_dense"])
    if policy != expected:
        raise ValueError("scope policy does not exactly bind the frozen baseline record")


def scope_plan_issues(candidate, plan):
    from .validation import ValidationIssue
    issues = []
    def error(code, path, message):
        issues.append(ValidationIssue(severity="error", code=code, path=path, message=message))
    if not isinstance(plan, dict) or not isinstance(candidate, ExtractionRecord):
        error("SCOPE_PLAN_REQUIRED", "extraction_scope", "Scope checks require a typed candidate and a controller-owned plan.")
        return issues
    if plan.get("rule_version") not in {"0.7", "0.8", "0.9"}:
        return []
    policy = plan.get("extraction_policy")
    try:
        _policy_structure(policy)
    except ValueError as exc:
        error("SCOPE_PLAN_REQUIRED", "extraction_scope", str(exc))
        return issues
    if candidate.extraction_scope != policy["mode"] or candidate.digitization_targets != policy.get("digitization_targets"):
        error("SCOPE_PLAN_MISMATCH", "extraction_scope", "The model changed the frozen extraction/digitization scope.")
    preserved = set(policy["preserved_observation_ids"])
    if not preserved <= {o.id for o in candidate.observations}:
        error("SCOPE_ARCHIVE_LOSS", "observations", "Existing observations must not be deleted to reduce workload or clear errors.")
    for index, observation in enumerate(candidate.observations):
        if observation.analysis_role != "dense_series_point" or observation.id in preserved:
            continue
        if not policy.get("allow_new_dense", False) or policy["mode"] == "research_core" or observation.series_id not in policy["digitization_targets"]:
            error("UNREQUESTED_DENSE_POINT", f"observations[{index}].analysis_role",
                  "New dense interior points require a separately requested matching digitization series.")
    return issues
