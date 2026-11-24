import json
from types import SimpleNamespace

import pytest

from watertreatment.domain import Fact
from watertreatment.focused_review_packet import FocusedReview, correction_result
from watertreatment.native_repair_review import _denied_request
from watertreatment.ownership_audit import _changes
from watertreatment.reading_tools import ReadingTools
from watertreatment.retained_review_packet import ReviewDecision
from watertreatment.scientific_revision_audit import ScientificScopeRejected
from watertreatment.storage import digest_json, hash_file, write_new_json
from test_scientific_revision_audit import fixture


def test_inherited_evidence_in_focused_packets_is_bound_to_its_own_findings(monkeypatch):
    from watertreatment.focused_review_packet import build
    from watertreatment.retained_review_packet import finish, prompt
    old,new,audit=fixture()
    doc=SimpleNamespace(source=SimpleNamespace(source_id="source",format="docx"),blocks=[
        SimpleNamespace(block_id=f"page{i}",kind="paragraph",text="Printed source passage.",
                        location=SimpleNamespace(physical_page=i)) for i in (1,2)])
    findings=[{"text":"Reconcile each prior source with the endpoint.","paths":["observations[0].value"],
               "required_evidence_ids":["text","plot"],"evidence":[]}]
    parent=SimpleNamespace(before=old,bundle={},documents=[doc],findings=findings,audit=audit,dossier=SimpleNamespace(coverage=[]))
    monkeypatch.setattr("watertreatment.focused_review_packet.reading_groups",lambda p:({},[]))
    monkeypatch.setattr("watertreatment.retained_review_packet.reading_groups",lambda p:({"scope_sha256":"a"*64},[]))
    monkeypatch.setattr("watertreatment.retained_review_packet.corrected_dossier",lambda *args:parent.dossier)
    packet,_,_=build(parent,new,version=5)
    assert set(packet["inherited_references"])=={"text","plot"}
    evidence=packet["required_group_sources"]["F1"]
    assert set(evidence)=={c["id"] for c in packet["sources"]}
    assert "list the group" in prompt(packet)
    report=FocusedReview(packet_hash=packet["packet_hash"],decisions=[ReviewDecision(
        groups=["F1",*(c["id"] for c in packet["changes"])],verdict="limited",source_ids=evidence,
        explanation="Both cited passages were checked for this exact endpoint.")],reading=[],
        reconciled_source_groups=[],additional_issues=[],restrictions=[],final_corrections=[])
    with pytest.raises(ValueError,match="explicitly reconcile"):
        finish(parent,new,packet,report)
    # The archived format explicitly requested an empty reconciliation list. Each actual
    # listed original must then have been cited in THIS finding's own signed disposition.
    packet.update(version=4,inherited_references={})
    assert not finish(parent,new,packet,report)[2]
    packet["sources"][1]["kind"]="image"
    with pytest.raises(ValueError,match="explicitly reconcile"):
        finish(parent,new,packet,report)
    packet["sources"][1]["image"]=1
    report.decisions[0].source_ids=evidence[:1]
    with pytest.raises(ValueError,match="explicitly reconcile"):
        finish(parent,new,packet,report)
    packet.update(version=7)
    packet["findings"][0].update(required_evidence_ids=[],native_group_ids=["C2","F9"],evidence=[
        {"source_id":c["source"],"block_id":c["block"]} for c in packet["sources"]])
    report.reconciled_source_groups=["F1"]
    with pytest.raises(ValueError,match="explicitly reconcile"):
        finish(parent,new,packet,report)
    report.decisions[0].source_ids=evidence
    assert not finish(parent,new,packet,report)[2]


def test_separating_a_verified_blank_never_claims_scientific_reading():
    from test_coverage_correction import fixture as source_fixture
    from watertreatment.domain import CoverageEntry
    from watertreatment.repaired_baseline import canonical_proposal
    dossier,documents=source_fixture()
    source=documents[0]
    old,_,_=fixture()
    old.coverage=[CoverageEntry(source_id=source.source.source_id,block_ids=[source.blocks[0].block_id],
                               state="out_of_scope",text_read=True)]
    revised=old.model_copy(deep=True)
    parent=SimpleNamespace(before=old,candidate=revised,dossier=dossier,documents=documents)
    revised.coverage[0].block_ids.append(source.blocks[1].block_id)
    canonical=canonical_proposal(parent)
    assert canonical.coverage[0]==old.coverage[0]
    assert canonical.coverage[1].block_ids==[source.blocks[1].block_id]
    assert not canonical.coverage[1].text_read and not canonical.coverage[1].visual_checked
    revised.coverage[0].block_ids[-1]=source.blocks[4].block_id
    with pytest.raises(ValueError,match="unverified block"):
        canonical_proposal(parent)


