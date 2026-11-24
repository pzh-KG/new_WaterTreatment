"""Regressions for experimental ownership, research projections and literal candidate transport."""
import json

import pytest

from watertreatment.arm_research import check_research_contract, research_tables
from watertreatment.arm_transport import assemble_arms, stage_arms
from watertreatment.domain import ExtractionRecord
from watertreatment.ownership_audit import _dependencies, ownership_changed
from watertreatment.scientific_revision_audit import _entities


def fact(value):
    return {"value": value, "status": "reported", "evidence_ids": ["fig"]}


def fixture_record():
    window = {"end": fact(1), "unit": fact("h"), "kind": "endpoint", "reference": fact("illumination onset")}
    return ExtractionRecord.model_validate({"rule_version": "0.9", "schema_version": "0.5.0", "paper": {"id": "test"},
        "substances": [{"id": "active", "name": fact("active")}, {"id": "solute", "name": fact("solute")}],
        "materials": [{"id": "film", "role": "primary_final", "components": [{"substance_id": "active",
            "retention": "retained", "measurements": [{"basis": "local_composition", "quantity": {"name": "EDS", "fact": fact(7),
            "unit": fact("at.%")}, "sampled_region": fact("surface spot"), "evidence_ids": ["fig"]}]}]}],
        "configurations": [{"id": "cell", "streams": [{"id": "water", "kind": "reaction_liquid"}, {"id": "distillate", "kind": "condensate"}]}],
        "experiments": [{"id": "run", "arm_kind": "treatment", "comparison_group": "cmp", "material_ids": ["film"], "configuration_id": "cell",
            "targets": [{"substance_id": "solute"}], "reaction_inputs": [{"substance_id": "active", "role": "catalyst",
                "source": "material_bound", "evidence_ids": ["fig"]}], "joint_evidence": {"level": "J1", "evidence_ids": ["fig"]},
            "pairs": [{"left_observation_id": "flux", "right_observation_id": "concentration", "level": "P1", "evidence_ids": ["fig"]}]}],
        "evidence": [{"id": "fig", "source_id": "main", "block_id": "page"}],
        "observations": [{"id": "flux", "experiment_id": "run", "material_id": "film", "endpoint_kind": "evaporation_flux",
            "value": fact(2), "unit": fact("kg m-2 h-1"), "origin": "reported_measurement", "time_window": window},
            {"id": "concentration", "experiment_id": "run", "material_id": "film", "substance_id": "solute", "stream_id": "water",
            "endpoint_kind": "liquid_parent_ratio", "removal_attribution": "parent_disappearance_only", "value": fact(0.2),
            "unit": fact("C/C0"), "origin": "reported_measurement", "time_window": window}]})


def issues(r):
    messages = set()
    check_research_contract(r, lambda code, *_: messages.add(code))
    return messages


def test_retention_is_not_operating_dose_and_qualified_explicit_absence():
    from watertreatment.domain import ReactionInput
    r = fixture_record()
    exp = r.experiments[0]
    reaction = exp.reaction_inputs[0]
    reaction.source = "external"
    assert "EXTERNAL_DOSE_UNSUPPORTED" in issues(r)
    reaction.addition_evidence_ids = ["fig"]
    assert not issues(r)
    exp.reaction_inputs.append(ReactionInput(role="catalyst", substance_id="active", source="explicit_none", absence_scope="external_addition"))
    assert "ARM_SUPPLY_CONFLICT" in issues(r)
    reaction.source = "material_bound"
    reaction.addition_evidence_ids = []
    assert not issues(r)


def test_features_exclude_performance_and_design_contrasts_require_owned_ids():
    from watertreatment.domain import DesignComparison
    r = fixture_record()
    before = research_tables(r)["variant_features"]
    r.observations[0].value.value = 10
    assert research_tables(r)["variant_features"] == before
    assert before[0]["components"][0]["measurements"][0]["basis"] == "local_composition"
    assert before[0]["components"][0]["amount"]["value"] is None
    local = research_tables(r)["typed_composition_features"][0]
    assert local["feature_value"] == 7 and local["measurement_basis"] == "local_composition"
    r.materials[0].components[0].measurements[0].sampled_region.value = None
    assert research_tables(r)["typed_composition_features"][0]["feature_value"] is None
    r.comparisons = [DesignComparison.model_validate({"id": "c", "observation_ids": ["flux"],
        "contrasts": [{"baseline_observation_id": "flux", "modified_observation_id": "missing", "evidence_ids": ["fig"]}]})]
    assert "DESIGN_CONTRAST_ENDPOINT" in issues(r)


