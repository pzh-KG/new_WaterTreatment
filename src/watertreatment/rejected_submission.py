"""Reproduce an invalid initial model submission and apply only explicit schema corrections."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from .domain import ExtractionRecord
from .literal_record_transport import expand_record_wire
from .storage import hash_file


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    path: list[str | StrictInt] = Field(min_length=1, max_length=16)
    operation: Literal["set", "remove_extra"]
    value: Any = None

    @model_validator(mode="after")
    def exact_edit(self):
        if any((type(p) is int and p < 0) or p == "" for p in self.path):
            raise ValueError("use exact field names and nonnegative indices")
        if ("value" in self.model_fields_set) != (self.operation == "set"):
            raise ValueError("only a set correction requires an explicit value (including null)")
        return self


class DraftRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: str = Field(pattern=r"^tool-[0-9]{4,12}-[a-f0-9]{64}$")
    edits: list[DraftEdit] = Field(min_length=1, max_length=100)


def locations(error):
    return [{"path": list(e["loc"]), "type": e["type"]}
            for e in error.errors(include_url=False, include_input=False, include_context=False)]


def repair_input(folder, repair):
    """The completed model request commits to the exact earlier raw request by SHA-256."""
    folder = Path(folder)
    name, expected = repair.draft_id.rsplit("-", 1)
    if (not re.fullmatch(r"tool-[0-9]{4,12}", folder.name) or folder.parent.name != "reading_tools"
            or int(name[5:]) >= int(folder.name[5:])):
        raise ValueError("draft must precede this submission in the same tool log")
    prior = folder.parent / name
    request, result = prior / "request.json", prior / "result.json"
    if any(path.resolve(strict=True) != path for path in (request, result)) or hash_file(request) != expected:
        raise ValueError("rejected draft request is missing or changed")
    envelope, failure = json.loads(request.read_bytes()), json.loads(result.read_bytes())
    if (envelope.get("tool") != "wt_submit_record" or failure.get("success") is not False
            or failure.get("rejected_draft_id") != repair.draft_id):
        raise ValueError("draft has no matching rejected initial submission")
    args = envelope["arguments"]
    if args.get("arm_part_ids"):
        from .arm_transport import replay_arm_assembly
        data = replay_arm_assembly(prior, args)
    else:
        data, _ = expand_record_wire(args["record"], args.get("shared_values"), args.get("field_layouts"))
    try:
        ExtractionRecord.model_validate(data)
    except ValidationError as error:
        invalid = {tuple(e["path"]): e["type"] for e in locations(error)}
    else:
        raise ValueError("a schema-valid result cannot be changed as a rejected draft")
    seen = set()
    for edit in repair.edits:
        path = tuple(edit.path)
        if path not in invalid or any(path[:len(p)] == p or p[:len(path)] == path for p in seen):
            raise ValueError("correction must cite a distinct, nonoverlapping error path from this exact draft")
        seen.add(path)
        cursor = data
        try:
            for part in path[:-1]:
                cursor = cursor[part]
            key = path[-1]
            if edit.operation == "remove_extra":
                if invalid[path] != "extra_forbidden" or not isinstance(cursor, dict):
                    raise ValueError("only a schema-forbidden extra field can be removed from a rejected input")
                del cursor[key]
            else:
                cursor[key] = edit.value
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("validation path does not address a literal field in the draft") from exc
    return data, {"mode": "initial_schema_correction_v1", "draft_id": repair.draft_id,
                  "rejected_request_sha256": expected, "rejected_result_sha256": hash_file(result),
                  "corrected_paths": [edit.path for edit in repair.edits], "automatic_scientific_transformations": 0}
