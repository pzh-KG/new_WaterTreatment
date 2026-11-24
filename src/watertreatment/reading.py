"""Public reading notes and evidence audits, never private reasoning traces."""
from __future__ import annotations

from typing import Literal, get_args
from pydantic import Field, model_serializer, model_validator
from .domain import CoverageEntry, StrictModel


class SourceAnchor(StrictModel):
    source_id: str
    block_id: str
    quote: str | None = None
    figure_or_table: str | None = None


class MaterialMapEntry(StrictModel):
    name: str
    aliases: list[str]
    role: str
    composition_and_structure: str
    distinction_from_other_samples: str
    evidence: list[SourceAnchor]


class ExperimentMapEntry(StrictModel):
    label: str
    final_material_names: list[str]
    purpose: str
    conditions_and_streams: str
    pollutant_catalyst_oxidant: str
    reported_metrics: list[str]
    simultaneous_operation_and_pairing: str
    evidence: list[SourceAnchor]


class ReadingScopeEntry(StrictModel):
    """Public source-anchored scope decisions before numeric extraction."""
    layer: Literal["core", "module", "detail"]
    subject: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    planned_action: str = Field(min_length=1)
    evidence: list[SourceAnchor] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PrintedEndpoint(StrictModel):
    """One literal, consequential author-printed observation flagged by the reader."""
    owner_and_conditions: str = Field(min_length=1)
    metric_and_stream: str = Field(min_length=1)
    number: float = Field(allow_inf_nan=False)
    unit_and_qualifier: str = Field(min_length=1)
    evidence: SourceAnchor


class ReadingDossier(StrictModel):
    paper_id: str
    article_question_and_design: str
    material_map: list[MaterialMapEntry]
    experiment_map: list[ExperimentMapEntry]
    coverage: list[CoverageEntry]
    cross_source_conflicts: list[str]
    unresolved_questions: list[str]
    schema_gaps: list[str]
    scope_outline: list[ReadingScopeEntry] = Field(default_factory=list)

    printed_endpoints: list[PrintedEndpoint] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def preserve_legacy_serialization(self, handler):
        result = handler(self)
        if not result.get("printed_endpoints"):
            result.pop("printed_endpoints", None)
        if not result.get("scope_outline"):
            result.pop("scope_outline", None)
        return result


class AuditFinding(StrictModel):
    record_path: str
    record_paths: list[str] = Field(default_factory=list, description="Exact resolvable record paths, one per entry, e.g. experiments[2].targets[0].concentration. No prose, wildcards or abbreviated index lists. record_path remains the human-readable label.")
    omission_indices: list[int] = Field(default_factory=list, description="Zero-based indices of this audit's potential_omissions explicitly located and explained by this blocking finding; never guess a scope for an unlocated omission.")
    severity: Literal["critical", "major", "minor"]
    verdict: Literal["supported", "unsupported", "unresolved", "not_checked"]
    needs_revision: bool
    explanation: str
    evidence: list[SourceAnchor]

    @model_serializer(mode="wrap")
    def preserve_legacy_finding(self, handler):
        result = handler(self)
        for field in ("record_paths", "omission_indices"):
            if not result.get(field):
                result.pop(field, None)
        return result


AffectedQueryType = Literal[
    "pointwise_comparison", "kinetic_recalculation", "condition_comparison",
    "cycle_comparison", "storage_comparison", "recalculation",
]


class QuantitativeFollowup(StrictModel):
    """Optional additional points in a faithfully scoped partial series.

    This cannot override unsupported claims or other blocking audit findings.
    Scope is checked against the candidate and actual source inventory.
    """
    kind: Literal["additional_visible_points"] = "additional_visible_points"
    coverage_ids: list[str] = Field(min_length=1)
    evidence: list[SourceAnchor] = Field(min_length=1)
    affected_query_types: list[AffectedQueryType] = Field(min_length=1)
    recommended_action: str = Field(min_length=1)


AuditDimension = Literal[
    "final_material_identity", "experiment_binding", "pollutant_catalyst_oxidant",
    "values_units_and_denominators", "joint_operation_and_pairing",
    "stability", "main_si_conflicts", "omissions_and_coverage", "evidence_locations",
]
AUDIT_DIMENSIONS = get_args(AuditDimension)


class ReadingAudit(StrictModel):
    findings: list[AuditFinding]
    potential_omissions: list[str] = Field(description="Blocking major/correctness omissions only. Return an empty list when none; positive no-omission findings belong in findings or dimension_notes, never in this list.")
    coverage_limitations: list[str]
    checked_dimensions: list[AuditDimension]
    dimension_notes: list[str] = Field(default_factory=list)
    quantitative_followups: list[QuantitativeFollowup] = Field(default_factory=list)
    human_validated: Literal[False] = False
    review_scope: Literal["full", "citation_delta", "scientific_delta"] = "full"
    review_scope_sha256: str | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_audit_serialization(self, handler):
        result = handler(self)
        if result.get("quantitative_followups") == []:
            result.pop("quantitative_followups", None)
        if result.get("review_scope") == "full":
            result.pop("review_scope", None)
        if result.get("review_scope_sha256") is None:
            result.pop("review_scope_sha256", None)
        return result

    @model_validator(mode="before")
    @classmethod
    def retain_legacy_dimension_explanations(cls, value):
        """Read existing audits without losing their public explanations.

        Only an explicitly named, exact known identifier followed by whitespace
        or a colon is separated. No scientific finding or status is changed, and
        the old artifact remains immutable. New model schemas allow enums only.
        """
        if not isinstance(value, dict):
            return value
        result = dict(value)
        dimensions, notes = [], list(result.get("dimension_notes", []))
        for item in result.get("checked_dimensions", []):
            match = next((key for key in AUDIT_DIMENSIONS if isinstance(item, str)
                          and item.startswith(key) and len(item) > len(key)
                          and (item[len(key)].isspace() or item[len(key)] == ":")), None)
            dimensions.append(match or item)
            if match:
                notes.append(item)
        result["checked_dimensions"] = dimensions
        result["dimension_notes"] = notes
        return result