def test_flux_liquid_pair_requires_true_owner_target_stream_and_time():
    r = fixture_record()
    def row():
        return research_tables(r)["dual_performance_pairs"][0]
    assert row()["eligible"] and not row()["eligible_as_degradation"]
    r.experiments[0].pairs[0].unresolved_conditions = ["Absolute area was not independently reported."]
    assert row()["same_arm_co_observed"] and not row()["eligible"]
    r.experiments[0].pairs[0].unresolved_conditions = []
    o = r.observations[1]
    o.time_window.end.value = 60
    o.time_window.unit.value = "min"
    assert row()["eligible"]
    o.time_window.end.value = 30
    assert "time_basis_or_window" in row()["restrictions"]
    o.experiment_id = "other"
    assert "experimental_arm" in row()["restrictions"]
    o.stream_id = "distillate"
    assert "target_or_liquid_stream" in row()["restrictions"]
    o.endpoint_kind = "condensate_transfer"
    assert research_tables(r)["dual_performance_pairs"] == []


def test_dependency_review_includes_control_endpoints_but_leaves_unrelated_groups():
    r = fixture_record()
    exp, o = r.experiments[0], r.observations[1]
    for identifier, group in (("blank", "cmp"), ("separate", "other")):
        r.experiments.append(exp.model_copy(update={"id": identifier, "arm_kind": "control", "pairs": [], "comparison_group": group}))
        r.observations.append(o.model_copy(update={"id": identifier + "-out", "experiment_id": identifier}))
    a = r.model_dump(mode="json")
    r.experiments[0].reaction_inputs[0].source = "unknown"
    b = r.model_dump(mode="json")
    assert ownership_changed(a, b)
    affected, _, _ = _dependencies(_entities(a), _entities(b), {("experiments", 0)}, set())
    assert {("experiments", 1), ("observations", 2)} <= affected
    assert not {("experiments", 2), ("observations", 3)} & affected


def test_arm_wire_is_lossless_scope_bound_and_detects_tampering(tmp_path):
    r = fixture_record().model_dump(mode="json")
    a, b = tmp_path / "tool-0001", tmp_path / "tool-0002"
    a.mkdir()
    b.mkdir()
    arm = {"experiment": r["experiments"][0], "observations": [{k: v for k, v in o.items() if k != "experiment_id"} for o in r["observations"]]}
    part = stage_arms(a, "turn", [arm], {}, {})["arm_part_id"]
    base = {k: v for k, v in r.items() if k not in {"observations", "experiments"}}
    assert assemble_arms(b, "turn", base, [part]) == r
    with pytest.raises(ValueError, match="different candidate"):
        assemble_arms(b, "other", base, [part])
    with pytest.raises(ValueError, match="Duplicate"):
        assemble_arms(b, "turn", r, [part])
    (a / "arm_part.json").write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        assemble_arms(b, "turn", base, [part])


def test_labelled_time_coordinates_can_prove_a_pair_without_inventing_a_window():
    from watertreatment.domain import NamedFact, TimeWindow
    r = fixture_record()
    for o in r.observations:
        o.time_window = TimeWindow()
        o.coordinates = [NamedFact.model_validate({"name": "time", "fact": fact(1), "unit": fact("h"), "basis": fact("light-on")})]
    assert research_tables(r)["dual_performance_pairs"][0]["alignment"] == "same_time_point"
    r.observations[1].coordinates[0].basis.value = "dark-contact onset"
    assert not research_tables(r)["dual_performance_pairs"][0]["eligible"]


def test_audited_invalid_dose_repaired_only_in_its_own_group():
    from test_ownership_audit import ownership_case
    from test_scientific_revision_audit import build
    from watertreatment.domain import Component, ReactionInput, Substance
    from watertreatment.scientific_revision_audit import NEW_SCOPE_LIMITS
    old, _, audit = ownership_case()
    old.rule_version, old.schema_version = "0.9", "0.5.0"
    for exp in old.experiments:
        exp.arm_kind = "treatment"
    for o in old.observations:
        o.endpoint_kind = "other"
    old.substances.append(Substance(id="solid"))
    old.materials[0].components.append(Component(substance_id="solid", retention="retained", evidence_ids=["text"]))
    old.experiments[0].reaction_inputs.append(ReactionInput(role="catalyst", substance_id="solid", source="external", evidence_ids=["text"]))
    new = old.model_copy(deep=True)
    new.experiments[0].reaction_inputs[-1].source = "material_bound"
    assert issues(old) == {"EXTERNAL_DOSE_UNSUPPORTED"}
    assert issues(new) == set()
    scope = build(old, new, audit, **NEW_SCOPE_LIMITS)
    assert scope is not None
    assert scope["baseline_errors_for_source_review"][0]["code"] == "EXTERNAL_DOSE_UNSUPPORTED"
    assert "experiments[1]" not in {r["record_path"] for r in scope["entity_refs"] if r["role"] == "affected"}


