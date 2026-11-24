"""Read-only source parsing with honest coverage and replayable locators.

Parsing is not scientific reading: all successfully extracted blocks start as
``parsed_unread``. PDF locators use physical, one-based pages. DOCX locators use
XML parts and ordered paragraphs/cells, never invented physical page numbers.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

PARSER_VERSION = "sources/0.1.0"
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
V = "urn:schemas-microsoft-com:vml"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "m": M, "a": A, "r": R}
Phase = Literal["parsed_unread", "unknown", "unreadable", "not_supported"]


class SourceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    physical_page: int | None = Field(default=None, ge=1)
    part: str | None = None
    paragraph_index: int | None = Field(default=None, ge=1)
    table_index: int | None = Field(default=None, ge=1)
    row_index: int | None = Field(default=None, ge=1)
    cell_index: int | None = Field(default=None, ge=1)
    xml_path: str | None = None
    relationship_id: str | None = None


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    source_family_id: str | None = None
    role: str = "unknown"
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    format: str
    parser_version: str = PARSER_VERSION
    page_count: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_id: str
    source_id: str
    kind: str
    order: int = Field(ge=1)
    text: str = ""
    location: SourceLocation = Field(default_factory=SourceLocation)
    phase: Phase = "parsed_unread"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParseIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    severity: Literal["info", "warning", "error"] = "warning"
    message: str
    block_id: str | None = None
    location: SourceLocation | None = None


class CoverageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    phase: Phase = "parsed_unread"
    location: SourceLocation | None = None
    block_ids: list[str] = Field(default_factory=list)
    details: str = ""


class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceFile
    blocks: list[SourceBlock] = Field(default_factory=list)
    issues: list[ParseIssue] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    phase: Phase = "parsed_unread"

    def add_block(self, kind: str, *, text: str = "", location: SourceLocation | None = None,
                  metadata: dict[str, Any] | None = None, phase: Phase = "parsed_unread") -> SourceBlock:
        order = len(self.blocks) + 1
        block = SourceBlock(block_id=f"{self.source.source_id}:b{order:06d}",
                            source_id=self.source.source_id, kind=kind, order=order,
                            text=text, location=location or SourceLocation(),
                            metadata=metadata or {}, phase=phase)
        self.blocks.append(block)
        return block


def sha256_file(path: str | Path) -> str:
    """Return a streaming content hash without changing the source."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue(document: SourceDocument, code: str, message: str, *, severity: str = "warning",
           location: SourceLocation | None = None, block_id: str | None = None) -> None:
    document.issues.append(ParseIssue(code=code, message=message, severity=severity,
                                      location=location, block_id=block_id))


def parse_source(path: str | Path, *, source_id: str | None = None,
                 source_family_id: str | None = None, role: str = "unknown",
                 output_dir: str | Path | None = None) -> SourceDocument:
    """Parse every PDF page or DOCX scientific-content part; never edit the input.

    Missing paths raise FileNotFoundError. Invalid/unsupported document contents
    return explicit issues. ``output_dir`` only stores embedded image bytes using
    generated content-addressed filenames, not archive-controlled paths.
    """
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not a regular file: {path}")
    digest = sha256_file(path)
    document = SourceDocument(source=SourceFile(
        source_id=source_id or f"src_{digest[:24]}", source_family_id=source_family_id,
        role=role, path=str(path), sha256=digest, size_bytes=path.stat().st_size,
        format=path.suffix.lower().lstrip(".") or "unknown"))
    if document.source.format == "pdf":
        _parse_pdf(path, document)
    elif document.source.format == "docx":
        _parse_docx(path, document, Path(output_dir) if output_dir is not None else None)
    else:
        document.phase = "not_supported"
        code = "legacy_doc_not_supported" if document.source.format == "doc" else "unsupported_format"
        _issue(document, code, "No content was read. Preserve the original and provide a DOCX/PDF derivative with its own source identity.")
        document.coverage.append(CoverageEntry(scope="document", phase="not_supported"))
    return document


