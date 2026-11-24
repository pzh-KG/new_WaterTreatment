import copy
import hashlib
import io
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image

from watertreatment.coverage_visual_inputs import coverage_tool_limit, coverage_visual_bundle
from watertreatment.rendering import word_occurrence_preview
from watertreatment.sources import SourceDocument, SourceFile
from watertreatment.storage import hash_file


def test_registered_legacy_vml_occurrence_is_rebuilt_and_hash_checked(tmp_path):
    original = tmp_path / "originals" / "s.docx"
    original.parent.mkdir()
    media = io.BytesIO()
    Image.new("RGB", (40, 20), "red").save(media, format="PNG")
    sha = hashlib.sha256(media.getvalue()).hexdigest()
    with ZipFile(original, "w") as archive:
        archive.writestr("word/media/science.png", media.getvalue())
    doc = SourceDocument(source=SourceFile(source_id="si", path=str(original), format="docx",
                           sha256=hash_file(original), size_bytes=original.stat().st_size))
    b = doc.add_block("image", metadata={"media_path": "word/media/science.png", "media_sha256": sha,
                      "crop": {"specified": True, "unit": "vml_raw", "raw": {"cropleft": "16384f", "cropbottom": "10%"}}})
    parent = SimpleNamespace(documents=[doc], scope={"required_dispositions": [
        {"kind": "image", "source_id": "si", "block_id": b.block_id}]},
        bundle={"documents": [doc.model_dump(mode="json")], "renderings": [{"source_id": "si", "images": [
            {"block_id": b.block_id, "source_media_sha256": sha, "error": "historical unrenderable crop"}]}]})
    retained = copy.deepcopy(parent.bundle)
    run = tmp_path / "runs" / "synthetic"
    run.mkdir(parents=True)
    current = coverage_visual_bundle(parent, run, create=True)
    preview = current["renderings"][0]["images"][0]
    assert preview["crop_pixels"] == [10, 0, 40, 18] and preview["status"] == "rendered_unread"
    assert coverage_visual_bundle(parent, run, create=False) == current and parent.bundle == retained
    assert current["documents"] == retained["documents"]
    from watertreatment.review_visuals import review_visuals
    from watertreatment.storage import write_new_json
    review = run.with_name("review")
    review.mkdir()
    write_new_json(review / "sources.json", retained)
    refreshed, paths, binding = review_visuals(review, parent.bundle, create=True)
    image = refreshed["renderings"][0]["images"][0]
    assert image["sha256"] == preview["sha256"] and image["status"] == "rendered_unread"
    assert review_visuals(review, retained, create=False, proof=binding) == (refreshed, paths, binding)
    with pytest.raises(ValueError, match="frozen plan"):
        review_visuals(review, retained, create=False, proof=None)
    with open(image["path"], "ab") as mutated:
        mutated.write(b"changed")
    with pytest.raises(ValueError, match="preview changed"):
        review_visuals(review, retained, create=False, proof=binding)
    with open(preview["path"], "ab") as stream:
        stream.write(b"modified")
    with pytest.raises(ValueError, match="preview changed"):
        coverage_visual_bundle(parent, run, create=False)


def test_unknown_word_raster_replays_as_unread_without_hiding_other_sources(tmp_path):
    from watertreatment.review_visuals import review_visuals
    from watertreatment.storage import write_new_json
    raw = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8"/></svg>'
    original = tmp_path / "graphic.docx"
    member = "word/media/vector.svg"
    sha = hashlib.sha256(raw).hexdigest()
    with ZipFile(original, "w") as archive:
        archive.writestr(member, raw)
    doc = SourceDocument(source=SourceFile(source_id="si", path=str(original), format="docx",
        sha256=hash_file(original), size_bytes=original.stat().st_size))
    block = doc.add_block("image", metadata={"media_path": member, "media_sha256": sha,
        "crop": {"specified": True, "unit": "vml_raw", "raw": {"croptop": "1%"}}})
    doc.add_block("paragraph", text="Separate supported result.")
    data = {"documents": [doc.model_dump(mode="json")], "renderings": [{"source_id": "si",
            "images": [{"source_id": "si", "block_id": block.block_id, "status": "unavailable", "source_media_sha256": sha}]}]}
    run = tmp_path / "runs" / "review"
    run.mkdir(parents=True)
    write_new_json(run / "sources.json", data)
    current, paths, binding = review_visuals(run, data, create=True)
    assert review_visuals(run, data, create=False, proof=binding) == (current, paths, binding)
    entry = current["renderings"][0]["images"][0]
    assert "path" not in entry and entry["status"] == "unavailable" and "decoder" in entry["recovery_limitation"]
    assert current["documents"] == data["documents"]
    with original.open("ab") as changed:
        changed.write(b"modified")
    with pytest.raises(ValueError, match="source.*changed"):
        review_visuals(run, data, create=False, proof=binding)


@pytest.mark.parametrize("raw", [{"cropleft": "65536f"}, {"cropleft": "-2%"}, {"cropright": "1in"}])
def test_invalid_vml_crop_never_defaults_to_an_uncropped_source(raw):
    with pytest.raises(ValueError):
        word_occurrence_preview(None, {"specified": True, "unit": "vml_raw", "raw": raw})


def test_scoped_tool_allowance_covers_each_required_image_and_text_context():
    scope = {"required_dispositions": [{"kind": "image"}] * 42 + [{"kind": "paragraph"}] * 82}
    assert 42 + 12 <= coverage_tool_limit(scope) <= 160