def test_arm_columns_and_scope_assignments_expand_only_explicit_rows(tmp_path):
    a, b = tmp_path / "tool-0001", tmp_path / "tool-0002"
    a.mkdir()
    b.mkdir()
    rows = [{"id": name, "value": fact(i)} for i, name in enumerate(("o1", "o2"))]
    group = {"observation_ids": ["o1", "o2"], "layer": "core", "purpose": "performance", "reason": "Reported endpoints."}
    series = {"columns": {"unit": fact("mg/L"), "origin": "reported_measurement"}, "rows": rows}
    arm = {"experiment": {"id": "e", "arm_kind": "treatment"}, "observations": [], "observation_series": [series], "assignments": [group]}
    part = stage_arms(a, "s", [arm], {}, {})["arm_part_id"]
    record = assemble_arms(b, "s", {"rule_version": "0.9", "design_scope": {}}, [part])
    assert [o["value"] for o in record["observations"]] == [r["value"] for r in rows]
    assert [r["observation_id"] for r in record["design_scope"]["observation_assignments"]] == ["o1", "o2"]
    assert all(r["experiment_id"] == "e" for r in record["observations"])
    series["columns"]["stream_id"] = "other"
    with pytest.raises(ValueError, match="common measurement"):
        stage_arms(b, "s", [arm], {}, {})
    series["columns"].pop("stream_id")
    group["observation_ids"] = ["out-of-arm"]
    with pytest.raises(ValueError, match="owned by this exact arm"):
        stage_arms(b, "s", [arm], {}, {})


def test_native_core_view_exposes_research_tables_without_relabeling_archived_scope(tmp_path):
    from test_design_scope import design_record
    from watertreatment.core_view import build_native_core_view
    r = design_record()
    old = build_native_core_view(r, source_sha256="a" * 64)
    assert old["provenance"]["policy_version"] == "0.8"
    assert "dual_performance_pairs" not in old["analysis_tables"]
    r.rule_version, r.schema_version = "0.9", "0.5.0"
    r.experiments[0].arm_kind = "treatment"
    r.observations[0].endpoint_kind = "other"
    new = build_native_core_view(r, source_sha256="b" * 64)
    assert new["provenance"]["policy_version"] == "0.9"
    assert {"arm_reagents", "variant_features", "typed_composition_features",
            "dual_performance_pairs", "design_outcome_contrasts"} <= set(new["analysis_tables"])
    from watertreatment.core_view import _native_mapping, write_core_view_artifact
    from watertreatment.storage import hash_file
    source, mapping = tmp_path / "candidate.json", tmp_path / "mapping.json"
    source.write_text(r.model_dump_json(), encoding="utf-8")
    mapping.write_text(json.dumps(_native_mapping(r.model_dump(mode="json"), hash_file(source))), encoding="utf-8")
    manifest = write_core_view_artifact(source, mapping, tmp_path / "view")
    assert manifest["policy_version"] == "0.9"


def test_target_rows_keep_no_input_as_unspecified_and_do_not_transfer_other_targets_results():
    from watertreatment.domain import Target
    r = fixture_record()
    e = r.experiments[0]
    e.reaction_inputs = []
    e.targets.append(Target(substance_id="other"))
    a, b = research_tables(r)["pollutant_treatment_arms"]
    assert a["has_supported_treatment_response"] and a["input_inventory_status"] == "unspecified"
    assert a["input_ids_by_supply"] == {}
    assert not a["eligible_for_input_specific_lookup"]
    assert not b["has_supported_treatment_response"] and b["liquid_response_observation_ids"] == []
    r = fixture_record()
    def target_row():
        return research_tables(r)["pollutant_treatment_arms"][0]
    assert target_row()["eligible_for_input_specific_lookup"] and target_row()["response_material_ids"] == ["film"]
    r.experiments[0].reaction_inputs[0].source = "unknown"
    assert target_row()["has_supported_treatment_response"] and target_row()["unresolved_input_indices"] == [0]
    assert not target_row()["eligible_for_input_specific_lookup"]
    from watertreatment.domain import MaterialRole
    control = r.materials[0].model_copy(update={"id": "comparison", "role": MaterialRole.NECESSARY_CONTROL})
    r.materials.append(control)
    r.experiments[0].material_ids.append("comparison")
    for observation in r.observations:
        observation.material_id = "comparison"
    assert not target_row()["has_supported_treatment_response"]