def test_single_residual_preserves_every_other_field_and_every_negative_group(tmp_path):
    from watertreatment.focused_review_packet import named_residual, _residual_finding
    old,_,_=fixture()
    new=old.model_copy(deep=True)
    new.observations[0].value.note="Only the reported measurement is comparable."
    parent=SimpleNamespace(plan={"workflow":"retained-issue-source-review-v1"},run=tmp_path,candidate=old)
    path=tmp_path/"calls"/"call-1"/"answer.json"
    report={"decisions":[{"groups":["F2","C1"],"verdict":"requires_revision","explanation":"One source question remains."}],
            "additional_issues":["F2 and C1 concern observations[0].value."]}
    write_new_json(path,report)
    before,updated=(r.model_dump(mode="json") for r in (old,new))
    assert _residual_finding(parent,before,updated)["paths"]==["observations[0].value"]
    updated["observations"][1]["limitations"]=["A second edit."]
    with pytest.raises(ValueError,match="outside"):
        _residual_finding(parent,before,updated)
    report["additional_issues"]=["F2 concerns observations[0].value."]
    path.write_text(json.dumps(report),encoding="utf8")
    assert named_residual(parent) is None


def test_validator_replay_order_does_not_hide_missing_or_modified_diagnostics():
    from watertreatment.retained_review import _canonical_parent_receipt
    def receipt(rows):
        return {"baseline":{"other":"retained","repaired_baseline_checks":{"reading":[],"validation":rows}}}
    a,b={"path":"scope","message":"First finding"},{"path":"scope","message":"Second finding"}
    assert _canonical_parent_receipt(receipt([a,b]))==_canonical_parent_receipt(receipt([b,a]))
    assert _canonical_parent_receipt(receipt([a,b]))!=_canonical_parent_receipt(receipt([a,a]))


def test_signed_pair_note_requires_the_actual_original_pair_and_its_evidence():
    from watertreatment.focused_review_packet import _check_dependency_text
    pair={"left_observation_id":"first","right_observation_id":"second","level":"P1","note":"Same chart window.",
          "evidence_ids":["plot"]}
    data={"experiments":[{"pairs":[pair]}],"evidence":[{"id":"plot","source_id":"main","block_id":"p"}]}
    packet={"current_targets":{"experiments[0]":[dict(pair)]},"sources":[{"id":"S1","source":"main","block":"p"}]}
    args=(data,packet,{"paths":["experiments[0]"]},("experiments",0,"pairs",0,"note"),SimpleNamespace(source_ids=["S1"]),"Common time only.")
    _check_dependency_text(*args)
    packet["current_targets"]["experiments[0]"][0]["right_observation_id"]="foreign"
    with pytest.raises(ValueError,match="exact original pair"):
        _check_dependency_text(*args)


def test_on_demand_word_navigation_freezes_only_original_registered_occurrences(tmp_path):
    from watertreatment.retained_review import _on_demand_word_previews
    selected,extra,changed=(tmp_path/name for name in ("included.png","on-demand.png","changed.png"))
    for file in (selected,extra,changed):
        file.write_bytes(file.name.encode())
    rows=[{"block_id":p.stem,"path":str(p),"sha256":hash_file(p)} for p in (selected,extra,changed)]
    doc=SimpleNamespace(source=SimpleNamespace(format="docx",source_id="si"),
                        blocks=[SimpleNamespace(block_id=row["block_id"],kind="image") for row in rows])
    parent=SimpleNamespace(documents=[doc],bundle={"renderings":[{"source_id":"si","images":rows}]})
    settings=SimpleNamespace(data_root=tmp_path)
    changed.write_bytes(b"different preview")
    assert set(_on_demand_word_previews(settings,parent,[selected]))=={selected,extra}
    with pytest.raises(ValueError,match="registered original"):
        _on_demand_word_previews(settings,parent,[changed])
    parent.bundle["renderings"][0]["source_id"]="foreign"
    assert not _on_demand_word_previews(settings,parent,[])


def test_primary_replacement_requires_its_own_source_and_separate_mode():
    old,_,_=fixture()
    revised=old.model_copy(deep=True)
    obs=revised.observations[0]
    obs.value=Fact(value=71,status="reported",evidence_ids=["text"])
    a,b=old.model_dump(mode="json"),revised.model_dump(mode="json")
    with pytest.raises(ScientificScopeRejected,match="primary value/unit"):
        _changes(a,b)
    delta=_changes(a,b,adjudicate_outcomes=True)
    assert [x["changed_path"] for x in delta]==["observations[0].value"]
    obs.value.evidence_ids=[]
    obs.evidence_links=[]
    with pytest.raises(ScientificScopeRejected,match="explicit source"):
        _changes(a,revised.model_dump(mode="json"),adjudicate_outcomes=True)


def test_last_denied_patch_replays_the_same_native_turn_and_rejects_foreign_or_successful_receipts(tmp_path):
    run=tmp_path
    call={"thread_id":"t","turn_id":"r"}
    cmd={"tool":"wt_patch_record","arguments":{"base_hash":"candidate","edits":[]}}
    result={"success":False,"contentItems":[{"type":"inputText","text":"source and revision pool exhausted"}]}
    wire={**result,"contentItems":[*result["contentItems"],{"type":"inputText","text":json.dumps(
        {"remaining_turn_seconds":5,"budget_source":"controller_monotonic_deadline"})}]}
    rows=[
        ("receive",{"method":"turn/started","params":{"threadId":"t","turn":{"id":"r"}}}),
        ("receive",{"id":18,"method":"item/tool/call","params":{"threadId":"t","turnId":"r",**cmd}}),
        ("send",{"id":18,"result":wire}),
        ("receive",{"method":"turn/completed","params":{"threadId":"t","turn":{"id":"r","status":"completed"}}})]
    events=run/"app_server"/"events.jsonl"
    events.parent.mkdir()
    def save():
        events.write_text("".join(json.dumps({"direction":d,"message":m})+"\n" for d,m in rows),encoding="utf-8")
    save()
    path=run/"reading_tools"/"tool-0001"/"request.json"
    write_new_json(path,cmd)
    write_new_json(path.with_name("result.json"),result)
    matched,receipt=_denied_request(run,call)
    assert matched==cmd and receipt["denied_request_sha256"]==hash_file(path)
    with pytest.raises(ValueError):
        _denied_request(run,{"thread_id":"t","turn_id":"another"})
    rows[2][1]["result"]={**wire,"success":True}
    save()
    with pytest.raises(ValueError):
        _denied_request(run,call)