def _parse_pdf(path: Path, document: SourceDocument) -> None:
    from pypdf import PdfReader

    document.source.parser_version = f"{PARSER_VERSION};pypdf/{importlib.metadata.version('pypdf')}"
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("PDF requires a password")
            document.source.page_count = len(reader.pages)
            for page_no, page in enumerate(reader.pages, 1):
                location = SourceLocation(physical_page=page_no)
                phase: Phase = "parsed_unread"
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    text, phase = "", "unreadable"
                    _issue(document, "pdf_page_text_failed", str(exc), severity="error", location=location)
                if not text.strip() and phase != "unreadable":
                    phase = "unknown"
                    _issue(document, "pdf_no_extractable_text",
                           "No extractable text: may be blank, scanned, graphical, or have unsupported text encoding; not evidence of no scientific content.",
                           location=location)
                block = document.add_block("pdf_page", text=text, location=location, phase=phase,
                                           metadata={"extraction": "pypdf_text", "visual_checked": False,
                                                     "table_structure_extracted": False})
                document.coverage.append(CoverageEntry(scope="pdf_page_text", phase=phase,
                    location=location, block_ids=[block.block_id]))
                document.coverage.append(CoverageEntry(scope="pdf_page_visual", phase="unknown",
                    location=location, details="Figures, tables, scans, vector graphics, legends and reading order require visual examination; text extraction does not establish coverage."))
    except Exception as exc:
        document.phase = "unreadable"
        _issue(document, "pdf_parse_failed", str(exc), severity="error")
        document.coverage.append(CoverageEntry(scope="document", phase="unreadable"))


def _xml(data: bytes) -> ET.Element:
    # ElementTree never fetches external resources. Explicitly reject DTD/entity
    # declarations too, including UTF-16/32 encoded forms, before parsing.
    probe = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise ValueError("DTD and entity declarations are not allowed")
    return ET.fromstring(data)


def _read_member(package: zipfile.ZipFile, name: str) -> bytes:
    info = package.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"DOCX member exceeds {MAX_MEMBER_BYTES} bytes: {name}")
    with package.open(info) as stream:
        data = stream.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise ValueError(f"DOCX member expanded beyond limit: {name}")
    return data


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _paths(root: ET.Element) -> tuple[dict[ET.Element, str], dict[ET.Element, ET.Element]]:
    paths: dict[ET.Element, str] = {}
    parents: dict[ET.Element, ET.Element] = {}

    def qualified(node: ET.Element) -> str:
        if not node.tag.startswith("{"):
            return node.tag
        namespace, local = node.tag[1:].split("}", 1)
        prefix = next((key for key, value in NS.items() if value == namespace), None)
        return f"{prefix}:{local}" if prefix else f"Q{{{namespace}}}{local}"

    def visit(node: ET.Element, path: str) -> None:
        paths[node] = path
        counts: dict[str, int] = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            parents[child] = node
            visit(child, f"{path}/{qualified(child)}[{counts[child.tag]}]")
    visit(root, f"/{qualified(root)}[1]")
    return paths, parents


def _text(element: ET.Element, *, include_deleted: bool = False) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if not include_deleted and node.tag in {f"{{{W}}}del", f"{{{W}}}moveFrom"}:
            return
        if node.tag in {f"{{{W}}}t", f"{{{W}}}delText", f"{{{M}}}t"}:
            parts.append(node.text or "")
        elif node.tag == f"{{{W}}}tab":
            parts.append("\t")
        elif node.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
            parts.append("\n")
        elif node.tag == f"{{{W}}}noBreakHyphen":
            parts.append("\u2011")
        elif node.tag == f"{{{W}}}sym":
            parts.append(f"[symbol:{node.get(f'{{{W}}}font', '')}:{node.get(f'{{{W}}}char', '')}]")
        else:
            for child in node:
                visit(child)
    visit(element)
    return "".join(parts)


def _relationships(package: zipfile.ZipFile, part: str) -> dict[str, dict[str, str]]:
    relationship_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relationship_part not in package.namelist():
        return {}
    root = _xml(_read_member(package, relationship_part))
    return {node.get("Id", ""): dict(node.attrib) for node in root
            if node.tag == f"{{{REL}}}Relationship"}


def drawingml_outset_fill(fill, crop):
    """Register fill semantics only for signed outsets on this specific blip."""
    if (fill is not None and _tag(fill) == "blipFill"
            and any((crop.get(side) or 0) < 0 for side in ("left", "top", "right", "bottom"))):
        tiled = fill.find(f"{{{A}}}tile") is not None
        stretched = fill.find(f"{{{A}}}stretch") is not None
        if stretched != tiled:
            return "stretch" if stretched else "tile"
    return None


