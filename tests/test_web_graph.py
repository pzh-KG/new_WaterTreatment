"""Offline graph-query boundaries; no database or provider is contacted."""
from __future__ import annotations

import copy
from contextlib import nullcontext

import pytest

from watertreatment import neo4j_connection as connection
from watertreatment.settings import Neo4jSettings
from watertreatment.web_graph import GraphQuery, graph_scope, read_graph


FIRST = "a" * 64
SECOND = "b" * 64


@pytest.fixture
def graph_context():
    return {"database": "watertreatment", "scope": "current_workbench_publications",
        "snapshots": [{"snapshot_id": FIRST}, {"snapshot_id": SECOND}],
        "record_mapping": [
            {"paper_id": "paper-a", "record_id": "record-a", "snapshot_id": FIRST,
             "latest_record_id": "record-a", "stale": False},
            {"paper_id": "paper-b", "record_id": "record-b", "snapshot_id": SECOND,
             "latest_record_id": "record-b", "stale": False},
        ]}


@pytest.fixture
def graph_settings(monkeypatch):
    monkeypatch.setenv("WT_GRAPH_TEST_PASSWORD", "synthetic-secret-never-expose")
    return Neo4jSettings(enabled=True, uri="bolt://127.0.0.1:7688", database="watertreatment",
                         username="neo4j", password_env="WT_GRAPH_TEST_PASSWORD", timeout_seconds=5)


class Result:
    def __init__(self, *, database=None, rows=None):
        self.database = database
        self.rows = rows or []

    def single(self, *, strict):
        assert strict
        return {"database": self.database}

    def data(self):
        return copy.deepcopy(self.rows)


class Driver:
    def __init__(self, rows=None, *, database="watertreatment", fail=False):
        self.rows = rows or []
        self.database = database
        self.fail = fail
        self.queries = []
        self.read_transactions = 0
        self.closed = False

    def factory(self, *args, **kwargs):
        self.options = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def session(self, **kwargs):
        self.session_options = kwargs
        return nullcontext(self)

    def execute_read(self, callback):
        self.read_transactions += 1
        assert callback.timeout == 5
        return callback(self)

    def run(self, query, **parameters):
        assert isinstance(query, str)
        self.queries.append((query, parameters))
        if "db.info()" in query:
            return Result(database=self.database)
        if self.fail:
            raise RuntimeError("driver failed with synthetic-secret-never-expose at bolt://hidden-endpoint")
        return Result(rows=self.rows)


def node(snapshot=FIRST, identifier="node-1"):
    return {"snapshot_id": snapshot, "node_id": identifier, "labels": ["WTEntity", "MaterialSample"],
            "properties": {"id": identifier, "wt_snapshot": snapshot, "name": "Synthetic final material",
                           "role": "primary_final", "evidence_ids": ["evidence-1"]}}


def test_graph_read_requires_explicit_live_before_secret_or_driver(graph_settings, graph_context, monkeypatch):
    monkeypatch.setattr(connection, "_password", lambda *_: pytest.fail("must not read secret"))
    with pytest.raises(ValueError):
        read_graph(graph_settings, graph_context, {}, driver_factory=lambda *a, **k: pytest.fail("must not connect"))
    disabled = Neo4jSettings()
    with pytest.raises(ValueError):
        read_graph(disabled, graph_context, {}, live=True, driver_factory=lambda *a, **k: pytest.fail("must not connect"))


def test_node_keyword_is_parameter_data_and_read_uses_exact_business_database(graph_settings, graph_context):
    driver = Driver([node()])
    injected = "Cu' MATCH (n) DETACH DELETE n //"
    result = read_graph(graph_settings, graph_context,
        {"keyword": injected, "entity": "material", "record_id": "record-a"}, live=True, driver_factory=driver.factory)
    assert driver.session_options == {"database": "watertreatment", "default_access_mode": "READ"}
    assert driver.read_transactions == 1 and driver.closed
    assert len(driver.queries) == 2
    query, parameters = driver.queries[1]
    assert injected not in query and "DETACH DELETE" not in query
    assert parameters["keyword"] == injected.casefold()
    assert parameters["snapshot_ids"] == [FIRST]
    assert parameters["entity_labels"] == ["MaterialSample"]
    assert parameters["offset"] == 0 and parameters["fetch_limit"] == 11
    assert result["items"][0]["record_id"] == "record-a"
    assert result["items"][0]["snapshot_id"] == FIRST
    assert result["items"][0]["properties"]["evidence_ids"] == ["evidence-1"]
    assert result["persistent_graph_write"] is False
    assert result["source"] == "neo4j_current_snapshots"
    assert "synthetic-secret" not in str(result)


