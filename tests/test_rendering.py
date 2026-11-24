from pypdf import PdfWriter

from watertreatment.rendering import render_pdf
from watertreatment.storage import hash_file


def test_transparent_black_axes_use_white_canvas_without_changing_media(tmp_path):
    from PIL import Image
    from watertreatment.rendering import prepare_reading_previews, render_word_images
    from watertreatment.sources import SourceDocument, SourceFile
    media = tmp_path / "transparent-plot.png"
    original = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
    original.putpixel((1, 0), (0, 0, 0, 255))
    original.putpixel((2, 0), (255, 0, 0, 128))
    original.save(media)
    before = hash_file(media)
    manifest = [{"attachment_index": 1, "source_id": "s", "block_id": "s:b000001", "path": str(media), "physical_page": None}]
    staged = prepare_reading_previews(manifest, tmp_path / "staged")
    assert manifest[0]["path"] == str(media) and staged[0]["original_preview_path"] == str(media)
    assert staged[0]["display_adjustment"]["background"] == "#ffffff"
    with Image.open(staged[0]["path"]) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((1, 0)) == (0, 0, 0)
        assert image.getpixel((2, 0)) == (255, 127, 127)
    doc = SourceDocument(source=SourceFile(source_id="s", path="paper.docx", sha256="a"*64, size_bytes=1, format="docx"))
    doc.add_block("image", metadata={"image_output_path": str(media)})
    rendered = render_word_images(doc, tmp_path / "rendered")["images"][0]
    assert rendered["display_adjustment"]["alpha_composited"]
    assert hash_file(media) == before


def test_palette_transparency_retains_opaque_black_labels():
    from PIL import Image
    from watertreatment.rendering import reading_canvas
    source = Image.new("P", (2, 1), 0)
    source.putpalette([0, 0, 0, 0, 0, 0] + [0] * 762)
    source.info["transparency"] = 0
    source.putpixel((1, 0), 1)
    displayed, adjustment = reading_canvas(source)
    assert adjustment["alpha_composited"]
    assert displayed.getpixel((0, 0)) == (255, 255, 255)
    assert displayed.getpixel((1, 0)) == (0, 0, 0)


def test_all_pdf_pages_render_without_claiming_reading(tmp_path):
    source = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.add_blank_page(width=200, height=100)
    writer.write(source)
    before = hash_file(source)
    result = render_pdf(source, tmp_path / "pages", scale=1)
    assert [p["physical_page"] for p in result["pages"]] == [1, 2]
    assert all(p["status"] == "rendered_unread" for p in result["pages"])
    assert hash_file(source) == before
    assert (tmp_path / "pages/page-0001.png").is_file()


def test_word_shared_media_has_distinct_crop_previews(tmp_path):
    from PIL import Image
    from watertreatment.rendering import render_word_images
    from watertreatment.sources import SourceDocument, SourceFile
    media = tmp_path / "media.png"
    picture = Image.new("RGB", (100, 20), "red")
    picture.paste("blue", (50, 0, 100, 20))
    picture.save(media)
    doc = SourceDocument(source=SourceFile(source_id="s", path="paper.docx", sha256="a" * 64, size_bytes=1, format="docx"))
    for crop in ({"right": 50000}, {"left": 50000}):
        doc.add_block("image", metadata={"image_output_path": str(media), "crop": {"specified": True, "unit": "1/1000_percent", **crop}})
    result = render_word_images(doc, tmp_path / "previews")
    assert len(result["images"]) == 2
    assert result["images"][0]["sha256"] != result["images"][1]["sha256"]
    assert result["images"][0]["block_id"] != result["images"][1]["block_id"]
    assert all(i["status"] == "rendered_unread" for i in result["images"])


def test_unsupported_word_crop_never_guesses(tmp_path):
    from PIL import Image
    from watertreatment.rendering import render_word_images
    from watertreatment.sources import SourceDocument, SourceFile
    media = tmp_path / "image.png"
    Image.new("RGB", (5, 5)).save(media)
    doc = SourceDocument(source=SourceFile(source_id="s", path="paper.docx", sha256="a" * 64, size_bytes=1, format="docx"))
    doc.add_block("image", metadata={"image_output_path": str(media), "crop": {"specified": True, "unit": "vml_raw", "raw": {"left": "3000f"}}})
    item = render_word_images(doc, tmp_path / "previews")["images"][0]
    assert item["status"] == "unavailable"
    assert "path" not in item



def test_signed_drawingml_source_crop_preserves_pixels_and_marks_padding(tmp_path):
    import pytest
    from PIL import Image
    from watertreatment.rendering import word_occurrence_preview

    media = tmp_path / "signed.png"
    original = Image.new("RGB", (10, 4), (11, 92, 73))
    original.putpixel((2, 1), (4, 5, 6))
    original.save(media)
    crop = {"specified":True,"unit":"1/1000_percent","left":20000,"right":-20000,"outset_fill":"stretch"}
    visible, bounds, adjustment = word_occurrence_preview(media, crop)
    assert visible.size == original.size and bounds == [2, 0, 12, 4]
    assert visible.getpixel((0, 1)) == (4, 5, 6)
    assert visible.getpixel((9, 1)) == (255, 255, 255)
    assert adjustment["source_outset_padding_ltrb_px"] == [0, 0, 2, 0]
    same, bounds, adjustment = word_occurrence_preview(media, {**crop,"left":0,"right":-1})
    assert same.tobytes() == original.tobytes() and bounds == [0, 0, 10, 4]
    for bad in [{**crop,"outset_fill":"tile"}, {**crop,"right":-200_000_000,"bottom":-200_000_000},
                {**crop,"left":120000,"right":-50000}]:
        with pytest.raises(ValueError):
            word_occurrence_preview(media, bad)
