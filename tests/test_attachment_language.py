"""Bounded Chinese document-role signals must not erase content or invent identity."""
import pytest

from watertreatment.attachment_language import (
    METHOD_HEADING, REFERENCE_HEADING, SI_HEADING, SUPPLEMENT_LABEL,
    article_structure, chinese_title, chinese_title_match, is_article_boundary,
    language_hint, main_article_marker,
)


TITLE = "多孔复合材料用于太阳能界面蒸发与污染物催化降解"


@pytest.mark.parametrize("abstract,intro", [
    ("摘要：本文研究水净化。", "1 引言"),
    ("摘 要 本文研究水净化。", "１．引 言"),
    ("【摘要】本文研究水净化。", "一、绪论"),
    ("摘要", "(1) 前言"),
    ("摘要", "第一章緒論"),
])
def test_chinese_structure_accepts_typographic_and_numbered_headings(abstract, intro):
    text = TITLE + "\n" + abstract + "\n研究内容。\n" + intro + "\n正文。"
    assert article_structure(text) == {"abstract": True, "introduction": True}
    assert is_article_boundary(abstract)
    assert is_article_boundary(intro)


def test_chinese_heading_words_embedded_in_sentences_are_not_structural_proof():
    assert article_structure("该摘要分析是一个术语。\n本文引言中的参考内容。") == {
        "abstract": False, "introduction": False}


@pytest.mark.parametrize("heading", ["补充材料", "支 撑 材 料", "支持信息：", "補充資訊", "輔助材料", "电子补充材料"])
def test_explicit_chinese_si_heading(heading):
    assert SI_HEADING.search(heading + "\n" + TITLE)


@pytest.mark.parametrize("notice", [
    "补充材料可在期刊网站下载。",
    "本研究的支持信息见网站。", "关于补充材料的说明：见链接。",
])
def test_si_availability_sentence_is_not_a_chinese_declaration(notice):
    assert not SI_HEADING.search(notice)


@pytest.mark.parametrize("line", ["2 实验部分", "S1 材料与方法", "1.2 实验方法", "三、表征", "2 材料與方法", "参考文献"])
def test_chinese_method_sections_are_not_titles(line):
    assert METHOD_HEADING.match(line)
    assert chinese_title(line + "\n图S1") is None


@pytest.mark.parametrize("line", ["参考文献", "參考文獻", "5 参考文献："])
def test_chinese_reference_heading(line):
    assert REFERENCE_HEADING.search(line)


@pytest.mark.parametrize("line", ["图S1. 样品表征", "圖 S2 样品表征", "表Ｓ3 浓度", "Figure S3. Spectrum", "Table S1. Conditions"])
def test_chinese_and_english_supplement_labels(line):
    assert SUPPLEMENT_LABEL.search(line)


def test_chinese_title_preserves_source_and_skips_journal_affiliation_authors():
    text = ("第12卷 第3期\n高等学校化学学报\n收稿日期：2026-01-01\n" + TITLE
            + "\n张三1，李四2，王五*\n某大学材料学院\n摘要：研究水净化。\n关键词：水净化\n1 引言")
    assert chinese_title(text) == TITLE
    assert text.startswith("第12卷")  # input was not normalized in place


def test_chinese_title_from_si_and_full_main_match_without_translation():
    title = "MoS₂复合材料用于太阳能界面蒸发与催化降解污染物"
    main = title + "\n张三，李四\n摘要\n摘要内容。\n1 引言"
    supplement = "补充材料\n" + title + "\n张三，李四\n实验方法\n图S1"
    assert chinese_title(main) == title
    assert chinese_title(supplement, supplementary=True) == title
    assert chinese_title_match(chinese_title(main), chinese_title(supplement, supplementary=True))


def test_verified_chinese_metadata_recovers_wrap_but_unrelated_metadata_does_not():
    full = "多孔复合材料用于太阳能界面蒸发与污染物催化降解"
    wrapped = "多孔复合材料用于太阳能界面\n蒸发与污染物催化降解"
    text = wrapped + "\n张三，李四\n摘要：实验性研究。"
    assert chinese_title(text, full) == full
    assert chinese_title(text, "一种用于锂离子电池的新型复合电极材料") == wrapped.replace("\n", " ")


def test_generic_word_overlap_does_not_link_different_chinese_papers_or_formulae():
    a = "MoS2复合材料用于太阳能界面蒸发与催化降解污染物"
    b = "WS2复合材料用于太阳能界面蒸发与催化降解污染物"
    assert chinese_title_match(a, b) is False
    assert chinese_title_match(TITLE, TITLE + "的机理研究") is False
    assert chinese_title_match("界面蒸发", "界面蒸发") is False
    assert chinese_title_match(TITLE, "Solar evaporation and catalytic degradation") is False
    assert chinese_title_match("Solar evaporation and catalytic degradation", "Water treatment") is None


def test_chinese_identity_can_ignore_wrap_and_punctuation_but_keeps_chemistry():
    a = "MoS₂复合材料：太阳能界面蒸发与污染物催化降解"
    b = "MoS2复合材料\n太阳能界面蒸发与污染物催化降解"
    assert chinese_title_match(a, b) is True
    assert chinese_title_match(a, b.replace("MoS2", "MoS3")) is False


