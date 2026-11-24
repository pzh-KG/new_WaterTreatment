"""Structural safeguards for the goal-directed extraction contract.

The decisions remain source-reviewable assertions.  These checks cannot decide
whether a figure contains an overlooked adverse result, nor establish accuracy.
They do ensure that default extraction cannot quietly turn into full digitization
and that purposeful exclusions have an accessible, source-bound record.
"""
from __future__ import annotations

from .domain import DESIGN_VERSIONS

from collections import Counter
from collections.abc import Mapping

from .domain import ExtractionRecord

DESIGN_RULE_VERSION = "0.8"
DESIGN_SCHEMA_VERSION = "0.4.0"
CORE_AREAS = frozenset({"paper_scope", "material_identity", "preparation_structure", "treatment_mapping",
    "paired_performance", "stability", "design_comparisons", "evidence_eligibility"})
RESEARCH_USES = frozenset({"material_retrieval", "condition_comparison", "performance_pairing",
    "composition_features", "design_hypothesis"})


def _source_level_limitation(entry):
    """No block can be claimed for a source that yielded no readable structure."""
    return (entry.reading_state in {"unreadable", "not_available"}
        and any(limit.strip() for limit in entry.limitations)
        and not entry.retained_observation_ids and not entry.quantitative_coverage_ids
        and not entry.deferred_topics
        and not any(value is not None for value in entry.locator.model_dump().values()))


