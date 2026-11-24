"""Local literature inventory and bounded, evidence-led attachment classification.

This is an acquisition aid, not a full-text reading or scientific eligibility
decision. It never renames source files and never calls a model or the database.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import stat
import threading
import zipfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .settings import Settings
from .sources import MAX_PACKAGE_BYTES, W, _read_member, _xml
from .storage import DataStore, digest_json, hash_file, safe_name, utc_now
from .attachment_language import (
    SI_HEADING, METHOD_HEADING, REFERENCE_HEADING, SUPPLEMENT_LABEL,
    is_article_boundary, article_structure, language_hint, chinese_title, chinese_title_match,
    main_article_marker,
)
from .collection_policy import load_collection_policy, is_inventory_noise
from .attachment_reviews import load_attachment_review, make_review_pointer


CLASSIFIER_VERSION = "attachment-roles/1.5"
EXTENSIONS = {".pdf", ".docx", ".doc"}
MAIN_ROLES = {"main_article", "main_article_with_supporting_information"}
IMPORT_ROLES = MAIN_ROLES | {"supporting_information"}
ROLES = IMPORT_ROLES | {"unknown", "other"}
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
STOP_WORDS = {"the", "a", "an", "of", "for", "and", "in", "with", "on", "to", "by", "from", "via", "as"}
TITLE_BOILERPLATE = re.compile(
    r"©|\bcopyright\b|\ball rights (?:are )?reserved\b|\bcreative commons\b|\bCC[ -]BY\b|"
    r"\bElsevier\s+B\.?V\.?|\blicen[cs]e(?:d)?\s+(?:under|agreement|terms)\b|"
    r"^(?:[ep]-?)?ISSN\b|^\d{4}-\d{3}[\dX](?:\b|/)|"
    r"doi\.org|https?://|www\.|^doi:|contents lists|journal homepage|science.?direct|"
    r"^ARTICLE\s*INFO\b|^keywords\b|^received\b|^accepted\b|^available online\b|^published\b|"
    r"^e-?mail\b|^correspond|university|department|institute|school of|college of|laboratory|@|"
    r"\bjournal\b|\b\d{1,4}\s*\((?:19|20)\d{2}\)\s*\d+", re.I)


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


def _clean_lines(text: str) -> str:
    # Some PDF decoders emit UTF-16 surrogate code units. Join valid pairs and
    # visibly replace isolated units before UTF-8 persistence; originals stay
    # untouched and the sample records the repair separately.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        text = text.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")
    # Some publisher layouts space out ABSTRACT or ARTICLE INFO.
    text = re.sub(r"\b(?:[A-Z] ){3,}[A-Z]\b", lambda m: m[0].replace(" ", ""), text)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\r", "\n").splitlines())


SUPPLEMENT_SECTION = re.compile(
    r"^\s*(?:(?:Fig(?:ure)?\.?|Table|Text|Note|Scheme|Equation|Appendix|Section)\s*S[.\s]*\d+\b|"
    r"(?:图|圖|表|附录|附錄)\s*S[.\s]*\d+\b|S\d+\s*[.:：])", re.I | re.M)


def _identity_head(text: str) -> str:
    """Stop a supplement's identity header before methods/captions/references."""
    lines = []
    for line in text.splitlines():
        if (METHOD_HEADING.match(line) or REFERENCE_HEADING.match(line)
                or SUPPLEMENT_SECTION.match(line) or is_article_boundary(line)):
            break
        lines.append(line)
        if len(lines) >= 35:
            break
    return "\n".join(lines)


def _dois(text: str) -> list[str]:
    return list(dict.fromkeys(m[0].rstrip(".,;:)]}").casefold() for m in DOI_RE.finditer(text)))


def _author_line(line: str) -> bool:
    """A conservative boundary for common author lists, not name inference."""
    if re.match(r"^(?:authors?|research authors?|submitted by|by)\b", line, re.I):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", line)
    if not words:
        return False
    capitalized = sum(w[0].isupper() for w in words) / len(words)
    # Capitalized short lines are also common wrapped titles. Use explicit
    # author-list/affiliation punctuation rather than guessing from case alone.
    return ("," in line and len(words) >= 4 and capitalized >= .7) or (
        len(words) >= 2 and capitalized >= .4 and bool(re.search(r"[A-Za-z][, ]*\*|\[[a-z]\]", line)))


