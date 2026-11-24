"""Conservative language signals for attachment identification, not paper reading.

Chinese headings and titles are inspected in the same bounded sample as English
ones. Original text is returned unchanged; normalization is used only for exact
title comparison. A language hint never determines a document role by itself.
"""
from __future__ import annotations

import re
import unicodedata


_SPACE = r"[ \t\u3000]*"
_CHINESE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _spaced(word: str) -> str:
    return _SPACE.join(map(re.escape, word))


def _alternatives(*words: str) -> str:
    return "(?:" + "|".join(_spaced(word) for word in words) + ")"


_NUMBER = (r"(?:(?:第[0-9０-９一二三四五六七八九十]+[章节節]|"
           r"[SsＳ]?[0-9０-９]+(?:[.．][0-9０-９]+)*|[一二三四五六七八九十]+)[.．、)）\]】|｜ \t\u3000]*)?")
_BULLETS = r"(?:[■▎•●▪‣◆▲▌►□◼◾]" + _SPACE + r")*"
_OPEN = _SPACE + _BULLETS + r"(?:[\[【（(]" + _SPACE + r")?"
_END = r"(?=[ \t\u3000:：\]】）)\n]|$)"
_ABSTRACT_ZH = _alternatives("摘要", "摘錄")
_INTRO_ZH = _alternatives("引言", "绪论", "緒論", "前言", "引论", "引論")
_KEYWORDS_ZH = _alternatives("关键词", "关键字", "關鍵詞", "關鍵字")
_SI_ZH = _alternatives("补充材料", "补充信息", "补充资料", "支持信息", "支撑材料", "辅助材料",
                       "補充材料", "補充資訊", "補充資料", "支持資訊", "支撐材料", "輔助材料")
_METHOD_ZH = _alternatives("实验", "实验部分", "实验方法", "实验材料与方法", "材料与方法", "材料和方法",
    "表征", "目录", "参考文献", "结果与讨论", "结果和讨论", "结论",
    "實驗", "實驗部分", "實驗方法", "材料與方法", "材料和方法", "表徵", "目錄", "參考文獻", "結果與討論", "結論")

_ABSTRACT_EN = _spaced("abstract") + r"\b"
_INTRO_EN = _spaced("introduction") + r"\b"
ABSTRACT_HEADING = re.compile(r"^" + _OPEN + _ABSTRACT_ZH + _END + r"|\b" + _ABSTRACT_EN, re.I | re.M)
INTRODUCTION_HEADING = re.compile(r"^" + _OPEN + _NUMBER + r"(?:" + _INTRO_ZH + _END
    + r"|" + _INTRO_EN + r")", re.I | re.M)
ARTICLE_BOUNDARY = re.compile(r"^" + _OPEN + _NUMBER + r"(?:" + _ABSTRACT_ZH + _END + "|"
    + _INTRO_ZH + _END + "|" + _KEYWORDS_ZH + _END
    + "|" + _ABSTRACT_EN + "|" + _spaced("articleinfo") + r"\b|"
    + _spaced("keywords") + r"\b|" + _INTRO_EN + r")", re.I)

SI_HEADING = re.compile(
    r"^" + _OPEN + r"(?:\d+[ \t]*)?(?:(?:electronic|online)\s+)?(?:supporting|supplementary|supplemental|support)\s*"
    r"(?:information|materials?|data|files?|appendi(?:x|ces))\b|^" + _OPEN
    + r"(?:(?:电子|電子|在线|在線|网络|網絡)" + _SPACE + r")?" + _SI_ZH + _END, re.I | re.M)
METHOD_HEADING = re.compile(r"^" + _OPEN + _NUMBER + r"(?:" + _METHOD_ZH + _END
    + r"|(?:S?\d+[.\s]*)?(?:experimental|materials? and methods|methods|characterization|references|contents)\b)", re.I)
REFERENCE_HEADING = re.compile(r"^" + _OPEN + _NUMBER + r"(?:references|bibliography|"
    + _alternatives("参考文献", "參考文獻") + r")" + _SPACE + r"[:：]?" + _SPACE + r"$", re.I | re.M)
SUPPLEMENT_LABEL = re.compile(r"\b(?:Fig(?:ure)?\.?|Table)\s+S\d+\b|(?:图|圖|表)\s*[SsＳ]\s*\d+", re.I)

_TITLE_NOISE = re.compile(r"https?://|doi\s*[:：]|doi\.org|www\.|@|版权|版權|收稿|修回|修订|修訂|录用|錄用|"
    r"基金|中图分类|中圖分類|文章编号|文章編號|文献标识|文獻標識|通讯作者|通信作者|通訊作者|"
    r"(?:大学|大學|学院|學院|研究所|实验室|實驗室|研究中心)|^第?\s*\d+\s*(?:卷|期)|"
    r"^(?:作者|著者|致谢|致謝|附件|说明|說明)[:：]?|^\s*(?:图|圖|表)\s*\d|(?:学报|學報|期刊)$", re.I)


def article_structure(text: str) -> dict[str, bool]:
    return {"abstract": bool(ABSTRACT_HEADING.search(text[:14000])),
            "introduction": bool(INTRODUCTION_HEADING.search(text))}


def is_article_boundary(line: str) -> bool:
    return bool(ARTICLE_BOUNDARY.match(line))


