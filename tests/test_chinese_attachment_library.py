"""Native Word fixtures exercise Chinese detection through the real library path."""
from test_web_library import library, folder, docx


TITLE = "多孔复合材料用于太阳能界面蒸发与污染物催化降解"


def main_text(title=TITLE):
    return (title + "\n张三1，李四2\n某大学材料学院\n摘 要：本文研究蒸发与催化功能。"
            "\n关键词：水处理；复合材料\n１．引 言\n利用自然太阳光开展水净化实验。")


def si_text(title=TITLE):
    return "支撑材料\n" + title + "\n张三，李四\nS1 实验方法\n图 S1 样品的形貌与结构表征。"


def test_native_chinese_docx_main_and_si_are_matched_by_complete_title(library):
    path = folder(library, "12_中文实验")
    docx(path / "article.docx", main_text())
    docx(path / "attachment.docx", si_text())
    original = {p.name: p.read_bytes() for p in path.iterdir()}
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["title"] == TITLE
    files = {file["name"]: file for file in paper["files"]}
    assert files["article.docx"]["role"] == "main_article"
    assert files["attachment.docx"]["role"] == "supporting_information"
    assert files["attachment.docx"]["identity_status"] == "matched"
    assert all(f["signals"]["language_hint"] == "zh" for f in files.values())
    assert all(f["signals"]["language_scope"] == "identification_sample_only" for f in files.values())
    assert library.roles_for_import(paper["id"]) == {"article.docx": "main_article", "attachment.docx": "supporting_information"}
    assert original == {p.name: p.read_bytes() for p in path.iterdir()}
    assert paper["scientific_eligibility"] == "not_assessed"


def test_chinese_article_si_availability_notice_does_not_become_a_mixed_document(library):
    path = folder(library, "13_中文说明")
    text = main_text() + "\n补充材料\n相关数据可在期刊网站获取。"
    docx(path / "article.docx", text)
    docx(path / "attachment.docx", si_text())
    paper = library.scan([str(path)])[0]
    main = next(f for f in paper["files"] if f["name"] == "article.docx")
    assert main["identification"]["role"] == "main_article"
    assert main["identification"]["status"] == "auto"
    assert main["segments"] == []
    assert paper["attachment_status"] == "ready"


def test_different_chemical_identity_in_chinese_si_does_not_match_by_common_words(library):
    path = folder(library, "14_中文材料对照")
    docx(path / "article.docx", main_text("Co复合材料用于太阳能界面蒸发与污染物催化降解"))
    docx(path / "attachment.docx", si_text("CO复合材料用于太阳能界面蒸发与污染物催化降解"))
    paper = library.scan([str(path)])[0]
    supplement = next(f for f in paper["files"] if f["name"] == "attachment.docx")
    assert supplement["role"] == "supporting_information"
    assert supplement["identity_status"] == "folder_only"
    assert paper["attachment_status"] == "needs_confirmation"
    assert paper["review_required_count"] == 1


def test_wrapped_chinese_title_matches_without_joining_chinese_author_names(library):
    path = folder(library, "15_中文换行")
    docx(path / "article.docx", main_text("多孔复合材料用于太阳能界面\n蒸发与污染物催化降解"))
    docx(path / "attachment.docx", si_text())
    paper = library.scan([str(path)])[0]
    assert paper["title"] == "多孔复合材料用于太阳能界面 蒸发与污染物催化降解"
    assert "张三" not in paper["title"]
    assert paper["attachment_status"] == "ready"


def test_short_rare_character_title_is_left_unidentified_not_translated_or_guessed(library):
    path = folder(library, "16_标题待核")
    docx(path / "article.docx", main_text("䶮龘靐"))
    docx(path / "attachment.docx", si_text("䶮龘靐"))
    paper = library.scan([str(path)])[0]
    main = next(f for f in paper["files"] if f["name"] == "article.docx")
    assert main["role"] == "main_article"
    assert main["signals"]["title"] is None
    assert "䶮龘靐" in main["preview_text"]
    assert paper["title"] is None
    assert paper["attachment_status"] == "needs_confirmation"


def test_traditional_chinese_headings_keep_native_title_and_identity(library):
    path = folder(library, "17_繁體中文")
    title = "多孔複合材料用於太陽能界面蒸發與污染物催化降解"
    docx(path / "article.docx", title + "\n王明，李文\n摘要：本研究具有雙功能。\n關鍵詞：水處理\n第一章緒論\n研究內容。")
    docx(path / "attachment.docx", "補充資訊\n" + title + "\n王明，李文\n實驗方法\n圖S1形貌。")
    paper = library.scan([str(path)])[0]
    assert paper["attachment_status"] == "ready"
    assert paper["title"] == title
    assert paper["supporting_information_state"] == "present"
