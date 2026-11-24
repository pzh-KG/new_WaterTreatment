"""Compact model delivery, distinct from immutable parsed sources and reading proof.

Every source block and its literal text remains in order. Mechanical locator
fields stay in sources.json and are available from registered source tools.
No paper-specific selection, summarization or text truncation happens here.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter

from .storage import digest_json


POLICY = "complete-source-text-compact-locators/2"
MAX_PROMPT_BYTES = 900000
ADAPTER_RESERVE_BYTES = 60000
CANDIDATE_RESERVE_BYTES = 200000
FEEDBACK_RESERVE_BYTES = 60000

# These are provenance/transport details, never scientific content. Unknown
# metadata, math XML, table properties, revisions and image transforms survive.
_DEFERRED_METADATA = {"paragraph_anchor", "image_output_path", "media_path", "relationship_target"}
_COMMON_PARSE_METADATA = {
    "text_projection": "excludes_deleted_and_moveFrom; not_layout_rendered",
    "has_revisions": False, "has_omml": False, "revision_states": [],
    "is_current_text": True, "content_role": "document_content",
    "visual_checked": False, "semantic_read": False,
}


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


# Factor literal run-formatting elements from Word equations. Math operators,
# operands, subscripts, and fraction structure remain in place. This is string
# substitution on parser-serialized XML, not XML evaluation or normalization.
_RUN_FORMAT = re.compile(r"<(?P<tag>[\w.-]+:rPr)\b[^<>]*?(?:/>|>[\s\S]*?</(?P=tag)>)")


def _share_word_markup(doc: dict) -> None:
    """Encode repeated cell properties and equation styling once per document."""
    fields = []
    counts = Counter()
    for block in doc["blocks"]:
        metadata = block.get("metadata", {})
        if block["kind"] == "table_cell":
            key = "properties_xml"
        elif block["kind"] == "equation":
            key = "xml"
        else:
            continue
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            continue
        if "&wtLiteral" in value:
            return  # Reserved marker already occurs in source. Keep literal XML.
        fields.append((metadata, key))
        if key == "properties_xml":
            counts[value] += 1
        else:
            counts.update(match.group() for match in _RUN_FORMAT.finditer(value))
    references = {}
    for literal, count in counts.items():
        marker = f"&wtLiteral{len(references)};"
        if count > 1 and (count - 1) * utf8_size(literal) > (count + 1) * len(marker) + 12:
            references[literal] = marker
    if not references:
        return
    saved = 0
    replacements = []
    for metadata, key in fields:
        original = metadata[key]
        if key == "properties_xml":
            encoded = references.get(original, original)
        else:
            encoded = _RUN_FORMAT.sub(lambda m: references.get(m.group(), m.group()), original)
        if encoded != original:
            saved += utf8_size(original) - utf8_size(encoded)
            replacements.append((metadata, key, encoded))
    dictionary = {marker: literal for literal, marker in references.items()}
    if saved <= utf8_size(json.dumps(dictionary, ensure_ascii=False)) + 100:
        return
    for metadata, key, encoded in replacements:
        metadata[key] = encoded
    doc["word_xml_literals"] = dictionary


def compact_source_documents(documents: list[dict]) -> list[dict]:
    """Retain all blocks/text and scientifically meaningful parse cautions.

