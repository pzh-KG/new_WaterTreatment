"""Versioned scientific records, independent of parsing, providers and graph storage.

Missing values are states, not zeroes.  Every scientific value can retain its
source wording and evidence.  Relationships are explicit rather than inferred
from a material name or a coincidentally similar number.
"""
from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_serializer

SCHEMA_VERSION = "0.5.0"
RULE_VERSION = "0.9"
DESIGN_VERSIONS = {"0.8": "0.4.0", RULE_VERSION: SCHEMA_VERSION}
Scalar: TypeAlias = str | int | float | bool
FactValue: TypeAlias = Scalar | list[Scalar] | None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class ResearchFields(StrictModel):
    """Elide absent additive fields from historical scientific fingerprints."""
    additive_fields: ClassVar[tuple[str, ...]] = ()

    @model_serializer(mode="wrap")
    def serialize_research_fields(self, handler):
        result = handler(self)
        for key in self.additive_fields:
            if result.get(key) in (None, []):
                result.pop(key, None)
        return result


class FactStatus(str, Enum):
    REPORTED = "reported"
    ESTIMATED = "estimated"
    NOT_REPORTED = "not_reported"
    NOT_AVAILABLE = "not_available"
    UNREAD = "unread"
    READ_UNQUANTIFIED = "read_unquantified"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    INFERRED = "inferred"
    EXPLICIT_ABSENCE = "explicit_absence"
    NOT_APPLICABLE = "not_applicable"


class FactAlternative(StrictModel):
    value: FactValue = None
    raw: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Fact(StrictModel):
    value: FactValue = None
    status: FactStatus = FactStatus.UNREAD
    evidence_ids: list[str] = Field(default_factory=list)
    raw: str | None = None
    normalized: FactValue = None
    normalization_reason: str | None = None
    alternatives: list[FactAlternative] = Field(default_factory=list)
    note: str | None = None


class NamedFact(StrictModel):
    name: str
    fact: Fact = Field(default_factory=Fact)
    unit: Fact = Field(default_factory=Fact)
    basis: Fact = Field(default_factory=Fact)


class EvidenceLocator(StrictModel):
    physical_page: int | None = Field(default=None, ge=1)
    printed_page: str | None = None
    paragraph_index: int | None = Field(default=None, ge=1)
    table_index: int | None = Field(default=None, ge=1)
    row_index: int | None = Field(default=None, ge=1)
    cell_index: int | None = Field(default=None, ge=1)
    part: str | None = None
    xml_path: str | None = None
    relationship_id: str | None = None
    figure: str | None = None
    panel: str | None = None
    section: str | None = None
    description: str | None = None


class EvidenceRef(StrictModel):
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    quote: str | None = None
    locator: EvidenceLocator = Field(default_factory=EvidenceLocator)


class CoverageState(str, Enum):
    PARSED_UNREAD = "parsed_unread"
    READ_STRUCTURED = "read_structured"
    READ_UNQUANTIFIED = "read_unquantified"
    UNAVAILABLE = "unavailable"
    UNREADABLE = "unreadable"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    OUT_OF_SCOPE = "out_of_scope"


class CoverageEntry(StrictModel):
    source_id: str
    block_ids: list[str] = Field(default_factory=list)
    state: CoverageState = CoverageState.PARSED_UNREAD
    text_read: bool = False
    visual_checked: bool = False
    note: str | None = None


class DatasetRole(str, Enum):
    DEVELOPMENT = "development"
    EXPANDED_CALIBRATION = "expanded_calibration"
    VALIDATION_RESERVED = "validation_reserved"
    VALIDATION = "validation"
    BACKGROUND = "background"
    UNSPECIFIED = "unspecified"


class RoleHistory(StrictModel):
    role: DatasetRole
    date: str
    reason: str


class Paper(StrictModel):
    id: str = Field(min_length=1)
    title: Fact = Field(default_factory=Fact)
    doi: Fact = Field(default_factory=Fact)
    article_type: Fact = Field(default_factory=Fact)
    publication_year: Fact = Field(default_factory=Fact)
    source_ids: list[str] = Field(default_factory=list)
    dataset_role: DatasetRole = DatasetRole.UNSPECIFIED
    role_history: list[RoleHistory] = Field(default_factory=list)