@pytest.mark.parametrize("left,right", [("Co", "CO"), ("Fe2+", "Fe2-"), ("(CH2)4", "CH24"), ("CoCl2·6H2O", "CoCl26H2O")])
def test_chinese_title_matching_preserves_chemical_case_signs_and_grouping(left, right):
    suffix = "复合材料用于太阳能界面蒸发与催化降解污染物"
    assert chinese_title_match(left + suffix, right + suffix) is False


def test_title_boundary_prevents_abstract_or_keywords_from_becoming_title():
    assert chinese_title("摘要：多孔复合材料用于太阳能界面蒸发与污染物催化降解\n1 引言") is None
    assert chinese_title("关键词：多孔复合材料用于太阳能界面蒸发与污染物催化降解") is None
    assert chinese_title("正文附加说明和材料性质在这里作了介绍。\n实验方法") is None


@pytest.mark.parametrize("line", ["关键词：水处理", "关 键 词 水处理", "關鍵字：材料", "【關鍵詞】水处理"])
def test_keywords_are_chinese_article_header_boundaries(line):
    assert is_article_boundary(line)


def test_language_hint_is_only_a_sample_signal_and_bilingual_is_not_excluded():
    assert language_hint(TITLE) == "zh"
    assert language_hint("This is a synthetic English article abstract.") == "en"
    assert language_hint(TITLE + "\n" + "English abstract contains several ordinary words. " * 5) == "mixed"
    assert language_hint("MoS2 TiO2 1 2") == "undetermined"


def test_english_structural_signals_remain_supported():
    assert article_structure("Title\nABSTRACT\nEnglish content\n1. Introduction") == {
        "abstract": True, "introduction": True}
    assert SI_HEADING.match("Electronic Supplementary Information")
    assert METHOD_HEADING.match("S1. Experimental details")
    assert REFERENCE_HEADING.match("3. References")


@pytest.mark.parametrize("bullet", ["■", "▎", "•", "▪", "◆"])
def test_publisher_bullets_and_lowercase_letterspaced_headings(bullet):
    abstract = bullet + " a b s t r a c t"
    intro = bullet + " 1. Introduction"
    assert is_article_boundary(abstract)
    assert is_article_boundary(intro)
    assert article_structure(abstract + "\nContent\n" + intro) == {"abstract": True, "introduction": True}
    assert is_article_boundary(bullet + " 摘 要：研究内容")


def test_heading_recognition_does_not_rewrite_chemical_names_or_input_text():
    text = "Co-N-C\nMoS2/g-C3N4\n■ a b s t r a c t\n1 Introduction"
    assert article_structure(text) == {"abstract": True, "introduction": True}
    assert text.startswith("Co-N-C\nMoS2/g-C3N4")
    assert not is_article_boundary("Co-N-C a catalyst with abstract-like pore architecture")
    assert SI_HEADING.match("■ Supporting Information")
    assert REFERENCE_HEADING.match("■ REFERENCES")


@pytest.mark.parametrize("marker", ["Research Article", "■ RESEARCH ARTICLE", "Original Article", "Full Paper",
    "Article", "Article Type: Research Article", "▎ Research Article — Open Access"])
def test_explicit_early_publication_markers_without_abstract(marker):
    assert main_article_marker(marker + "\n" + TITLE + "\nDOI:10.1234/example\n1. Introduction")


@pytest.mark.parametrize("text", [
    "Supporting Information\nResearch Article\nTitle\nExperimental section",
    "Research Article\nSupporting Information\nTitle\nExperimental section",
    "Supporting Information for a Research Article\nTitle",
    "This research article reports additional characterization.",
    "Title\n1. Introduction\nResearch Article",
    "Header\n" * 36 + "Research Article",
    "补充材料\nArticle\n实验方法",
])
def test_publication_markers_are_not_inferred_from_si_citations_or_later_body(text):
    assert not main_article_marker(text)


def test_corrupted_chinese_metadata_can_locate_but_not_replace_source_title():
    raw = "泡沫碳/g-C3N4 光热-光催化协同\n蒸发器构建及其污水蒸发\n降解性能"
    bad_metadata = "泡沫碳/g-C3N4光热-光催化协同?蒸发器构建及其污水蒸发?降解性能"
    text = raw + "\n王明，李文\n摘要：论文内容。\n1 引言"
    result = chinese_title(text, bad_metadata)
    assert result == raw.replace("\n", " ")
    assert "?" not in result
    assert "降解性能" in result


def test_unmatched_replacement_character_in_metadata_cannot_fill_source_text():
    text = TITLE + "\n张三，李四\n摘要：论文内容。"
    assert chinese_title(text, "不相关的损坏�标题以及其他论文的研究内容") == TITLE


def test_spaced_chinese_journal_banner_is_not_a_title_when_metadata_is_missing():
    text = "高 等 学 校 化 学 学 报\n" + TITLE + "\n张三，李四\n摘要：本文内容。"
    assert chinese_title(text) == TITLE
