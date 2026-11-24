from watertreatment.domain import (
    Configuration, CoverageEntry, EvidenceRef, Experiment, ExtractionRecord, Fact,
    MaterialSample, MaterialState, Observation, ObservationPair, Paper, ReactionInput, Substance,
)
from watertreatment.validation import validate_record


def reported(value, evidence="ev"):
    return Fact(value=value, status="reported", evidence_ids=[evidence])


def record():
    return ExtractionRecord(
        paper=Paper(id="paper", title=reported("Synthetic study"), source_ids=["main"]),
        materials=[MaterialSample(id="final", role="primary_final", name=reported("Composite"))],
        substances=[Substance(id="oxidant", name=reported("Oxidant"))],
        configurations=[Configuration(id="open", material_ids=["final"], boundary=reported("open"))],
        experiments=[Experiment(id="experiment", material_ids=["final"], configuration_id="open", purpose="coupled")],
        observations=[Observation(id="evap", experiment_id="experiment", material_id="final",
            value=reported(2.1), unit=reported("kg/m2/h"), origin="reported_measurement")],
        evidence=[EvidenceRef(id="ev", source_id="main", block_id="main:b1", quote="Synthetic study")])


def errors(value, docs=()):
    return [x for x in validate_record(value, docs) if x.severity == "error"]


def test_unknown_conditions_do_not_reject_an_otherwise_usable_paper():
    value = record()
    value.observations[0].volume_correction = Fact(status="not_reported")
    value.experiments[0].physical_run_id = Fact(status="not_reported")
    assert errors(value) == []
    empty = validate_record(ExtractionRecord(paper=Paper(id="paper")))
    assert {x.code for x in empty} >= {"NO_FINAL_MATERIAL", "NO_OBSERVATIONS"}
    assert all(x.severity == "warning" for x in empty)


def test_real_source_binding_catches_cross_file_block_and_wrong_page():
    value = record()
    docs = [{"source": {"source_id": "main", "page_count": 2, "format": "pdf"},
             "blocks": [{"source_id": "main", "block_id": "main:b1", "text": "Synthetic study",
                         "location": {"physical_page": 1}}]}]
    assert errors(value, docs) == []
    value.evidence[0].locator.physical_page = 2
    assert "LOCATOR_BLOCK_MISMATCH" in {x.code for x in errors(value, docs)}
    value.evidence[0].block_id = "supplement:b1"
    assert "MISSING_SOURCE_BLOCK" in {x.code for x in errors(value, docs)}


def test_wrong_material_assignment_is_error_even_when_numeric_value_is_valid():
    value = record()
    value.materials.append(MaterialSample(id="other_variant", role="optimized_variant"))
    value.observations[0].material_id = "other_variant"
    assert "OBSERVATION_MATERIAL_MISMATCH" in {x.code for x in errors(value)}


def test_cannot_pair_open_evaporation_with_closed_purification_as_same_conditions():
    value = record()
    value.configurations.append(Configuration(id="closed", material_ids=["final"], boundary=reported("closed")))
    value.experiments.append(Experiment(id="closed_test", material_ids=["final"], configuration_id="closed"))
    value.observations.append(Observation(id="removal", experiment_id="closed_test", material_id="final",
        value=reported(97.8), origin="reported_measurement"))
    value.experiments[0].pairs = [ObservationPair(left_observation_id="evap", right_observation_id="removal",
        level="P2", evidence_ids=["ev"])]
    assert "PAIR_CONDITION_MISMATCH" in {x.code for x in errors(value)}
    value.experiments[0].pairs[0].level = "P4"
    assert errors(value) == []


def test_different_oxidant_optima_are_not_a_joint_best_performance_pair():
    value = record()
    value.experiments[0].reaction_inputs = [ReactionInput(substance_id="oxidant", role="oxidant", source="external",
        amount=reported(1), unit=reported("mM"))]
    second = value.experiments[0].model_copy(deep=True)
    second.id = "second"
    second.reaction_inputs[0].amount = reported(4)
    value.experiments.append(second)
    value.observations.append(Observation(id="catalysis", experiment_id="second", material_id="final",
        value=reported(90), origin="reported_measurement"))
    value.experiments[0].pairs = [ObservationPair(left_observation_id="evap", right_observation_id="catalysis", level="P1", evidence_ids=["ev"])]
    issues = errors(value)
    assert any(x.code == "PAIR_CONDITION_MISMATCH" and "reaction_input_amount" in x.message for x in issues)


def test_shared_time_course_does_not_require_invented_physical_run_id():
    value = record()
    value.observations.append(Observation(id="removal", experiment_id="experiment", material_id="final",
        value=reported(91), origin="reported_measurement"))
    value.experiments[0].joint_evidence.level = "J1"
    value.experiments[0].joint_evidence.evidence_ids = ["ev"]
    value.experiments[0].pairs = [ObservationPair(left_observation_id="evap", right_observation_id="removal",
        level="P1", evidence_ids=["ev"])]
    assert errors(value) == []