class MaterialRole(str, Enum):
    PRIMARY_FINAL = "primary_final"
    FINAL_COMPONENT = "final_component"
    OPTIMIZED_VARIANT = "optimized_variant"
    NECESSARY_CONTROL = "necessary_control"
    ACTIVE_COMPONENT = "active_component"
    PRECURSOR = "precursor"
    ACCESSORY = "accessory"
    MECHANISM_ONLY = "mechanism_only"
    UNRESOLVED = "unresolved"


FINAL_ROLES = frozenset({MaterialRole.PRIMARY_FINAL, MaterialRole.FINAL_COMPONENT,
                         MaterialRole.OPTIMIZED_VARIANT})


class Retention(str, Enum):
    RETAINED = "retained"
    PRELOADED_CONSUMABLE = "preloaded_consumable"
    REMOVED_OR_TRANSFORMED = "removed_or_transformed"
    ANALYTICAL_ONLY = "analytical_only"
    UNKNOWN = "unknown"


class Classification(StrictModel):
    # Open vocabulary permits all material families and future dictionary versions.
    axis: str
    label: Fact = Field(default_factory=Fact)
    dictionary_version: str | None = None


class CompositionMeasurement(StrictModel):
    basis: Literal["bulk_retained", "areal_loading", "local_composition", "nominal_recipe"]
    quantity: NamedFact
    method: Fact = Field(default_factory=Fact)
    sampled_region: Fact = Field(default_factory=Fact)
    evidence_ids: list[str] = Field(min_length=1)


class Component(ResearchFields):
    additive_fields = ("measurements",)
    measurements: list[CompositionMeasurement] = Field(default_factory=list)
    substance_id: str
    roles: list[Fact] = Field(default_factory=list)
    retention: Retention = Retention.UNKNOWN
    amount: Fact = Field(default_factory=Fact)
    unit: Fact = Field(default_factory=Fact)
    basis: Fact = Field(default_factory=Fact)
    position: Fact = Field(default_factory=Fact)
    evidence_ids: list[str] = Field(default_factory=list)


class PreparationStep(StrictModel):
    order: int = Field(ge=1)
    operation: Fact = Field(default_factory=Fact)
    input_substance_ids: list[str] = Field(default_factory=list)
    parameters: list[NamedFact] = Field(default_factory=list)


class MaterialSample(StrictModel):
    id: str = Field(min_length=1)
    name: Fact = Field(default_factory=Fact)
    family_id: str | None = None
    role: MaterialRole = MaterialRole.UNRESOLVED
    aliases: list[str] = Field(default_factory=list)
    classification: list[Classification] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    state: Fact = Field(default_factory=Fact)
    morphology: list[Fact] = Field(default_factory=list)
    selected_for: list[Fact] = Field(default_factory=list)
    preparation: list[PreparationStep] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Substance(StrictModel):
    id: str = Field(min_length=1)
    name: Fact = Field(default_factory=Fact)
    original_names: list[str] = Field(default_factory=list)
    formula: Fact = Field(default_factory=Fact)
    salt_or_isomer: Fact = Field(default_factory=Fact)
    identity_scope: Fact = Field(default_factory=Fact)
    evidence_ids: list[str] = Field(default_factory=list)


class MaterialPlacement(StrictModel):
    material_id: str
    position: Fact = Field(default_factory=Fact)
    function: Fact = Field(default_factory=Fact)


class StreamKind(str, Enum):
    FEED = "feed"
    REACTION_LIQUID = "reaction_liquid"
    TREATED_OUTLET = "treated_outlet"
    CONCENTRATE = "concentrate"
    CONDENSATE = "condensate"
    ESCAPED_VAPOR = "escaped_vapor"
    RECIRCULATION = "recirculation"
    BYPASS = "bypass"
    OTHER = "other"
    UNKNOWN = "unknown"


