import copy
import json

import pytest

from watertreatment.coverage_transport import POLICY, expand_dossier, order_gaps, verified_dossier
from watertreatment.reading import ReadingDossier


def sample():
    docs = [{"source": {"source_id": sid}, "blocks": [
        {"order": n, "block_id": f"{sid}/{n}", "source_id": sid} for n in range(1, count + 1)]}
        for sid, count in [("primary", 3), ("si", 2)]]
    raw = {"paper_id": "example", "article_question_and_design": "synthetic",
           "material_map": [], "experiment_map": [], "cross_source_conflicts": [],
           "unresolved_questions": [], "schema_gaps": [], "coverage": [
               {"source_id": "primary", "block_ids": ["@orders:1-2"], "state": "read_structured", "text_read": True},
               {"source_id": "primary", "block_ids": ["primary/3"], "state": "unreadable"},
               {"source_id": "si", "block_ids": ["@orders:1-2"], "state": "out_of_scope"}]}
    return raw, {"documents": docs}


def test_verifiable_source_local_dispositions_keep_raw_and_all_reading_states():
    raw, bundle = sample()
    retained = copy.deepcopy(raw)
    dossier, receipt = expand_dossier(raw, bundle)
    assert [e.block_ids for e in dossier.coverage] == [["primary/1", "primary/2"], ["primary/3"], ["si/1", "si/2"]]
    assert [e.text_read for e in dossier.coverage] == [True, False, False]
    assert all(not e.visual_checked for e in dossier.coverage) and receipt["ranges"] == 2 and retained == raw
    req = {"dossier_coverage_transport": POLICY}
    assert verified_dossier(raw, bundle, req, {"dossier_coverage_expansion": receipt}) == dossier
    assert verified_dossier(raw, bundle, {}, {}) == ReadingDossier.model_validate(raw)
    with pytest.raises(ValueError):
        verified_dossier(raw, bundle, req, {"dossier_coverage_expansion": {**receipt, "ranges": 5}})


@pytest.mark.parametrize("change", ["overlap", "foreign", "reversed", "outside", "reordered", "wrong_source"])
def test_invalid_ranges_do_not_earn_a_disposition(change):
    raw, bundle = sample()
    if change == "overlap":
        raw["coverage"][1]["block_ids"] = ["primary/1"]
    elif change == "foreign":
        raw["coverage"][1]["block_ids"] = ["si/2"]
    elif change == "reordered":
        bundle["documents"][0]["blocks"].reverse()
    elif change == "wrong_source":
        bundle["documents"][0]["blocks"][0]["source_id"] = "si"
    else:
        raw["coverage"][0]["block_ids"] = ["@orders:2-1" if change == "reversed" else "@orders:1-99"]
    with pytest.raises(ValueError):
        expand_dossier(raw, bundle)


def test_gap_tool_arithmetic_does_not_create_cover_or_imply_reading():
    assert order_gaps(8, [[6, 7], [2, 3], [3, 5]]) == [[1, 1], [8, 8]]
    assert order_gaps(8, []) == [[1, 8]]
    with pytest.raises(ValueError):
        order_gaps(8, [[1, 9]])


@pytest.mark.parametrize("overlap", [False, True])
def test_dossier_prompt_roundtrips_without_merging_reading_flags_or_order(overlap):
    from watertreatment.coverage_transport import dossier_for_prompt
    raw, bundle = sample()
    dossier, _ = expand_dossier(raw, bundle)
    if overlap:
        dossier.coverage[1].block_ids.extend(["primary/2", "unresolved_block"])
    encoded = json.loads(dossier_for_prompt(dossier, bundle).split("\n", 1)[1])
    assert expand_dossier(encoded, bundle)[0] == dossier
    assert encoded["coverage"][0]["block_ids"] == (["primary/1","primary/2"] if overlap else ["@orders:1-2"])
