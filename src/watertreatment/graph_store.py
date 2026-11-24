"""Explicit atomic Neo4j loading of candidate snapshots, never scientific acceptance."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import neo4j_connection as connection
from .projection import GraphProjection
from .settings import Neo4jSettings
from .storage import digest_json, utc_now

LABELS = {"Paper", "MaterialSample", "Substance", "Configuration", "Experiment", "Observation", "DesignComparison", "ObservationSeries", "ReferenceAssociation"}
RELATIONS = {"DESCRIBES", "HAS_RETAINED_COMPONENT", "PRELOADS", "USES", "HAS_ACCESSORY",
    "USES_CONFIGURATION", "TESTS", "TARGETS", "HAS_REACTION_INPUT", "REPORTS", "MEASURES",
    "ANALYZES", "PAIRED_WITH", "COMPARES", "USES_EVIDENCE", "HAS_SERIES", "IN_SERIES",
    "USES_REFERENCE", "REFERENCES_OBSERVATION"}


def _properties(values: dict) -> dict:
    result = {}
    encoded = []
    for key, value in values.items():
        if key == "id" or key.startswith("wt_"):
            raise ValueError("projection properties cannot override graph bookkeeping")
        if value is None:
            continue
        # Neo4j properties cannot store heterogeneous arrays. Preserve them
        # losslessly and explicitly mark the encoded property names.
        if isinstance(value, list) and len({type(x) for x in value}) > 1:
            result[key] = json.dumps(value, ensure_ascii=False, allow_nan=False)
            encoded.append(key)
        else:
            result[key] = value
    if encoded:
        result["wt_json_properties"] = encoded
    return result


def _rows(graph: GraphProjection, snapshot_id: str) -> tuple[dict, dict]:
    nodes, edges = defaultdict(list), defaultdict(list)
    identifiers = {n.id for n in graph.nodes}
    if len(identifiers) != len(graph.nodes) or len({e.id for e in graph.edges}) != len(graph.edges):
        raise ValueError("duplicate graph IDs")
    for node in graph.nodes:
        if node.label not in LABELS:
            raise ValueError("unsupported graph node label")
        nodes[node.label].append({"id": digest_json([snapshot_id, node.id]), "properties": {
            **_properties(node.properties), "wt_snapshot": snapshot_id, "wt_review_state": "candidate_unverified"}})
    for edge in graph.edges:
        if edge.relationship not in RELATIONS or edge.source not in identifiers or edge.target not in identifiers:
            raise ValueError("unsupported relationship or dangling graph endpoint")
        edges[edge.relationship].append({"id": digest_json([snapshot_id, edge.id]),
            "source": digest_json([snapshot_id, edge.source]), "target": digest_json([snapshot_id, edge.target]),
            "properties": _properties(edge.properties)})
    return nodes, edges


def load_graph(settings: Neo4jSettings, graph: GraphProjection, *, source_record: Path,
               live: bool = False, rollback: bool = False) -> dict:
    if not live:
        raise ValueError("no graph connection attempted; graph loading requires --live")
    settings = Neo4jSettings.model_validate(settings.model_dump())
    if not settings.enabled:
        raise ValueError("Neo4j is disabled")
    snapshot_id = digest_json(graph.model_dump(mode="json"))
    nodes, edges = _rows(graph, snapshot_id)
    from neo4j import Query
    def query(text: str):
        return Query(text, timeout=settings.timeout_seconds)
    metadata = {"paper_id": graph.paper_id, "record_fingerprint": graph.record_fingerprint,
        "schema_version": graph.schema_version, "rule_version": graph.rule_version,
        "source_check": graph.source_check, "source_record": str(source_record.resolve()),
        "review_state": "candidate_unverified", "scientific_accuracy": "not_established"}
    try:
        with connection._driver_factory(settings.uri, auth=(settings.username, connection._password(settings)),
                connection_timeout=settings.timeout_seconds,
                connection_acquisition_timeout=settings.timeout_seconds) as driver:
            with driver.session(database=settings.database, default_access_mode="WRITE") as session:
                actual = session.run(query("CALL db.info() YIELD name RETURN name AS database")).single(strict=True)
                if actual is None or actual["database"] != settings.database:
                    raise connection.Neo4jConnectionError("Database identity mismatch; graph loading refused.")
                if not rollback:
                    # Independent, idempotent schema transactions precede data.
                    for label, name in (("WTProjection", "wt_projection_id"), ("WTEntity", "wt_entity_id")):
                        session.run(query(f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")).consume()
                with session.begin_transaction(timeout=settings.timeout_seconds) as tx:
                    tx.run("MERGE (s:WTProjection {id:$id}) ON CREATE SET s.created_at=$created SET s += $metadata",
                           id=snapshot_id, created=utc_now(), metadata=metadata).consume()
                    for label, rows in nodes.items():
                        tx.run(f"UNWIND $rows AS row MERGE (n:WTEntity:{label} {{id:row.id}}) SET n += row.properties "
                               "WITH n MATCH (s:WTProjection {id:$snapshot}) MERGE (s)-[:CONTAINS]->(n)",
                               rows=rows, snapshot=snapshot_id).consume()
                    for relationship, rows in edges.items():
                        tx.run("UNWIND $rows AS row MATCH (a:WTEntity {id:row.source}), (b:WTEntity {id:row.target}) "
                               f"MERGE (a)-[r:{relationship} {{id:row.id}}]->(b) SET r += row.properties", rows=rows).consume()
                    if rollback:
                        tx.rollback()
                    else:
                        tx.commit()
    except connection.Neo4jConnectionError:
        raise
    except Exception:
        raise connection.Neo4jConnectionError("Neo4j graph transaction failed; no successful load is reported. Check the endpoint, permissions and retained local projection.") from None
    return {"status": "rolled_back" if rollback else "loaded_as_candidate", "database": settings.database,
        "snapshot_id": snapshot_id, "nodes_in_projection": len(graph.nodes), "edges_in_projection": len(graph.edges),
        "persistent_graph_write": not rollback, "scientific_accuracy": "not_established"}