def test_whole_installed_catalyst_needs_no_synthetic_self_component():
    from watertreatment.domain import ReactionInput
    r = fixture_record()
    rx = r.experiments[0].reaction_inputs[0]
    rx.material_id, rx.substance_id = "film", None
    assert not issues(r)
    rx.material_id = "unregistered"
    assert "BOUND_SUPPLY_UNSUPPORTED" in issues(r)
    rx.material_id = "film"
    r.experiments[0].reaction_inputs.extend([
        ReactionInput(role="oxidant", source="explicit_none", absence_scope="external_addition"),
        ReactionInput(role="oxidant", substance_id="solute", source="external", evidence_ids=["fig"])])
    assert "ARM_SUPPLY_CONFLICT" in issues(r)


def test_initial_schema_signatures_and_unused_column_layouts_keep_all_values(tmp_path):
    from watertreatment.arm_transport import candidate_schema_outline
    catalog = candidate_schema_outline(ExtractionRecord.model_json_schema())["definitions"]
    assert catalog["Observation"]["experiment_id*"] == "string"
    assert "liquid_parent_ratio" in catalog["Observation"]["endpoint_kind"]
    assert "material_bound" in catalog["InputSource"]
    part = {"experiment": {"id": "r", "arm_kind": "unresolved"}, "observations": []}
    receipt = stage_arms(tmp_path, "turn", [part], {}, {"other_series": ["id", "value"]})
    assert receipt["transport"]["unused_column_layouts"] == ["other_series"]
    assert json.loads((tmp_path / "arm_part.json").read_bytes())["observations"] == []
    with pytest.raises(ValueError, match="unused shared"):
        stage_arms(tmp_path, "turn", [part], {"unclaimed": 1}, {})


def test_stream_names_are_bound_only_by_the_explicit_configuration():
    from watertreatment.domain import Configuration, Experiment, Observation, Stream
    from watertreatment.stream_identity import bind_stream_identifiers
    r = fixture_record()
    r.configurations.append(Configuration(id="collection", streams=[Stream(id="water", kind="condensate")]))
    r.experiments.append(Experiment(id="collected", configuration_id="collection", arm_kind="treatment"))
    r.observations.append(Observation(id="dist", experiment_id="collected", stream_id="water"))
    r.observations.append(Observation(id="unassigned", experiment_id="missing", stream_id="water"))
    before = list(o.value.model_dump() for o in r.observations)
    proof = bind_stream_identifiers(r)
    assert proof["references"] == {"definitions": 2, "observations": 2}
    assert r.observations[1].stream_id == r.configurations[0].streams[0].id != r.configurations[1].streams[0].id
    assert r.observations[-2].stream_id == r.configurations[1].streams[0].id
    assert r.observations[-1].stream_id == "water"
    assert before == [o.value.model_dump() for o in r.observations]
    assert bind_stream_identifiers(r) is None
    r = fixture_record()
    r.configurations[0].streams.append(Stream(id="water", kind="condensate"))
    assert bind_stream_identifiers(r) is None


def test_reported_plot_clock_is_queryable_with_its_unknown_averaging_window():
    from watertreatment.quantitative import has_usable_coordinates
    r = fixture_record()
    flux = r.observations[0]
    flux.time_window.kind = "plot_coordinate"
    assert has_usable_coordinates(flux, "time_series", observation_window=True)
    assert not has_usable_coordinates(flux, "time_series")
    pair = research_tables(r)["dual_performance_pairs"][0]
    assert pair["same_arm_co_observed"] and not pair["eligible"]
    assert pair["alignment"] == "same_reported_coordinate_only"
    flux.time_window.end.evidence_ids = []
    assert not has_usable_coordinates(flux, "time_series", observation_window=True)