def design_scope_issues(record: ExtractionRecord):
    from .research_scope import protected_observation_ids
    from .validation import ValidationIssue

    issues = []

    def error(code, path, message):
        issues.append(ValidationIssue(severity="error", code=code, path=path, message=message))

    def nonblank(text, path):
        if not text.strip():
            error("DESIGN_EMPTY_REASON", path, "An explicit nonblank research explanation is required.")

    if record.rule_version not in DESIGN_VERSIONS:
        if record.design_scope is not None:
            error("DESIGN_SCOPE_VERSION", "design_scope", "Three-layer extraction requires a supported rule/schema pair.")
        return issues
    if record.schema_version != DESIGN_VERSIONS[record.rule_version]:
        error("DESIGN_SCOPE_VERSION", "schema_version", f"Rule {record.rule_version} requires schema {DESIGN_VERSIONS[record.rule_version]}.")
    scope = record.design_scope
    if scope is None:
        error("DESIGN_SCOPE_REQUIRED", "design_scope", "A three-layer extraction contract is required, independently of frontend filtering.")
        return issues

    area_counts = Counter(check.area for check in scope.core_checks)
    for area in CORE_AREAS:
        if area_counts[area] != 1:
            error("DESIGN_CORE_CHECK_REQUIRED", "design_scope.core_checks", f"Core area {area!r} must have exactly one checking disposition; missing facts are not invented.")
    for i, check in enumerate(scope.core_checks):
        nonblank(check.reason, f"design_scope.core_checks[{i}].reason")
        if check.state in {"unread", "not_available"} and not check.limitations:
            error("DESIGN_CORE_LIMITATION_REQUIRED", f"design_scope.core_checks[{i}].limitations", "Unread or unavailable core content needs an explicit affected-use limitation.")

    entities = {
        "paper": {record.paper.id}, "material": {m.id for m in record.materials},
        "configuration": {c.id for c in record.configurations}, "experiment": {e.id for e in record.experiments},
        "observation": {o.id for o in record.observations}, "comparison": {c.id for c in record.comparisons},
    }
    evidence_ids = {e.id for e in record.evidence}
    observations = {o.id: o for o in record.observations}
    modules = {m.id: m for m in scope.modules}
    coverage = {q.id: q for q in record.quantitative_coverage}
    source_ids = set(record.paper.source_ids)

    def references(values, available, path):
        for j, value in enumerate(values):
            if value not in available:
                error("DESIGN_MISSING_REFERENCE", f"{path}[{j}]", f"Unknown scoped reference {value!r}.")
        if len(set(values)) != len(values):
            error("DESIGN_DUPLICATE_REFERENCE", path, "Repeated IDs are not distinct research items.")

    def unique(items, path):
        counts = Counter(item.id for item in items)
        if any(count > 1 for count in counts.values()):
            error("DESIGN_DUPLICATE_ID", path, "Scoped identifiers must be unique within their collection.")

    unique(scope.modules, "design_scope.modules")
    unique(scope.source_index, "design_scope.source_index")
    for i, module in enumerate(scope.modules):
        path = f"design_scope.modules[{i}]"
        nonblank(module.reason, path + ".reason")
        references(module.evidence_ids, evidence_ids, path + ".evidence_ids")
        references(module.material_ids, entities["material"], path + ".material_ids")
        references(module.experiment_ids, entities["experiment"], path + ".experiment_ids")
        if not module.evidence_ids:
            error("DESIGN_MODULE_EVIDENCE_REQUIRED", path + ".evidence_ids", "Activate a module only with evidence that this study actually involves it.")
        if not module.material_ids and not module.experiment_ids:
            error("DESIGN_MODULE_OWNER_REQUIRED", path, "Bind a triggered module to its actual material or experiment.")

    counts = Counter(a.observation_id for a in scope.observation_assignments)
    for oid in observations:
        if counts[oid] != 1:
            error("DESIGN_ASSIGNMENT_REQUIRED", "design_scope.observation_assignments", f"Observation {oid!r} needs exactly one explicit layer and research purpose.")
    protected = protected_observation_ids(record)
    for i, assignment in enumerate(scope.observation_assignments):
        path = f"design_scope.observation_assignments[{i}]"
        nonblank(assignment.reason, path + ".reason")
        observation = observations.get(assignment.observation_id)
        if observation is None:
            error("DESIGN_MISSING_REFERENCE", path + ".observation_id", "A scope assignment cannot invent an observation.")
            continue
        if assignment.module_id is not None and assignment.module_id not in modules:
            error("DESIGN_MISSING_REFERENCE", path + ".module_id", "Unknown activated module.")
        if assignment.layer == "module":
            module = modules.get(assignment.module_id)
            if module is None:
                error("DESIGN_MODULE_REQUIRED", path + ".module_id", "B-layer results need a source-grounded activated module.")
            elif (observation.experiment_id not in module.experiment_ids
                  and observation.material_id not in module.material_ids):
                error("DESIGN_MODULE_OWNER_MISMATCH", path, "A B-layer result must belong to the material or experiment that activated its module.")
        elif assignment.layer == "core" and assignment.module_id is not None:
            error("DESIGN_CORE_MODULE_CONFLICT", path + ".module_id", "Core assignment does not require a module; use a B assignment for a module-specific result.")
        if assignment.layer == "detail":
            if record.extraction_scope != "series_digitization" or observation.series_id not in record.digitization_targets:
                error("DESIGN_UNREQUESTED_DETAIL", path, "C-layer detail is source-indexed by default. Numerical detail requires an explicitly targeted digitization task.")
            if assignment.purpose != "requested_detail" or observation.analysis_role != "dense_series_point":
                error("DESIGN_DETAIL_ROLE", path, "A targeted optional point must explicitly declare requested_detail and dense_series_point; meaningful endpoints stay A or B.")
            if observation.id in protected:
                error("DESIGN_PROTECTED_DETAIL", path, "Performance pairing, design comparison and observation-specific interpretations cannot be hidden as optional detail.")
        elif assignment.purpose == "requested_detail" or observation.analysis_role == "dense_series_point":
            error("DESIGN_DETAIL_MISCLASSIFIED", path, "Dense optional detail may not be relabeled as a core/module result to bypass scope limits.")

    if not scope.source_index:
        error("DESIGN_SOURCE_INDEX_REQUIRED", "design_scope.source_index", "Record source understanding and deferred content, independently of numeric extraction.")
    indexed_sources = {entry.source_id for entry in scope.source_index}
    for sid in source_ids - indexed_sources:
        error("DESIGN_SOURCE_INDEX_INCOMPLETE", "design_scope.source_index", f"Source {sid!r} lacks an understanding/index disposition; main and SI remain in reading scope.")
    indexed_coverage = set()
    for i, entry in enumerate(scope.source_index):
        path = f"design_scope.source_index[{i}]"
        for field in ("summary", "material_relation", "reason"):
            nonblank(getattr(entry, field), path + "." + field)
        if any(not value.strip() for value in entry.content_types):
            error("DESIGN_EMPTY_CONTENT_TYPE", path + ".content_types", "Source content types must be nonblank.")
        if entry.source_id not in source_ids:
            error("DESIGN_SOURCE_NOT_IN_PAPER", path + ".source_id", "Source index must use this paper's registered source inventory.")
        if entry.block_id is None and not _source_level_limitation(entry):
            error("DESIGN_SOURCE_LEVEL_LIMITATION_REQUIRED", path, "A blockless index is only an unreadable/not_available source disposition, with explicit limitations and no invented locator, retained observations, quantitative references or deferred topics.")
        references(entry.retained_observation_ids, observations, path + ".retained_observation_ids")
        references(entry.quantitative_coverage_ids, coverage, path + ".quantitative_coverage_ids")
        indexed_coverage.update(entry.quantitative_coverage_ids)
        if entry.deferred_topics and entry.reading_state != "read":
            error("DESIGN_DEFERRED_NOT_READ", path, "Deliberately deferred quantification means read/indexed content; unread or unavailable content keeps its distinct limitation.")
        dispositions = [c for c in record.coverage if c.source_id == entry.source_id and entry.block_id in c.block_ids]
        if (entry.reading_state == "read" and dispositions
                and not any(c.state.value in {"read_structured", "read_unquantified", "ambiguous", "conflict"} for c in dispositions)):
            error("DESIGN_INDEX_READING_CONFLICT", path + ".reading_state", "The index claims reading while this block's coverage records only unread/unavailable/out-of-scope content.")
        for qid in entry.quantitative_coverage_ids:
            series = coverage.get(qid)
            if series and (series.source_id != entry.source_id or series.block_id != entry.block_id):
                error("DESIGN_INDEX_SOURCE_MISMATCH", path + ".quantitative_coverage_ids", "A series index must point to its actual source block, not an unrelated figure.")
        for oid in entry.retained_observation_ids:
            observation = observations.get(oid)
            if observation is None:
                continue
            cited = set(observation.evidence_ids) | set(observation.value.evidence_ids)
            cited |= {link.evidence_id for link in observation.evidence_links}
            if cited and not any(e.id in cited and e.source_id == entry.source_id and e.block_id == entry.block_id for e in record.evidence):
                error("DESIGN_INDEX_OBSERVATION_SOURCE", path + ".retained_observation_ids", "A retained result needs evidence in this indexed source block; unrelated citations cannot justify indexing.")
    for qid in coverage.keys() - indexed_coverage:
        error("DESIGN_SERIES_NOT_INDEXED", "design_scope.source_index", f"Quantitative source item {qid!r} lacks a scope/index disposition.")

    eligibility_keys = set()
    paper_uses = set()
    for i, entry in enumerate(scope.eligibility):
        path = f"design_scope.eligibility[{i}]"
        nonblank(entry.reason, path + ".reason")
        if entry.target_id not in entities[entry.target_kind]:
            error("DESIGN_MISSING_REFERENCE", path + ".target_id", "Use eligibility must refer to a real object in this record.")
        key = (entry.use, entry.target_kind, entry.target_id)
        if key in eligibility_keys:
            error("DESIGN_DUPLICATE_ELIGIBILITY", path, "Use assessments must not give conflicting duplicate dispositions for one target.")
        eligibility_keys.add(key)
        if entry.target_kind == "paper" and entry.target_id == record.paper.id:
            paper_uses.add(entry.use)
        references(entry.evidence_ids, evidence_ids, path + ".evidence_ids")
        if entry.status == "limited" and not entry.limitations:
            error("DESIGN_ELIGIBILITY_LIMITATION_REQUIRED", path + ".limitations", "State which comparisons or queries remain limited.")
        if entry.status == "eligible" and not entry.evidence_ids:
            error("DESIGN_ELIGIBILITY_EVIDENCE_REQUIRED", path + ".evidence_ids", "A positive use assessment requires supporting evidence; it is still not scientific validation.")
    for use in RESEARCH_USES - paper_uses:
        error("DESIGN_USE_ASSESSMENT_REQUIRED", "design_scope.eligibility", f"Assess the paper's {use!r} use; not_assessed is distinct from permission to rank.")
    for i, check in enumerate(scope.core_checks):
        references(check.evidence_ids, evidence_ids, f"design_scope.core_checks[{i}].evidence_ids")
    return issues


