"""Content-addressed initial-candidate parts. Only the explicit enclosing owner is shared."""
import json
import re

from .domain import Experiment, Observation, ResearchObservationAssignment
from .literal_record_transport import expand_record_wire
from .storage import digest_json, hash_file, write_new_json


SERIES_COLUMNS = {"metric", "metric_kind", "unit", "origin", "measured_object", "importance", "analysis_role",
                  "endpoint_kind", "role_reason", "series_id"}


def candidate_schema_outline(schema):
    """Field names, requiredness, types and literal choices for initial authoring."""
    def shape(node):
        if "$ref" in node:
            return node["$ref"].rsplit("/", 1)[-1]
        if "enum" in node:
            return " | ".join(map(str, node["enum"]))
        if "const" in node:
            return repr(node["const"])
        if "anyOf" in node:
            return " | ".join(shape(i) for i in node["anyOf"])
        if node.get("type") == "array":
            return "[" + shape(node.get("items", {})) + "]"
        return str(node.get("type", "value"))

    def describe(node):
        if "properties" not in node:
            return shape(node)
        required = set(node.get("required", []))
        return {key + ("*" if key in required else ""): shape(v) for key, v in node["properties"].items()}

    return {"record": describe(schema), "definitions": {k: describe(v) for k, v in schema.get("$defs", {}).items()},
            "legend": "* means required. Omitted defaults and numerical constraints are enforced by the full schema. "
                      "If a constraint is unclear, request only the relevant exact names with wt_record_schema definitions=[...]."}


def _observation_rows(arm):
    yield from arm["observations"]
    for series in arm.get("observation_series", []):
        if (not isinstance(series, dict) or set(series) != {"columns", "rows"}
                or not isinstance(series["columns"], dict) or not series["columns"]
                or not set(series["columns"]) <= SERIES_COLUMNS
                or not isinstance(series["rows"], list) or not series["rows"]):
            raise ValueError("A series must explicitly name common measurement columns and its nonempty rows.")
        for row in series["rows"]:
            if not isinstance(row, dict) or not {"id", "value"} <= set(row) or set(row) & set(series["columns"]):
                raise ValueError("Each series row needs its own ID and value, without common-column overrides.")
            yield {**series["columns"], **row}


def _check_ids(data):
    for field in ("experiments", "observations"):
        ids = [row["id"] for row in data.get(field, [])]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Duplicate {field} IDs across arm parts or the final record.")


def stage_arms(folder, scope, raw, shared, layouts):
    # A column-name dictionary may be reused across parts. Unused columns
    # define no scientific values; retain their names in the receipt. Unused
    # shared values, bad references and malformed rows remain errors.
    data, transport = expand_record_wire({"arms": raw}, shared, layouts, allow_unused_layouts=True)
    experiments, observations, assignments = [], [], []
    for arm in data["arms"]:
        if (not isinstance(arm, dict) or not {"experiment", "observations"} <= set(arm)
                or set(arm) - {"experiment", "observations", "observation_series", "assignments"}
                or any(not isinstance(arm.get(k, []), list) for k in ("observations", "observation_series", "assignments"))):
            raise ValueError("Each arm requires its experiment and observations; series and assignments are optional lists.")
        experiment = Experiment.model_validate(arm["experiment"])
        if not experiment.arm_kind:
            raise ValueError("Each staged arm needs its own arm_kind.")
        experiments.append(arm["experiment"])
        owned_ids = set()
        for row in _observation_rows(arm):
            if not isinstance(row, dict) or row.get("experiment_id", experiment.id) != experiment.id:
                raise ValueError("A staged observation cannot have a different owner from its enclosing arm.")
            row = {**row, "experiment_id": experiment.id}
            Observation.model_validate(row)
            observations.append(row)
            owned_ids.add(row["id"])
        assigned = set()
        for group in arm.get("assignments", []):
            if not isinstance(group, dict) or not isinstance(group.get("observation_ids"), list) or not group["observation_ids"]:
                raise ValueError("An assignment group must explicitly list its observations.")
            fields = {k: v for k, v in group.items() if k != "observation_ids"}
            if "observation_id" in fields:
                raise ValueError("Assignment owner must be specified in observation_ids.")
            for oid in group["observation_ids"]:
                if not isinstance(oid, str) or oid not in owned_ids or oid in assigned:
                    raise ValueError("Assignment IDs must be unique and owned by this exact arm.")
                entry = {"observation_id": oid, **fields}
                ResearchObservationAssignment.model_validate(entry)
                assignments.append(entry)
                assigned.add(oid)
    part = {"scope": scope, "experiments": experiments, "observations": observations}
    if assignments:
        part["observation_assignments"] = assignments
    _check_ids(part)
    path = folder / "arm_part.json"
    write_new_json(path, part)
    return {"arm_part_id": folder.name + "-" + hash_file(path), "experiments": len(experiments),
            "observations": len(observations), "transport": transport, "candidate_complete": False}