def test_relationship_mode_uses_fixed_query_and_preserves_endpoint_properties(graph_settings, graph_context):
    row = {"snapshot_id": SECOND, "relationship_id": "edge-1", "relationship": "TARGETS",
        "properties": {"concentration": 8, "unit": "mg/L"},
        "source_node_id": "experiment-graph-id", "source_labels": ["Experiment"],
        "source_properties": {"purpose": "coupled", "wt_snapshot": SECOND},
        "target_node_id": "substance-graph-id", "target_labels": ["Substance"],
        "target_properties": {"name": "Synthetic pollutant", "wt_snapshot": SECOND}}
    driver = Driver([row])
    result = read_graph(graph_settings, graph_context,
        GraphQuery(mode="relationships", snapshot_id=SECOND, entity="experiment", node_id="experiment-graph-id"),
        live=True, driver_factory=driver.factory)
    query, params = driver.queries[1]
    assert "relationship_types" in params and "TARGETS" in params["relationship_types"]
    assert params["snapshot_ids"] == [SECOND] and params["node_id"] == "experiment-graph-id"
    assert "experiment-graph-id" not in query
    assert result["items"][0]["record_id"] == "record-b"
    assert result["items"][0]["source_node_id"] == "experiment-graph-id"
    assert result["items"][0]["target_node_id"] == "substance-graph-id"
    assert result["items"][0]["target_properties"]["name"] == "Synthetic pollutant"
    assert result["items"][0]["properties"]["concentration"] == 8


def test_reference_navigation_preserves_applicability_without_new_measurement_claims(graph_settings, graph_context):
    row = {"snapshot_id": FIRST, "relationship_id": "shared-reference-edge", "relationship": "REFERENCES_OBSERVATION",
        "properties": {"ownership_changed": False, "independent_replicate": False},
        "source_node_id": "reference-node", "source_labels": ["ReferenceAssociation"],
        "source_properties": {"applicability": "inferred", "relationship": "shared_control",
                              "independent_replicates_added": 0, "wt_snapshot": FIRST},
        "target_node_id": "source-observation", "target_labels": ["Observation"],
        "target_properties": {"value": 72, "value_status": "reported", "wt_snapshot": FIRST}}
    driver = Driver([row])
    result = read_graph(graph_settings, graph_context,
        GraphQuery(entity="reference", mode="relationships", record_id="record-a", node_id="reference-node"),
        live=True, driver_factory=driver.factory)
    query, parameters = driver.queries[1]
    assert parameters["entity_labels"] == ["ReferenceAssociation"]
    assert parameters["snapshot_ids"] == [FIRST]
    assert {"USES_REFERENCE", "REFERENCES_OBSERVATION"} <= set(parameters["relationship_types"])
    assert "reference-node" not in query
    item = result["items"][0]
    assert item["source_properties"]["applicability"] == "inferred"
    assert item["properties"]["independent_replicate"] is False
    assert item["target_properties"]["value"] == 72
    assert "independent replication" in result["evidence_instruction"]
    assert result["persistent_graph_write"] is False


def test_lookahead_pagination_returns_at_most_twenty_and_can_continue(graph_settings, graph_context):
    driver = Driver([node(identifier=f"node-{index:02d}") for index in range(21)])
    result = read_graph(graph_settings, graph_context, {"limit": 20, "offset": 40}, live=True, driver_factory=driver.factory)
    assert len(result["items"]) == 20 and result["has_more"] is True
    assert result["next_offset"] == 60 and result["offset"] == 40 and result["limit"] == 20
    assert driver.queries[1][1]["fetch_limit"] == 21
    assert driver.queries[1][1]["offset"] == 40
    last = Driver([node()])
    result = read_graph(graph_settings, graph_context, {"limit": 20, "offset": 60}, live=True, driver_factory=last.factory)
    assert not result["has_more"] and result["next_offset"] is None


@pytest.mark.parametrize("query", [
    {"snapshot_id": "c" * 64}, {"record_id": "outside"},
    {"record_id": "record-a", "snapshot_id": SECOND},
    {"limit": 21}, {"offset": -1}, {"offset": 100001},
    {"cypher": "MATCH (n) DELETE n"}, {"entity": "MaterialSample) DETACH DELETE n"},
])
def test_out_of_scope_or_arbitrary_query_is_rejected_before_database_read(graph_settings, graph_context, query):
    with pytest.raises(ValueError):
        read_graph(graph_settings, graph_context, query, live=True,
                   driver_factory=lambda *a, **k: pytest.fail("out-of-scope query connected"))


@pytest.mark.parametrize("mutation", [
    lambda c: c["record_mapping"][0].update(stale=True),
    lambda c: c["record_mapping"][0].update(latest_record_id="newer-version"),
    lambda c: c["record_mapping"][1].update(snapshot_id=FIRST),
    lambda c: c["snapshots"].pop(),
])
def test_graph_scope_cannot_accept_stale_duplicate_or_mismatched_snapshots(graph_context, mutation):
    mutation(graph_context)
    with pytest.raises(ValueError):
        graph_scope(graph_context)


def test_rows_returned_outside_bound_scope_are_rejected_not_exposed(graph_settings, graph_context):
    driver = Driver([node(snapshot="c" * 64)])
    with pytest.raises((ValueError, connection.Neo4jConnectionError)):
        read_graph(graph_settings, graph_context, {"record_id": "record-a"}, live=True, driver_factory=driver.factory)


@pytest.mark.parametrize("driver", [Driver(database="old-database"), Driver(fail=True)])
def test_database_identity_and_driver_errors_are_sanitized(graph_settings, graph_context, driver):
    with pytest.raises((ValueError, connection.Neo4jConnectionError)) as caught:
        read_graph(graph_settings, graph_context, {}, live=True, driver_factory=driver.factory)
    assert "synthetic-secret" not in str(caught.value)
    assert "hidden-endpoint" not in str(caught.value)
    assert driver.closed
    if driver.database != "watertreatment":
        assert len(driver.queries) == 1