This is a model-view schema, not a replacement SourceDocument. In particular,
omitted raw metadata is not absent evidence, and parser coverage is not reading.
"""
    result = []
    for original in documents:
        doc = copy.deepcopy(original)
        source = doc["source"]
        blocks = []
        for block in doc["blocks"]:
            compact = {key: block[key] for key in ("block_id", "kind", "order", "text")}
            location = {key: value for key, value in block.get("location", {}).items()
                        if value is not None and key != "xml_path"}
            if location:
                compact["location"] = location
            if block.get("phase", "parsed_unread") != "parsed_unread":
                compact["phase"] = block["phase"]
            metadata = {key: value for key, value in block.get("metadata", {}).items()
                        if key not in _DEFERRED_METADATA
                        and not (key in _COMMON_PARSE_METADATA and value == _COMMON_PARSE_METADATA[key])}
            if metadata:
                compact["metadata"] = metadata
            blocks.append(compact)
        result.append({
            "source": {key: source[key] for key in ("source_id", "role", "sha256", "format", "page_count")
                       if key in source},
            "source_metadata": {key: value for key, value in source.get("metadata", {}).items()
                                if key not in {"original_path", "xml_namespaces", "acquisition_context"}},
            "phase": doc.get("phase", "parsed_unread"),
            "block_count": len(blocks),
            "blocks": blocks,
            "issues": doc.get("issues", []),
            "parse_coverage_limitations": [entry for entry in doc.get("coverage", [])
                                           if entry.get("phase", "parsed_unread") != "parsed_unread"],
        })
    return result


def project_source_scope(scope: dict) -> tuple[dict, dict]:
    """Return a fresh delivery scope and a reproducible losslessness receipt."""
    projected = copy.deepcopy(scope)
    original = scope["source_representation"]
    projected["source_representation"] = compact_source_documents(original)
    for doc in projected["source_representation"]:
        if doc["source"].get("format") == "docx":
            _share_word_markup(doc)
    receipt = {
        "policy": POLICY,
        "source_documents_sha256": digest_json(original),
        "projected_documents_sha256": digest_json(projected["source_representation"]),
        "block_count": sum(len(doc["blocks"]) for doc in original),
        "text_characters": sum(len(block.get("text", "")) for doc in original for block in doc["blocks"]),
        "original_documents_utf8_bytes": utf8_size(json.dumps(original, ensure_ascii=False)),
        "projected_documents_utf8_bytes": utf8_size(json.dumps(projected["source_representation"], ensure_ascii=False, separators=(",", ":"))),
        "scientific_validation": "not_established",
    }
    projected["source_projection"] = {
        **receipt,
        "notice": "All original blocks, literal text (including empty blocks), order, table coordinates, math XML, "
                  "image occurrence/crop metadata, parse issues and nondefault reading limitations are retained. "
                  "Each block inherits its parent source_id. Omitted block phase is parsed_unread, never read/verified. "
                  "In Word metadata.xml and properties_xml only, replace each &wtLiteralN; token once with its "
                  "literal value in this document's word_xml_literals to recover the identical XML. "
                  "Mechanical XML paths, duplicate file paths and common parser flags remain in the unchanged "
                  "sources.json; wt_read_blocks returns the complete original block metadata on demand. "
                  "An omitted metadata field is not author-not-reported. Word has XML locators, not physical pages. "
                  "Images and captions must still be checked; parsed text and this projection are not reading proof.",
        "omitted_common_parser_values": copy.deepcopy(_COMMON_PARSE_METADATA),
    }
    return projected, receipt


def prompt_budget_preflight(*, context: str, comprehension_rules: str, extraction_rules: str,
                           audit_rules: str) -> dict:
    """Budget full-source stages before any paid/slow model work starts.

