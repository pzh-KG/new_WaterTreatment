import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from watertreatment.interrupted_reading import public_progress, progress_prompt
from watertreatment.scope_policy import first_audit_prompt_transition, scientific_prompt
from watertreatment.storage import hash_file, write_new_json


def test_only_attested_unique_crops_and_public_comments_from_one_turn_survive(tmp_path):
    run=tmp_path
    events=run/"app_server"/"events.jsonl"
    events.parent.mkdir()
    rows=[]
    def emit(direction,content):
        rows.append(json.dumps({"direction":direction,"message":content}))
    def comment(turn,phase,text):
        emit("receive",{"method":"item/completed","params":{"threadId":"thread","turnId":turn,
            "item":{"type":"agentMessage","phase":phase,"text":text}}})
    comment("turn","commentary","Inspect the original panel.")
    comment("other","commentary","Different turn.")
    comment("turn","final_answer","Separate final answer.")
    for i in (1,2):
        folder=run/"reading_tools"/f"tool-{i:04d}"
        folder.mkdir(parents=True)
        image=folder/"view.png"
        image.write_bytes(b"synthetic crop bytes")
        args={"source_id":"doc","physical_page":2,"rectangle":[0.1,0.1,0.5,0.5],"scale":2}
        data={**args,"block_ids":["page2"],"tool_call_number":i,"image_path":str(image),"image_sha256":hash_file(image)}
        write_new_json(folder/"request.json",{"tool":"wt_view","arguments":args})
        write_new_json(folder/"result.json",{"success":True,"data":data})
        url=b"data:image/png;base64,"+base64.b64encode(image.read_bytes())
        emit("receive",{"method":"item/tool/call","id":i,"params":{"threadId":"thread","turnId":"turn","tool":"wt_view","arguments":args}})
        emit("send",{"id":i,"result":{"success":True,"contentItems":[
            {"type":"inputText","text":json.dumps(data)},{"type":"inputImage","imageUrl":{
                "data_url_sha256":hashlib.sha256(url).hexdigest(),"bytes":len(url)}},
            {"type":"inputText","text":"time left"}]}})
    events.write_text("\n".join(rows),encoding="utf-8")
    outcome={"thread_id":"thread","turn_id":"turn"}
    result=public_progress(run,outcome,include_crops=True)
    assert result["public_comments"]==["Inspect the original panel."]
    assert len(result["source_views"])==1
    text=progress_prompt(result,crops=result["source_views"])
    assert "page2" in text and "Different turn" not in text and str(run) not in text
    image.write_bytes(b"tampered crop")
    with pytest.raises(ValueError,match="public tool receipt"):
        public_progress(run,outcome,include_crops=True)


@pytest.mark.parametrize("stem",["understand","extract"])
def test_explicit_first_audit_template_transition_rejects_unregistered_changes(stem):
    settings=SimpleNamespace(project_root=Path(__file__).resolve().parents[1],rules_version="0.8")
    active=scientific_prompt(settings,stem)
    original=active.replace(next(line+"\n\n" for line in active.splitlines()
                                 if line.startswith("Experimental windows and controls.")),"")
    if stem=="extract":
        original=original.replace(next(line+"\n" for line in original.splitlines()
                                  if line.startswith("For each probe or diagnostic assay,")),"")
    proof=first_audit_prompt_transition(settings,stem,original,None)
    assert proof["original_text_sha256"]!=proof["current_text_sha256"]
    assert first_audit_prompt_transition(settings,stem,active,None) is None
    with pytest.raises(ValueError,match="registered"):
        first_audit_prompt_transition(settings,stem,original+"unregistered",None)


def test_candidate_navigation_requires_this_turns_successful_delivered_query(tmp_path):
    rows=[]
    binding={"path":"frozen","sha256":"registered"}
    for number,turn in ((1,"previous"),(2,"audit")):
        args={"path":"observations[0]"}
        data={"tool_call_number":number,"candidate_binding":binding,"returned_count":1}
        folder=tmp_path/"reading_tools"/f"tool-{number:04d}"
        write_new_json(folder/"request.json",{"tool":"wt_query_candidate","arguments":args})
        write_new_json(folder/"result.json",{"success":True,"data":data})
        rows.extend([{"direction":"receive","message":{"method":"item/tool/call","id":number,"params":{
            "threadId":"thread","turnId":turn,"arguments":args,"tool":"wt_query_candidate"}}},
            {"direction":"send","message":{"id":number,"result":{"success":True,"contentItems":[
                {"type":"inputText","text":json.dumps(data)}]}}}])
    events=tmp_path/"app_server"/"events.jsonl"
    events.parent.mkdir()
    events.write_text("\n".join(json.dumps(row) for row in rows),encoding="utf8")
    outcome={"thread_id":"thread","turn_id":"audit"}
    actual=public_progress(tmp_path,outcome,query_binding=binding)["candidate_queries"]
    assert [q["result_sha256"] for q in actual]==[hash_file(folder/"result.json")]
    (folder/"result.json").write_text('{"success":false}',encoding="utf8")
    with pytest.raises(ValueError,match="delivered query"):
        public_progress(tmp_path,outcome,query_binding=binding)