class Stream(StrictModel):
    id: str
    kind: StreamKind = StreamKind.UNKNOWN
    description: Fact = Field(default_factory=Fact)
    upstream_stream_ids: list[str] = Field(default_factory=list)


class Configuration(StrictModel):
    id: str = Field(min_length=1)
    material_ids: list[str] = Field(default_factory=list)
    placements: list[MaterialPlacement] = Field(default_factory=list)
    accessory_substance_ids: list[str] = Field(default_factory=list)
    boundary: Fact = Field(default_factory=Fact)
    parameters: list[NamedFact] = Field(default_factory=list)
    streams: list[Stream] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ExperimentPurpose(str, Enum):
    COUPLED = "coupled"
    EVAPORATION = "evaporation"
    CATALYSIS = "catalysis"
    STABILITY = "stability"
    CHARACTERIZATION = "characterization"
    MECHANISM = "mechanism"
    UNRESOLVED = "unresolved"


class MixtureType(str, Enum):
    SINGLE_TARGET = "single_target"
    MIXTURE = "mixture"
    REAL_WATER_SPIKED = "real_water_spiked"
    REAL_WATER = "real_water"
    UNKNOWN = "unknown"


class Target(StrictModel):
    substance_id: str
    concentration: Fact = Field(default_factory=Fact)
    unit: Fact = Field(default_factory=Fact)
    basis: Fact = Field(default_factory=Fact)
    role: str = "target"


class InputSource(str, Enum):
    EXTERNAL = "external"
    MATERIAL_BOUND = "material_bound"
    PRELOADED = "preloaded"
    IN_SITU = "in_situ"
    EXPLICIT_NONE = "explicit_none"
    NOT_REPORTED = "not_reported"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class ReactionInput(ResearchFields):
    additive_fields = ("addition_evidence_ids", "absence_scope", "material_id")
    material_id: str | None = Field(default=None, min_length=1)
    absence_scope: Literal["external_addition", "all_routes", "unspecified"] | None = None
    # For a distinct administration during operation, even if the same species
    # is also retained in the material. Preparation evidence alone is insufficient.
    addition_evidence_ids: list[str] = Field(default_factory=list)
    substance_id: str | None = None
    role: str
    source: InputSource = InputSource.UNKNOWN
    amount: Fact = Field(default_factory=Fact)
    unit: Fact = Field(default_factory=Fact)
    basis: Fact = Field(default_factory=Fact)
    timing: Fact = Field(default_factory=Fact)
    stream_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EnergyInput(StrictModel):
    kind: str
    source: Fact = Field(default_factory=Fact)
    parameters: list[NamedFact] = Field(default_factory=list)


class TimeWindow(StrictModel):
    start: Fact = Field(default_factory=Fact)
    end: Fact = Field(default_factory=Fact)
    unit: Fact = Field(default_factory=Fact)
    reference: Fact = Field(default_factory=Fact)
    kind: str = "unspecified"


class JointLevel(str, Enum):
    J1 = "J1"
    J2 = "J2"
    J3 = "J3"
    J4 = "J4"
    J5 = "J5"
    UNKNOWN = "J?"