def test_final_review_cannot_resolve_a_primary_change_using_an_unrelated_source():
    record,_,_=fixture()
    packet={"changes":[{"id":"C1","changes":[["observations[0].value",None,{}]]}],
            "findings":[],"primary_source_cards":{"observations[0].value":["S1"]}}
    row=ReviewDecision(groups=["C1"],verdict="limited",source_ids=["S2"],explanation="The named source only supports an estimate.")
    report=FocusedReview(packet_hash="X",decisions=[row],reading=[],reconciled_source_groups=[],
                         additional_issues=[],restrictions=[],final_corrections=[])
    with pytest.raises(ValueError,match="own original source"):
        correction_result(record,packet,report)
    row.source_ids=["S1"]
    assert correction_result(record,packet,report)==record


def test_rejected_tail_requires_the_same_prior_success_and_exact_original_rejection(tmp_path):
    turn={"threadId":"t","turnId":"r"}
    call={"thread_id":"t","turn_id":"r"}
    previous="tool-0001-digest"
    answer={"submission_id":previous}
    command={"tool":"wt_patch_record","arguments":{"base_hash":"bound","edits":[]}}
    denial={"success":False,"contentItems":[{"type":"inputText","text":"grouped edits cannot overlap; submit sequential batches explicitly"}]}
    rows=[("receive",{"method":"turn/started","params":{"threadId":"t","turn":{"id":"r"}}}),
          ("receive",{"id":1,"method":"item/tool/call","params":{**turn,**command}}),
          ("send",{"id":1,"result":{"success":True,"contentItems":[{"type":"inputText","text":json.dumps(answer)}]}}),
          ("receive",{"id":2,"method":"item/tool/call","params":{**turn,**command}}),("send",{"id":2,"result":denial}),
          ("receive",{"method":"item/completed","params":{**turn,"item":{"type":"agentMessage","phase":"final_answer","text":json.dumps(answer)}}}),
          ("receive",{"method":"turn/completed","params":{"threadId":"t","turn":{"id":"r","status":"completed"}}})]
    events=tmp_path/"app_server/events.jsonl"
    events.parent.mkdir()
    events.write_text("".join(json.dumps({"direction":d,"message":m})+"\n" for d,m in rows),encoding="utf8")
    tool=tmp_path/"reading_tools/tool-0002/request.json"
    write_new_json(tool,command)
    write_new_json(tool.with_name("result.json"),denial)
    assert _denied_request(tmp_path,call,submitted=previous)[0]==command
    with pytest.raises(ValueError):
        _denied_request(tmp_path,call,submitted="other")
    with pytest.raises(ValueError):
        _denied_request(tmp_path,call)
    tool.write_text(json.dumps({**command,"arguments":{"base_hash":"changed"}}),encoding="utf8")
    with pytest.raises(ValueError,match="does not exactly match"):
        _denied_request(tmp_path,call,submitted=previous)


def test_rebasing_a_rejected_proposal_preserves_later_source_judgments():
    from watertreatment.native_repair_review import _merge_reviewed_fields
    base=[{"id":"a","note":"before","metric":1}]
    reviewed=[{"id":"a","note":"source qualification","metric":1}]
    proposed=[{"id":"a","note":"before","metric":2},{"id":"b","metric":3}]
    assert _merge_reviewed_fields(base,reviewed,proposed)==[dict(reviewed[0],metric=2),proposed[1]]
    with pytest.raises(ValueError,match="overlaps"):
        _merge_reviewed_fields(base,reviewed,[dict(base[0],note="other reading")])


def test_structured_source_judgment_remains_pending_and_does_not_replace_the_record():
    from watertreatment.focused_review_packet import pending_structured_corrections
    from watertreatment.retained_review import _reviewed_candidate
    record,_,_=fixture()
    before=record.model_dump(mode="json")
    field="observations[0].time_window"
    packet={"version":4,"changes":[],"primary_source_cards":{},"findings":[{"id":"F1","paths":[field]}]}
    correction={"group":"F1","path":field,"replacement_json":'{"end":{"value":2,"status":"estimated"}}',
                "source_ids":["S1"],"explanation":"The elapsed-time marker gives an approximate end."}
    report=FocusedReview(packet_hash="a",decisions=[ReviewDecision(groups=["F1"],verdict="limited",source_ids=["S1"],
                         explanation="The figure gives a time coordinate.")],reading=[],reconciled_source_groups=[],
                         additional_issues=[],restrictions=[],final_corrections=[correction])
    retained,pending=_reviewed_candidate(SimpleNamespace(documents=[]),record,packet,report)
    assert retained.model_dump(mode="json")==before and pending==report.final_corrections
    report.final_corrections[0].source_ids=["S2"]
    assert not pending_structured_corrections(record,packet,report)
    report.final_corrections[0].source_ids=["S1"]
    report.final_corrections[0].path="observations[1].time_window"
    assert not pending_structured_corrections(record,packet,report)


