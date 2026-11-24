"""Fixed, read-only Neo4j retrieval over the selected publication snapshots.

Scientific text is passed only as a parameter. Neither the model nor source
documents can choose Cypher, a database, a relationship type or a file path.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from . import neo4j_connection as connection
from .graph_store import RELATIONS
from .reading_tools import Args
from .settings import Neo4jSettings


class GraphQuery(Args):
    keyword: str = Field(default="", max_length=200)
    entity: Literal["all", "material", "substance", "experiment", "series", "reference"] = "all"
    mode: Literal["nodes", "relationships"] = "nodes"
    record_id: str | None = Field(default=None, min_length=1, max_length=128)
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=10, ge=1, le=20)


DATABASE_QUERY = "CALL db.info() YIELD name RETURN name AS database"
NODE_QUERY = """MATCH (s:WTProjection)-[:CONTAINS]->(n:WTEntity)
WHERE s.id IN $snapshot_ids AND n.wt_snapshot = s.id
  AND any(label IN labels(n) WHERE label IN $entity_labels)
  AND ($node_id IS NULL OR n.id = $node_id)
  AND ($keyword = '' OR any(key IN ['name','formula','purpose','role','family_id','state','label','source_series_id']
       WHERE n[key] IS NOT NULL AND toLower(toString(n[key])) CONTAINS $keyword))
RETURN s.id AS snapshot_id, n.id AS node_id, labels(n) AS labels,
       properties(n) AS properties
ORDER BY snapshot_id, node_id
SKIP $offset LIMIT $fetch_limit"""

RELATION_QUERY = """MATCH (s:WTProjection)-[:CONTAINS]->(n:WTEntity)
WHERE s.id IN $snapshot_ids AND n.wt_snapshot = s.id
  AND any(label IN labels(n) WHERE label IN $entity_labels)
  AND ($node_id IS NULL OR n.id = $node_id)
  AND ($keyword = '' OR any(key IN ['name','formula','purpose','role','family_id','state','label','source_series_id']
       WHERE n[key] IS NOT NULL AND toLower(toString(n[key])) CONTAINS $keyword))
MATCH (n)-[r]-(other:WTEntity)
WHERE other.wt_snapshot = s.id AND type(r) IN $relationship_types
  AND EXISTS { MATCH (s)-[:CONTAINS]->(other) }
RETURN DISTINCT s.id AS snapshot_id, r.id AS relationship_id,
       type(r) AS relationship, properties(r) AS properties,
       startNode(r).id AS source_node_id, labels(startNode(r)) AS source_labels,
       properties(startNode(r)) AS source_properties,
       endNode(r).id AS target_node_id, labels(endNode(r)) AS target_labels,
       properties(endNode(r)) AS target_properties