class PairingLevel(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    UNKNOWN = "P?"


class JointEvidence(StrictModel):
    level: JointLevel = JointLevel.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ObservationPair(StrictModel):
    left_observation_id: str
    right_observation_id: str
    level: PairingLevel = PairingLevel.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    matched_conditions: list[str] = Field(default_factory=list)
    unresolved_conditions: list[str] = Field(default_factory=list)
    differing_conditions: list[str] = Field(default_factory=list)
    note: str | None = None


class MaterialState(StrictModel):
    material_id: str
    state: Fact = Field(default_factory=Fact)


class Experiment(ResearchFields):
    additive_fields = ("arm_kind", "comparison_group")
    arm_kind: Literal["treatment", "control", "characterization", "summary", "unresolved"] | None = None
    comparison_group: str | None = Field(default=None, min_length=1)
    id: str = Field(min_length=1)
    material_ids: list[str] = Field(default_factory=list)
    material_states: list[MaterialState] = Field(default_factory=list)
    configuration_id: str | None = None
    purpose: ExperimentPurpose = ExperimentPurpose.UNRESOLVED
    reported_group: Fact = Field(default_factory=Fact)
    physical_run_id: Fact = Field(default_factory=Fact)
    flow_mode: Fact = Field(default_factory=Fact)
    mixture_type: MixtureType = MixtureType.UNKNOWN
    targets: list[Target] = Field(default_factory=list)
    reaction_inputs: list[ReactionInput] = Field(default_factory=list)
    energy_inputs: list[EnergyInput] = Field(default_factory=list)
    conditions: list[NamedFact] = Field(default_factory=list)
    time_window: TimeWindow = Field(default_factory=TimeWindow)
    joint_evidence: JointEvidence = Field(default_factory=JointEvidence)
    pairs: list[ObservationPair] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ObservationOrigin(str, Enum):
    REPORTED_MEASUREMENT = "reported_measurement"
    AUTHOR_CALCULATION = "author_calculation"
    AUTHOR_SIMULATION = "author_simulation"
    AUTHOR_HYPOTHESIS = "author_hypothesis"
    PLOT_ESTIMATE = "plot_estimate"
    LOCAL_CALCULATION = "local_calculation"
    MODEL_INFERENCE = "model_inference"
    UNRESOLVED = "unresolved"


class ObservationEvidenceLink(StrictModel):
    """A field-specific support assertion, not a scientific verification stamp."""
    evidence_id: str = Field(min_length=1)
    supported_fields: list[str] = Field(min_length=1)
    support_type: Literal["direct", "context", "conflict"] = "direct"
    explanation: str = Field(min_length=1)


class Observation(StrictModel):
    id: str = Field(min_length=1)
    experiment_id: str
    material_id: str | None = None
    substance_id: str | None = None
    stream_id: str | None = None
    measured_object: Fact = Field(default_factory=Fact)
    metric: Fact = Field(default_factory=Fact)
    metric_kind: str = "unspecified"
    endpoint_kind: Literal["evaporation_flux", "condensate_yield", "liquid_parent_removal",
                           "liquid_parent_ratio", "liquid_parent_concentration", "condensate_rejection",
                           "condensate_transfer", "mineralization", "other", "unresolved"] | None = None
    removal_attribution: Literal["degradation_supported", "parent_disappearance_only", "mixed_or_unresolved",
                                 "adsorption", "transfer", "not_applicable"] | None = None
    value: Fact = Field(default_factory=Fact)
    unit: Fact = Field(default_factory=Fact)
    uncertainty: Fact = Field(default_factory=Fact)
    origin: ObservationOrigin = ObservationOrigin.UNRESOLVED
    time_window: TimeWindow = Field(default_factory=TimeWindow)
    numerator: Fact = Field(default_factory=Fact)
    denominator: Fact = Field(default_factory=Fact)
    reference: Fact = Field(default_factory=Fact)
    analytical_method: Fact = Field(default_factory=Fact)
    detection_limit: Fact = Field(default_factory=Fact)
    volume_correction: Fact = Field(default_factory=Fact)
    importance: str = "core"
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    series_id: str | None = None
    coordinates: list[NamedFact] = Field(default_factory=list)
    evidence_links: list[ObservationEvidenceLink] = Field(default_factory=list)
    # Extraction-purpose classification, not an author claim or confidence score.
    analysis_role: Literal["key_result", "condition_comparison", "stability", "design_control",
                           "mechanistic_support", "dense_series_point", "unclassified"] = "unclassified"
    role_reason: str | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_serialization(self, handler):
        # Old immutable snapshots did not contain these keys. Do not change
        # their fingerprints merely by loading them through the newer schema.
        result = handler(self)
        for name in ("series_id", "coordinates", "evidence_links", "endpoint_kind", "removal_attribution"):
            if result.get(name) in (None, []):
                result.pop(name, None)
        if result.get("analysis_role") == "unclassified":
            result.pop("analysis_role", None)
        if result.get("role_reason") is None:
            result.pop("role_reason", None)
        return result


class QuantitativeCoverage(StrictModel):
    """A figure/table series inventory independent of page-reading coverage.

    expected_points is a source-inspected count, not a target invented by the
    extractor. A complete declaration is still an auditable model assertion.
    """
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    label: str = Field(min_length=1)
    data_kind: Literal["time_series", "cycle_series", "condition_series", "table_cells", "reaction_network", "scalar", "other"] = "other"
    scope: Literal["final_material", "necessary_control", "mechanism", "external_benchmark", "other"] = "final_material"
    state: Literal["complete", "partial", "unquantified", "unread", "unreadable", "not_available", "out_of_scope"] = "unread"
    expected_points: int | None = Field(default=None, ge=0)
    observation_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class DesignOutcomeContrast(StrictModel):
    baseline_observation_id: str = Field(min_length=1)
    modified_observation_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class DesignComparison(StrictModel):
    id: str
    material_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    operation: Fact = Field(default_factory=Fact)
    held_conditions: list[NamedFact] = Field(default_factory=list)
    changed_conditions: list[NamedFact] = Field(default_factory=list)
    outcomes: list[NamedFact] = Field(default_factory=list)
    causal_status: str = "unresolved"
    limitations: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    structure_changes: list[NamedFact] = Field(default_factory=list)
    author_interpretation: Fact | None = None
    counterevidence: list[Fact] = Field(default_factory=list)
    confounders: list[Fact] = Field(default_factory=list)
    contrasts: list[DesignOutcomeContrast] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def preserve_legacy_serialization(self, handler):
        result = handler(self)
        for name in ("structure_changes", "author_interpretation", "counterevidence", "confounders", "contrasts"):
            if result.get(name) in (None, []):
                result.pop(name, None)
        return result


class ReferenceAssociation(StrictModel):
    """An evidenced reuse of existing results, never another measurement.

    A reported reference relationship does not establish identical physical
    runs, matched experimental conditions, paired performance or replicates.
    Those assertions remain owned by their respective scientific records.
    """
    id: str = Field(min_length=1)
    target_kind: Literal["experiment", "series", "comparison"]
    target_id: str = Field(min_length=1)
    observation_ids: list[str] = Field(min_length=1)
    relationship: Literal["shared_control", "shared_reference", "contextual_comparison"]
    applicability: Literal["reported", "inferred", "ambiguous", "conflict"]
    evidence_ids: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    matched_conditions: list[str] = Field(default_factory=list)
    differing_conditions: list[str] = Field(default_factory=list)
    unresolved_conditions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExtensionModule(StrictModel):
    """Named, versioned modules with typed fields, never arbitrary JSON maps.

    Module vocabulary can grow without changing the six core entities. M1--M6
    identify the electrical/flow/mixture/transformation/assembly/document modules.
    Scientific field values still use Fact and therefore retain evidence/status.
    """
    module: str
    version: str
    material_id: str | None = None
    experiment_id: str | None = None
    observation_id: str | None = None
    fields: list[NamedFact] = Field(default_factory=list)


TypedExtension = ExtensionModule


CoreResearchArea = Literal["paper_scope", "material_identity", "preparation_structure",
    "treatment_mapping", "paired_performance", "stability", "design_comparisons", "evidence_eligibility"]
ResearchPurpose = Literal["performance", "condition_endpoint", "stability", "material_feature",
    "design_comparison", "mechanism_claim", "adverse_result", "application_summary", "process_parameter", "requested_detail"]
ResearchModuleKind = Literal["preloaded_consumable", "electrical_assistance", "flow_multistream",
    "mixed_pollutants", "selective_conversion", "dispersed_assembly", "outdoor_application",
    "mechanistic_detail", "other"]
DeferredTopic = Literal["dense_time_series", "spectral_peak_catalog", "intermediate_network",
    "toxicity_matrix", "simulation_grid", "outdoor_time_series", "other"]
ResearchUse = Literal["material_retrieval", "condition_comparison", "performance_pairing",
    "composition_features", "design_hypothesis"]


class CoreResearchCheck(StrictModel):
    """A reading/checking disposition, never a completeness or truth certificate."""
    area: CoreResearchArea
    state: Literal["checked", "unread", "not_available", "not_applicable"]
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResearchObservationAssignment(StrictModel):
    observation_id: str = Field(min_length=1)
    layer: Literal["core", "module", "detail"]
    purpose: ResearchPurpose
    reason: str = Field(min_length=1)
    module_id: str | None = None


class ResearchModuleActivation(StrictModel):
    id: str = Field(min_length=1)
    kind: ResearchModuleKind
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    material_ids: list[str] = Field(default_factory=list)
    experiment_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResearchSourceIndex(StrictModel):
    """Source-level understanding and deliberate deferred quantification.

    Indexing is independent of both page-reading proof and scientific review.
    A deferred topic is not an assertion that the source did not report data.
    """
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    # A registered source that could not yield any blocks still needs an honest
    # availability disposition. It must not acquire an invented block locator.
    block_id: str | None = Field(default=None, min_length=1)
    locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    content_types: list[str] = Field(min_length=1)
    reading_state: Literal["read", "unread", "unreadable", "not_available"]
    summary: str = Field(min_length=1)
    material_relation: str = Field(min_length=1)
    retained_observation_ids: list[str] = Field(default_factory=list)
    quantitative_coverage_ids: list[str] = Field(default_factory=list)
    deferred_topics: list[DeferredTopic] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)