def test_signed_missing_input_qualifiers_remain_proposals_until_independent_review():
    from watertreatment.domain import ReactionInput
    from watertreatment.focused_review_packet import pending_structured_corrections
    from watertreatment.retained_review import _reviewed_candidate

    record, _, _ = fixture()
    ref = record.evidence[0]
    record.experiments[0].reaction_inputs = [
        ReactionInput(role="oxidant", source="external", unit=Fact(value="mM", status="reported"))]
    base = record.model_dump(mode="json")
    group = "experiments[0].reaction_inputs"
    packet = {"version": 4, "changes": [], "primary_source_cards": {},
              "sources": [{"id": "S1", "source": ref.source_id, "block": ref.block_id}],
              "findings": [{"id": "A1", "paths": [group]}]}
    proposal = {"status": "not_reported", "evidence_ids": [ref.id], "note": "No benchmark dose stated."}
    correction = {"group": "A1", "path": group + "[0].unit", "replacement_json": json.dumps(proposal),
                  "source_ids": ["S1"], "explanation": "Source identifies addition only."}
    report = FocusedReview(packet_hash="a", decisions=[ReviewDecision(groups=["A1"], verdict="limited",
        source_ids=["S1"], explanation="Benchmark addition verified; fixed dose unresolved.")],
        reading=[], reconciled_source_groups=[], additional_issues=[], restrictions=[], final_corrections=[correction])
    current, pending = _reviewed_candidate(SimpleNamespace(documents=[]), record, packet, report)
    assert current.model_dump(mode="json") == base and pending == report.final_corrections
    for invalid in (dict(proposal, evidence_ids=["unknown"]), dict(proposal, value=3), dict(proposal, status="reported")):
        report.final_corrections[0].replacement_json = json.dumps(invalid)
        assert not pending_structured_corrections(record, packet, report)
    report.final_corrections[0].replacement_json = json.dumps(proposal)
    record.experiments[0].reaction_inputs[0].amount = Fact(value=3, status="reported")
    assert not pending_structured_corrections(record, packet, report)


def test_deferred_proposal_binds_previous_report_and_exact_original_blocks(tmp_path):
    from watertreatment.retained_review import _deferred_proposal
    current=fixture()[0].model_dump(mode="json")
    write_new_json(tmp_path/"local_review_packet.json",{"sources":[{"id":"S1","source":"si","block":"figure"}]})
    pending=[{"group":"F1","path":"observations[0].reference","source_ids":["S1"],
              "replacement_json":'{"status":"not_reported","note":"The denominator was not supplied."}'}]
    write_new_json(tmp_path/"result.json",{"deferred_source_review_corrections":pending})
    suggestion=_deferred_proposal(SimpleNamespace(run=tmp_path),current,pending)
    assert suggestion["base_hash"]==digest_json(current) and suggestion["source_blocks"]==[["si","figure"]]
    assert suggestion["proposal_origin"]["result_sha256"]==hash_file(tmp_path/"result.json")
    assert suggestion["candidate"]["observations"][0]["reference"]["value"] is None
    assert suggestion["candidate"]["observations"][0]["reference"]["evidence_ids"]==[]


def test_annotating_coverage_cannot_alter_a_reading_claim():
    old, _, _ = fixture()
    proposed = old.model_copy(deep=True)
    proposed.coverage[0].note = "The caption and plotted legend disagree."
    before, after = (r.model_dump(mode="json") for r in (old,proposed))
    with pytest.raises(ScientificScopeRejected):
        _changes(before,after,adjudicate_outcomes=True)
    assert [(d["kind"],d["changed_path"]) for d in _changes(before,after,review_source_edits=True)] == [
        ("coverage_note","coverage[0].note")]
    for field, value in (("visual_checked",False),("block_ids",["page2"]),("source_id","second-source")):
        assert before["coverage"][0][field] != value
        edited = {**after,"coverage":[{**after["coverage"][0],field:value}]}
        with pytest.raises(ScientificScopeRejected):
            _changes(before,edited,review_source_edits=True)