ORDER BY snapshot_id, relationship_id
SKIP $offset LIMIT $fetch_limit"""

ENTITY_LABELS = {"material": "MaterialSample", "substance": "Substance", "experiment": "Experiment", "series": "ObservationSeries", "reference": "ReferenceAssociation"}
QUANTITATIVE_RELATIONSHIPS = {"HAS_RETAINED_COMPONENT", "PRELOADS", "TARGETS", "HAS_REACTION_INPUT"}

NORMALIZATION_INSTRUCTION = (
    "ReferenceAssociation and USES_REFERENCE/REFERENCES_OBSERVATION reuse existing observations as explicitly "
    "qualified controls or references. Follow the reference node with entity=reference and mode=relationships, "
    "then inspect reference_associations in the bound record for both source anchors and applicability. "
    "reported applicability describes the reference relationship, not a shared physical run, matched conditions "
    "or scientific verification. Do not copy a referenced observation into another experiment, count its reuse "
    "as independent replication, infer performance pairing or automatically average control values. Primary "
    "REPORTS/IN_SERIES ownership is unchanged; inferred/ambiguous/conflict applicability and limitations stay explicit. "
    "ObservationSeries is a source-series/archive index, not an independent experiment or a verified measurement. "
    "Use its bound record_id with wt_query_records section quantitative_coverage, then observations, to read "
    "the exact series members, source locators, coverage limits and retained raw points. A series node does not "
    "prove complete digitization or authorize counting its points as independent studies. "
    "Observation value/metric/unit with value_semantics=source_fact_value_v1 retain source Fact.value. "
    "source_metric_status and source_unit_status independently describe the source metric and unit; "
    "value_status does not replace them or the context-field statuses. An estimated or unknown unit/metric "
    "must not inherit reported from the numerical value; missing status requires checking the bound record. "
    "normalized_value, normalized_metric, normalized_unit and their normalization reasons are separate, "
    "unvalidated conversions. value_status applies only to the source value; normalized_value_status does not "
    "inherit reported/estimated or establish a new author-stated measurement. A completed reading audit does "
    "not validate conversion equivalence. Do not assume they measure the same quantity, infer a new metric from free text, "
    "or pair normalized_value with the source unit when normalized_unit is absent. "
    "For legacy_projection_unverified observations, stored value may already have replaced source value "
    "with a conversion while retaining the old metric/unit. Do not use legacy graph values directly for "
    "quantitative ranking, pointwise comparison or recomputation: first call wt_query_records with the "
    "bound record_id to recover source value, metric, unit and normalization fields. "
    "Observation measured_object/denominator/reference are source fields only when "
    "context_semantics=source_fact_value_v1; corresponding normalized_* fields are separate unvalidated candidates. "
    "Their individual *_status fields describe the source context, independently of observation value_status. "
    "legacy_context_unverified requires reading the bound record before object-, denominator- or reference-based "
    "comparison, even when value_semantics is already source_fact_value_v1. "
    "HAS_RETAINED_COMPONENT, PRELOADS, TARGETS and HAS_REACTION_INPUT keep source amount/concentration, unit "
    "and basis only when quantity_semantics=source_fact_value_v1. Their normalized_* fields and reasons are "
    "separate unvalidated conversions, not author-stated conditions; missing normalized_unit or normalized_basis "
    "does not authorize pairing a converted amount/concentration with the source unit or basis. For "
    "legacy_projection_unverified relationship quantities, first read the bound record before any condition, "
    "dosage or composition comparison, numerical ranking or recomputation. "
    "Response annotations do not rewrite or certify an old database snapshot."
)


def annotate_observation_semantics(result: dict) -> None:
    """Annotate a response copy without changing stored values or snapshots."""
    legacy_count = 0
    legacy_context_count = 0
    legacy_relation_count = 0
    for item in result.get("items", []):
        statuses = {}
        context_statuses = {}
        for property_field, label_field in (("properties", "labels"),
                ("source_properties", "source_labels"), ("target_properties", "target_labels")):
            properties = item.get(property_field)
            if not isinstance(properties, dict) or "Observation" not in item.get(label_field, []):
                continue
            source_preserved = properties.get("value_semantics") == "source_fact_value_v1"
            statuses[property_field] = (
                "source_values_with_separate_normalization" if source_preserved else "legacy_projection_unverified")
            legacy_count += int(not source_preserved)
            context_preserved = properties.get("context_semantics") == "source_fact_value_v1"
            context_statuses[property_field] = (
                "source_context_with_separate_normalization" if context_preserved else "legacy_context_unverified")
            legacy_context_count += int(not context_preserved)
        if statuses:
            item["observation_value_semantics"] = statuses
            item["observation_context_semantics"] = context_statuses
        if item.get("relationship") in QUANTITATIVE_RELATIONSHIPS:
            properties = item.get("properties")
            source_preserved = isinstance(properties, dict) and properties.get("quantity_semantics") == "source_fact_value_v1"
            item["relationship_quantity_semantics"] = (
                "source_values_with_separate_normalization" if source_preserved else "legacy_projection_unverified")
            legacy_relation_count += int(not source_preserved)
    result["legacy_observation_occurrences"] = legacy_count
    result["legacy_observation_context_occurrences"] = legacy_context_count
    result["legacy_quantitative_relationship_occurrences"] = legacy_relation_count
    result["normalization_instruction"] = NORMALIZATION_INSTRUCTION


def graph_scope(context: dict) -> dict[str, dict]:
    """Bind immutable graph snapshots to the exact current JSON record versions."""
    if not isinstance(context, dict) or not isinstance(context.get("record_mapping"), list):
        raise ValueError("graph retrieval requires an explicit current-publication scope")
    bound = {}
    records, papers = set(), set()
    for item in context["record_mapping"]:
        if not isinstance(item, dict):
            raise ValueError("invalid publication mapping")
        snapshot, record, paper = (item.get(k) for k in ("snapshot_id", "record_id", "paper_id"))
        if any(not isinstance(value, str) or not value for value in (snapshot, record, paper)):
            raise ValueError("publication identifiers must be nonempty")
        if snapshot in bound or record in records or paper in papers:
            raise ValueError("duplicate publication mapping cannot define current scope")
        if item.get("stale") is not False or item.get("latest_record_id") != record or item.get("source_change"):
            raise ValueError("stale publications are excluded from graph questions")
        bound[snapshot] = dict(item)
        records.add(record)
        papers.add(paper)
    snapshots = context.get("snapshots", [])
    if (not bound or not isinstance(snapshots, list)
            or len(snapshots) != len(bound)
            or any(not isinstance(s, dict) for s in snapshots)
            or {s.get("snapshot_id") for s in snapshots} != set(bound)):
        raise ValueError("graph snapshots do not match the current record mapping")
    return bound


def query_scope(context: dict, query: GraphQuery) -> dict[str, dict]:
    selected = graph_scope(context)
    if query.snapshot_id is not None:
        if query.snapshot_id not in selected:
            raise ValueError("snapshot_id is outside the selected graph scope")
        selected = {query.snapshot_id: selected[query.snapshot_id]}
    if query.record_id is not None:
        selected = {key: value for key, value in selected.items() if value["record_id"] == query.record_id}
        if not selected:
            raise ValueError("record_id is outside the selected graph scope")
    return selected


def read_graph(settings: Neo4jSettings, graph_context: dict, query: GraphQuery | dict,
               *, live: bool = False, driver_factory=None) -> dict:
    if not live:
        raise ValueError("graph retrieval requires explicit live mode; no connection attempted")
    settings = Neo4jSettings.model_validate(settings.model_dump())
    if not settings.enabled:
        raise ValueError("Neo4j is disabled")
    arguments = GraphQuery.model_validate(query)
    selected = query_scope(graph_context, arguments)
    labels = list(ENTITY_LABELS.values()) if arguments.entity == "all" else [ENTITY_LABELS[arguments.entity]]
    parameters = {"snapshot_ids": list(selected), "entity_labels": labels,
        "keyword": arguments.keyword.casefold(), "node_id": arguments.node_id,
        "offset": arguments.offset, "fetch_limit": arguments.limit + 1,
        "relationship_types": sorted(RELATIONS)}
    from neo4j import unit_of_work
    text = NODE_QUERY if arguments.mode == "nodes" else RELATION_QUERY
    try:
        with (driver_factory or connection._driver_factory)(settings.uri,
                auth=(settings.username, connection._password(settings)),
                connection_timeout=settings.timeout_seconds,
                connection_acquisition_timeout=settings.timeout_seconds,
                max_transaction_retry_time=settings.timeout_seconds) as driver:
            with driver.session(database=settings.database, default_access_mode="READ") as session:
                @unit_of_work(timeout=settings.timeout_seconds)
                def retrieve(transaction):
                    actual = transaction.run(DATABASE_QUERY).single(strict=True)
                    if actual is None or actual["database"] != settings.database:
                        raise ValueError("database identity mismatch")
                    return transaction.run(text, **parameters).data()
                rows = session.execute_read(retrieve)
        if len(rows) > arguments.limit + 1 or any(row.get("snapshot_id") not in selected for row in rows):
            raise ValueError("graph returned data outside the bounded selected scope")
        items = [{**row, "record_id": selected[row["snapshot_id"]]["record_id"]} for row in rows[:arguments.limit]]
    except Exception:
        # Driver errors may contain endpoint and credential details. Never pass
        # those strings into model context, browser responses or research logs.
        raise connection.Neo4jConnectionError("当前图谱范围读取失败，请检查新数据库连接、发布快照和读取权限；未执行写入。") from None
    more = len(rows) > arguments.limit
    result = {"items": items, "offset": arguments.offset, "limit": arguments.limit, "has_more": more,
        "next_offset": arguments.offset + arguments.limit if more else None,
        "scope_record_count": len(selected), "snapshot_ids": list(selected), "database": settings.database,
        "persistent_graph_write": False, "source": "neo4j_current_snapshots",
        "scientific_validation": "not_established",
        "evidence_instruction": "Use record_id with wt_query_records to recover complete experiment conditions and original evidence; graph projection omits unknown/conflicting values. " + NORMALIZATION_INSTRUCTION}
    annotate_observation_semantics(result)
    return result