def _media_target(part: str, target: str) -> str:
    target = unquote(target)
    if "\\" in target or "\x00" in target or urlsplit(target).scheme or urlsplit(target).netloc:
        raise ValueError("Unsafe or non-package image target")
    # Leading slash is package-root-relative in OPC, not a filesystem path.
    normalized = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                    else posixpath.join(posixpath.dirname(part), target))
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("Image target escapes the DOCX package")
    return normalized


def _write_image(data: bytes, media_name: str, output_dir: Path, source: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir.resolve(strict=True)
    suffix = Path(media_name).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".wmf", ".emf", ".svg"}:
        suffix = ".bin"
    destination = base / (hashlib.sha256(data).hexdigest() + suffix)
    if destination.is_symlink() or not destination.resolve().is_relative_to(base) or destination.resolve() == source:
        raise ValueError("Unsafe image output path")
    try:
        with destination.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if sha256_file(destination) != hashlib.sha256(data).hexdigest():
            raise ValueError("Existing image output has different content; refusing overwrite")
    return str(destination)


class _DocxPartReader:
    def __init__(self, package: zipfile.ZipFile, document: SourceDocument, part: str,
                 root: ET.Element, output_dir: Path | None):
        self.package, self.document, self.part = package, document, part
        self.output_dir = output_dir
        self.paths, self.parents = _paths(root)
        self.relationships = _relationships(package, part)
        self.paragraph_index = 0
        self.table_index = 0
        self.image_count = 0
        self.root = root

    def location(self, node: ET.Element, context: dict[str, int] | None = None,
                 **extra: Any) -> SourceLocation:
        return SourceLocation(part=self.part, xml_path=self.paths[node], **(context or {}), **extra)

    def read(self) -> None:
        self.walk(self.root)

    def walk(self, node: ET.Element, context: dict[str, int] | None = None) -> None:
        if node.tag == f"{{{W}}}p":
            self.paragraph(node, context)
        elif node.tag == f"{{{W}}}tbl":
            self.table(node)
        else:
            for child in node:
                self.walk(child, context)

    def table(self, node: ET.Element) -> None:
        self.table_index += 1
        table_index = self.table_index
        rows = node.findall(f"{{{W}}}tr")
        self.document.add_block("table", location=self.location(node, {"table_index": table_index}),
            metadata={"row_count": len(rows), "representation": "physical_xml_cells", "semantic_read": False})
        for row_index, row in enumerate(rows, 1):
            for cell_index, cell in enumerate(row.findall(f"{{{W}}}tc"), 1):
                context = {"table_index": table_index, "row_index": row_index, "cell_index": cell_index}
                props = cell.find(f"{{{W}}}tcPr")
                self.document.add_block("table_cell", location=self.location(cell, context),
                    metadata={"properties_xml": ET.tostring(props, encoding="unicode") if props is not None else None})
                for child in cell:
                    self.walk(child, context)

    def paragraph(self, node: ET.Element, context: dict[str, int] | None) -> None:
        self.paragraph_index += 1
        context = dict(context or {}, paragraph_index=self.paragraph_index)
        inherited_revisions = self._revision_ancestors(node)
        deleted_container = any(state in {"del", "moveFrom"} for state in inherited_revisions)
        revisions = [x for x in node.iter() if x.tag in {
            f"{{{W}}}{name}" for name in ("ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange")}]
        equations = [x for x in node.iter() if x.tag == f"{{{M}}}oMath"]
        container_metadata: dict[str, Any] = {}
        ancestor = node
        while ancestor in self.parents:
            ancestor = self.parents[ancestor]
            if _tag(ancestor) in {"comment", "footnote", "endnote"}:
                container_metadata = {"container_type": _tag(ancestor),
                                      "container_id": ancestor.get(f"{{{W}}}id"),
                                      "container_author": ancestor.get(f"{{{W}}}author")}
                break
        block = self.document.add_block("paragraph", text="" if deleted_container else _text(node), location=self.location(node, context),
            metadata={"text_projection": "excludes_deleted_and_moveFrom; not_layout_rendered",
                      "has_revisions": bool(revisions or inherited_revisions), "has_omml": bool(equations),
                      "revision_states": inherited_revisions, "is_current_text": not deleted_container,
                      "content_role": "editorial_comment" if self.part == "word/comments.xml" else "document_content",
                      "paragraph_anchor": self.paths[node], **container_metadata})
        if inherited_revisions:
            self.document.add_block("revision", text=_text(node, include_deleted=True),
                location=self.location(node, context), metadata={
                    "parent_block_id": block.block_id, "revision_state": inherited_revisions[-1],
                    "revision_states": inherited_revisions, "inherited_from_container": True,
                    "xml": ET.tostring(node, encoding="unicode"), "is_current_text": not deleted_container})
        for revision in revisions:
            self.document.add_block("revision", text=_text(revision, include_deleted=True),
                location=self.location(revision, context), metadata={
                    "parent_block_id": block.block_id, "revision_state": _tag(revision),
                    "revision_id": revision.get(f"{{{W}}}id"), "author": revision.get(f"{{{W}}}author"),
                    "date": revision.get(f"{{{W}}}date"), "xml": ET.tostring(revision, encoding="unicode"),
                    "is_current_text": _tag(revision) in {"ins", "moveTo"}})
        for equation in equations:
            self.document.add_block("equation", text=_text(equation), location=self.location(equation, context),
                metadata={"parent_block_id": block.block_id, "format": "OMML",
                          "xml": ET.tostring(equation, encoding="unicode"),
                          "text_is_lossy_projection": True, "visual_checked": False,
                          "revision_states": self._revision_ancestors(equation)})
        for ref in node.iter():
            if ref.tag == f"{{{A}}}blip" or ref.tag == f"{{{V}}}imagedata":
                self.image(ref, node, block, context)
        for marker in node.iter():
            if marker.tag in {f"{{{W}}}{name}" for name in
                              ("commentReference", "commentRangeStart", "commentRangeEnd", "footnoteReference", "endnoteReference")}:
                self.document.add_block("reference", location=self.location(marker, context),
                    metadata={"parent_block_id": block.block_id, "reference_type": _tag(marker),
                              "reference_id": marker.get(f"{{{W}}}id")})

    def image(self, ref: ET.Element, paragraph: ET.Element, parent_block: SourceBlock,
              context: dict[str, int]) -> None:
        self.image_count += 1
        rid = ref.get(f"{{{R}}}embed") or ref.get(f"{{{R}}}link") or ref.get(f"{{{R}}}id")
        location = self.location(ref, context, relationship_id=rid)
        ancestor = ref
        while ancestor in self.parents and _tag(ancestor) not in {"pic", "shape"}:
            ancestor = self.parents[ancestor]
            if ancestor is paragraph:
                break
        fill = self.parents.get(ref) if ref.tag == f"{{{A}}}blip" else None
        crop_node = (fill.find(f"{{{A}}}srcRect") if fill is not None and _tag(fill) == "blipFill"
                     else ancestor.find(f".//{{{A}}}srcRect"))
        crop_raw = dict(crop_node.attrib) if crop_node is not None else {}
        crop_coordinates = {}
        for side, attribute in (("left", "l"), ("top", "t"), ("right", "r"), ("bottom", "b")):
            try:
                crop_coordinates[side] = int(crop_raw.get(attribute, "0"))
            except ValueError:
                crop_coordinates[side] = None
        crop: dict[str, Any] = {"format": "drawingml", "unit": "1/1000_percent", "raw": crop_raw,
                                "specified": crop_node is not None, **crop_coordinates}
        fill_mode = drawingml_outset_fill(fill, crop)
        if fill_mode:
            crop["outset_fill"] = fill_mode
        if ref.tag == f"{{{V}}}imagedata":
            vml_crop = {key: value for key, value in ref.attrib.items()
                        if key.rsplit("}", 1)[-1] in {"cropleft", "croptop", "cropright", "cropbottom"}}
            crop = {"format": "vml", "unit": "vml_raw", "raw": vml_crop,
                    "specified": bool(vml_crop), "left": None, "top": None, "right": None, "bottom": None}
        metadata: dict[str, Any] = {
            "parent_block_id": parent_block.block_id, "paragraph_anchor": self.paths[paragraph],
            "occurrence": self.image_count, "relationship_id": rid, "visual_checked": False,
            "crop": crop,
            "revision_states": self._revision_ancestors(ref),
        }
        image_block = self.document.add_block("image", location=location, metadata=metadata)
        if crop["format"] == "vml" and crop["specified"]:
            _issue(self.document, "vml_crop_not_normalized", "VML crop attributes retained verbatim; units and displayed crop need verification before rendering.",
                   location=location, block_id=image_block.block_id)
        relationship = self.relationships.get(rid or "")
        if relationship is None:
            _issue(self.document, "image_relationship_missing", f"Missing image relationship {rid!r}",
                   location=location, block_id=image_block.block_id)
            image_block.phase = "unreadable"
            return
        image_block.metadata["relationship_target"] = relationship.get("Target")
        if relationship.get("TargetMode", "").lower() == "external":
            image_block.phase = "unknown"
            _issue(self.document, "external_image_not_fetched", "External image was not fetched.",
                   location=location, block_id=image_block.block_id)
            return
        try:
            target = _media_target(self.part, relationship.get("Target", ""))
            data = _read_member(self.package, target)
            image_block.metadata.update(media_path=target, media_sha256=hashlib.sha256(data).hexdigest(),
                                        size_bytes=len(data))
            if self.output_dir is not None:
                image_block.metadata["image_output_path"] = _write_image(
                    data, target, self.output_dir, Path(self.document.source.path))
        except Exception as exc:
            image_block.phase = "unreadable"
            _issue(self.document, "image_read_failed", str(exc), location=location, block_id=image_block.block_id)

    def _revision_ancestors(self, node: ET.Element) -> list[str]:
        states = []
        while node in self.parents:
            node = self.parents[node]
            if node.tag in {f"{{{W}}}{name}" for name in ("del", "ins", "moveFrom", "moveTo")}:
                states.append(_tag(node))
        return states