def test_local_review_prompt_shares_only_identical_literals_and_round_trips():
    from watertreatment.review_transport import POLICY, compact_review
    fact_row = {"value": 6, "status": "estimated", "unit": "mg/L", "$same": "the literal source label",
                "evidence": ["the directly located original result in the relevant method table"]}
    different = {**fact_row, "value": -6}
    packet = {"prompt_transport": POLICY, "changes": [fact_row, different], "current_targets": {"left": fact_row, "right": different},
              "sources": ["originals are left directly readable"]}
    transported = compact_review(packet)
    marker, table = transported["literal_reference_key"], transported["shared_exact_literals"]
    assert marker != "$same" and len(table) == 2 and transported["sources"] == packet["sources"]
    def expand(value):
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return table[value[marker]] if set(value) == {marker} else {k: expand(v) for k,v in value.items()}
        return value
    for k in ("changes", "current_targets"):
        assert expand(transported[k]) == packet[k]


def test_native_review_accepts_only_an_added_quote_in_its_original_block(monkeypatch):
    from types import SimpleNamespace
    from watertreatment.focused_review_packet import build
    from watertreatment.scientific_revision_audit import ScientificScopeRejected
    old = fixture_record()
    ev = old.evidence[0]
    ev.quote = None
    block = SimpleNamespace(block_id=ev.block_id, text="The located specimen retained the original coating.",
                            kind="paragraph", location=SimpleNamespace(physical_page=1))
    doc = SimpleNamespace(source=SimpleNamespace(source_id=ev.source_id, format="pdf"),blocks=[block])
    parent = SimpleNamespace(before=old, documents=[doc], bundle={}, findings=[])
    monkeypatch.setattr("watertreatment.focused_review_packet.reading_groups", lambda p: ({},[]))
    proposed = old.model_copy(deep=True)
    proposed.evidence[0].quote = "specimen retained the original coating"
    packet, _, _ = build(parent, proposed, version=4)
    assert any(f.get("locator_change") for f in packet["findings"])
    assert packet["required_group_sources"] and packet["prompt_transport"]
    proposed.evidence[0].quote = "an invented new concentration"
    with pytest.raises(ValueError, match="quotation"):
        build(parent, proposed, version=4)
    old.evidence[0].quote = "original coating"
    with pytest.raises(ScientificScopeRejected):
        build(parent, proposed, version=4)


def test_public_arm_checkpoint_matches_saved_command_and_ignores_only_controller_clock(tmp_path):
    from watertreatment.arm_transport import arm_checkpoint_response
    from watertreatment.storage import hash_file, write_new_json
    tool = "wt_submit_record"
    args = {"arms": [{"experiment":{"id":"a"}, "observations":[]}]}
    folder = tmp_path / "tool-0002"
    folder.mkdir()
    part = folder / "arm_part.json"
    write_new_json(part, {"scope":"turn","experiments":[{"id":"a"}],"observations":[]})
    payload = {"arm_part_id":folder.name+"-"+hash_file(part), "candidate_complete":False}
    write_new_json(folder/"request.json", {"tool":tool,"arguments":args})
    write_new_json(folder/"result.json", {"success":True,"data":payload})
    def reply(data):
        return {"type":"inputText","text":json.dumps(data)}
    result = {"contentItems":[reply(payload),reply({"budget_source":"controller_monotonic_deadline","remaining_turn_seconds":30})]}
    assert arm_checkpoint_response(tool,args,result,tmp_path)
    assert not arm_checkpoint_response(tool,{"arms":[]},result,tmp_path)
    assert not arm_checkpoint_response(tool,args,{"contentItems":[reply(payload),reply({"submission_id":"foreign"})]},tmp_path)
    part.write_text("{}",encoding="utf-8")
    assert not arm_checkpoint_response(tool,args,result,tmp_path)


def test_replay_final_header_may_share_unused_arm_column_names(tmp_path):
    from watertreatment.arm_transport import replay_arm_assembly
    from watertreatment.storage import digest_json, write_new_json
    record = fixture_record().model_dump(mode="json")
    first, last = tmp_path / "tool-0001", tmp_path / "tool-0002"
    first.mkdir()
    last.mkdir()
    part = stage_arms(first, "turn", [{"experiment":record["experiments"][0],
                      "observations":record["observations"]}], {}, {})
    write_new_json(first / "result.json", {"success":True,"data":part})
    base = {key:value for key,value in record.items() if key not in {"experiments","observations"}}
    write_new_json(last / "arm_assembly.json", {"scope":"turn","expanded_sha256":digest_json(record)})
    write_new_json(last / "expanded_record_input.json", record)
    args = {"record":base,"arm_part_ids":[part["arm_part_id"]],"field_layouts":{"arm_row":["id","value"]}}
    assert replay_arm_assembly(last, args) == record
    with pytest.raises(ValueError):
        replay_arm_assembly(last, {**args,"shared_values":{"unused_scientific_value":3}})
