from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment.retained_amendment import check_changes, declared_findings
from watertreatment.storage import hash_file


def test_located_followup_preserves_every_unrelated_field():
    old = {"experiments": [{"id": "a", "inputs": ["unknown"], "temperature": 30}]}
    new = {"experiments": [{"id": "a", "inputs": ["externally dosed"], "temperature": 30}]}
    questions = [{"paths": ["experiments[0].inputs"]}]
    check_changes(old, new, questions)
    new["experiments"][0]["temperature"] = 15
    with pytest.raises(ValueError, match="outside"):
        check_changes(old, new, questions)


def test_declared_finding_requires_an_immutable_audit_and_original_block(tmp_path):
    for name in ("candidate.json", "model_review.json"):
        (tmp_path / name).write_text("{}")
    docs = [SimpleNamespace(source=SimpleNamespace(source_id="s"), blocks=[SimpleNamespace(block_id="b")])]
    q = {"text": "Check the reference phase", "paths": ["experiments[0].inputs"],
         "evidence": [{"source_id": "s", "block_id": "b"}]}
    statement = {"parent_run": tmp_path.name, "record_sha256": hash_file(tmp_path / "candidate.json"),
                 "audit_sha256": hash_file(tmp_path / "model_review.json"), "findings": [q]}
    assert declared_findings(tmp_path, docs, statement) == ([q], [("s", "b")])
    q["evidence"][0]["block_id"] = "another"
    with pytest.raises(ValueError, match="outside"):
        declared_findings(tmp_path, docs, statement)
    statement["audit_sha256"] = "wrong"
    with pytest.raises(ValueError, match="identify"):
        declared_findings(tmp_path, docs, statement)
