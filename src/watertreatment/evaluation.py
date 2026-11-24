"""Auditable small-gold-set evaluation, independent of the extraction domain model.

Counts describe the supplied reviewed scope, not accuracy on an unreviewed corpus.
Exact source-location overlap is deliberately not called semantic evidence support.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
import math
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

FieldStatus = Literal[
    "reported", "not_reported", "not_applicable", "not_obtained", "unread",
    "read_not_quantified", "ambiguous", "source_conflict", "inferred", "rejected",
]
NON_GOLD_STATUSES = frozenset({"not_obtained", "unread", "read_not_quantified"})
DEVELOPMENT_ROLES = frozenset({"development", "calibration", "expanded_calibration"})
REQUIRED_VERSIONS = frozenset({"rules", "taxonomy", "schema", "prompt", "model", "parser", "code"})


class ReviewKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    paper_id: str = Field(min_length=1)
    material_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    time_window: str = Field(min_length=1)
    stream_id: str | None = None
    catalyst_id: str | None = None
    oxidant_id: str | None = None


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)


class ReviewItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: ReviewKey
    status: FieldStatus
    value: str | int | float | None = None
    unit: str | None = None
    evidence: tuple[ReviewEvidence, ...] = ()
    status_reason: str | None = None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: str | int | float | None) -> str | int | float | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite numeric values are not reviewable")
        return value

    @model_validator(mode="after")
    def value_status_consistent(self) -> ReviewItem:
        if self.status == "reported" and self.value is None:
            raise ValueError("Reported scalar observations require a value")
        if self.status not in {"reported", "inferred", "source_conflict", "ambiguous"}:
            if self.value is not None or self.unit is not None:
                raise ValueError("A missing/unread/rejected item cannot assert a scalar value or unit")
        return self


def item_fingerprint(item: ReviewItem) -> str:
    payload = json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


class ReviewJudgment(BaseModel):
    """A supplied adjudication; this module does not generate semantic judgments."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: ReviewKey
    aspect: Literal["value_unit", "evidence_support"]
    verdict: Literal["accepted", "rejected", "uncertain"]
    reviewer_kind: Literal["human", "model_judge"]
    reviewer_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    recorded_at: datetime
    expected_fingerprint: str = Field(min_length=1)
    actual_fingerprint: str = Field(min_length=1)
    judge_version: str | None = None

    @model_validator(mode="after")
    def judge_is_identified(self) -> ReviewJudgment:
        if self.reviewer_kind == "model_judge" and not self.judge_version:
            raise ValueError("Model judgments require an explicit model/prompt judge_version")
        return self

    @classmethod
    def for_items(cls, expected: ReviewItem, actual: ReviewItem, **fields: object) -> ReviewJudgment:
        if expected.key != actual.key:
            raise ValueError("A semantic value judgment cannot repair a different experimental binding")
        return cls(
            key=expected.key, expected_fingerprint=item_fingerprint(expected),
            actual_fingerprint=item_fingerprint(actual), **fields,
        )


class ReviewScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    paper_id: str = Field(min_length=1)
    coverage: Literal["selected_items", "complete_paper"] = "selected_items"


class RoleEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["heldout", "development", "calibration", "expanded_calibration", "background"]
    recorded_at: datetime
    reason: str = Field(min_length=1)
    used_for_rule_revision: bool = False
    exposure: Literal["none", "metadata", "abstract", "fulltext"] = "fulltext"


class PaperRoleHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    paper_id: str = Field(min_length=1)
    events: tuple[RoleEvent, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def chronological(self) -> PaperRoleHistory:
        try:
            if any(a.recorded_at > b.recorded_at for a, b in zip(self.events, self.events[1:])):
                raise ValueError("Role events must be in chronological order")
        except TypeError as exc:
            raise ValueError("Role timestamps must consistently include or omit timezone") from exc
        return self

    @property
    def independent_eligible(self) -> bool:
        return self.events[-1].role == "heldout" and not any(
            e.role in DEVELOPMENT_ROLES or e.used_for_rule_revision or
            (e.role == "background" and e.exposure == "fulltext") for e in self.events
        )


class MetricCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tp: int = Field(default=0, ge=0)
    fp: int = Field(default=0, ge=0)
    fn: int = Field(default=0, ge=0)

    @computed_field
    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @computed_field
    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @computed_field
    @property
    def f1(self) -> float | None:
        denominator = 2 * self.tp + self.fp + self.fn
        return 2 * self.tp / denominator if denominator else None


class PaperEvaluation(BaseModel):
    paper_id: str
    scope: Literal["selected_items", "complete_paper"]
    metrics: dict[str, MetricCounts]
    expected_status_counts: dict[str, int]
    actual_status_counts: dict[str, int]
    missing_keys: tuple[ReviewKey, ...]
    extra_keys: tuple[ReviewKey, ...]
    unscored_actual_keys: tuple[ReviewKey, ...]
    unreviewed_gold_keys: tuple[ReviewKey, ...]
    judgments: tuple[ReviewJudgment, ...]
    judgment_counts: dict[str, dict[str, int]]
    role_history: PaperRoleHistory | None = None
    role_independent_eligible: bool = False


class EvaluationReport(BaseModel):
    evaluation_version: str = "0.1"
    per_paper: dict[str, PaperEvaluation]
    counts: dict[str, MetricCounts]
    versions: dict[str, str]
    missing_versions: tuple[str, ...]
    eligible_for_independent_summary: bool
    warning: str = (
        "Metrics describe only the supplied reviewed scope. They do not estimate accuracy on "
        "unreviewed papers. Exact evidence-location overlap does not establish semantic support. "
        "Model-judge agreement is not human verification; empty denominators are undefined."
    )


def _counts(expected: set[object], actual: set[object]) -> MetricCounts:
    return MetricCounts(tp=len(expected & actual), fp=len(actual - expected), fn=len(expected - actual))


def _index(items: Sequence[ReviewItem], name: str) -> dict[ReviewKey, ReviewItem]:
    result: dict[ReviewKey, ReviewItem] = {}
    for item in items:
        if item.key in result:
            raise ValueError(f"Duplicate {name} key: {item.key.model_dump()}")
        result[item.key] = item
    return result


def _scalar(item: ReviewItem) -> tuple[str, str | None]:
    # Intentionally strict: no unrecorded unit conversion, rounding or semantic inference.
    return json.dumps(item.value, ensure_ascii=False, allow_nan=False), item.unit


def compare_reviewed_items(
    expected: Sequence[ReviewItem], actual: Sequence[ReviewItem], *,
    semantic_judgments: Sequence[ReviewJudgment] = (),
    paper_roles: Sequence[PaperRoleHistory] = (),
    paper_scopes: Sequence[ReviewScope] = (),
    versions: Mapping[str, str] | None = None,
) -> EvaluationReport:
    """Compare a human-reviewed gold set without credit for incorrect bindings.

    Default scope is selected_items: unexpected keys are unscored, NOT false
    positives or true positives. Declare complete_paper only when reverse review
    has established an exhaustive target inventory for that paper. No text
    similarity or model judgment is automatically treated as human adjudication.
    """
    exp, act = _index(expected, "expected"), _index(actual, "actual")
    roles = {r.paper_id: r for r in paper_roles}
    scopes = {s.paper_id: s for s in paper_scopes}
    if len(roles) != len(paper_roles) or len(scopes) != len(paper_scopes):
        raise ValueError("Duplicate paper role history or scope")
    judgments: dict[tuple[ReviewKey, str, str], ReviewJudgment] = {}
    for judgment in semantic_judgments:
        key = judgment.key
        if key not in exp or key not in act:
            raise ValueError("Judgment requires an expected and actual item with identical binding")
        if (judgment.expected_fingerprint != item_fingerprint(exp[key]) or
                judgment.actual_fingerprint != item_fingerprint(act[key])):
            raise ValueError("Stale judgment fingerprints: review the changed items again")
        if exp[key].status in NON_GOLD_STATUSES:
            raise ValueError("Unread or unquantified gold cannot be semantically adjudicated here")
        if judgment.aspect == "value_unit" and (
            exp[key].status != "reported" or act[key].status != "reported"
        ):
            raise ValueError("Value equivalence judgments require two reported observations")
        if judgment.aspect == "evidence_support" and not act[key].evidence:
            raise ValueError("Evidence-support judgments require actual evidence references")
        jkey = (key, judgment.aspect, judgment.reviewer_kind)
        if jkey in judgments:
            raise ValueError("Conflicting/duplicate judgments require explicit prior adjudication")
        judgments[jkey] = judgment

    paper_ids = sorted({k.paper_id for k in exp} | {k.paper_id for k in act} | set(scopes))
    reports: dict[str, PaperEvaluation] = {}
    for paper_id in paper_ids:
        scope = scopes.get(paper_id, ReviewScope(paper_id=paper_id)).coverage
        all_exp = {k: v for k, v in exp.items() if k.paper_id == paper_id}
        all_act = {k: v for k, v in act.items() if k.paper_id == paper_id}
        e = {k: v for k, v in all_exp.items() if v.status not in NON_GOLD_STATUSES}
        a = {k: v for k, v in all_act.items() if (
            k in e or (scope == "complete_paper" and k not in all_exp)
        )}
        ekeys, akeys = set(e), set(a)
        ev = {k: v for k, v in e.items() if v.status == "reported"}
        av = {k: v for k, v in a.items() if v.status == "reported"}
        metrics = {
            "material_identity": _counts({k.material_id for k in e}, {k.material_id for k in a}),
            "target_identity": _counts({k.target_id for k in e}, {k.target_id for k in a}),
            "experimental_binding": _counts(ekeys, akeys),
            "field_status": _counts({(k, v.status) for k, v in e.items()}, {(k, v.status) for k, v in a.items()}),
            "value_unit_exact": _counts({(k, _scalar(v)) for k, v in ev.items()}, {(k, _scalar(v)) for k, v in av.items()}),
        }
        exact = metrics["value_unit_exact"]
        semantic = MetricCounts(tp=exact.tp, fp=exact.fp, fn=exact.fn)
        paper_judgments = [j for j in judgments.values() if j.key.paper_id == paper_id]
        judge_counts: dict[str, dict[str, int]] = {}
        for judgment in paper_judgments:
            group = f"{judgment.reviewer_kind}:{judgment.aspect}"
            bucket = judge_counts.setdefault(group, {"accepted": 0, "rejected": 0, "uncertain": 0})
            bucket[judgment.verdict] += 1
            if judgment.reviewer_kind == "human" and judgment.aspect == "value_unit":
                was_exact = _scalar(ev[judgment.key]) == _scalar(av[judgment.key])
                if judgment.verdict == "accepted" and not was_exact:
                    semantic.tp += 1
                    semantic.fp -= 1
                    semantic.fn -= 1
                elif judgment.verdict == "rejected" and was_exact:
                    semantic.tp -= 1
                    semantic.fp += 1
                    semantic.fn += 1
        metrics["value_unit_human_semantic"] = semantic
        evidence_counts = MetricCounts()
        for key in ekeys | akeys:
            ee = set(e[key].evidence) if key in e else set()
            ae = set(a[key].evidence) if key in a else set()
            if ee and ae and ee & ae:
                evidence_counts.tp += 1
            else:
                evidence_counts.fn += bool(ee)
                evidence_counts.fp += bool(ae)
        metrics["evidence_locator_overlap"] = evidence_counts
        reports[paper_id] = PaperEvaluation(
            paper_id=paper_id, scope=scope, metrics=metrics,
            expected_status_counts=dict(Counter(v.status for v in all_exp.values())),
            actual_status_counts=dict(Counter(v.status for v in all_act.values())),
            missing_keys=tuple(k for k in e if k not in a),
            extra_keys=tuple(k for k in a if k not in e),
            unscored_actual_keys=tuple(k for k in all_act if k not in a),
            unreviewed_gold_keys=tuple(k for k in all_exp if k not in e),
            judgments=tuple(paper_judgments), judgment_counts=judge_counts,
            role_history=roles.get(paper_id),
            role_independent_eligible=bool(roles.get(paper_id) and roles[paper_id].independent_eligible),
        )
    totals: dict[str, MetricCounts] = {}
    for report in reports.values():
        for name, metric in report.metrics.items():
            total = totals.setdefault(name, MetricCounts())
            total.tp += metric.tp
            total.fp += metric.fp
            total.fn += metric.fn
    frozen_versions = dict(versions or {})
    missing = tuple(sorted(k for k in REQUIRED_VERSIONS if not frozen_versions.get(k, "").strip()))
    scored_papers = [r for r in reports.values() if (
        r.metrics["experimental_binding"].tp + r.metrics["experimental_binding"].fp +
        r.metrics["experimental_binding"].fn
    )]
    has_reviewed_gold = any(v.status not in NON_GOLD_STATUSES for v in exp.values())
    return EvaluationReport(
        per_paper=reports, counts=totals, versions=frozen_versions, missing_versions=missing,
        eligible_for_independent_summary=has_reviewed_gold and bool(scored_papers) and not missing and all(
            r.role_independent_eligible for r in scored_papers
        ),
    )