def _title(text: str, metadata_title: str | None = None, *, supplementary: bool = False) -> str | None:
    translated = chinese_title(text, metadata_title, supplementary=supplementary)
    if translated:
        return translated
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if supplementary:
        for index, line in enumerate(lines[:12]):
            match = SI_HEADING.search(line)
            if match:
                inline = re.sub(r"^\s*(?:for\b|[:：-])\s*", "", line[match.end():], flags=re.I).strip()
                lines = ([inline] if inline else []) + lines[index + 1:index + 12]
                break
        else:
            return None
    header = []
    for line in lines[:24]:
        if is_article_boundary(line):
            break
        header.append(line)
    if metadata_title:
        candidate = " ".join(metadata_title.split())
        normalized = re.sub(r"[^a-z0-9]", "", candidate.casefold())
        header_text = re.sub(r"[^a-z0-9]", "", " ".join(header).casefold())
        if (20 <= len(candidate) <= 500 and len(candidate.split()) >= 4 and not TITLE_BOILERPLATE.search(candidate)
                and not re.search(r"^(?:untitled|microsoft word|supporting|supplementary|supplemental|document)\b|\.pdf$", candidate, re.I)
                and normalized and normalized in header_text):
            return candidate
    for index, line in enumerate(lines[:24]):
        if is_article_boundary(line):
            break
        if supplementary and METHOD_HEADING.match(line):
            break
        if TITLE_BOILERPLATE.search(line):
            continue
        if SUPPLEMENT_SECTION.match(line):
            break
        if "\ufffd" in line or SI_HEADING.search(line) or METHOD_HEADING.match(line) or _author_line(line):
            continue
        words = re.findall(r"[A-Za-z][A-Za-z-]*", line)
        if len(words) < 3 or len(line) < 18 or len(line) > 350 or len(re.findall(r"\d", line)) > 12:
            continue
        parts = [line]
        for continuation in lines[index + 1:index + 5]:
            if (TITLE_BOILERPLATE.search(continuation) or _author_line(continuation)
                    or SI_HEADING.search(continuation) or METHOD_HEADING.match(continuation) or SUPPLEMENT_SECTION.match(continuation)
                    or is_article_boundary(continuation)
                    or not re.search(r"[A-Za-z]", continuation)):
                break
            if len(" ".join([*parts, continuation])) > 500:
                break
            parts.append(continuation)
        candidate = " ".join(parts)
        if len(re.findall(r"[A-Za-z][A-Za-z-]*", candidate)) >= 5 and len(candidate) >= 30:
            return candidate
    return None


def _title_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    chinese_match = chinese_title_match(left, right)
    if chinese_match is not None:
        return chinese_match
    a = {w for w in re.findall(r"[a-z0-9]+", left.casefold()) if w not in STOP_WORDS and len(w) > 1}
    b = {w for w in re.findall(r"[a-z0-9]+", right.casefold()) if w not in STOP_WORDS and len(w) > 1}
    return min(len(a), len(b)) >= 4 and len(a & b) >= 4 and len(a & b) / min(len(a), len(b)) >= .8


def _publisher_id(name: str) -> str | None:
    # Publisher identifiers are stronger than generic names like main/si.
    pii = re.search(r"S\d{8,20}", name, re.I)
    return pii[0].casefold() if pii else None


def _supplement_markers(segments: list[dict], title: str | None, *, word: bool) -> list[dict]:
    """Distinguish SI contents from the article's SI availability notice."""
    markers = []
    for index, segment in enumerate(segments):
        match = SI_HEADING.search(segment["text"])
        if not match:
            continue
        text = segment["text"][match.start():]
        if word:
            text += "\n" + "\n".join(s["text"] for s in segments[index + 1:index + 12])
        repeated_title = _title_match(title, _title(text, supplementary=True))
        supplementary_sections = (bool(SUPPLEMENT_LABEL.search(text))
                                  and any(METHOD_HEADING.match(line) for line in text.splitlines()[1:]))
        if repeated_title or supplementary_sections:
            markers.append({"location": segment["location"], "marker": match[0].strip(),
                            "content_evidence": "repeated_article_title" if repeated_title else "supplementary_sections"})
    return markers


def _sample_file(path: Path) -> dict:
    """Read first two and last PDF pages, or bounded native Word paragraphs."""
    sampled = {"segments": [], "metadata_title": None, "page_count": None,
               "scope": "identification_sample_only", "status": "ok"}
    try:
        if path.suffix.casefold() == ".pdf":
            with path.open("rb") as stream:
                header = stream.read(1024).lstrip(b"\xef\xbb\xbf\x00 \t\r\n").lower()
            if header.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                sampled.update(status="unreadable", format_issue="html_saved_as_pdf",
                    error="文件实际是HTML网页，不是真正的PDF；请重新取得论文或SI文件，现文件保留。")
                return sampled
            from pypdf import PdfReader
            with path.open("rb") as stream:
                reader = PdfReader(stream)
                if reader.is_encrypted and not reader.decrypt(""):
                    raise ValueError("PDF requires a password")
                sampled["page_count"] = len(reader.pages)
                title = (reader.metadata or {}).get("/Title")
                sampled["metadata_title"] = str(title)[:500] if title else None
                for page in sorted({0, 1, len(reader.pages) - 1} & set(range(len(reader.pages)))):
                    text = _clean_lines((reader.pages[page].extract_text() or "")[:64000])
                    sampled["segments"].append({"location": {"physical_page": page + 1}, "text": text})
        elif path.suffix.casefold() == ".docx":
            with zipfile.ZipFile(path) as package:
                if sum(member.file_size for member in package.infolist()) > MAX_PACKAGE_BYTES:
                    raise ValueError("DOCX expanded size exceeds limit")
                root = _xml(_read_member(package, "word/document.xml"))
                if root.tag != f"{{{W}}}document":
                    raise ValueError("not a Word document")
                total = 0
                for index, paragraph in enumerate(root.iter(f"{{{W}}}p"), 1):
                    if index > 100 or total >= 64000:
                        break
                    text = _clean_lines("".join(n.text or "" for n in paragraph.iter(f"{{{W}}}t")))[:4000]
                    total += len(text)
                    if text:
                        sampled["segments"].append({"location": {"part": "word/document.xml", "paragraph_index": index}, "text": text})
        else:
            sampled.update(status="unsupported", format_issue="legacy_doc_requires_readable_derivative",
                           error="旧版DOC需要提供DOCX/PDF衍生文件；原件保留。")
        if sampled["status"] == "ok" and not any(s["text"].strip() for s in sampled["segments"]):
            sampled.update(status="unreadable", error="抽样页没有可识别文字，不能仅按文件名确认角色。")
    except Exception as error:
        sampled.update(status="unreadable", error=f"附件识别读取失败：{type(error).__name__}")
        if isinstance(error, zipfile.BadZipFile):
            sampled.update(format_issue="invalid_docx_package",
                error="Word文件容器无法读取；请检查文件能否正常打开并另存有效DOCX，现文件保留。")
    # A metadata title can be malformed even when page text is readable. Keep
    # that separate from role confidence and never let it abort the corpus.
    metadata = sampled.get("metadata_title")
    if metadata and any(0xD800 <= ord(char) <= 0xDFFF for char in metadata):
        sampled["metadata_title"] = _clean_lines(metadata)
        sampled.setdefault("warnings", []).append("metadata_utf16_surrogates_repaired_for_identification")
    if any("\ufffd" in segment["text"] for segment in sampled["segments"]):
        sampled.setdefault("warnings", []).append("sample_contains_replacement_characters_not_scientific_reading")
    return sampled