def test_relocation_packet_requires_both_source_cards_and_every_claim_that_cites_it(monkeypatch):
    from watertreatment.focused_review_packet import build
    old, _, _ = fixture()
    new = old.model_copy(deep=True)
    new.evidence[0].block_id = "page2"
    pages = [SimpleNamespace(block_id=f"page{i}",kind="paragraph",text="Synthetic passage",
                location=SimpleNamespace(physical_page=i)) for i in range(1,4)]
    doc = SimpleNamespace(source=SimpleNamespace(source_id="source",format="pdf"),blocks=pages)
    parent = SimpleNamespace(before=old,documents=[doc],bundle={},findings=[])
    monkeypatch.setattr("watertreatment.focused_review_packet.reading_groups",lambda p:({},[]))
    packet,_,_ = build(parent,new,version=4)
    assert {s["block"] for s in packet["sources"]} == {"page1","page2"}
    consumer, = [f for f in packet["findings"] if f.get("locator_change")]
    assert "observations[0]" in consumer["paths"]
    assert set(packet["required_group_sources"][consumer["id"]]) == {"S1","S2"}
    report=FocusedReview(packet_hash="X",decisions=[ReviewDecision(groups=["C1",consumer["id"]],
        verdict="limited",source_ids=["S1"],explanation="Only the former location was examined.")],
        reading=[],reconciled_source_groups=[],additional_issues=[],restrictions=[],final_corrections=[])
    with pytest.raises(ValueError,match="omitted a bound original"):
        correction_result(new,packet,report)
    new.evidence[0].quote = "A new claim cannot be smuggled into a locator correction."
    with pytest.raises(ScientificScopeRejected):
        build(parent,new,version=4)


def test_supplemental_method_note_requires_the_same_experiment_and_a_signed_sibling():
    from watertreatment.focused_review_packet import _supplemental_note_sources
    def note(i,group):
        return SimpleNamespace(path=f"experiments[0].reaction_inputs[{i}].basis.note",
                               group=group,source_ids=["P","M"])
    basis={"status":"reported","value":"assay","evidence_ids":["primary"]}
    data={"experiments":[{"reaction_inputs":[{"basis":basis},{"basis":basis}],
                          "conditions":[{"fact":{"evidence_ids":["method"]}}]}],
          "evidence":[{"id":"primary","source_id":"doc","block_id":"sample"},
                      {"id":"method","source_id":"si","block_id":"protocol"}]}
    packet={"sources":[{"id":"P","source":"doc","block":"sample"},{"id":"M","source":"si","block":"protocol"}]}
    first, second=note(0,"C1"),note(1,"C2")
    groups={c.group:{"changes":[[c.path,None,"signed"]]} for c in (first,second)}
    row=SimpleNamespace(groups=["C1"],source_ids=["P"],verdict="limited")
    sibling=SimpleNamespace(groups=["C2"],source_ids=["P","M"],verdict="limited")
    report=SimpleNamespace(final_corrections=[first,second],decisions=[row,sibling])
    allowed=lambda: _supplemental_note_sources(data,packet,report,first,row,groups)
    assert allowed()
    sibling.verdict="requires_revision"
    assert not allowed()
    sibling.verdict="limited"
    second.path=second.path.replace("experiments[0]","experiments[1]")
    assert not allowed()
    second.path=note(1,"C2").path
    data["experiments"][0]["conditions"]=[]
    assert not allowed()


def test_many_leaves_on_the_same_scientific_subjects_keep_every_revised_path(monkeypatch):
    from watertreatment.focused_review_packet import build
    old, _, _ = fixture()
    exemplar=old.observations[0]
    old.observations=[exemplar.model_copy(deep=True,update={"id":f"measurement-{i}"}) for i in range(64)]
    new=old.model_copy(deep=True)
    for obs in new.observations:
        obs.value.note="Precision qualified at the printed endpoint."
        obs.unit.note="Unit is reported only at this endpoint."
        obs.limitations=["Shared preparation does not identify an independent replicate."]
    block=SimpleNamespace(block_id="page1",kind="paragraph",text="A printed endpoint.",
                          location=SimpleNamespace(physical_page=1))
    doc=SimpleNamespace(source=SimpleNamespace(source_id="source",format="pdf"),blocks=[block])
    parent=SimpleNamespace(before=old,documents=[doc],bundle={},findings=[])
    monkeypatch.setattr("watertreatment.focused_review_packet.reading_groups",lambda p:({},[]))
    raw=_changes(old.model_dump(mode="json"),new.model_dump(mode="json"),adjudicate_outcomes=True)
    assert len(raw)==192
    with pytest.raises(ValueError,match="bounded"):
        build(parent,new,version=3)
    packet,_,_=build(parent,new,version=4)
    grouped=[row[0] for group in packet["changes"] for row in group["changes"]]
    assert sorted(grouped)==sorted(item["changed_path"] for item in raw)
    assert all(len(group["changes"])<=12 for group in packet["changes"])


def test_final_correction_can_touch_only_the_checked_field():
    record,_,_=fixture()
    record.observations[0].limitations=["Attribution uncertain."]
    path="observations[0].limitations[0]"
    packet={"changes":[{"id":"C1","changes":[[path,None,"Attribution uncertain."]]}],
            "findings":[],"primary_source_cards":{}}
    report=FocusedReview.model_validate({"packet_hash":"X","decisions":[{"groups":["C1"],"verdict":"limited",
        "source_ids":["S1"],"explanation":"The plotted endpoint has no documented uncertainty."}],
        "reading":[],"reconciled_source_groups":[],"additional_issues":[],"restrictions":[],
        "final_corrections":[{"group":"C1","path":path,"replacement_json":json.dumps("Precision is unreported."),
            "source_ids":["S1"],"explanation":"The source does not supply a precision estimate."}]})
    revised=correction_result(record,packet,report)
    assert revised.observations[0].limitations==["Precision is unreported."]
    assert record.observations[0].limitations!=revised.observations[0].limitations
    report.final_corrections[0].path="observations[0].experiment_id"
    with pytest.raises(ValueError):
        correction_result(record,packet,report)