def _get(item, name, default=None):
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def design_source_index_issues(record: ExtractionRecord, source_documents=()):
    """Optional source-block checks; passing proves location consistency only."""
    from .validation import ValidationIssue

    if record.rule_version not in DESIGN_VERSIONS or record.design_scope is None:
        return []
    documents = list(source_documents)
    if not documents:
        return []
    sources, blocks = {}, {}
    for document in documents:
        source = _get(document, "source", document)
        sid = _get(source, "source_id", _get(source, "id"))
        sources[sid] = source
        for block in _get(document, "blocks", []):
            bid = _get(block, "block_id", _get(block, "id"))
            blocks[(sid, bid)] = block
    issues = []

    def error(code, path, message):
        issues.append(ValidationIssue(severity="error", code=code, path=path, message=message))

    for i, entry in enumerate(record.design_scope.source_index):
        path = f"design_scope.source_index[{i}]"
        source = sources.get(entry.source_id)
        if entry.block_id is None:
            if source is None:
                error("DESIGN_INDEX_BLOCK_MISSING", path, "An availability disposition must identify an actual registered source; unavailable unregistered attachments remain separate limitations.")
            elif not _source_level_limitation(entry):
                error("DESIGN_SOURCE_LEVEL_LIMITATION_REQUIRED", path, "A blockless entry cannot claim reading, a source locator or extracted scientific content.")
            elif any(sid == entry.source_id for sid, _ in blocks):
                error("DESIGN_INDEX_BLOCK_REQUIRED", path + ".block_id", "This source has registered blocks; preserve their actual locations and dispositions instead of labeling the entire source blockless/unavailable.")
            continue
        block = blocks.get((entry.source_id, entry.block_id))
        if source is None or block is None:
            error("DESIGN_INDEX_BLOCK_MISSING", path, "Indexed content must resolve to an actual registered source block; unavailable attachments need a separate limitation.")
            continue
        location = _get(block, "location", {})
        for field in ("physical_page", "paragraph_index", "table_index", "row_index", "cell_index", "part", "xml_path", "relationship_id"):
            claimed, actual = getattr(entry.locator, field), _get(location, field)
            if claimed is not None and actual is not None and claimed != actual:
                error("DESIGN_INDEX_LOCATOR_MISMATCH", path + ".locator." + field, "Index locator disagrees with its actual source block.")
        page_count = _get(source, "page_count")
        if entry.locator.physical_page is not None and page_count is not None and entry.locator.physical_page > page_count:
            error("DESIGN_INDEX_PAGE_RANGE", path + ".locator.physical_page", "Indexed physical page exceeds source page count.")
    return issues
