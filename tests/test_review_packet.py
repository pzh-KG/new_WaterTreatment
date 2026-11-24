import json

import pytest

from watertreatment.domain import ExtractionRecord
from watertreatment.review_packet import review_packet, select_observations


def test_review_packet_prioritizes_final_pair_and_maps_native_source(tmp_path):
    record = ExtractionRecord.model_validate({
        "paper": {"id": "synthetic-review"},
        "materials": [{"id": "control", "role": "necessary_control"},
                      {"id": "final", "role": "primary_final"}],
        "substances": [{"id": "target", "name": {"value": "Local dye name", "status": "reported"}}],
        "experiments": [{"id": "separate", "material_ids": ["final"], "purpose": "evaporation"},
                        {"id": "control-test", "material_ids": ["control"]},
                        {"id": "joint", "material_ids": ["final"], "purpose": "coupled",
                         "targets": [{"substance_id": "target", "concentration": {"value": 8, "status": "reported"}}],
                         "joint_evidence": {"level": "J2"},
                         "pairs": [{"left_observation_id": "joint-evap", "right_observation_id": "joint-treatment", "level": "P2"}]},
                        {"id": "cycles", "material_ids": ["final"], "purpose": "stability"},
                        {"id": "storage", "material_ids": ["final"], "purpose": "stability"},
                        {"id": "catalytic-cycles", "material_ids": ["final"], "purpose": "stability"}],
        "observations": [{"id": "control-result", "material_id": "control", "experiment_id": "control-test"},
                         {"id": "pure-water", "material_id": "final", "experiment_id": "separate"},
                         {"id": "joint-evap", "material_id": "final", "experiment_id": "joint"},
                         {"id": "joint-treatment", "material_id": "final", "experiment_id": "joint", "evidence_ids": ["cell"]},
                         {"id": "stability", "material_id": "final", "experiment_id": "cycles", "metric_kind": "evaporation_stability",
                          "value": {"status": "read_unquantified", "note": "Trend visible, endpoint unclear"}},
                         {"id": "water-storage", "material_id": "final", "experiment_id": "storage", "metric_kind": "evaporation_stability"},
                         {"id": "catalytic-stability", "material_id": "final", "experiment_id": "catalytic-cycles", "metric_kind": "catalytic_stability"}],
        "evidence": [{"id": "cell", "source_id": "si", "block_id": "cell-3",
                      "locator": {"table_index": 1, "row_index": 2, "cell_index": 3}}],
    })
    (tmp_path / "sources.json").write_text(json.dumps({"documents": [{"source": {
        "source_id": "si", "role": "supporting_information", "format": "docx", "path": "frozen.docx",
        "metadata": {"original_path": "original-supplement.docx"}}}]}), encoding="utf-8")
    chosen = select_observations(record)
    assert [o.id for o in chosen[:3]] == ["joint-evap", "joint-treatment", "stability"]
    assert chosen[3].id == "catalytic-stability"
    assert len(chosen) <= 6
    exported = tmp_path / "new-export.txt"
    review_packet(tmp_path, record, None, {"status": "candidate_model_audited"}, destination=exported)
    text = exported.read_text(encoding="utf-8")
    assert "original-supplement.docx" in text and "frozen.docx" in text
    assert "Local dye name" in text and "8 [reported]" in text and "P2" in text and "J2" in text
    assert "table_index" in text and "physical_page" not in text
    assert "Trend visible, endpoint unclear" in text
    with pytest.raises(FileExistsError):
        review_packet(tmp_path, record, None, {"status": "candidate_model_audited"}, destination=exported)
    assert exported.read_text(encoding="utf-8") == text