def _classify(path: Path, digest: str, sampled: dict) -> dict:
    segments = sampled["segments"]
    first = "\n".join(s["text"] for s in segments[:2]) if path.suffix.casefold() == ".pdf" else "\n".join(s["text"] for s in segments[:24])
    all_text = "\n".join(s["text"] for s in segments)
    first_page = segments[0]["text"] if path.suffix.casefold() == ".pdf" and segments else first
    si = bool(SI_HEADING.search(first[:2000]))
    structure = article_structure(first[:14000])
    abstract, intro = structure["abstract"], structure["introduction"]
    primary_title = _title(first, sampled.get("metadata_title"))
    si_markers = _supplement_markers(segments, primary_title, word=path.suffix.casefold() != ".pdf")
    mixed = bool(abstract and intro and si_markers)
    main_hint = bool(re.search(r"(?:^|[-_\s])main(?:[-_\s.]|$)|正文|主文", path.name, re.I))
    si_hint = bool(re.search(r"(?:^|[-_\s])(?:si|supp|mmc\d*)(?:[-_\s.]|$)|support|supplement|补充|支撑", path.name, re.I))
    head = REFERENCE_HEADING.split(first_page, maxsplit=1)[0]
    if path.suffix.casefold() != ".pdf" or (si or si_hint) and not (abstract and intro):
        head = _identity_head(head)
    doi_candidates = _dois(head)
    article_marker = main_article_marker(first_page, physical_first_page=True) if path.suffix.casefold() == ".pdf" else main_article_marker(first)
    journal_main = bool(intro and primary_title and len(doi_candidates) == 1 and article_marker and not si)
    # Some letters have a labelled abstract but no introduction heading. A
    # first-page publisher citation/read-online block is additional evidence;
    # a generic SI-availability notice is not this publication signature.
    publisher_citation = bool(re.search(r"\bCite\s+This\s*:", first_page, re.I)
                              and re.search(r"\bRead\s+Online", first_page, re.I))
    short_article = bool(abstract and publisher_citation and len(doi_candidates) == 1 and not si)
    accepted_article = bool(article_marker and primary_title and len(doi_candidates) == 1 and not si
        and re.search(r"^\s*Accepted\s+Article\s*$", first_page, re.I | re.M)
        and re.search(r"accepted\s+after\s+peer\s+review", first_page, re.I)
        and re.search(r"^\s*Authors?\s*:", first_page, re.I | re.M))
    role, status = "unknown", "unknown"
    reasons = []
    if sampled["status"] != "ok":
        status = sampled["status"]
        reasons.append(sampled.get("error", "无法确定角色"))
    elif mixed:
        status = "mixed"
        reasons.append("抽样中同时出现正文结构和补充材料标题，需确认是否为合并文件；未推定完整分界。")
    elif abstract and intro or journal_main or short_article or accepted_article:
        role, status = "main_article", "auto"
        reasons.append("起始页具有摘要和引言的正文结构。" if abstract and intro else
                       "物理首页具有摘要、出版社引用/在线阅读标识及主DOI；短篇文章未单列引言标题。" if short_article else
                       "物理首页明确标注Accepted Article、接受声明、作者、题名与DOI；作为正文接受稿识别。" if accepted_article else
                       "物理首页有明确论文类型、引言、论文标题及主DOI；该期刊版式未单列摘要标题。")
    elif si:
        role, status = "supporting_information", "auto"
        reasons.append("起始内容明确声明Supporting/Supplementary Information或中文补充材料标题。")
    else:
        reasons.append("抽样内容不足以确认正文或SI，文件名仅作线索。")
    if (role == "supporting_information" and main_hint) or (role == "main_article" and si_hint):
        role, status = "unknown", "unknown"
        reasons.append("文件名与内容角色线索冲突，需要确认。")
    title = _title(first, sampled.get("metadata_title"), supplementary=si and not (abstract and intro))
    return {"id": "file-" + digest_json(str(path))[:24], "name": path.name,
            "path": str(path), "format": path.suffix.lstrip(".").casefold(), "sha256": digest,
            "role": role, "status": status, "reasons": reasons, "readability": sampled["status"],
            "format_issue": sampled.get("format_issue"),
            "identification": {"role": role, "status": status, "reasons": list(reasons)},
            "signals": {"si_heading": si, "abstract": abstract, "introduction": intro,
                        "filename_main_hint": main_hint, "filename_si_hint": si_hint,
                        "article_type_marker": article_marker,
                        "publisher_citation_marker": publisher_citation,
                        "supplement_sections": len(SUPPLEMENT_SECTION.findall(all_text)),
                        "title": title, "doi_candidates": doi_candidates, "publisher_id": _publisher_id(path.name),
                        "page_count": sampled.get("page_count"), "sample_scope": sampled["scope"],
                        "language_hint": language_hint(first), "language_scope": "identification_sample_only",
                        "sampled_locations": [s["location"] for s in segments], "sample_warnings": sampled.get("warnings", []),
                        "reference_dois": _dois(all_text[len(head):])[:20]},
            "preview_text": first[:5000], "segments": si_markers,
            "classifier_version": CLASSIFIER_VERSION, "identity_status": "not_checked"}


