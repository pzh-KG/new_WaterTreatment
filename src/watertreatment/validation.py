"""Offline referential and scientific-consistency checks, not truth assessment."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel

from .domain import (
    FINAL_ROLES, Configuration, CoverageState, Experiment, ExperimentPurpose,
    ExtractionRecord, Fact, FactStatus, JointLevel, MaterialRole,
    InputSource, Observation, ObservationOrigin, PairingLevel, StreamKind, StrictModel,
)
from .quantitative import (
    COORDINATE_SERIES_KINDS, NUMERIC_SERIES_KINDS, has_numeric_value,
    has_quantitative_value, has_source_numeric_point, has_usable_coordinates, coordinate_value_key,
)


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(StrictModel):
    severity: Severity
    code: str
    path: str
    message: str


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


def _walk(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name), f"{path}.{name}" if path else name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _known(fact: Fact) -> Any:
    if fact.status not in {FactStatus.REPORTED, FactStatus.EXPLICIT_ABSENCE}:
        return None
    return fact.normalized if fact.normalized is not None else fact.value


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _different(left: Fact, right: Fact) -> bool:
    a, b = _known(left), _known(right)
    if a is None or b is None:
        return False
    return a != b


def _comparable_units(left: Fact, right: Fact) -> bool:
    a, b = _known(left), _known(right)
    if a is not None and b is not None:
        return a == b
    return left.status == right.status == FactStatus.NOT_APPLICABLE


def _named_differences(left: list[Any], right: list[Any], prefix: str) -> list[str]:
    # Only compare common named fields with the same reported unit/basis.
    # Different units are not evidence of different physical conditions.
    other = {item.name: item for item in right}
    result = []
    for item in left:
        match = other.get(item.name)
        if match is None:
            continue
        lb, rb = _known(item.basis), _known(match.basis)
        if _comparable_units(item.unit, match.unit) and lb == rb and _different(item.fact, match.fact):
            result.append(f"{prefix}.{item.name}")
    return result


def _condition_differences(left: Experiment, right: Experiment,
                           configurations: dict[str, Configuration]) -> list[str]:
    differences = []
    if left.material_ids and right.material_ids and set(left.material_ids) != set(right.material_ids):
        differences.append("material_ids")
    if left.mixture_type.value != "unknown" and right.mixture_type.value != "unknown" and left.mixture_type != right.mixture_type:
        differences.append("mixture_type")
    right_states = {item.material_id: item.state for item in right.material_states}
    for item in left.material_states:
        if item.material_id in right_states and _different(item.state, right_states[item.material_id]):
            differences.append(f"material_state:{item.material_id}")
    if left.targets and right.targets:
        lt = {(x.substance_id, x.role): x for x in left.targets}
        rt = {(x.substance_id, x.role): x for x in right.targets}
        if set(lt) != set(rt):
            differences.append("target_identity")
        for key in set(lt) & set(rt):
            a, b = lt[key], rt[key]
            if _comparable_units(a.unit, b.unit) and _known(a.basis) == _known(b.basis):
                if _different(a.concentration, b.concentration):
                    differences.append(f"target_concentration:{key}")
    li = {(x.role, x.substance_id): x for x in left.reaction_inputs}
    ri = {(x.role, x.substance_id): x for x in right.reaction_inputs}
    if li and ri and set(li) != set(ri):
        differences.append("reaction_input_identity")
    for key in set(li) & set(ri):
        a, b = li[key], ri[key]
        known_supplies = {InputSource.EXTERNAL, InputSource.MATERIAL_BOUND, InputSource.PRELOADED, InputSource.IN_SITU, InputSource.EXPLICIT_NONE}
        if a.source in known_supplies and b.source in known_supplies and a.source != b.source:
            differences.append(f"reaction_input_supply:{key[0]}:{key[1]}")
        if _comparable_units(a.unit, b.unit) and _known(a.basis) == _known(b.basis):
            if _different(a.amount, b.amount):
                differences.append(f"reaction_input_amount:{key[0]}:{key[1]}")
    differences += _named_differences(left.conditions, right.conditions, "condition")
    re = {item.kind: item for item in right.energy_inputs}
    for item in left.energy_inputs:
        if item.kind in re:
            differences += _named_differences(item.parameters, re[item.kind].parameters,
                                             f"energy.{item.kind}")
    lc = configurations.get(left.configuration_id or "")
    rc = configurations.get(right.configuration_id or "")
    if lc is not None and rc is not None:
        if lc.material_ids and rc.material_ids and set(lc.material_ids) != set(rc.material_ids):
            differences.append("configuration.material_ids")
        if _different(lc.boundary, rc.boundary):
            differences.append("configuration.boundary")
        differences += _named_differences(lc.parameters, rc.parameters, "configuration")
    return differences


def validate_record(record: ExtractionRecord, source_documents: Iterable[Any] = ()) -> list[ValidationIssue]:
    """Return field-scoped issues without deleting data or calling external services.

    Source documents may be SourceDocument instances or their JSON dictionaries.
    Omitting them performs internal reference checks only, never claims source
    verification. Warnings do not invalidate an entire paper.
    """
    from .research_scope import scope_issues
    issues: list[ValidationIssue] = scope_issues(record)

    def issue(code: str, path: str, message: str, severity: Severity = Severity.ERROR) -> None:
        issues.append(ValidationIssue(severity=severity, code=code, path=path, message=message))

    entities: dict[str, dict[str, Any]] = {}
    global_ids = {record.paper.id}
    for group in ("materials", "substances", "configurations", "experiments", "observations",
                  "evidence", "comparisons", "quantitative_coverage"):
        indexed = {}
        for i, item in enumerate(getattr(record, group)):
            if item.id in global_ids:
                issue("DUPLICATE_ID", f"{group}[{i}].id", f"ID {item.id!r} is not unique in this record.")
            global_ids.add(item.id)
            indexed[item.id] = item
        entities[group] = indexed

    def reference(identifier: str | None, group: str, path: str) -> None:
        if identifier is not None and identifier not in entities[group]:
            issue("MISSING_REFERENCE", path, f"Unknown {group} ID {identifier!r}.")

    evidence_ids = set(entities["evidence"])
    no_value_states = {FactStatus.NOT_REPORTED, FactStatus.NOT_AVAILABLE, FactStatus.UNREAD,
                       FactStatus.READ_UNQUANTIFIED, FactStatus.NOT_APPLICABLE}
    for path, value in _walk(record):
        if isinstance(value, BaseModel) and "evidence_ids" in type(value).model_fields:
            for j, identifier in enumerate(value.evidence_ids):
                if identifier not in evidence_ids:
                    issue("MISSING_EVIDENCE", f"{path}.evidence_ids[{j}]",
                          f"Unknown evidence ID {identifier!r}.")
        if not isinstance(value, Fact):
            continue
        if value.status in no_value_states and (value.value is not None or value.normalized is not None):
            issue("VALUE_STATE_CONFLICT", path, "An unavailable/unread/unquantified fact cannot contain a quantified value.")
        if value.status == FactStatus.REPORTED:
            if value.value is None and value.raw is None:
                issue("EMPTY_REPORTED_FACT", path, "Reported fact has no value or source wording.", Severity.WARNING)
            if not value.evidence_ids:
                issue("UNSOURCED_REPORTED_FACT", path, "Reported fact has no field-level evidence reference.", Severity.WARNING)
        if value.status == FactStatus.ESTIMATED:
            if value.value is None:
                issue("EMPTY_ESTIMATED_FACT", path, "An estimate needs a value; use read_unquantified when no value can be read.", Severity.WARNING)
            if not value.evidence_ids:
                issue("UNSOURCED_ESTIMATE", path, "An estimate needs field-level source evidence.", Severity.WARNING)
        if value.status == FactStatus.CONFLICT and len(value.alternatives) < 2:
            issue("CONFLICT_SIDES_MISSING", path, "Preserve the conflicting alternatives and their evidence.", Severity.WARNING)
        if value.normalized is not None and value.normalized != value.value and not value.normalization_reason:
            issue("NORMALIZATION_REASON_MISSING", path, "Changed normalized value lacks an explanation.", Severity.WARNING)

    documents = list(source_documents)
    from .design_scope import design_source_index_issues
    issues.extend(design_source_index_issues(record, documents))
    sources: dict[str, Any] = {}
    blocks: dict[tuple[str, str], Any] = {}
    for document in documents:
        source = _get(document, "source", document)
        sid = _get(source, "source_id", _get(source, "id"))
        if sid is not None:
            if sid in sources:
                issue("DUPLICATE_SOURCE_ID", "source_documents", f"Source ID {sid!r} was supplied more than once.")
            sources[sid] = source
        for block in _get(document, "blocks", []):
            bid = _get(block, "block_id", _get(block, "id"))
            block_sid = _get(block, "source_id", sid)
            if block_sid != sid:
                issue("SOURCE_BLOCK_OWNER_MISMATCH", "source_documents", "A source block declares another source owner.")
            if (block_sid, bid) in blocks:
                issue("DUPLICATE_SOURCE_BLOCK", "source_documents", f"Block {bid!r} is duplicated within its source.")
            blocks[(block_sid, bid)] = block
    paper_sources = set(record.paper.source_ids)
    if documents:
        for sid in sources.keys() - paper_sources:
            issue("SOURCE_INVENTORY_INCOMPLETE", "paper.source_ids",
                  f"Supplied source {sid!r} is absent from the paper inventory.", Severity.WARNING)
        for i, sid in enumerate(record.paper.source_ids):
            if sid not in sources:
                issue("MISSING_SOURCE", f"paper.source_ids[{i}]", f"Source {sid!r} was not supplied.")
    for i, evidence in enumerate(record.evidence):
        path = f"evidence[{i}]"
        if evidence.source_id not in paper_sources:
            issue("SOURCE_NOT_IN_PAPER", path + ".source_id", "Evidence source is absent from this paper's source inventory.")
        if not documents:
            continue
        source = sources.get(evidence.source_id)
        block = blocks.get((evidence.source_id, evidence.block_id))
        if source is None:
            issue("MISSING_SOURCE", path, "Evidence source was not supplied.")
        if block is None:
            issue("MISSING_SOURCE_BLOCK", path + ".block_id", "Evidence block does not exist in the specified source.")
            continue
        location = _get(block, "location", {})
        for field in ("paragraph_index", "table_index", "row_index", "cell_index", "part", "xml_path", "relationship_id"):
            claimed = getattr(evidence.locator, field)
            actual = _get(location, field)
            if claimed is not None and actual is not None and claimed != actual:
                issue("LOCATOR_BLOCK_MISMATCH", path + ".locator." + field,
                      "Locator disagrees with the referenced source block.")
        metadata = _get(block, "metadata", {})
        if _get(metadata, "content_role") == "editorial_comment":
            issue("EDITORIAL_SOURCE_REVIEW", path, "Evidence points to an editorial comment; distinguish it from scientific body text.", Severity.WARNING)
        page = evidence.locator.physical_page
        if page is not None:
            count = _get(source, "page_count")
            if count is not None and page > count:
                issue("PAGE_OUT_OF_RANGE", path + ".locator.physical_page", "Physical page exceeds source page count.")
            actual = _get(_get(block, "location", {}), "physical_page")
            if actual is not None and actual != page:
                issue("LOCATOR_BLOCK_MISMATCH", path + ".locator.physical_page", "Physical page disagrees with the referenced source block.")
            if str(_get(source, "format", "")).lower().lstrip(".") in {"docx", "doc"}:
                issue("WORD_PHYSICAL_PAGE_UNVERIFIED", path + ".locator.physical_page",
                      "Native Word locations do not establish physical pages without a linked rendered source.", Severity.WARNING)
        text = _get(block, "text", "") or ""
        if evidence.quote and text and _text(evidence.quote) not in _text(text):
            issue("QUOTE_NOT_FOUND", path + ".quote", "Quotation is not found in the block text; check the original visual/source representation.", Severity.WARNING)

    materials = entities["materials"]
    if not any(m.role in FINAL_ROLES for m in record.materials):
        issue("NO_FINAL_MATERIAL", "materials", "No confirmed final material is recorded; the paper remains incomplete or outside the intended main analysis.", Severity.WARNING)
    for i, material in enumerate(record.materials):
        for j, component in enumerate(material.components):
            reference(component.substance_id, "substances", f"materials[{i}].components[{j}].substance_id")
        for j, step in enumerate(material.preparation):
            for k, sid in enumerate(step.input_substance_ids):
                reference(sid, "substances", f"materials[{i}].preparation[{j}].input_substance_ids[{k}]")

    stream_config: dict[str, str] = {}
    streams: dict[str, Any] = {}
    for i, config in enumerate(record.configurations):
        for j, mid in enumerate(config.material_ids):
            reference(mid, "materials", f"configurations[{i}].material_ids[{j}]")
        for j, placement in enumerate(config.placements):
            reference(placement.material_id, "materials", f"configurations[{i}].placements[{j}].material_id")
            if placement.material_id not in config.material_ids:
                issue("PLACEMENT_NOT_IN_CONFIGURATION", f"configurations[{i}].placements[{j}]", "Placed material must be listed as used by this configuration.")
        for j, sid in enumerate(config.accessory_substance_ids):
            reference(sid, "substances", f"configurations[{i}].accessory_substance_ids[{j}]")
        for j, stream in enumerate(config.streams):
            if stream.id in streams:
                issue("DUPLICATE_STREAM_ID", f"configurations[{i}].streams[{j}].id", "Stream IDs must be unique in the paper.")
            stream_config[stream.id] = config.id
            streams[stream.id] = stream
    for i, config in enumerate(record.configurations):
        for j, stream in enumerate(config.streams):
            for k, sid in enumerate(stream.upstream_stream_ids):
                if stream_config.get(sid) != config.id:
                    issue("STREAM_REFERENCE_MISMATCH", f"configurations[{i}].streams[{j}].upstream_stream_ids[{k}]", "Upstream stream is absent from the same configuration.")

    def stream_reference(sid: str | None, config_id: str | None, path: str) -> None:
        if sid is not None and (sid not in streams or stream_config.get(sid) != config_id):
            issue("STREAM_REFERENCE_MISMATCH", path, "Stream is not part of this experiment's configuration.")

    experiments = entities["experiments"]
    configs = entities["configurations"]
    observations = entities["observations"]
    for i, experiment in enumerate(record.experiments):
        path = f"experiments[{i}]"
        reference(experiment.configuration_id, "configurations", path + ".configuration_id")
        config = configs.get(experiment.configuration_id)
        for j, mid in enumerate(experiment.material_ids):
            reference(mid, "materials", f"{path}.material_ids[{j}]")
            if config and mid not in config.material_ids:
                issue("EXPERIMENT_CONFIGURATION_MISMATCH", f"{path}.material_ids[{j}]", "Experiment material is not used by its configuration.")
        used_materials = set(experiment.material_ids) | (set(config.material_ids) if config else set())
        for j, state in enumerate(experiment.material_states):
            reference(state.material_id, "materials", f"{path}.material_states[{j}].material_id")
            if state.material_id not in used_materials:
                issue("EXPERIMENT_STATE_MISMATCH", f"{path}.material_states[{j}]", "Working state belongs to a material outside the experiment/configuration.")
        for j, target in enumerate(experiment.targets):
            reference(target.substance_id, "substances", f"{path}.targets[{j}].substance_id")
        for j, reaction in enumerate(experiment.reaction_inputs):
            if reaction.material_id is not None:
                reference(reaction.material_id, "materials", f"{path}.reaction_inputs[{j}].material_id")
            reference(reaction.substance_id, "substances", f"{path}.reaction_inputs[{j}].substance_id")
            stream_reference(reaction.stream_id, experiment.configuration_id, f"{path}.reaction_inputs[{j}].stream_id")
            amount = _known(reaction.amount)
            if reaction.source == InputSource.EXPLICIT_NONE and amount not in (None, 0, False, "0"):
                issue("ABSENT_INPUT_HAS_DOSE", f"{path}.reaction_inputs[{j}]", "An explicitly absent input cannot have a nonzero reported dose.")
        joint = experiment.joint_evidence
        if joint.level in {JointLevel.J1, JointLevel.J2} and not joint.evidence_ids:
            issue("UNSUPPORTED_JOINT_EVIDENCE", path + ".joint_evidence", "J1/J2 requires a located joint-operation source.", Severity.WARNING)
        for j, pair in enumerate(experiment.pairs):
            ppath = f"{path}.pairs[{j}]"
            reference(pair.left_observation_id, "observations", ppath + ".left_observation_id")
            reference(pair.right_observation_id, "observations", ppath + ".right_observation_id")
            if pair.left_observation_id == pair.right_observation_id:
                issue("SELF_PAIR", ppath, "A performance pair must reference two different observations.")
            left, right = observations.get(pair.left_observation_id), observations.get(pair.right_observation_id)
            if left is None or right is None:
                continue
            if experiment.id not in {left.experiment_id, right.experiment_id}:
                issue("PAIR_OWNER_MISMATCH", ppath, "The containing experiment must own at least one observation in the pair.")
            if pair.level not in {PairingLevel.P1, PairingLevel.P2}:
                continue
            if not pair.evidence_ids:
                issue("UNSUPPORTED_PERFORMANCE_PAIR", ppath, "P1/P2 needs evidence of the shared time process or reported condition group.", Severity.WARNING)
            if pair.differing_conditions:
                issue("PAIR_CONDITION_MISMATCH", ppath, "P1/P2 conflicts with declared differing key conditions.")
            le, rexp = experiments.get(left.experiment_id), experiments.get(right.experiment_id)
            if le is not None and rexp is not None:
                different = _condition_differences(le, rexp, configs)
                if left.material_id and right.material_id and left.material_id != right.material_id:
                    shared_config = configs.get(le.configuration_id) if le.configuration_id == rexp.configuration_id else None
                    jointly_used = set(shared_config.material_ids) if shared_config else set()
                    if {left.material_id, right.material_id} <= jointly_used:
                        issue("ASSEMBLY_PAIR_SCOPE", ppath, "Different components share one assembly configuration; the pair describes that system, not either component's intrinsic dual performance.", Severity.WARNING)
                    else:
                        different.append("measured_material")
                if different:
                    issue("PAIR_CONDITION_MISMATCH", ppath, "P1/P2 contradicts reported differences: " + ", ".join(different))
                if le.id != rexp.id:
                    issue("CROSS_EXPERIMENT_PAIR_REVIEW", ppath, "Separate experiment IDs require source confirmation of shared conditions/time process; equal values alone are insufficient.", Severity.WARNING)
            if _comparable_units(left.time_window.unit, right.time_window.unit):
                if _different(left.time_window.end, right.time_window.end):
                    issue("PAIR_TIME_WINDOW_MISMATCH", ppath, "The observations end at different reported times; preserve the distinct windows and restrict exact paired ranking.", Severity.WARNING)

    for i, observation in enumerate(record.observations):
        path = f"observations[{i}]"
        reference(observation.experiment_id, "experiments", path + ".experiment_id")
        reference(observation.material_id, "materials", path + ".material_id")
        reference(observation.substance_id, "substances", path + ".substance_id")
        experiment = experiments.get(observation.experiment_id)
        if experiment:
            config = configs.get(experiment.configuration_id)
            used = set(experiment.material_ids) | (set(config.material_ids) if config else set())
            if observation.material_id is not None and observation.material_id not in used:
                issue("OBSERVATION_MATERIAL_MISMATCH", path + ".material_id", "Measured material is not assigned to this experiment/configuration.")
            stream_reference(observation.stream_id, experiment.configuration_id, path + ".stream_id")
            material = materials.get(observation.material_id)
            if (material and material.role in {MaterialRole.PRECURSOR, MaterialRole.MECHANISM_ONLY}
                    and experiment.purpose == ExperimentPurpose.COUPLED):
                issue("NONFINAL_COUPLED_ATTRIBUTION", path, "Precursor/mechanism-only object cannot own a claimed final coupled-performance observation.")
        if (observation.value.status == FactStatus.INFERRED
                and observation.origin != ObservationOrigin.MODEL_INFERENCE):
            issue("INFERENCE_AS_OBSERVATION", path + ".origin", "Inferred performance must be labeled model_inference, not measured or author-reported.")
        if observation.origin == ObservationOrigin.MODEL_INFERENCE and observation.value.status == FactStatus.REPORTED:
            issue("INFERENCE_AS_REPORTED_FACT", path + ".value", "Model inference cannot have reported fact status.")
        stream = streams.get(observation.stream_id)
        if stream and stream.kind == StreamKind.CONDENSATE and observation.metric_kind in {"mineralization", "degradation"}:
            issue("CONDENSATE_ENDPOINT_REVIEW", path, "Condensate depletion alone does not establish bulk-liquid chemical degradation or mineralization; retain the measured phase and supporting evidence.", Severity.WARNING)
    if not record.observations:
        issue("NO_OBSERVATIONS", "observations", "No performance/structural observations have been recorded yet.", Severity.WARNING)

    for i, entry in enumerate(record.coverage):
        path = f"coverage[{i}]"
        if entry.source_id not in paper_sources:
            issue("SOURCE_NOT_IN_PAPER", path + ".source_id", "Coverage source is absent from the paper inventory.")
        for j, bid in enumerate(entry.block_ids):
            if documents and (entry.source_id, bid) not in blocks:
                issue("MISSING_SOURCE_BLOCK", f"{path}.block_ids[{j}]", "Covered block does not exist in this source.")
        if entry.state == CoverageState.PARSED_UNREAD and (entry.text_read or entry.visual_checked):
            issue("COVERAGE_STATE_CONFLICT", path, "Parsed-unread coverage cannot claim completed reading/visual verification.")
        if entry.state in {CoverageState.READ_STRUCTURED, CoverageState.READ_UNQUANTIFIED} and not (entry.text_read or entry.visual_checked):
            issue("UNCONFIRMED_READING", path, "Read coverage has neither text-reading nor visual-inspection confirmation.", Severity.WARNING)
    for sid in paper_sources - {entry.source_id for entry in record.coverage}:
        issue("SOURCE_COVERAGE_MISSING", "coverage", f"Source {sid!r} has no explicit reading-coverage entry.", Severity.WARNING)
    for i, comparison in enumerate(record.comparisons):
        for j, mid in enumerate(comparison.material_ids):
            reference(mid, "materials", f"comparisons[{i}].material_ids[{j}]")
        for j, oid in enumerate(comparison.observation_ids):
            reference(oid, "observations", f"comparisons[{i}].observation_ids[{j}]")
    for i, extension in enumerate(record.extensions):
        for field, group in (("material_id", "materials"), ("experiment_id", "experiments"), ("observation_id", "observations")):
            reference(getattr(extension, field), group, f"extensions[{i}].{field}")
    _validate_quantitative(record, entities, paper_sources, blocks, bool(documents), issue, reference)
    from .reference_associations import reference_association_issues
    issues.extend(reference_association_issues(record))
    from .missingness import missingness_issues
    issues.extend(missingness_issues(record))
    from .arm_research import check_research_contract
    check_research_contract(record, issue)
    return issues


def _valid_observation_field_path(observation: Observation, path: str) -> bool:
    """Resolve observation-relative fields without treating metadata as a value.

    Both coordinates.0.fact and coordinates[0].fact address an existing item.
    A real field with a null value exists; invented children, out-of-range
    indices and traversal through scalar values do not. This never mutates a
    record or changes its historical serialization.
    """
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|\d+)|\[\d+\])*", path) is None:
        return False
    parts = re.findall(r"[A-Za-z_]\w*|\d+", path)
    if parts[0] in {"evidence_ids", "evidence_links", "id", "series_id"}:
        return False
    current: Any = observation
    for part in parts:
        if isinstance(current, BaseModel):
            if part not in type(current).model_fields:
                return False
            current = getattr(current, part)
        elif isinstance(current, list):
            if not part.isdecimal():
                return False
            try:
                index = int(part)
            except ValueError:
                return False
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _validate_quantitative(record, entities, paper_sources, blocks, has_documents, issue, reference):
    """Check declarations/links; source semantics still require original review."""
    inventory = entities["quantitative_coverage"]
    if not inventory:
        issue("QUANTITATIVE_INVENTORY_MISSING", "quantitative_coverage",
              "No figure/table data inventory; page reading does not establish quantitative completeness.", Severity.WARNING)
    identities = set()
    seen_points = set()
    for i, item in enumerate(record.quantitative_coverage):
        path = f"quantitative_coverage[{i}]"
        if item.source_id not in paper_sources:
            issue("SOURCE_NOT_IN_PAPER", path + ".source_id", "Quantitative inventory source is absent from this paper.")
        block = blocks.get((item.source_id, item.block_id))
        if has_documents and block is None:
            issue("MISSING_SOURCE_BLOCK", path + ".block_id", "Quantitative inventory block does not exist in its source.")
        if block is not None:
            location = _get(block, "location", {})
            for field in ("physical_page", "paragraph_index", "table_index", "row_index", "cell_index", "part", "xml_path", "relationship_id"):
                claimed, actual = getattr(item.locator, field), _get(location, field)
                if claimed is not None and actual is not None and claimed != actual:
                    issue("LOCATOR_BLOCK_MISMATCH", path + ".locator." + field, "Quantitative locator disagrees with its source block.")
        identity = (item.source_id, item.block_id, item.locator.figure, item.locator.panel,
                    item.locator.table_index, item.label.strip().casefold(), item.data_kind)
        if identity in identities:
            issue("DUPLICATE_QUANTITATIVE_SERIES", path, "The same labeled source series appears more than once.")
        identities.add(identity)
        if len(item.observation_ids) != len(set(item.observation_ids)):
            issue("DUPLICATE_SERIES_POINT", path + ".observation_ids", "A series cannot count the same observation twice.")
        for j, oid in enumerate(item.observation_ids):
            reference(oid, "observations", f"{path}.observation_ids[{j}]")
            observation = entities["observations"].get(oid)
            if observation is not None and observation.series_id != item.id:
                issue("SERIES_OWNER_MISMATCH", f"{path}.observation_ids[{j}]", "The observation must name this quantitative series as its owner.")
        linked = [entities["observations"][oid] for oid in set(item.observation_ids) if oid in entities["observations"]]
        count = sum(has_source_numeric_point(obs) for obs in linked)
        if item.expected_points is not None and count > item.expected_points:
            issue("SERIES_COUNT_OVERFLOW", path, "More numeric observations than the declared source point count; check duplicate points or inventory granularity.")
        if item.state == "complete":
            if item.expected_points is None:
                issue("SERIES_COMPLETENESS_UNCOUNTED", path, "Completeness has no source point count; it cannot establish pointwise coverage.", Severity.WARNING)
            elif count != item.expected_points:
                issue("SERIES_COMPLETENESS_CONFLICT", path, "Complete series does not contain the declared number of numeric points.")
            if item.data_kind in NUMERIC_SERIES_KINDS and count == 0:
                issue("SERIES_COMPLETENESS_EMPTY", path, "An empty numeric series does not establish quantitative completeness even when expected_points is zero.", Severity.WARNING)
        elif not (item.reason and item.reason.strip()):
            issue("SERIES_LIMITATION_REASON_MISSING", path + ".reason", "Incomplete or excluded series needs a source-specific reason, not an assumed absence.", Severity.WARNING)
        if item.state in {"unread", "not_available", "unquantified"} and count:
            issue("SERIES_STATE_CONFLICT", path, "This series state contradicts its numeric extracted points; represent partial coverage when appropriate.")
        if item.scope == "external_benchmark" and item.observation_ids:
            issue("EXTERNAL_BENCHMARK_AS_OWN_EXPERIMENT", path, "Cited other-paper benchmarks must not be linked as this paper's experimental observations.")
        if item.state not in {"complete", "out_of_scope"}:
            issue("QUANTITATIVE_SERIES_INCOMPLETE", path, "Series is not quantitatively complete; restrict affected curve/cell comparisons.", Severity.WARNING)

    legacy = record.schema_version == "0.1.0"
    for i, observation in enumerate(record.observations):
        path = f"observations[{i}]"
        value = observation.value
        numeric = has_numeric_value(observation)
        quantified = has_quantitative_value(observation)
        if quantified and observation.origin == ObservationOrigin.PLOT_ESTIMATE and value.status != FactStatus.ESTIMATED:
            issue("PLOT_ESTIMATE_STATUS", path + ".value.status",
                  "Plot-read value must remain estimated, not author-reported; preserve legacy source and revise as a new version.",
                  Severity.WARNING if legacy else Severity.ERROR)
        if value.status == FactStatus.ESTIMATED:
            if observation.origin == ObservationOrigin.LOCAL_CALCULATION:
                # The arithmetic can be exact while its inputs are approximate.
                # Keep both its derivation and the precision of its inputs queryable.
                has_inputs = any(f.value is not None and f.evidence_ids for f in (
                    observation.numerator, observation.denominator, *(c.fact for c in observation.coordinates)))
                cited_raw = bool(value.raw and value.evidence_ids)
                if not ((has_inputs or cited_raw) and (value.note or observation.metric.note or value.normalization_reason)):
                    issue("LOCAL_ESTIMATE_BASIS_MISSING", path + ".value",
                          "An estimated local calculation needs a sourced operand and an explanation of the derivation.")
            elif observation.origin not in {ObservationOrigin.PLOT_ESTIMATE, ObservationOrigin.AUTHOR_SIMULATION}:
                issue("ESTIMATE_ORIGIN_CONFLICT", path + ".origin", "The estimated status needs a graph reading, an estimated simulation reading, or a source-bound local calculation.")
        if observation.series_id is not None:
            reference(observation.series_id, "quantitative_coverage", path + ".series_id")
            owner = inventory.get(observation.series_id)
            if owner is not None and observation.id not in owner.observation_ids:
                issue("SERIES_OWNER_MISMATCH", path + ".series_id", "Series ownership must agree in both directions.")
            if (has_source_numeric_point(observation) and owner is not None and owner.data_kind in COORDINATE_SERIES_KINDS
                    and not has_usable_coordinates(observation, owner.data_kind, observation_window=record.rule_version == "0.9")):
                issue("SERIES_POINT_COORDINATE_MISSING", path + ".coordinates", "This series point lacks available time/cycle/condition/cell coordinate values; preserve the original axis and restrict comparison if it is unclear. An operating day can index a time series. Count it as a cycle only when the source documents a repeated operation.", Severity.WARNING)
        names = [coordinate.name.strip().casefold() for coordinate in observation.coordinates]
        if len(names) != len(set(names)):
            issue("DUPLICATE_POINT_COORDINATE", path + ".coordinates", "Repeated coordinate names make a series point ambiguous.")
        if observation.series_id and observation.coordinates:
            signature = tuple(sorted((coordinate.name.strip().casefold(), coordinate_value_key(coordinate.fact.value),
                                      repr(coordinate.unit.value), repr(coordinate.basis.value))
                                     for coordinate in observation.coordinates))
            key = (observation.series_id, observation.metric_kind, repr(observation.metric.value),
                   observation.material_id, observation.substance_id, observation.stream_id, signature)
            if record.rule_version == "0.9":
                key = (observation.experiment_id, *key)
            if key in seen_points:
                issue("DUPLICATE_SERIES_COORDINATES", path + ".coordinates", "Duplicate point coordinates in one measured series; distinguish replicates or remove a duplicate in a new version.")
            seen_points.add(key)
        direct_value = set()
        for j, link in enumerate(observation.evidence_links):
            lpath = f"{path}.evidence_links[{j}]"
            if link.evidence_id not in entities["evidence"]:
                issue("MISSING_EVIDENCE", lpath + ".evidence_id", "Unknown evidence in field-specific support link.")
            for k, field in enumerate(link.supported_fields):
                if not _valid_observation_field_path(observation, field):
                    issue("UNKNOWN_SUPPORTED_FIELD", f"{lpath}.supported_fields[{k}]", "Support link must name an actual scientific observation field.")
                if field in {"value", "value.value"} and link.support_type == "direct":
                    direct_value.add(link.evidence_id)
                    if link.evidence_id not in value.evidence_ids:
                        issue("VALUE_EVIDENCE_LINK_MISMATCH", lpath, "Direct value support must also appear in value.evidence_ids.")
        if quantified and value.evidence_ids and not set(value.evidence_ids) <= direct_value:
            issue("VALUE_EVIDENCE_NOT_FIELD_LINKED", path + ".value.evidence_ids",
                  "Each value citation needs an explicit direct value-support link; table/context support does not automatically support this number or sample.", Severity.WARNING)
        # Only canonical metric kinds have sign semantics here. Never guess
        # signs from free-text names or convert a curve to absolute values.
        if numeric and observation.metric_kind in {"mass_loss", "evaporation_mass_loss"} and value.value < 0:
            issue("MASS_LOSS_SIGN_REVIEW", path + ".value", "Negative signed mass change is not positive mass loss; preserve raw sign and define any normalization explicitly.", Severity.WARNING)