def test_model_inference_cannot_be_presented_as_measured_performance():
    value = record()
    value.observations[0].value = Fact(value=4.5, status="inferred", note="Cross-material transfer hypothesis")
    assert "INFERENCE_AS_OBSERVATION" in {x.code for x in errors(value)}
    value.observations[0].origin = "model_inference"
    assert errors(value) == []


def test_parsing_and_reading_coverage_are_not_interchangeable():
    value = record()
    value.coverage = [CoverageEntry(source_id="main", block_ids=["main:b1"], state="parsed_unread", text_read=True)]
    assert "COVERAGE_STATE_CONFLICT" in {x.code for x in errors(value)}


def test_precursor_performance_is_not_final_coupled_performance():
    value = record()
    value.materials[0].role = "precursor"
    assert "NONFINAL_COUPLED_ATTRIBUTION" in {x.code for x in errors(value)}
    value.experiments[0].purpose = "characterization"
    assert errors(value) == []


def test_missing_evidence_and_false_missing_value_are_local_errors():
    value = record()
    value.observations[0].value = Fact(value=0, status="not_reported", evidence_ids=["invented"])
    codes = {x.code for x in errors(value)}
    assert {"MISSING_EVIDENCE", "VALUE_STATE_CONFLICT"} <= codes


def test_same_formula_different_working_states_are_not_same_condition_pair():
    value = record()
    value.experiments[0].material_states = [MaterialState(material_id="final", state=reported("dry"))]
    second = value.experiments[0].model_copy(deep=True)
    second.id = "wet_test"
    second.material_states[0].state = reported("wet")
    value.experiments.append(second)
    value.observations.append(Observation(id="wet_rate", experiment_id="wet_test", material_id="final", value=reported(3), origin="reported_measurement"))
    value.experiments[0].pairs = [ObservationPair(left_observation_id="evap", right_observation_id="wet_rate", level="P2", evidence_ids=["ev"])]
    assert any(x.code == "PAIR_CONDITION_MISMATCH" and "material_state" in x.message for x in errors(value))


def test_word_location_is_bound_to_original_paragraph_not_invented_page():
    value = record()
    value.evidence[0].locator.paragraph_index = 9
    docs = [{"source": {"source_id": "main", "format": "docx", "page_count": None},
             "blocks": [{"block_id": "main:b1", "source_id": "main", "text": "Synthetic study", "location": {"paragraph_index": 4}}]}]
    assert "LOCATOR_BLOCK_MISMATCH" in {x.code for x in errors(value, docs)}


def test_source_inventory_omission_is_visible_without_erasing_valid_observations():
    value = record()
    docs = [{"source": {"source_id": "main"}, "blocks": [{"source_id": "main", "block_id": "main:b1"}]},
            {"source": {"source_id": "supplement"}, "blocks": []}]
    issues = validate_record(value, docs)
    assert any(x.code == "SOURCE_INVENTORY_INCOMPLETE" and x.severity == "warning" for x in issues)
    assert not [x for x in issues if x.severity == "error"]


def test_unknown_dose_units_are_not_assumed_equal_to_fabricate_a_conflict():
    value = record()
    value.experiments[0].reaction_inputs = [ReactionInput(substance_id="oxidant", role="oxidant", amount=reported(1))]
    second = value.experiments[0].model_copy(deep=True)
    second.id = "second"
    second.reaction_inputs[0].amount = reported(1000)
    value.experiments.append(second)
    value.observations.append(Observation(id="second_value", experiment_id="second", material_id="final", value=reported(80), origin="reported_measurement"))
    value.experiments[0].pairs = [ObservationPair(left_observation_id="evap", right_observation_id="second_value", level="P2", evidence_ids=["ev"], unresolved_conditions=["oxidant dose units"])]
    assert errors(value) == []
    assert any(x.code == "CROSS_EXPERIMENT_PAIR_REVIEW" for x in validate_record(value))


def test_two_final_components_can_share_a_coupled_assembly_without_merging_identity():
    value = record()
    value.materials.append(MaterialSample(id="second_component", role="final_component"))
    value.configurations[0].material_ids.append("second_component")
    value.experiments[0].material_ids.append("second_component")
    value.observations.append(Observation(id="component_catalysis", experiment_id="experiment", material_id="second_component", value=reported(90), origin="reported_measurement"))
    value.experiments[0].pairs = [ObservationPair(left_observation_id="evap", right_observation_id="component_catalysis", level="P1", evidence_ids=["ev"])]
    assert errors(value) == []
    assert any(x.code == "ASSEMBLY_PAIR_SCOPE" for x in validate_record(value))
