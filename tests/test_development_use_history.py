"""Development history blocks later blind-test claims without rewriting records."""
import json

import pytest

from watertreatment.storage import DataStore, cohort_at_receipt, hash_file, utc_now, write_new_json


@pytest.fixture
def ledger(tmp_path):
    store = DataStore(tmp_path / "data")
    store.initialize()
    run = store.new_run("extraction", {"paper_id": "paper-a", "cohort_role": "unassigned"})
    return store, run


def test_history_preserves_unassigned_candidate_and_frozen_plan(ledger):
    store, run = ledger
    hashes = ["a" * 64, "b" * 64]
    assert store.register_cohort("paper-a", hashes, "unassigned") == "unassigned"
    write_new_json(run / "candidate.json", {"paper": {"dataset_role": "unspecified"}})
    write_new_json(run / "plan.json", {"cohort_role": "unassigned"})
    before = {p: hash_file(p) for p in run.iterdir() if p.is_file()}
    store.record_development_use(run.name, "paper-a", hashes, "Diagnosed source delivery and revised audit rules")
    event = store.last_event(run.name, "development_use")
    assert event["paper_id"] == "paper-a" and event["source_hashes"] == hashes
    assert event["reason"] == "Diagnosed source delivery and revised audit rules"
    assert store.has_development_exposure("paper-a", hashes) is False
    assert store.register_cohort("paper-a", hashes, "unassigned") == "unassigned"
    assert {p: hash_file(p) for p in before} == before
    assert json.loads((run / "candidate.json").read_text())["paper"]["dataset_role"] == "unspecified"


@pytest.mark.parametrize("paper_id,hashes", [("paper-a", ["b" * 64]), ("renamed-paper", ["a" * 64])])
def test_same_paper_or_identical_bytes_cannot_become_heldout(ledger, paper_id, hashes):
    store, run = ledger
    store.record_development_use(run.name, "paper-a", ["a" * 64], "Engineering calibration")
    with pytest.raises(ValueError, match="independent heldout"):
        store.register_cohort(paper_id, hashes, "heldout")
    assert store.register_cohort("unseen-paper", ["c" * 64], "heldout") == "heldout"


def test_existing_development_cohort_promotion_rule_is_unchanged(ledger):
    store, _ = ledger
    store.register_cohort("paper-a", ["a" * 64], "development")
    assert store.has_development_exposure("other-name", ["a" * 64])
    assert store.register_cohort("paper-a", ["a" * 64], "unassigned") == "development"


def test_archived_receipt_replays_exposure_at_registration_only(ledger):
    from concurrent.futures import ThreadPoolExecutor

    store, _ = ledger
    hashes = ["a" * 64]
    registered = utc_now()
    store.register_cohort("paper-a", hashes, "development")
    with cohort_at_receipt(registered), ThreadPoolExecutor(max_workers=1) as pool:
        assert not store.has_development_exposure("renamed", hashes)
        assert pool.submit(store.has_development_exposure, "renamed", hashes).result()
        with cohort_at_receipt(utc_now()):
            assert not store.has_development_exposure("paper-a", [])
    assert store.has_development_exposure("paper-a", hashes)
    with cohort_at_receipt(utc_now()):
        assert store.has_development_exposure("renamed", hashes)


@pytest.mark.parametrize("run_id,paper_id,hashes,reason", [
    ("unknown", "p", ["a" * 64], "calibration"),
    (None, "", ["a" * 64], "calibration"),
    (None, "p", [], "calibration"),
    (None, "p", ["not-a-source-hash"], "calibration"),
    (None, "p", ["a" * 64], " "),
])
def test_history_requires_registered_run_exact_sources_and_reason(ledger, run_id, paper_id, hashes, reason):
    store, run = ledger
    with pytest.raises(ValueError):
        store.record_development_use(run_id or run.name, paper_id, hashes, reason)
    assert store.last_event(run.name, "development_use") is None