def test_exhausted_source_pool_preserves_a_final_bound_patch_without_increasing_total(tmp_path):
    record,_,_=fixture()
    tools=ReadingTools({"documents":[]},tmp_path,max_calls=1,expected_inputs=[],candidate_query_limit=3,submission_reserve=1)
    tools.prepare_revision(record)
    tools.begin_submission(True)
    assert tools.handle("wt_revision_state",{})["success"]
    assert not tools.handle("wt_inventory",{})["success"]
    result=tools.handle("wt_patch_record",{"base_hash":digest_json(record.model_dump(mode="json")),
        "edits":[{"operation":"set","path":["observations",0,"limitations"],"value":["Original-unit uncertainty retained."]}]})
    assert result["success"],result
    counts=tools.tool_budget_state()
    assert counts["limits"]["total_attempts"]==4
    assert counts["remaining"]["reserved_submission"]==0
    assert tools.resolve_submission(SimpleNamespace(submission_id=tools.latest_submission_id)).observations[0].limitations


def test_calculated_value_keeps_graphical_input_precision_and_requires_a_sourced_operand():
    from watertreatment.quantitative import observation_value_kind
    from watertreatment.validation import validate_record
    record,_,_=fixture()
    o=record.observations[0]
    o.origin="local_calculation"
    o.value=Fact(value=3.5,status="estimated",evidence_ids=["text"],note="Divide signed difference by the measured time.")
    o.numerator=Fact(value=7,status="estimated",evidence_ids=["text"])
    assert observation_value_kind(o)=="estimated"
    check=lambda: {i.code for i in validate_record(record) if i.severity=="error"}
    assert not check() & {"ESTIMATE_ORIGIN_CONFLICT","LOCAL_ESTIMATE_BASIS_MISSING"}
    o.numerator=Fact()
    o.denominator=Fact()
    o.coordinates=[]
    assert "LOCAL_ESTIMATE_BASIS_MISSING" in check()
    o.origin="reported_measurement"
    assert "ESTIMATE_ORIGIN_CONFLICT" in check()


def test_review_can_clarify_visible_reference_rationale_only_from_its_own_source():
    from watertreatment.domain import ReferenceAssociation
    record, _, _ = fixture()
    ev = next(e for e in record.evidence if e.id == "text")
    ref = ReferenceAssociation(id="shared", target_kind="experiment", target_id=record.experiments[0].id,
        observation_ids=[record.observations[0].id], relationship="shared_reference", applicability="reported",
        evidence_ids=[ev.id], rationale="These two measurements were repeated.")
    record.reference_associations.append(ref)
    owner = "reference_associations[0]"
    packet = {"changes": [], "primary_source_cards": {}, "findings": [{"id":"D1", "paths":[owner]}],
        "current_targets": {owner:ref.model_dump(mode="json")},
        "sources": [{"id":"S1","source":ev.source_id,"block":ev.block_id},{"id":"S2","source":"elsewhere","block":"b"}]}
    report = FocusedReview.model_validate({"packet_hash":"X","reading":[],"reconciled_source_groups":[],
        "additional_issues":[],"restrictions":[],"decisions":[{"groups":["D1"],"verdict":"limited",
        "source_ids":["S1","S2"],"explanation":"The same reference is named in both panels."}],
        "final_corrections":[{"group":"D1","path":owner+".rationale","source_ids":["S1"],
        "replacement_json":json.dumps("The two panels cite the same measurement."),
        "explanation":"The figure captions identify the common reference explicitly."}]})
    correction = report.final_corrections[0]
    assert correction_result(record,packet,report).reference_associations[0].rationale == json.loads(correction.replacement_json)
    correction.source_ids = ["S2"]
    with pytest.raises(ValueError, match="own original source"):
        correction_result(record,packet,report)
    correction.source_ids = ["S1"]
    correction.path = owner+".relationship"
    with pytest.raises(ValueError, match="only explicitly reviewed relation text"):
        correction_result(record,packet,report)
    correction.path = owner+".rationale"
    packet["current_targets"][owner]["rationale"] = "A shortened rendering."
    with pytest.raises(ValueError, match="exact original relation text"):
        correction_result(record,packet,report)


def test_comparison_text_requires_its_own_field_evidence_and_preserves_numeric_facts():
    from watertreatment.focused_review_packet import _check_dependency_text
    fact = {"value":"Both assays used the same feed.", "normalized":None, "evidence_ids":["method"]}
    comparison = {"outcomes":[{"fact":fact}], "evidence_ids":["context"]}
    owner = "comparisons[0]"
    path = ("comparisons",0,"outcomes",0,"fact","value")
    data = {"comparisons":[comparison],"evidence":[{"id":"method","source_id":"paper","block_id":"method"},
        {"id":"context","source_id":"paper","block_id":"discussion"}]}
    packet = {"current_targets":{owner:comparison},"sources":[{"id":"S1","source":"paper","block":"method"},
        {"id":"S2","source":"paper","block":"discussion"}]}
    finding = {"paths":[owner]}
    correction = SimpleNamespace(source_ids=["S1"])
    revised = "Sample alignment across these assays is unresolved."
    _check_dependency_text(data,packet,finding,path,correction,revised)
    correction.source_ids = ["S2"]
    with pytest.raises(ValueError,match="own original source"):
        _check_dependency_text(data,packet,finding,path,correction,revised)
    correction.source_ids = ["S1"]
    fact["value"] = 3
    with pytest.raises(ValueError,match="only explicitly reviewed relation text"):
        _check_dependency_text(data,packet,finding,path,correction,revised)