def _parse_docx(path: Path, document: SourceDocument, output_dir: Path | None) -> None:
    document.source.parser_version = f"{PARSER_VERSION};stdlib-zipfile-elementtree"
    document.source.metadata["locator_scheme"] = "part+xml_path+paragraph/table/cell; no_physical_pages"
    document.source.metadata["xml_namespaces"] = NS
    try:
        with zipfile.ZipFile(path) as package:
            infos = package.infolist()
            names = [x.filename for x in infos]
            if len(names) != len(set(names)):
                raise ValueError("DOCX contains duplicate ZIP member names")
            if sum(x.file_size for x in infos) > MAX_PACKAGE_BYTES:
                raise ValueError("DOCX expanded size exceeds package limit")
            if "word/document.xml" not in names:
                raise ValueError("Missing word/document.xml")
            parts = ["word/document.xml"] + sorted(name for name in names if re.fullmatch(
                r"word/(?:footnotes|endnotes|comments|header\d*|footer\d*)\.xml", name))
            for part in parts:
                before = len(document.blocks)
                try:
                    root = _xml(_read_member(package, part))
                    if root.tag.startswith("{http://purl.oclc.org/ooxml/"):
                        raise ValueError("Strict OOXML namespaces are not supported by this parser")
                    if part == "word/document.xml" and (
                        root.tag != f"{{{W}}}document" or root.find(f"{{{W}}}body") is None
                    ):
                        raise ValueError("Expected a Word document root and body in word/document.xml")
                    reader = _DocxPartReader(package, document, part, root, output_dir)
                    reader.read()
                    document.coverage.append(CoverageEntry(scope="docx_part_structure", location=SourceLocation(part=part),
                        block_ids=[x.block_id for x in document.blocks[before:]],
                        details="Ordered XML text/cells, OMML and image references parsed; no scientific interpretation or layout verification."))
                except Exception as exc:
                    _issue(document, "docx_part_failed", str(exc), severity="error", location=SourceLocation(part=part))
                    document.coverage.append(CoverageEntry(scope="docx_part_structure", phase="unreadable", location=SourceLocation(part=part)))
                    if part == "word/document.xml":
                        document.phase = "unreadable"
            represented = set(parts) | {name for name in names if name.endswith(".rels")} | {"[Content_Types].xml"}
            unprocessed = [name for name in names if name not in represented and not name.startswith("word/media/")]
            document.source.metadata["unprocessed_package_parts"] = unprocessed
            if any(name.startswith(("word/charts/", "word/embeddings/")) for name in unprocessed):
                _issue(document, "embedded_object_not_interpreted", "Embedded charts/objects are preserved in the source but not interpreted by this parser.")
    except Exception as exc:
        document.phase = "unreadable"
        _issue(document, "docx_parse_failed", str(exc), severity="error")
        document.coverage.append(CoverageEntry(scope="document", phase="unreadable"))
    document.coverage.append(CoverageEntry(scope="docx_visual_layout", phase="unknown",
        details="Physical pagination, displayed image crops, shapes, symbols, equations and revision display require visual verification; media extraction is not viewing."))
    if any(block.kind == "revision" for block in document.blocks):
        _issue(document, "tracked_changes_not_resolved", "Tracked changes retained separately. Current-text projection excludes deletions/moveFrom; original has not been accepted, rejected, or modified.", severity="info")

