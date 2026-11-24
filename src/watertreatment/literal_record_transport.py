"""Lossless, explicit wire compaction for model-authored scientific records.

There is no scientific inference here.  A shared value is expanded only at an
explicit marker, and a grouped edit targets only the enumerated paths.  Original
requests and expanded edits are retained by the caller before schema validation.
"""
from __future__ import annotations

import copy
import json
import re


SHARED_MARKER = "$wt_shared_value"
# This is a wire dictionary. Depth, expanded nodes and expanded UTF-8 bytes
# are independently bounded even when many small literal definitions are used.
MAX_SHARED_VALUES = 1024
MAX_DEPTH = 64
MAX_EXPANDED_NODES = 500_000
MAX_EXPANDED_BYTES = 16_000_000
MAX_PATCH_OPERATIONS = 100
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
_OMITTED_LIST_FIELDS = frozenset({"quantitative_coverage", "coordinates", "evidence_links",
                                  "reference_associations"})


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def expand_record_wire(record, shared_values=None, field_layouts=None, *, allow_unused_layouts=False):
    """Return an independent literal record and transport-only statistics.

    The optional dictionary is an explicit wire protocol, not defaults or an
    inheritance rule.  Without it, legacy literal JSON (even marker-shaped
    scientific values) is untouched.  Dictionary expansion has finite resource
    limits and rejects cycles, unknown names and ambiguous markers.
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    shared_values = {} if shared_values is None else shared_values
    if not isinstance(shared_values, dict) or len(shared_values) > MAX_SHARED_VALUES:
        raise ValueError(f"shared_values must contain at most {MAX_SHARED_VALUES} literal definitions")
    field_layouts = {} if field_layouts is None else field_layouts
    if (not isinstance(field_layouts, dict) or len(field_layouts) > 128 or any(
            not isinstance(k, str) or not _NAME.fullmatch(k) or not isinstance(cols, list)
            or not 1 <= len(cols) <= 128 or any(not isinstance(c, str) or not _NAME.fullmatch(c) for c in cols)
            or len(set(cols)) != len(cols) for k, cols in field_layouts.items())):
        raise ValueError("field_layouts must name bounded lists of unique literal field names")
    if not shared_values and not field_layouts:
        return copy.deepcopy(record), {"mode": "literal", "shared_value_count": 0,
            "expanded_reference_count": 0, "automatic_scientific_transformations": 0}
    if any(not isinstance(key, str) or not _NAME.fullmatch(key) for key in shared_values):
        raise ValueError("shared value names must be explicit simple identifiers")
    # Budget both expansion work and final content.  Conservative counting also
    # covers definitions while resolving nested references, before large copies.
    state = {"nodes": 0, "text_bytes": 0, "references": 0}
    active = set()
    used = set()
    used_layouts = set()
    rows = 0

    def visit(value, depth):
        nonlocal rows
        if depth > MAX_DEPTH:
            raise ValueError("shared-value expansion exceeds maximum depth")
        state["nodes"] += 1
        if state["nodes"] > MAX_EXPANDED_NODES:
            raise ValueError("shared-value expansion exceeds node budget")
        if isinstance(value, dict):
            if shared_values and SHARED_MARKER in value:
                if set(value) != {SHARED_MARKER}:
                    raise ValueError("a shared-value marker cannot have sibling fields or overrides")
                key = value[SHARED_MARKER]
                if not isinstance(key, str) or key not in shared_values:
                    raise ValueError("unknown explicit shared-value reference")
                if key in active:
                    raise ValueError("cyclic shared-value references are forbidden")
                active.add(key)
                used.add(key)
                state["references"] += 1
                try:
                    return visit(shared_values[key], depth + 1)
                finally:
                    active.remove(key)
            if any(not isinstance(key, str) for key in value):
                raise ValueError("JSON object keys must be strings")
            result = {}
            for key, item in value.items():
                state["text_bytes"] += len(_json_bytes(key))
                if state["text_bytes"] > MAX_EXPANDED_BYTES:
                    raise ValueError("shared-value expansion exceeds byte budget")
                result[key] = visit(item, depth + 1)
            return result
        if isinstance(value, list):
            if field_layouts and value and isinstance(value[0], str) and value[0].startswith("@wt:"):
                name = value[0][4:]
                columns = field_layouts.get(name)
                if columns is None or len(value) != len(columns) + 1:
                    raise ValueError("explicit field row must provide exactly one value per named column")
                rows += 1
                used_layouts.add(name)
                return visit(dict(zip(columns, value[1:])), depth + 1)
            return [visit(item, depth + 1) for item in value]
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("shared values must contain only literal JSON")
        state["text_bytes"] += len(_json_bytes(value))
        if state["text_bytes"] > MAX_EXPANDED_BYTES:
            raise ValueError("shared-value expansion exceeds byte budget")
        return copy.deepcopy(value)

    expanded = visit(record, 0)
    unused_layouts = set(field_layouts) - used_layouts
    if unused_layouts and not allow_unused_layouts:
        raise ValueError("unused explicit field layouts")
    if set(shared_values) != used:
        raise ValueError("unused shared definitions are not silently extracted facts")
    size = len(_json_bytes(expanded))
    if size > MAX_EXPANDED_BYTES:
        raise ValueError("shared-value expansion exceeds byte budget")
    return expanded, {"mode": "explicit_field_rows_v1" if field_layouts else "explicit_shared_values_v1",
        **({"unused_column_layouts": sorted(unused_layouts)} if unused_layouts else {}),
        **({"field_layout_count": len(field_layouts), "expanded_rows": rows} if field_layouts else {}),
        "shared_value_count": len(shared_values), "expanded_reference_count": state["references"],
        "expanded_compact_utf8_bytes": size, "automatic_scientific_transformations": 0,
        "notice": "Exact model-authored values expanded at explicit markers only; no condition inheritance, evidence verification or scientific validation."}


def expand_record_edits(edits, *, original=None):
    """Expand exact targets, retaining the order of explicitly appended items."""
    if not isinstance(edits, list) or not edits or len(edits) > MAX_PATCH_OPERATIONS:
        raise ValueError("provide 1 to 100 explicit edit groups")
    expanded = []
    grouped = False
    for edit in edits:
        if hasattr(edit, "model_dump"):
            edit = edit.model_dump(mode="json")
        if not isinstance(edit, dict) or set(edit) - {"operation", "path", "paths", "value"}:
            raise ValueError("invalid explicit edit object")
        if edit.get("operation") not in {"set", "append"} or "value" not in edit:
            raise ValueError("an edit requires operation and value")
        path, paths = edit.get("path"), edit.get("paths", [])
        if (path is not None) == bool(paths):
            raise ValueError("provide exactly one path or a nonempty paths list")
        if path is not None:
            paths = [path]
        else:
            grouped = True
            if edit["operation"] != "set":
                raise ValueError("grouped paths support set only; append each new item explicitly")
        for target in paths:
            if (not isinstance(target, list) or not 1 <= len(target) <= 16
                    or any(not ((isinstance(part, str) and part) or
                               (type(part) is int and part >= 0)) for part in target)):
                raise ValueError("edit paths require exact field names and nonnegative indices")
            expanded.append({"operation": edit["operation"], "path": copy.deepcopy(target),
                             "value": copy.deepcopy(edit["value"])})
            if len(expanded) > MAX_PATCH_OPERATIONS:
                raise ValueError("grouped edits exceed 100 expanded operations")
    # A tail append cannot move existing indices. A set of the list itself,
    # a repeated set or an edit that depends on a newly appended row is ambiguous.
    if grouped:
        for index, edit in enumerate(expanded):
            for other in expanded[:index]:
                short, long = sorted((edit, other), key=lambda e: len(e["path"]))
                prefix, target = short["path"], long["path"]
                if target[:len(prefix)] != prefix:
                    continue
                if prefix == target and short["operation"] == long["operation"] == "append":
                    continue
                if (len(prefix) < len(target) and short["operation"] == "append"
                        and type(target[len(prefix)]) is int):
                    if original is not None:
                        existing = original
                        try:
                            for part in prefix:
                                existing = existing[part]
                        except (KeyError, IndexError, TypeError):
                            existing = None
                        if not isinstance(existing, list) or target[len(prefix)] >= len(existing):
                            raise ValueError("grouped edits must address existing list indices")
                    continue
                raise ValueError("grouped edits cannot overlap; submit sequential batches explicitly")
    return expanded


def apply_record_edits(original, edits):
    """Apply the audited set/append wire protocol; caller validates the schema."""
    expanded = expand_record_edits(edits, original=original)
    changed = copy.deepcopy(original)
    for edit in expanded:
        cursor = changed
        for part in edit["path"][:-1]:
            if isinstance(cursor, dict) and isinstance(part, str) and part in cursor:
                cursor = cursor[part]
            elif isinstance(cursor, list) and type(part) is int and 0 <= part < len(cursor):
                cursor = cursor[part]
            else:
                raise ValueError("edit path does not identify an existing record field")
        key = edit["path"][-1]
        if isinstance(cursor, dict) and isinstance(key, str):
            if edit["operation"] == "append":
                if key not in cursor and key in _OMITTED_LIST_FIELDS:
                    cursor[key] = []
                target = cursor.get(key)
                if not isinstance(target, list):
                    raise ValueError("append requires a list field")
                target.append(copy.deepcopy(edit["value"]))
            else:
                cursor[key] = copy.deepcopy(edit["value"])
        elif isinstance(cursor, list) and type(key) is int and 0 <= key < len(cursor) and edit["operation"] == "set":
            cursor[key] = copy.deepcopy(edit["value"])
        else:
            raise ValueError("invalid edit target; array insertion/deletion is not supported")
    return changed, expanded


def select_schema_definitions(schema, names):
    """Exact named definitions plus every transitive local dependency."""
    if not names:
        return copy.deepcopy(schema)
    definitions = schema.get("$defs", {})
    if any(name not in definitions for name in names):
        unknown = ", ".join(sorted(set(names) - definitions.keys()))
        raise ValueError(f"unknown schema definition: {unknown}. Available definitions: {', '.join(sorted(definitions))}")
    needed = set()

    def references(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                    raise ValueError("unsupported nonlocal schema reference")
                name = reference[len("#/$defs/"):]
                if name not in definitions:
                    raise ValueError("unresolved local schema reference")
                include(name)
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)

    def include(name):
        if name in needed:
            return
        needed.add(name)
        references(definitions[name])

    for name in names:
        include(name)
    return {"$defs": {name: copy.deepcopy(definitions[name]) for name in sorted(needed)},
            "anyOf": [{"$ref": "#/$defs/" + name} for name in names]}