Reserves are bounded-delivery planning allowances, not predicted token usage.
The adapter still checks the exact eventual UTF-8 prompt on every call.
"""
    source_bytes = utf8_size(context)
    stages = {}
    for name, rules, candidate, feedback in (
        ("comprehension", comprehension_rules, 0, 0),
        ("full_audit", audit_rules + "\nSCIENTIFIC RULES:\n" + extraction_rules,
         CANDIDATE_RESERVE_BYTES, FEEDBACK_RESERVE_BYTES),
    ):
        base = source_bytes + utf8_size(rules)
        required = base + candidate + feedback + ADAPTER_RESERVE_BYTES
        stages[name] = {"source_context_utf8_bytes": source_bytes, "fixed_prompt_utf8_bytes": base,
                        "candidate_reserve_utf8_bytes": candidate, "feedback_reserve_utf8_bytes": feedback,
                        "adapter_reserve_utf8_bytes": ADAPTER_RESERVE_BYTES,
                        "planned_utf8_bytes": required, "within_budget": required <= MAX_PROMPT_BYTES}
    return {"policy": "source-stages-utf8-preflight/1", "max_prompt_utf8_bytes": MAX_PROMPT_BYTES,
            "within_budget": all(stage["within_budget"] for stage in stages.values()), "stages": stages,
            "notice": "Text-only conservative preflight, not a token/image/context-window guarantee. "
                      "No source text was truncated. Oversize sources require an explicit complete-reading partition "
                      "before model execution; later candidate/feedback calls retain exact runtime guards."}


def _pack_word_blocks(doc: dict) -> bool:
    """Encode common block/locator headers once, preserving exact IDs, text and order."""
    layouts, metadata, rows = [], [], []
    layout_indices, metadata_indices = {}, {}
    prefix = doc["source"]["source_id"] + ":"
    if not all(block["block_id"].startswith(prefix) for block in doc["blocks"]):
        prefix = ""
    for block in doc["blocks"]:
        if block.keys() - {"block_id", "kind", "order", "text", "location", "metadata", "phase"}:
            return False
        location = block.get("location", {})
        fields = tuple(key for key in location if key != "part")
        layout_key = (block["kind"], location.get("part"), block.get("phase"), fields)
        if layout_key not in layout_indices:
            layout_indices[layout_key] = len(layouts)
            layout = {"kind": block["kind"], "location_fields": list(fields)}
            if "part" in location:
                layout["part"] = location["part"]
            if "phase" in block:
                layout["phase"] = block["phase"]
            layouts.append(layout)
        meta_index = None
        if "metadata" in block:
            value = block["metadata"]
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key not in metadata_indices:
                metadata_indices[key] = len(metadata)
                metadata.append(value)
            meta_index = metadata_indices[key]
        rows.append([layout_indices[layout_key], block["order"], block["block_id"][len(prefix):], block["text"],
                     meta_index, *(location[field] for field in fields)])
    packed = {"word_block_layouts": layouts, "word_block_metadata": metadata, "word_block_rows": rows,
              "block_id_prefix": prefix,
              "row_columns": ["layout_index", "order", "block_id_suffix", "literal_text", "metadata_index",
                              "...location values in layout.location_fields order"],
              "row_reading": "Indices are zero-based within this document. Each layout specifies the block kind, "
                             "common XML part and location field names. Null metadata_index means no metadata. "
                             "Rows are in original reading order. The exact block ID for tools and citations is "
                             "block_id_prefix followed by the literal suffix in this row, without any other characters."}
    def encoded_size(value):
        return utf8_size(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    if encoded_size(packed) >= encoded_size({"blocks": doc["blocks"]}):
        return False
    doc.pop("blocks")
    doc.update(packed)
    return True


def prepare_active_source(scope, *, comprehension_rules, extraction_rules, audit_rules):
    projected, receipt = project_source_scope(scope)
    prefix = ("\n\nDELIVERY: All parsed source blocks and the prepared image index follow. "
              "Initial image attachments cover all available prepared pages/Word occurrences. "
              "Read the full substantive sources; use registered wt_ tools for source/block retrieval, "
              "alternative PDF parsing and high-resolution regional viewing. For native Word tables use "
              "wt_table_rows to read headers, cells and adjoining text together. Distinguish this study's "
              "results from comparison rows citing other studies. File paths are provenance, "
              "not authorization for arbitrary file access. Mark unread/unavailable content honestly.\n"
              "\nINPUT MANIFEST (metadata, not scientific evidence):\n")
    def delivery():
        context = prefix + json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        check = prompt_budget_preflight(context=context, comprehension_rules=comprehension_rules,
                                        extraction_rules=extraction_rules, audit_rules=audit_rules)
        return context, check
    context, check = delivery()
    if not check["within_budget"]:
        documents = projected["source_representation"]
        packed_sources = [d["source"]["source_id"] for d in documents
                          if d["source"].get("format") == "docx" and _pack_word_blocks(d)]
        if packed_sources:
            before = {key: stage["planned_utf8_bytes"] for key, stage in check["stages"].items()}
            receipt.update(word_block_rows={"format": "word-block-rows/1", "source_ids": packed_sources},
                           projected_documents_sha256=digest_json(documents),
                           projected_documents_utf8_bytes=utf8_size(json.dumps(documents, ensure_ascii=False, separators=(",", ":"))))
            projected["source_projection"].update(receipt)
            context, check = delivery()
            check["planned_bytes_before_word_block_rows"] = before
    return projected, receipt, context, check