def test_reviewed_absence_retains_source_and_never_converts_a_measured_exposure():
    from watertreatment.domain import EnergyInput, NamedFact
    from watertreatment.retained_review_packet import _snapshot

    record, _, _ = fixture()
    ref = record.evidence[0]
    parameter = NamedFact(name="exposure",
        fact=Fact(value=0, status="reported", evidence_ids=[ref.id]),
        unit=Fact(value="dimensioned units", status="reported", evidence_ids=[ref.id]))
    input_group = EnergyInput(kind="light", source=Fact(status="explicit_absence", evidence_ids=[ref.id]),
                              parameters=[parameter])
    record.experiments[0].energy_inputs = [input_group]
    group = "experiments[0].energy_inputs"
    packet = {"changes": [], "primary_source_cards": {}, "findings": [{"id": "A1", "paths": [group]}],
              "sources": [{"id": "S1", "source": ref.source_id, "block": ref.block_id}],
              "current_targets": {group: [_snapshot(input_group.model_dump(mode="json"))]}}
    proposal = {"status": "not_reported", "note": "The procedure gives the absence qualitatively.",
                "evidence_ids": [ref.id]}
    edits = [{"group": "A1", "path": group + "[0].parameters[0]." + field,
              "replacement_json": json.dumps(proposal), "source_ids": ["S1"],
              "explanation": "The source reports no exposure and leaves the setting unquantified."}
             for field in ("unit", "fact")]
    report = FocusedReview(packet_hash="a", decisions=[ReviewDecision(groups=["A1"], verdict="limited",
        source_ids=["S1"], explanation="Original source confirms removal of the unsupported precision.")],
        reading=[], reconciled_source_groups=[], additional_issues=[], restrictions=[], final_corrections=edits)
    v = correction_result(record, packet, report).experiments[0].energy_inputs[0]
    assert v.source == input_group.source and v.parameters[0].fact == v.parameters[0].unit == Fact(**proposal)
    with pytest.raises(ValueError, match="whole scientific object"):
        correction_result(record, packet, report.model_copy(update={"final_corrections": report.final_corrections[:1]}))
    packet["current_targets"][group] = []
    with pytest.raises(ValueError, match="whole scientific object"):
        correction_result(record, packet, report)
    for attribute, updated in (("value", 0.02), ("raw", "meter reading"), ("normalized", 0.0)):
        old = getattr(parameter.fact, attribute)
        setattr(parameter.fact, attribute, updated)
        packet["current_targets"][group] = [_snapshot(input_group.model_dump(mode="json"))]
        with pytest.raises(ValueError, match="whole scientific object"):
            correction_result(record, packet, report)
        setattr(parameter.fact, attribute, old)
    input_group.source.status = "unread"
    packet["current_targets"][group] = [_snapshot(input_group.model_dump(mode="json"))]
    with pytest.raises(ValueError, match="whole scientific object"):
        correction_result(record, packet, report)

def test_unfamiliar_condition_is_retained_as_a_pending_proposal_with_source_bound_scalar_edits():
    from watertreatment.domain import NamedFact
    from watertreatment.retained_review import _reviewed_candidate
    record, _, _ = fixture()
    ref = record.evidence[0]
    group = "experiments[0].conditions"
    record.experiments[0].conditions = [NamedFact(name="unfamiliar operating condition",
        fact=Fact(value=2.5, status="reported", evidence_ids=[ref.id], note="Initial interpretation.")),
        NamedFact(name="measurement protocol", fact=Fact(value="timed", status="reported", evidence_ids=[ref.id], note="Earlier note."))]
    packet = {"version": 4, "changes": [], "primary_source_cards": {}, "findings": [{"id": "A1", "paths": [group]}],
              "sources": [{"id": "S1", "source": ref.source_id, "block": ref.block_id}]}
    edits = [{"group": "A1", "path": group + "[0].fact", "source_ids": ["S1"],
              "replacement_json": json.dumps({"status": "ambiguous", "note": "Units and attribution unresolved.",
                                              "evidence_ids": [ref.id]}),
              "explanation": "The source leaves this particular operating condition unresolved."},
             {"group": "A1", "path": group + "[1].fact.note", "source_ids": ["S1"],
              "replacement_json": json.dumps("The article identifies this as a timed measurement."),
              "explanation": "The source describes the timing protocol for a separate field."}]
    report = FocusedReview(packet_hash="a", decisions=[ReviewDecision(groups=["A1"], verdict="limited",
        source_ids=["S1"], explanation="The setting requires a separate source review.")],
        reading=[], reconciled_source_groups=[], additional_issues=[], restrictions=[], final_corrections=edits)
    original = record.model_dump(mode="json")
    unchanged, pending = _reviewed_candidate(SimpleNamespace(documents=[]), record, packet, report)
    assert unchanged.model_dump(mode="json") == original and pending == report.final_corrections

