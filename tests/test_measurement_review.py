from types import SimpleNamespace as NS

from watertreatment.measurement_review import audit_navigation_text, measurement_navigation
from watertreatment.reading import PrintedEndpoint, ReadingDossier
from watertreatment.source_review import review_navigation


def test_number_collision_cannot_verify_owner_and_absent_matches_survive():
    claim = PrintedEndpoint(owner_and_conditions="film B, light", metric_and_stream="feed disappearance",
        number=80, unit_and_qualifier="reported %", evidence={"source_id":"main","block_id":"p4"})
    unrelated = NS(id="wrong-arm", value=NS(value=80, evidence_ids=["other"]))
    estimate = NS(id="graph", value=NS(value="80", evidence_ids=["same"]))
    rows = [unrelated, estimate, NS(id="boolean", value=NS(value=True, evidence_ids=["same"]))]
    for row in rows:
        row.experiment_id, row.material_id, row.stream_id = "arm", "film", "feed"
        row.endpoint_kind, row.origin, row.evidence_links = "other", "plot_estimate", []
    record = NS(evidence=[NS(id="same",source_id="main",block_id="p4"),
                          NS(id="other",source_id="si",block_id="p4")], observations=rows,
                model_dump=lambda **kwargs: {"ids":[o.id for o in rows]})
    report = measurement_navigation(NS(printed_endpoints=[claim, claim.model_copy(update={"number":81})]),record,
                                    [NS(source=NS(source_id="main"), blocks=[NS(block_id="p4")])])
    first, second = report["flags"]
    assert first["source_anchor_registered"] and first["candidate_examples"][0]["id"] == "graph"
    assert first["same_value_other_block_count"] == 1
    assert not second["candidate_examples"]
    assert report["scientific_validation"] == "not_established"
    assert audit_navigation_text(report, byte_limit=5) == ""
    assert '81' in audit_navigation_text(report)


def test_old_reader_wire_shape_is_preserved():
    dossier = ReadingDossier(paper_id="paper", article_question_and_design="water",
        material_map=[], experiment_map=[], coverage=[], cross_source_conflicts=[], unresolved_questions=[], schema_gaps=[])
    assert "printed_endpoints" not in dossier.model_dump(mode="json")


def test_audit_navigation_covers_text_and_each_distinct_required_visual():
    blocks = [NS(block_id=f"b{i}",order=i) for i in range(1,27)]
    rows = [{"source_id":"s","block_id":b.block_id,"requires_full_view":False,"physical_page":b.order}
            for b in blocks[:23]]
    rows += [{"source_id":"s","block_id":"b24","physical_page":24,"requires_full_view":True},
             {"source_id":"s","block_id":"b25","physical_page":24,"requires_full_view":False},
             {"source_id":"word","block_id":"b26","physical_page":None,"requires_full_view":False,
              "native_word_structure":{"block_ids":["b26"],"full_view_block_ids":["b26"]}}]
    result = review_navigation(rows, {"s":NS(blocks=blocks,source=NS(format="pdf")),
                                       "word":NS(blocks=blocks,source=NS(format="docx"))})
    assert [(r["start_order"],r["end_order"]) for r in result["suggested_wt_read_blocks"]] == [(1,20),(21,23),(26,26)]
    assert len(result["suggested_full_wt_views"]) == 2
    assert len(result["required_current_source_deliveries"]) == len(rows)