class WebLibrary:
    """Persistent library facade; source files are always read-only."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self.directory = settings.data_root / "workbench"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "library.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, folder TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS role_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id TEXT NOT NULL, file_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                    role TEXT NOT NULL, reviewer TEXT NOT NULL, note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id TEXT NOT NULL, event TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _safe_path(self, value: str | Path, *, directory: bool = False) -> Path:
        raw = Path(value)
        if not raw.is_absolute() or ".." in raw.parts:
            raise ValueError("仅允许配置文献目录中的绝对路径，不能使用路径跳转。")
        if not any(raw == root or root in raw.parents for root in self.settings.source_roots):
            raise ValueError("路径不在配置的文献根目录中。")
        # Resolve before any read, and reject links even when they remain inside
        # the allowed root. Junctions on Windows are reparse points as well.
        resolved = self.settings.assert_source_allowed(raw)
        roots = [root for root in self.settings.source_roots if resolved == root or root in resolved.parents]
        root = max(roots, key=lambda p: len(p.parts))
        for node in [raw, *raw.parents]:
            if node == root or root not in node.parents:
                break
            if node.is_symlink() or getattr(node.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                raise ValueError("不读取符号链接或目录连接中的文献。")
        if directory and not resolved.is_dir():
            raise ValueError("文献路径不是文件夹。")
        if not directory and not resolved.is_file():
            raise ValueError("附件不是普通文件。")
        return resolved

    def _existing(self, folder: Path) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM papers WHERE folder=?", (str(folder),)).fetchone()
        return json.loads(row[0]) if row else None

    def _registered_imports(self) -> list[tuple[dict, dict, Path]]:
        store = DataStore(self.settings.data_root)
        if not (self.settings.data_root / "store.json").exists():
            return []
        result = []
        for run in store.runs():
            if run["kind"] != "import":
                continue
            directory = self.settings.data_root / "runs" / safe_name(run["run_id"])
            try:
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                bundle = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
                if manifest.get("paper_id") != bundle.get("paper_id"):
                    continue
                result.append((manifest, bundle, directory))
            except (OSError, ValueError):
                continue
        return result

    def _overrides(self, paper_id: str, files: list[dict]) -> None:
        with self._db() as db:
            for file in files:
                if file["status"] in {"stale_source", "unavailable"}:
                    continue
                row = db.execute("SELECT * FROM role_history WHERE paper_id=? AND file_id=? AND sha256=? ORDER BY sequence DESC LIMIT 1",
                                 (paper_id, file["id"], file["sha256"])).fetchone()
                if row:
                    file.update(role=row["role"], status="unknown" if row["role"] == "unknown" else "confirmed", role_origin="human",
                                confirmed_by=row["reviewer"], confirmed_at=row["created_at"], confirmation_note=row["note"])
                    file["reasons"].append("此内容版本的角色已由人工确认；不代表科学内容已审核。")

    def _finalize(self, folder: Path, files: list[dict], paper_id: str, *, previous: dict | None = None,
                  registered_run: str | None = None, cohort_role: str = "unassigned") -> dict:
        # Derived duplicate/identity decisions must be recomputed after every
        # role edit. Retain the original bounded classifier result separately.
        for file in files:
            original_signals = file.pop("attachment_review_original_signals", None)
            if original_signals is not None:
                file["signals"] = original_signals
            file.pop("attachment_review", None)
            baseline = file.get("identification")
            if baseline:
                file.update(role=baseline["role"], status=baseline["status"],
                            reasons=list(baseline["reasons"]), identity_status="not_checked")
                for key in ("duplicate_of", "role_origin", "confirmed_by", "confirmed_at", "confirmation_note"):
                    file.pop(key, None)
                assigned = file.get("registered_assignment")
                if assigned and assigned["sha256"] == file["sha256"]:
                    file.update(role=assigned["role"], status="registered", role_origin="registered")
                    file["reasons"] = ["沿用已注册导入中的明确文件角色；这不是新增人工科学审核。"]
                if file.get("source_status") in {"stale_source", "unavailable"}:
                    file.update(role="unknown", status=file["source_status"])
                    file["reasons"] = ["原附件不可用或与已注册快照不同，请重新扫描；历史快照和结果保留。"]
        # The configured literature workflow has an explicit single-document
        # convention. Role certainty is separate from readability, embedded SI
        # boundaries and a prior content-bound explicit assignment.
        substantive_entries = [p for p in folder.iterdir() if not is_inventory_noise(p)]
        if len(files) == 1 and len(substantive_entries) == 1 and substantive_entries[0] == Path(files[0]["path"]):
            file = files[0]
            if (file.get("role_origin") != "registered"
                    and file.get("source_status") not in {"stale_source", "unavailable"}):
                status = file["status"] if file["status"] in {"mixed", "unreadable", "unsupported"} else "auto"
                file.update(role="main_article", status=status, role_origin="single_document_rule")
                file["reasons"].append("按用户指定的单文件正文规则确定角色；文件可读性和内嵌SI范围仍单独检查。")
        review_pointer = (previous or {}).get("attachment_review")
        review = load_attachment_review(self.settings, review_pointer, paper_id, files)
        if review is not None:
            decisions = {item["file_id"]: item for item in review["decisions"]}
            for file in files:
                decision = decisions[file["id"]]
                # A review establishes role/identity, never successful parsing
                # or the boundaries of an embedded supplement.
                if file["status"] in {"stale_source", "unavailable"}:
                    continue
                status = file["status"] if file["status"] in {"mixed", "unreadable", "unsupported"} else "auto"
                file.update(role=decision["role"], status=status, role_origin="codex_attachment_review")
                file["attachment_review"] = {"review_id": review["review_id"],
                    "main_file_id": decision.get("main_file_id"), "reason": decision["reason"],
                    "evidence": decision["evidence"], "human_validated": False,
                    "scientific_validated": False}
                if decision.get("title"):
                    file["attachment_review_original_signals"] = copy.deepcopy(file["signals"])
                    file["signals"]["title"] = decision["title"]
                file["reasons"].append("Codex已依据原文核对附件角色及归属；这不是人工审核或科学数据验证。")
                file["reasons"].append(decision["reason"])
        self._overrides(paper_id, files)
        # Supplement declarations are not always headed "Supporting...".
        # Require both document content and independent same-paper identity;
        # neither a filename nor sharing a folder is sufficient.
        identified_main = [f for f in files if f["role"] in MAIN_ROLES
                           and f["status"] in {"auto", "confirmed", "registered"}]
        if len(identified_main) == 1:
            primary = identified_main[0]
            primary_dois = primary["signals"].get("doi_candidates", [])
            for file in files:
                signals = file["signals"]
                if (file is primary or file["role"] != "unknown" or file["status"] != "unknown"
                        or file.get("role_origin") in {"human", "registered"}
                        or file.get("readability", "ok") != "ok"
                        or signals.get("abstract") and signals.get("introduction")):
                    continue
                own_dois = signals.get("doi_candidates", [])
                if primary_dois and own_dois and not set(primary_dois).intersection(own_dois):
                    continue
                same_title = _title_match(primary["signals"].get("title"), signals.get("title"))
                same_publisher = bool(primary["signals"].get("publisher_id") and
                                      primary["signals"]["publisher_id"] == signals.get("publisher_id"))
                same_doi = bool(len(primary_dois) == 1 and primary_dois[0] in own_dois)
                sections = signals.get("supplement_sections", 0)
                numbered_supplement = ((same_title or same_publisher or same_doi) and sections > 0
                    and (signals.get("filename_si_hint") or sections >= 2 or signals.get("si_heading")))
                publisher_supplement = same_publisher and same_title and signals.get("filename_si_hint")
                if numbered_supplement or publisher_supplement:
                    file.update(role="supporting_information", status="auto", role_origin="paired_source_evidence")
                    file["reasons"].append("正文身份已确定；该附件有补充编号及同篇身份依据。" if numbered_supplement else
                        "正文身份已确定；附件标题与正文一致，出版社文献标识相同且文件具有补充附件标记。")
        # Collapse exact binary duplicates for import, retaining every original
        # path in the inventory. Prefer an explicitly identified main file.
        by_hash: dict[str, list[dict]] = {}
        for file in files:
            by_hash.setdefault(file["sha256"], []).append(file)
        for duplicates in by_hash.values():
            if len(duplicates) < 2:
                continue
            ordered = sorted(duplicates, key=lambda f: (f["role"] not in MAIN_ROLES,
                             f["status"] != "confirmed", not f["signals"].get("filename_main_hint"), f["name"]))
            for file in ordered[1:]:
                file.update(role="other", status="duplicate", duplicate_of=ordered[0]["id"])
                file["reasons"].append("与同篇另一个文件字节完全相同，仅排除重复读取，原件保留。")
        main = [f for f in files if f["role"] in MAIN_ROLES and f["status"] not in {"stale_source", "unavailable"}]
        identity_conflict = False
        if len(main) == 1:
            primary = main[0]
            primary_dois = primary["signals"].get("doi_candidates", [])
            title = primary["signals"].get("title")
            doi = primary_dois[0] if len(primary_dois) == 1 else None
            if len(primary_dois) > 1 and primary.get("role_origin") != "registered":
                primary["identity_status"] = "multiple_dois_unresolved"
                primary["reasons"].append("首页出现多个不同DOI，未自动选第一个作为本篇DOI。")
            else:
                primary["identity_status"] = "primary_document"
            for file in files:
                if file["role"] != "supporting_information":
                    continue
                own_dois = file["signals"].get("doi_candidates", [])
                same_title = _title_match(title, file["signals"].get("title"))
                same_publisher = bool(primary["signals"].get("publisher_id") and
                                      primary["signals"].get("publisher_id") == file["signals"].get("publisher_id"))
                conflicting_doi = bool(primary_dois and own_dois and not set(primary_dois).intersection(own_dois))
                if conflicting_doi:
                    file["identity_status"] = "conflict"
                    file["status"] = "identity_conflict"
                    file["reasons"].append("附件起始部分DOI与正文不同，需核对归属，不能只确认文件角色。")
                    identity_conflict = True
                elif file.get("role_origin") in {"human", "registered"}:
                    file["identity_status"] = "explicit_assignment"
                elif (file.get("role_origin") == "codex_attachment_review"
                      and file.get("attachment_review", {}).get("main_file_id") == primary["id"]):
                    file["identity_status"] = "source_review_matched"
                    file["reasons"].append("已核对该附件与当前正文的原文身份或明确交叉引用，证据随文件版本保存。")
                elif (doi and doi in own_dois) or same_title or same_publisher:
                    file["identity_status"] = "matched"
                    file["reasons"].append("与正文的主DOI、标题或出版社文件标识相符。")
                else:
                    file["identity_status"] = "folder_only"
                    file["status"] = "unknown"
                    file["reasons"].append("已识别为SI，但目前只有同文件夹线索，需确认属于本篇。")
        else:
            title, doi = None, None
            if len(main) > 1:
                identity_conflict = True
                for file in main:
                    file["identity_status"] = "multiple_main"
                    file["reasons"].append("目录存在多个非重复正文候选，需明确论文归属或排除无关附件。")
        unavailable = any(f["status"] in {"unavailable", "stale_source"} for f in files)
        unreadable = [f for f in files if f["role"] != "other"
                      and f.get("readability", "ok") in {"unreadable", "unsupported"}]
        pending = [f for f in files if f["role"] != "other" and (f["role"] not in IMPORT_ROLES or f["status"] not in {"auto", "confirmed", "registered"}
                   or f["identity_status"] in {"conflict", "ambiguous", "multiple_main"})]
        if unavailable:
            status = "source_unavailable"
        elif unreadable:
            status = "source_unreadable"
        elif identity_conflict:
            status = "identity_conflict"
        elif pending:
            status = "needs_confirmation"
        elif not main:
            status = "missing_main"
        else:
            status = "ready"
        si_state = "present" if any(f["role"] in {"supporting_information", "main_article_with_supporting_information"}
                                    for f in files) else "not_obtained_or_not_identified"
        policy = load_collection_policy(self.settings)
        verification = policy.match(folder, files) if policy else None
        embedded_unresolved = any(f.get("segments") for f in files if f["role"] != "other")
        if si_state != "present" and status == "ready" and verification is not None and not embedded_unresolved:
            si_state = "author_not_provided"
        fingerprint_values = [{"id": f["id"], "sha256": f["sha256"], "role": f["role"],
                               "duplicate_of": f.get("duplicate_of")} for f in sorted(files, key=lambda f: f["id"])]
        # An explicit acquisition declaration is a versioned input too. Keep
        # legacy fingerprints unchanged when there is no matching declaration.
        fingerprint_input = fingerprint_values if verification is None else {
            "files": fingerprint_values, "collection_verification": verification, "si_state": si_state}
        if review is not None:
            fingerprint_input = {"sources": fingerprint_input, "attachment_review": review_pointer}
        fingerprint = digest_json(fingerprint_input)
        source_version = (previous or {}).get("source_version", 0)
        if not previous or previous.get("source_fingerprint") != fingerprint:
            source_version += 1
        number = re.match(r"^(\d+)(?:_|$)", folder.name)
        # A year found in an introduction, submission date or copyright line
        # is not sufficient evidence of the article's publication year.
        year = None
        return {"id": paper_id, "folder": str(folder), "folder_name": folder.name,
                "folder_number": int(number[1]) if number else None, "title": title, "doi": doi, "year": year,
                "cohort_role": cohort_role, "attachment_status": status, "files": files,
                "source_fingerprint": fingerprint, "source_version": source_version,
                "review_required_count": len(pending), "readability_issue_count": len(unreadable),
                "registered_run": registered_run or (previous or {}).get("registered_run"),
                "updated_at": utc_now(), "scientific_eligibility": "not_assessed",
                "identification_scope": "limited_content_sample_not_full_reading",
                "supporting_information_state": si_state,
                "supporting_information_verification": verification if si_state == "author_not_provided" else None,
                "collection_verification": verification,
                "attachment_review": review_pointer,
                "attachment_review_details": ({"state": "applied", "review_id": review["review_id"],
                    "reviewed_at": review["reviewed_at"], "origin": review["origin"],
                    "human_validated": False, "scientific_validated": False,
                    "limitations": review["limitations"]} if review is not None else
                    {"state": "expired", "human_validated": False, "scientific_validated": False,
                     "limitations": ["文件清单或内容已变化，原自动附件核对不再适用，请重新核对。"]}
                    if review_pointer else None)}

    @_serialized
    def apply_attachment_review(self, paper_id: str, manifest: Path) -> dict:
        """Apply a traceable automated source review, never a human sign-off.

        The explicit manifest supplies reviewed evidence. Schema and hashes
        protect provenance; they do not independently validate the judgment.
        Every subsequent scan rechecks the complete source inventory before
        reusing it. Human file-role choices retain priority.
        """
        previous = self.get(paper_id)
        pointer = make_review_pointer(self.settings, manifest)
        files = copy.deepcopy(previous["files"])
        review = load_attachment_review(self.settings, pointer, paper_id, files)
        if review is None:
            raise ValueError("原文文件清单或内容已变化，不能应用这份自动附件核对。")
        updated = self._finalize(self._safe_path(previous["folder"], directory=True), files, paper_id,
            previous={**previous, "attachment_review": pointer},
            registered_run=previous.get("registered_run"), cohort_role=previous["cohort_role"])
        self._save(updated, event="codex_attachment_reviewed")
        return updated

    def _save(self, paper: dict, *, event: str) -> None:
        with self._db() as db:
            previous = db.execute("SELECT payload FROM papers WHERE id=?", (paper["id"],)).fetchone()
            if previous:
                old = json.loads(previous[0])
                if old.get("source_fingerprint") != paper["source_fingerprint"]:
                    db.execute("INSERT INTO paper_events(paper_id,event,payload,created_at) VALUES(?,?,?,?)",
                               (paper["id"], "source_version_changed", json.dumps(old, ensure_ascii=False), utc_now()))
            db.execute("INSERT INTO papers(id,folder,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                       (paper["id"], paper["folder"], json.dumps(paper, ensure_ascii=False), paper["updated_at"]))
            db.execute("INSERT INTO paper_events(paper_id,event,payload,created_at) VALUES(?,?,?,?)",
                       (paper["id"], event, json.dumps({"source_fingerprint": paper["source_fingerprint"], "source_version": paper["source_version"]}), utc_now()))

    @_serialized
    def scan(self, folders: list[str] | None = None, *, on_progress=None) -> list[dict]:
        candidates: list[Path] = []
        if folders is None:
            for root in self.settings.source_roots:
                if not root.is_dir():
                    raise ValueError(f"文献根目录不可用：{root}")
                candidates.extend(p for p in sorted(root.iterdir()) if p.is_dir())
        else:
            if not isinstance(folders, list) or len(folders) > 10000:
                raise ValueError("文献文件夹清单无效。")
            candidates = [Path(value) for value in folders]
        registered = {str(Path(m["folder"]).resolve()): (m, b, r)
                      for m, b, r in reversed(self._registered_imports()) if m.get("folder")}
        result = []
        for raw in dict.fromkeys(candidates):
            folder = self._safe_path(raw, directory=True)
            if folder.parent not in self.settings.source_roots:
                raise ValueError("扫描仅限配置根目录下的直接文献文件夹。")
            paths = [p for p in sorted(folder.iterdir()) if p.suffix.casefold() in EXTENSIONS and p.is_file()
                     and not p.name.startswith("~$")]
            if not paths:
                continue
            previous = self._existing(folder)
            registration = registered.get(str(folder))
            paper_id = (previous or {}).get("id") or (registration[0]["paper_id"] if registration else "paper-" + digest_json(str(folder))[:16])
            safe_name(paper_id)
            files = []
            for raw_path in paths:
                path = self._safe_path(raw_path)
                digest = hash_file(path)
                sampled = _sample_file(path)
                if hash_file(path) != digest:
                    raise ValueError("附件在识别期间发生变化，请重新扫描。")
                file = _classify(path, digest, sampled)
                if registration:
                    for document in registration[1].get("documents", []):
                        source = document.get("source", {})
                        metadata = source.get("metadata", {})
                        original = metadata.get("original_path")
                        if (original and Path(original).resolve() == path and source.get("sha256") == digest
                                and source.get("role") in IMPORT_ROLES and metadata.get("role_assignment") == "explicit"):
                            file["registered_assignment"] = {"role": source["role"], "sha256": digest}
                            break
                files.append(file)
            cohort = (previous or {}).get("cohort_role", "unassigned")
            if (self.settings.data_root / "store.json").exists() and DataStore(self.settings.data_root).has_development_exposure(paper_id, [f["sha256"] for f in files]):
                cohort = "development"
            paper = self._finalize(folder, files, paper_id, previous=previous, cohort_role=cohort)
            self._save(paper, event="scanned")
            result.append(paper)
            if on_progress is not None:
                on_progress(paper, len(result), len(candidates))
        return result

    def list_papers(self) -> list[dict]:
        with self._db() as db:
            values = [json.loads(row[0]) for row in db.execute("SELECT payload FROM papers")]
        return sorted(values, key=lambda p: (p["folder_number"] if p["folder_number"] is not None else 10**12, p["folder_name"]))

    def get(self, paper_id: str) -> dict:
        safe_name(paper_id)
        with self._db() as db:
            row = db.execute("SELECT payload FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise KeyError("文献未登记。")
        return json.loads(row[0])

    @_serialized
    def set_role(self, paper_id: str, file_id: str, role: str, reviewer: str, note: str = "") -> dict:
        if role not in ROLES or not reviewer.strip() or len(reviewer) > 128 or len(note) > 4000:
            raise ValueError("请提供有效角色和审核人；备注不能超过4000字符。")
        paper = self.get(paper_id)
        target = next((f for f in paper["files"] if f["id"] == file_id), None)
        if not target:
            raise KeyError("附件不属于此文献。")
        path = self._safe_path(target["path"])
        if hash_file(path) != target["sha256"]:
            raise ValueError("附件内容已变化，请重新扫描后确认角色。")
        with self._db() as db:
            db.execute("INSERT INTO role_history(paper_id,file_id,sha256,role,reviewer,note,created_at) VALUES(?,?,?,?,?,?,?)",
                       (paper_id, file_id, target["sha256"], role, reviewer.strip(), note, utc_now()))
        # Recompute derived identity/duplicate decisions from the saved bounded
        # classifier result and the current content-bound role history.
        files = paper["files"]
        for file in files:
            file["reasons"] = [r for r in file["reasons"] if "此内容版本" not in r]
        updated = self._finalize(Path(paper["folder"]), files, paper_id, previous=paper,
                                 registered_run=paper.get("registered_run"), cohort_role=paper["cohort_role"])
        self._save(updated, event="role_confirmed")
        return updated

    def roles_for_import(self, paper_id: str) -> dict[str, str]:
        paper = self.get(paper_id)
        folder = self._safe_path(paper["folder"], directory=True)
        inventory = {str(self._safe_path(p)) for p in folder.iterdir() if p.is_file() and p.suffix.casefold() in EXTENSIONS
                     and not p.name.startswith("~$")}
        if inventory != {f["path"] for f in paper["files"]}:
            raise ValueError("附件清单已变化，请先重新扫描。")
        for file in paper["files"]:
            if hash_file(self._safe_path(file["path"])) != file["sha256"]:
                raise ValueError("附件内容已变化，请先重新扫描。")
        if ((paper.get("attachment_review_details") or {}).get("state") == "applied" and load_attachment_review(
                self.settings, paper["attachment_review"], paper_id, paper["files"]) is None):
            raise ValueError("附件清单已变化，原自动附件核对已失效，请重新扫描核对。")
        if paper["attachment_status"] != "ready":
            if paper["attachment_status"] == "source_unreadable":
                raise ValueError("附件内容尚不可读；角色判定不能替代内容读取，请先处理可读性问题。")
            raise ValueError("正文/SI角色或归属尚未确认，不能启动此篇抽取。")
        return {f["name"]: f["role"] for f in paper["files"] if f["role"] in IMPORT_ROLES and f["status"] != "duplicate"}

    @_serialized
    def sync_registered_runs(self) -> None:
        """Seed already imported papers without scanning unprocessed literature."""
        seen = set()
        for manifest, bundle, directory in self._registered_imports():
            if not manifest.get("folder"):
                continue
            raw = Path(manifest["folder"])
            folder = raw.resolve()
            if str(folder) in seen or self._existing(folder):
                continue
            seen.add(str(folder))
            if not any(root == folder.parent for root in self.settings.source_roots):
                continue
            files = []
            for document in bundle.get("documents", []):
                source = document["source"]
                original_path = source.get("metadata", {}).get("original_path")
                if not original_path:
                    continue
                path = Path(original_path)
                if path.parent.resolve() != folder:
                    continue
                blocks = document.get("blocks", [])
                if source["format"] == "pdf":
                    eligible = [b for b in blocks if b.get("kind") == "pdf_page"]
                    selected = [b for index, b in enumerate(eligible) if index < 2 or index == len(eligible) - 1]
                else:
                    selected = [b for b in blocks if b.get("kind") in {"paragraph", "table_cell"}][:100]
                sample = {"segments": [{"location": b.get("location", {}), "text": _clean_lines(b.get("text", ""))[:64000]} for b in selected],
                          "metadata_title": source.get("metadata", {}).get("title"), "page_count": source.get("page_count"),
                          "scope": "registered_identification_sample_only", "status": "ok" if selected else "unreadable"}
                file = _classify(path, source["sha256"], sample)
                assigned = source.get("role") in IMPORT_ROLES and source.get("metadata", {}).get("role_assignment") == "explicit"
                if assigned:
                    file["registered_assignment"] = {"role": source["role"], "sha256": source["sha256"]}
                try:
                    resolved = self._safe_path(path)
                    if hash_file(resolved) != source["sha256"]:
                        file.update(role="unknown", status="stale_source", source_status="stale_source")
                        file["reasons"] = ["原附件与已注册快照不同，请重新扫描。"]
                except (ValueError, OSError):
                    file.update(role="unknown", status="unavailable", source_status="unavailable")
                    file["reasons"] = ["原附件目前不可用；已有快照和结果仍保留。"]
                files.append(file)
            if not files:
                continue
            paper_id = safe_name(bundle["paper_id"])
            development = DataStore(self.settings.data_root).has_development_exposure(paper_id, [f["sha256"] for f in files])
            paper = self._finalize(folder, files, paper_id, registered_run=directory.name,
                                   cohort_role="development" if development else "unassigned")
            self._save(paper, event="registered_import_discovered")