def test_reviewed_pair_differences_preserve_identity_level_and_unresolved_conditions():
    from watertreatment.domain import ObservationPair
    record, _, _ = fixture()
    ref = record.evidence[0]
    pair = ObservationPair(left_observation_id="flux", right_observation_id="liquid",
        level="P?", evidence_ids=[ref.id], differing_conditions=["Different assays."],
        unresolved_conditions=["Matching run and sample times are unconfirmed."])
    record.experiments[0].pairs = [pair]
    root = "experiments[0].pairs"
    group = {"id": "C1", "changes": [[root, None, []]]}
    packet = {"changes": [group], "findings": [], "primary_source_cards": {},
              "sources": [{"id": "S1", "source": ref.source_id, "block": ref.block_id},
                          {"id": "S2", "source": "other", "block": "elsewhere"}]}
    text = "The panels separately identify a gravimetric assay and a liquid concentration measurement."
    edit = {"group": "C1", "path": root + "[0].differing_conditions[0]", "source_ids": ["S1"],
            "replacement_json": json.dumps(text), "explanation": "The cited original distinguishes the measurements."}
    report = FocusedReview(packet_hash="a", decisions=[ReviewDecision(groups=["C1"], verdict="limited",
        source_ids=["S1", "S2"], explanation="Pairing remains unresolved after checking the originals.")],
        reading=[], reconciled_source_groups=[], additional_issues=[], restrictions=[], final_corrections=[edit])
    new = correction_result(record, packet, report).experiments[0].pairs[0]
    assert new == pair.model_copy(update={"differing_conditions": [text]})
    correction = report.final_corrections[0]
    correction.source_ids = ["S2"]
    with pytest.raises(ValueError, match="exact pair"):
        correction_result(record, packet, report)
    correction.source_ids = ["S1"]
    correction.replacement_json = '""'
    with pytest.raises(ValueError, match="substantive description"):
        correction_result(record, packet, report)
    correction.path = root + "[0].level"
    correction.replacement_json = '"P1"'
    with pytest.raises(ValueError, match="scientific identity"):
        correction_result(record, packet, report)


def test_comparison_counterevidence_reuses_only_cited_member_measurements():
    from types import SimpleNamespace
    from watertreatment.focused_review_packet import _check_dependency_text

    owner = {"value":"The two controls responded.", "evidence_ids":["ev"], "status":"inferred"}
    comp = {"id":"cmp", "observation_ids":["control"], "counterevidence":[owner]}
    member = {"id":"control", "origin":"reported_measurement",
              "value":{"status":"reported","value":12.5,"evidence_ids":["ev"]},
              "unit":{"status":"reported","value":"mg/L","evidence_ids":["ev"]}}
    data = {"comparisons":[comp],"observations":[member],
            "evidence":[{"id":"ev","source_id":"src","block_id":"blk"}]}
    packet = {"sources":[{"id":"S1","source":"src","block":"blk"}],
              "current_targets":{"comparisons[0]":comp}}
    args = (data,packet,{"paths":["comparisons[0]"]},["comparisons",0,"counterevidence",0,"value"],
            SimpleNamespace(source_ids=["S1"]))
    _check_dependency_text(*args,"The control gave 12.5 mg/L. Its cause is unresolved.")
    for replacement in ("It gave 22.5 mg/L.", "It gave 12.5 %.","It gave 12.5 mg/L/day."):
        with pytest.raises(ValueError,match="source-linked comparison operand"):
            _check_dependency_text(*args,replacement)
    comp["observation_ids"] = []
    with pytest.raises(ValueError,match="source-linked comparison operand"):
        _check_dependency_text(*args,"The control gave 12.5 mg/L.")
    owner["normalized"] = "previous normalization"
    with pytest.raises(ValueError,match="normalized"):
        _check_dependency_text(*args,"The origin is unresolved.")


def test_reference_condition_withdrawal_needs_same_source_limitation_and_preserves_order():
    from types import SimpleNamespace
    from watertreatment.focused_review_packet import _check_dependency_text
    ref = {"differing_conditions":["feed","duration","pressure"],"limitations":["Limited applicability."],"evidence_ids":["ev"]}
    data = {"reference_associations":[ref],"evidence":[{"id":"ev","source_id":"s","block_id":"b"}]}
    packet = {"sources":[{"id":"S1","source":"s","block":"b"}],"current_targets":{"reference_associations[0]":ref}}
    correction = SimpleNamespace(group="D1",source_ids=["S1"])
    limitation = SimpleNamespace(group="D1",path="reference_associations[0].limitations[0]",source_ids=["S1"],
                                 replacement_json='"The original feed is unresolved."')
    path = ["reference_associations",0,"differing_conditions"]
    args = (data,packet,{"paths":["reference_associations[0]"]},path,correction)
    _check_dependency_text(*args,["duration","pressure"],companions=[limitation])
    for invalid in (["pressure","duration"],["new condition"],["duration","duration"]):
        with pytest.raises(ValueError,match="ordered subset"):
            _check_dependency_text(*args,invalid,companions=[limitation])
    with pytest.raises(ValueError,match="source-reviewed limitation"):
        _check_dependency_text(*args,["duration"])
    limitation.source_ids=["S2"]
    with pytest.raises(ValueError,match="source-reviewed limitation"):
        _check_dependency_text(*args,["duration"],companions=[limitation])