def main_article_marker(text: str, *, physical_first_page: bool = False) -> bool:
    """Return a bounded, explicit publication-type signal, never a role verdict.

    The caller must also require an introduction, a title, a source-local DOI
    and absence of SI signals. A citation mentioning a research article is not
    a publication-type declaration. SI declarations defeat any prefix marker.
    """
    marker = re.compile(r"^" + _OPEN + r"(?:article" + _SPACE + r"type" + _SPACE + r"[:：]" + _SPACE + r")?"
        + r"(?:research" + _SPACE + r"article|original" + _SPACE + r"article|original" + _SPACE
        + r"research(?:" + _SPACE + r"article)?|research" + _SPACE + r"paper|full" + _SPACE
        + r"paper|paper|accepted" + _SPACE + r"article|article)" + _SPACE + r"(?:[|:：—-]" + _SPACE + r"open" + _SPACE + r"access)?" + _SPACE + r"$", re.I)
    prefix = []
    for line in text[:64000 if physical_first_page else 8000].splitlines()[:500 if physical_first_page else 35]:
        if not physical_first_page and is_article_boundary(line):
            break
        prefix.append(line)
    # PDF text order can place physical-page headers after columns. Keep this
    # relaxation limited to the explicitly supplied first physical page.
    declarations = prefix[:12] if physical_first_page else prefix
    if any(SI_HEADING.search(line) for line in declarations):
        return False
    return any(marker.fullmatch(line) for line in prefix)


def language_hint(text: str) -> str:
    """Describe the bounded sample only; bilingual abstracts can yield mixed."""
    chinese = len(_CHINESE.findall(text))
    english_words = len(re.findall(r"\b[A-Za-z]{3,}\b", text))
    if chinese >= 20 and english_words >= 20:
        return "mixed"
    if chinese >= 8:
        return "zh"
    if english_words >= 4:
        return "en"
    return "undetermined"


def _normalized_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("−", "-")
    # Latin case, signs and grouping can distinguish chemical identities (Co
    # versus CO, Fe2+ versus Fe2-, (CH2)4 versus CH24). Ambiguous typography
    # should cause a conservative nonmatch, never automatic chemical merging.
    return "".join(c for c in normalized if c.isalnum() or c in "+-±·()./")


def chinese_title_match(left: str | None, right: str | None) -> bool | None:
    """Require exact complete title identity, never common Chinese keywords.

    None asks the caller to apply its existing English-only comparison.
    General title punctuation and line wrapping are insignificant; chemical
    punctuation, formula case and qualifiers remain significant. No automatic
    bilingual translation is used.
    """
    if not left or not right:
        return False
    if not _CHINESE.search(left + right):
        return None
    if min(len(_CHINESE.findall(left)), len(_CHINESE.findall(right))) < 8:
        return False
    return _normalized_title(left) == _normalized_title(right)


def _title_line(line: str) -> bool:
    return (8 <= len(_CHINESE.findall(line)) and len(line) <= 350
            and not _TITLE_NOISE.search(line)
            and not _TITLE_NOISE.search(re.sub(r"[ \t\u3000]+", "", line))
            and not is_article_boundary(line)
            and not METHOD_HEADING.match(line) and not SI_HEADING.match(line)
            and not SUPPLEMENT_LABEL.match(line)
            and not re.search(r"[。！？；;]", line))


def _author_line(line: str) -> bool:
    if re.match(r"^(?:作者|著者)" + _SPACE + r"[:：]", line):
        return True
    names = re.split(r"[,，、;； \t]+", re.sub(r"[0-9０-９*†‡①②③④⑤⑥⑦⑧⑨]", "", line).strip())
    return len(names) >= 2 and all(re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,4}", part) for part in names)


def chinese_title(text: str, metadata_title: str | None = None, *, supplementary: bool = False) -> str | None:
    """Recover a source-language title from the header, conservatively.

    Metadata is accepted only when its complete normalized text occurs in the
    header. Wrapped Chinese titles require two substantial title lines, or
    verified metadata; this avoids absorbing short Chinese author names.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if supplementary:
        for index, line in enumerate(lines[:12]):
            marker = SI_HEADING.match(line)
            if marker:
                inline = re.sub(r"^\s*(?:for\b|[:：-])\s*", "", line[marker.end():], flags=re.I).strip()
                lines = ([inline] if inline else []) + lines[index + 1:index + 24]
                break
        else:
            return None
    header = []
    for line in lines[:24]:
        if is_article_boundary(line) or METHOD_HEADING.match(line):
            break
        header.append(line)
    if metadata_title:
        candidate = " ".join(metadata_title.split())
        if (_title_line(candidate) and _normalized_title(candidate)
                and _normalized_title(candidate) in _normalized_title(" ".join(header))):
            if not re.search(r"[?\ufffd\ue000-\uf8ff]", candidate):
                return candidate
            # Corrupted metadata may substitute '?' for a line break. It can
            # locate a complete matching source span but must never become the
            # displayed title. Only actual header characters are returned.
            expected = _normalized_title(candidate)
            for index, line in enumerate(header):
                if not _title_line(line) or _author_line(line):
                    continue
                span = []
                for continuation in header[index:index + 6]:
                    if (_author_line(continuation) or _TITLE_NOISE.search(continuation)
                            or is_article_boundary(continuation) or METHOD_HEADING.match(continuation)
                            or SI_HEADING.match(continuation) or not _CHINESE.search(continuation)):
                        break
                    span.append(continuation)
                    observed = _normalized_title(" ".join(span))
                    if observed == expected:
                        return " ".join(span)
                    if not expected.startswith(observed):
                        break
    for index, line in enumerate(header):
        if not _title_line(line) or _author_line(line):
            continue
        parts = [line]
        for continuation in header[index + 1:index + 3]:
            if not _title_line(continuation) or _author_line(continuation):
                break
            if len(" ".join([*parts, continuation])) > 500:
                break
            parts.append(continuation)
        return " ".join(parts)
    return None
