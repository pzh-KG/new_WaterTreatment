from contextlib import nullcontext
from pathlib import Path

import pytest
from neo4j import Query

from watertreatment import neo4j_connection as connection
from watertreatment.graph_store import _properties, load_graph
from watertreatment.projection import GraphProjection, GraphNode, GraphEdge
from watertreatment.settings import Neo4jSettings


@pytest.fixture
def graph():
    return GraphProjection(schema_version="0.1.0", rule_version="0.5", paper_id="synthetic", record_fingerprint="a"*64,
        nodes=[GraphNode(id="p", label="Paper", properties={"title":"quote ' \\ UNWIND"}),
               GraphNode(id="m", label="MaterialSample", properties={"name":"fixture"})],
        edges=[GraphEdge(id="e",source="p",target="m",relationship="DESCRIBES")])


class Driver:
    def __init__(self, database="watertreatment", fail=False):
        self.database, self.fail = database, fail
        self.queries=[]
        self.committed=self.rolled_back=False
    def __enter__(self): return self
    def __exit__(self,*args):
        if args[0]: self.rolled_back=True
    def session(self, **kwargs):
        self.session_options=kwargs
        return nullcontext(self)
    def begin_transaction(self, **kwargs): return self
    def run(self, query, **params):
        self.queries.append((query.text if isinstance(query,Query) else query,params))
        if self.fail and params.get("rows"):
            raise RuntimeError("fixture PASSWORD must never be exposed")
        return self
    def single(self, **kwargs): return {"database":self.database}
    def consume(self): return None
    def commit(self): self.committed=True
    def rollback(self): self.rolled_back=True


def setup(tmp_path, monkeypatch, driver):
    password=tmp_path/"password.txt"
    password.write_text("fixture PASSWORD")
    monkeypatch.setattr(connection,"_driver_factory",lambda *a,**k:driver)
    return Neo4jSettings(enabled=True,uri="bolt://127.0.0.1:7688",database="watertreatment",username="neo4j",password_file=password)


@pytest.mark.parametrize("rollback",[False,True])
def test_atomic_candidate_loading_and_rollback(tmp_path,monkeypatch,graph,rollback):
    driver=Driver()
    result=load_graph(setup(tmp_path,monkeypatch,driver),graph,source_record=tmp_path/"record.json",live=True,rollback=rollback)
    assert driver.session_options=={"database":"watertreatment","default_access_mode":"WRITE"}
    assert driver.committed is not rollback and driver.rolled_back is rollback
    assert result["scientific_accuracy"]=="not_established"
    assert result["persistent_graph_write"] is not rollback
    assert any("CREATE CONSTRAINT" in q for q,p in driver.queries) is not rollback
    for query, params in driver.queries:
        assert "quote '" not in query
        for row in params.get("rows",[]):
            if "wt_snapshot" in row["properties"]:
                assert row["properties"]["wt_review_state"]=="candidate_unverified"


def test_wrong_database_refuses_all_writes(tmp_path,monkeypatch,graph):
    driver=Driver(database="old-db")
    with pytest.raises(connection.Neo4jConnectionError,match="identity"):
        load_graph(setup(tmp_path,monkeypatch,driver),graph,source_record=tmp_path/"record.json",live=True)
    assert len(driver.queries)==1 and not driver.committed


def test_failed_data_transaction_is_not_reported_as_success(tmp_path,monkeypatch,graph):
    driver=Driver(fail=True)
    with pytest.raises(connection.Neo4jConnectionError) as caught:
        load_graph(setup(tmp_path,monkeypatch,driver),graph,source_record=tmp_path/"record.json",live=True)
    assert "PASSWORD" not in str(caught.value)
    assert driver.rolled_back and not driver.committed


def test_explicit_live_and_safe_labels_required(tmp_path,monkeypatch,graph):
    config=setup(tmp_path,monkeypatch,Driver())
    monkeypatch.setattr(connection,"_driver_factory",lambda *a,**k:pytest.fail("must not connect"))
    with pytest.raises(ValueError,match="--live"):
        load_graph(config,graph,source_record=Path("record.json"))
    graph.nodes[0].label="Paper`) DETACH DELETE n"
    with pytest.raises(ValueError,match="label"):
        load_graph(config,graph,source_record=Path("record.json"),live=True)


def test_properties_preserve_mixed_values_and_protect_identity():
    assert _properties({"values":["two",2]})=={"values":'["two", 2]',"wt_json_properties":["values"]}
    with pytest.raises(ValueError,match="bookkeeping"):
        _properties({"id":"overwrite"})