def assemble_arms(folder, scope, record, identifiers):
    if record.get("rule_version") != "0.9" or not scope or len(set(identifiers)) != len(identifiers):
        raise ValueError("Arm assembly requires distinct receipts and a native 0.9 candidate.")
    output = dict(record)
    for field in ("experiments", "observations"):
        output[field] = list(record.get(field, []))
    for identifier in identifiers:
        if not re.fullmatch(r"tool-[0-9]{4,12}-[a-f0-9]{64}", identifier):
            raise ValueError("Invalid arm receipt identifier.")
        name, sha = identifier.rsplit("-", 1)
        path = folder.parent / name / "arm_part.json"
        if int(name[5:]) >= int(folder.name[5:]) or path.resolve(strict=True) != path or hash_file(path) != sha:
            raise ValueError("Arm receipt outside current log or content changed.")
        part = json.loads(path.read_bytes())
        if part["scope"] != scope:
            raise ValueError("Arm receipt belongs to a different candidate turn.")
        for field in ("experiments", "observations"):
            output[field].extend(part[field])
        if part.get("observation_assignments"):
            if not isinstance(output.get("design_scope"), dict):
                raise ValueError("Staged research assignments need an explicit design_scope in the final record.")
            design = dict(output["design_scope"])
            design["observation_assignments"] = [*design.get("observation_assignments", []), *part["observation_assignments"]]
            ids = [a["observation_id"] for a in design["observation_assignments"]]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate observation assignment across the final record and arm checkpoints.")
            output["design_scope"] = design
    _check_ids(output)
    return output


def arm_checkpoint_response(tool, arguments, response, folder):
    if tool != "wt_submit_record" or not isinstance(arguments, dict) or "arms" not in arguments or set(arguments) - {"arms", "field_layouts", "shared_values"}:
        return False
    items = response.get("contentItems", [])
    if len(items) not in (1, 2) or any(not isinstance(i, dict) or i.get("type") != "inputText" for i in items):
        return False
    try:
        payload = json.loads(items[0]["text"])
        if len(items) == 2:
            clock = json.loads(items[1]["text"])
            if (not isinstance(clock, dict) or set(clock) != {"budget_source", "remaining_turn_seconds"}
                    or clock["budget_source"] != "controller_monotonic_deadline" or type(clock["remaining_turn_seconds"]) is not int):
                return False
        if (not isinstance(payload, dict) or payload.get("candidate_complete") is not False or "submission_id" in payload
                or re.fullmatch(r"tool-[0-9]{4,12}-[a-f0-9]{64}", str(payload.get("arm_part_id", ""))) is None):
            return False
        name, sha = payload["arm_part_id"].rsplit("-", 1)
        part = folder / name
        return (part.resolve(strict=True) == part and hash_file(part / "arm_part.json") == sha
                and json.loads((part / "request.json").read_bytes()) == {"tool": tool, "arguments": arguments}
                and json.loads((part / "result.json").read_bytes()) == {"success": True, "data": payload})
    except (OSError, KeyError, TypeError, ValueError):
        return False


def replay_arm_assembly(folder, arguments):
    receipt = json.loads((folder / "arm_assembly.json").read_bytes())
    plain, _ = expand_record_wire(arguments["record"], arguments.get("shared_values"),
                                  arguments.get("field_layouts"), allow_unused_layouts=True)
    assembled = assemble_arms(folder, receipt["scope"], plain, arguments["arm_part_ids"])
    if digest_json(assembled) != receipt["expanded_sha256"] or assembled != json.loads((folder / "expanded_record_input.json").read_bytes()):
        raise ValueError("Replayed arm assembly differs from its frozen input.")
    for identifier in arguments["arm_part_ids"]:
        name, _ = identifier.rsplit("-", 1)
        result = json.loads((folder.parent / name / "result.json").read_bytes())
        if result.get("success") is not True or result.get("data", {}).get("arm_part_id") != identifier:
            raise ValueError("Arm receipt has no matching successful checkpoint.")
    return assembled