class ResearchEligibility(StrictModel):
    """Explicit use limitations, not automatic acceptance into a ranking."""
    use: ResearchUse
    target_kind: Literal["paper", "material", "configuration", "experiment", "observation", "comparison"]
    target_id: str = Field(min_length=1)
    status: Literal["eligible", "limited", "not_assessed", "not_applicable"]
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResearchDesignScope(StrictModel):
    """Rule 0.8 extraction contract: A core, B triggered modules, C indexed detail."""
    version: Literal["design-scope-v1"] = "design-scope-v1"
    core_checks: list[CoreResearchCheck] = Field(default_factory=list)
    observation_assignments: list[ResearchObservationAssignment] = Field(default_factory=list)
    modules: list[ResearchModuleActivation] = Field(default_factory=list)
    source_index: list[ResearchSourceIndex] = Field(default_factory=list)
    eligibility: list[ResearchEligibility] = Field(default_factory=list)


class ExtractionRecord(StrictModel):
    # Keep the original omission semantics for frozen old audit prompts that
    # serialized with exclude_defaults=True. New runs set current versions
    # explicitly from their frozen plan rather than changing legacy defaults.
    schema_version: str = "0.1.0"
    rule_version: str = "0.5"
    paper: Paper
    materials: list[MaterialSample] = Field(default_factory=list)
    substances: list[Substance] = Field(default_factory=list)
    configurations: list[Configuration] = Field(default_factory=list)
    experiments: list[Experiment] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    comparisons: list[DesignComparison] = Field(default_factory=list)
    extensions: list[ExtensionModule] = Field(default_factory=list)
    quantitative_coverage: list[QuantitativeCoverage] = Field(default_factory=list)
    extraction_scope: Literal["research_core", "series_digitization"] | None = None
    digitization_targets: list[str] = Field(default_factory=list)
    design_scope: ResearchDesignScope | None = None
    reference_associations: list[ReferenceAssociation] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def preserve_legacy_serialization(self, handler):
        result = handler(self)
        if result.get("quantitative_coverage") == []:
            result.pop("quantitative_coverage", None)
        if result.get("extraction_scope") is None:
            result.pop("extraction_scope", None)
        if not result.get("digitization_targets"):
            result.pop("digitization_targets", None)
        if result.get("design_scope") is None:
            result.pop("design_scope", None)
        if not result.get("reference_associations"):
            result.pop("reference_associations", None)
        return result
