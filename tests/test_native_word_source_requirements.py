"""Native Word tables require complete XML/text, never fabricated page views."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from PIL import Image

from test_workflow import synthetic_docx
from watertreatment.reading_tools import ReadingTools
from watertreatment.sources import parse_source
from watertreatment.storage import digest_json, hash_file, write_new_json
from watertreatment.source_review import (
    review_requirements, build_source_review_union_proof, verify_source_review_union,
    build_native_word_requirement_amendment, PARTIAL_COMPLETION_POLICY,
)


def word_case(tmp_path, *, table_image=False):
    root=tmp_path/"data"; original=root/"originals/support.docx"; original.parent.mkdir(parents=True)
    synthetic_docx(original)
    if table_image:
        with zipfile.ZipFile(original) as archive:
            contents={n:archive.read(n) for n in archive.namelist()}
        xml=contents["word/document.xml"].decode()
        start,end=xml.index("<w:drawing>"),xml.index("</w:drawing>")+len("</w:drawing>")
        xml=xml.replace("</w:tc>","<w:p><w:r>"+xml[start:end]+"</w:r></w:p></w:tc>",1)
        contents["word/document.xml"]=xml.encode()
        with zipfile.ZipFile(original,"w") as archive:
            for name,data in contents.items():archive.writestr(name,data)
    doc=parse_source(original,source_id="word")
    table=next(b for b in doc.blocks if b.kind=="table")
    family=[b for b in doc.blocks if b.location.table_index==table.location.table_index]
    run=root/"runs/native-test"; run.mkdir(parents=True)
    images=[]
    for block in doc.blocks:
        if block.kind=="image":
            path=root/"originals"/(block.block_id.replace(":","-")+".png")
            Image.new("RGB",(20,15),"white").save(path)
            images.append({"block_id":block.block_id,"path":str(path)})
    bundle={"documents":[doc.model_dump(mode="json")],"renderings":[{"source_id":"word","images":images}]}
    write_new_json(run/"sources.json",bundle)
    tools=ReadingTools(bundle,run,max_calls=30,expected_inputs=[{"path":str(p),"sha256":hash_file(p)}
        for p in [original,*[Path(i["path"]) for i in images]]])
    scope={"policy":"bounded-revision-audit-v1","source_anchors":[{"source_id":"word","block_id":table.block_id,
        "evidence_id":"table-source","locator":{"table_index":table.location.table_index}}]}
    scope["scope_sha256"]=digest_json(scope)
    return SimpleNamespace(run=run,doc=doc,table=table,family=family,tools=tools,scope=scope)


def begin(c,scope,call):
    c.tools.begin_query_turn();c.tools.begin_source_review(scope,call_id=call)


def read_range(c,start,end):
    result=c.tools.handle("wt_read_blocks",{"source_id":"word","start_order":start,"end_order":end})
    assert result["success"],result


def modern(scope,*,policy=PARTIAL_COMPLETION_POLICY):
    scope=copy.deepcopy(scope);scope["policy"]=policy
    if policy!=PARTIAL_COMPLETION_POLICY:scope["source_requirement_policy"]="native-word-structure-v1"
    scope["scope_sha256"]=digest_json({k:v for k,v in scope.items() if k!="scope_sha256"})
    return scope


def test_legacy_table_view_is_preserved_and_explicit_amendment_requires_complete_structure(tmp_path):
    c=word_case(tmp_path);begin(c,c.scope,"legacy")
    read_range(c,c.table.order,c.table.order)
    partial=c.tools.source_review_progress()
    assert not partial["complete"] and partial["requirements"][0]["requires_full_view"]
    amendment=build_native_word_requirement_amendment(c.run,c.scope,partial["missing_full_views"])
    parts=[{"run":str(c.run),"scope":c.scope,"call_id":"legacy","progress":partial}]
    with pytest.raises(ValueError,match="remains incomplete"):
        build_source_review_union_proof(c.run,c.scope,parts,requirement_amendment=amendment)
    read_range(c,c.family[1].order,c.family[-1].order)
    partial=c.tools.source_review_progress();parts[0]["progress"]=partial
    assert not partial["complete"] and partial["full_views"]==[]
    with pytest.raises(ValueError,match="remains incomplete"):
        build_source_review_union_proof(c.run,c.scope,parts)
    proof=build_source_review_union_proof(c.run,c.scope,parts,requirement_amendment=amendment)
    assert verify_source_review_union(c.run,c.scope,proof)
    assert proof["requirements"][0]["requires_full_view"]
    assert proof["source_requirement_amendment"]["substitutions"][0]["replacement_requires_full_view"] is False
    changed=copy.deepcopy(amendment);changed["substitutions"][0]["native_word_structure"]["block_ids"]=[]
    changed["amendment_sha256"]=digest_json({k:v for k,v in changed.items() if k!="amendment_sha256"})
    with pytest.raises(ValueError,match="amendment changed"):
        build_source_review_union_proof(c.run,c.scope,parts,requirement_amendment=changed)


@pytest.mark.parametrize("policy",[PARTIAL_COMPLETION_POLICY,"bounded-scientific-revision-v1"])
def test_new_version_requires_all_native_children_and_records_no_visual_claim(tmp_path,policy):
    c=word_case(tmp_path);scope=modern(c.scope,policy=policy);begin(c,scope,"new-table")
    req=review_requirements(c.run,scope,"new-table")[0]
    assert not req["requires_full_view"] and req["native_word_structure"]["block_ids"]==[b.block_id for b in c.family]
    read_range(c,c.table.order,c.table.order)
    assert c.tools.source_review_progress()["missing_native_structures"]
    with pytest.raises(ValueError,match="native structures"):
        c.tools.source_review_proof()
    read_range(c,c.family[1].order,c.family[-1].order)
    proof=c.tools.source_review_proof()
    assert proof["requirements"][0]["native_word_structure"]["visual_status"]=="native_table_has_no_registered_page_view"
    assert c.tools.source_review_progress()["full_views"]==[]


def test_reading_only_a_native_value_cannot_omit_its_table_context(tmp_path):
    c=word_case(tmp_path)
    paragraph=next(b for b in c.family if b.kind=="paragraph" and b.text.strip())
    scope=modern(c.scope);scope["source_anchors"][0]["block_id"]=paragraph.block_id
    scope["scope_sha256"]=digest_json({k:v for k,v in scope.items() if k!="scope_sha256"})
    begin(c,scope,"value-only");read_range(c,paragraph.order,paragraph.order)
    progress=c.tools.source_review_progress()
    assert progress["missing_blocks"]==[] and progress["missing_native_structures"]
    assert not progress["complete"]
    with pytest.raises(ValueError,match="native structures"):
        build_source_review_union_proof(c.run,scope,
            [{"run":str(c.run),"scope":scope,"call_id":"value-only","progress":progress}])


def test_embedded_table_image_still_requires_its_own_full_occurrence(tmp_path):
    c=word_case(tmp_path,table_image=True);scope=modern(c.scope);begin(c,scope,"native-image")
    read_range(c,c.table.order,c.family[-1].order)
    required=c.tools.source_review_progress()["requirements"][0]["native_word_structure"]["full_view_block_ids"]
    assert len(required)==1
    with pytest.raises(ValueError,match="native structures"):
        c.tools.source_review_proof()
    outside=next(b for b in c.doc.blocks if b.kind=="image" and b.block_id not in required)
    assert c.tools.handle("wt_view",{"source_id":"word","block_id":outside.block_id,"scale":1})["success"]
    assert not c.tools.source_review_progress()["complete"]
    assert c.tools.handle("wt_view",{"source_id":"word","block_id":required[0],"scale":1})["success"]
    assert c.tools.source_review_progress()["complete"]


def test_linked_word_caption_requires_text_and_full_single_registered_figure(tmp_path):
    from watertreatment.word_review_links import LINKED_POLICY
    from watertreatment.source_review import review_navigation
    c = word_case(tmp_path)
    caption = next(b for b in c.doc.blocks if b.text.startswith("Fig. S1"))
    image = next(b for b in c.doc.blocks if b.kind == "image")
    scope = {"policy": "bounded-scientific-revision-v1", "source_requirement_policy": LINKED_POLICY,
             "source_anchors": [{"source_id": "word", "block_id": b.block_id, "locator": {"figure": "S1"},
                                 "evidence_id": eid} for b, eid in [(caption, "caption"), (image, "figure")]]}
    scope["scope_sha256"] = digest_json(scope)
    begin(c, scope, "linked")
    requirements = review_requirements(c.run, scope, "linked")
    native = next(r["native_word_structure"] for r in requirements if r["block_id"] == caption.block_id)
    assert native["full_view_block_ids"] == [image.block_id]
    wire = review_navigation(requirements + requirements)
    assert len(wire["native_structures"]) == 1
    decoded = []
    for row in wire["required_current_source_deliveries"]:
        row = dict(row)
        if "native_structure_index" in row:
            row["native_word_structure"] = wire["native_structures"][row.pop("native_structure_index")]
        decoded.append(row)
    assert decoded == requirements + requirements
    read_range(c, image.order, caption.order)
    assert not c.tools.source_review_progress()["complete"]
    args = {"source_id": "word", "block_id": image.block_id, "scale": 1}
    assert c.tools.handle("wt_view", {**args, "rectangle": [0.1, 0.1, 0.8, 0.8]})["success"]
    assert not c.tools.source_review_progress()["complete"]
    assert c.tools.handle("wt_view", args)["success"]
    assert c.tools.source_review_progress()["complete"]
    orphan = {**scope, "source_anchors": scope["source_anchors"][:1]}
    orphan["scope_sha256"] = digest_json({k: v for k, v in orphan.items() if k != "scope_sha256"})
    assert review_requirements(c.run, orphan, "ambiguous")[0]["unresolved_word_visual"]


@pytest.mark.parametrize("caption,expected", [("图S9样品的光谱", ("figure", "S9")),
                                              ("表2实验条件", ("table", "2")),
                                              ("See Figure S9", None)])
def test_exact_english_and_chinese_caption_labels(caption, expected):
    from watertreatment.word_review_links import _label
    assert _label(caption) == expected


def test_draft_disposition_checker_shares_final_path_and_source_requirements(tmp_path):
    c = word_case(tmp_path)
    anchor = c.scope["source_anchors"][0]
    pair = [anchor["source_id"], anchor["block_id"]]
    owner = "observations[0]"
    field = owner + ".value"
    scope = {**c.scope, "policy": "bounded-scientific-revision-v1",
             "changes": [{"changed_path": field, "required_evidence_ids": ["table-source"]}],
             "blocking_obligations": [{"paths": [field], "source_blocks": [pair]}],
             "entity_refs": [{"role": "affected", "record_path": owner, "required_evidence_ids": ["table-source"]}]}
    scope["scope_sha256"] = digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})
    begin(c, scope, "draft")
    def submit(path, ids):
        return c.tools.handle("wt_review_gaps", {"groups": [{"record_paths": [path], "evidence_ids": ids}]})
    def gaps(response):
        assert response["success"], response
        return json.loads(response["contentItems"][0]["text"])["disposition_gaps"]
    assert not submit("all observations", ["table-source"])["success"]
    assert [x["kind"] for x in gaps(submit(field, ["table-source"]))] == ["affected_entity"]
    assert all(x["missing_source_blocks"] == [pair] for x in gaps(submit(owner, [])))
    assert not gaps(submit(owner, ["table-source"]))
    assert not c.tools.source_review_progress()["complete"]
