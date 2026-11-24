"""Read-only quantitative completeness/provenance summaries, never validation."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from enum import Enum
from math import isfinite
from numbers import Integral, Real
import re

COORDINATE_SERIES_KINDS = frozenset({"time_series", "cycle_series", "condition_series", "table_cells"})
NUMERIC_SERIES_KINDS = COORDINATE_SERIES_KINDS | {"scalar"}

# Explicit coordinate vocabulary, not a semantic guess from arbitrary labels.
# Unknown axis names retain a local comparison limitation until classified.
TIME_COORDINATE_NAMES = frozenset({
    "time", "elapsed_time", "reaction_time", "irradiation_time",
    "illumination_time", "exposure_time", "sampling_time", "time_point", "t",
})
# An ordinal operating/sampling day can locate a time-series point without
# establishing hours of irradiation or a count of repeated treatment cycles.
DAY_COORDINATE_NAMES = frozenset({
    "day", "day_index", "day_number", "operating_day", "operation_day",
    "day_of_operation", "sampling_day", "study_day", "experimental_day",
    "test_day", "calendar_day", "run_day",
})
CLOCK_COORDINATE_NAMES = frozenset({"clock_time", "time_of_day", "local_time"})
CYCLE_COORDINATE_NAMES = frozenset({"cycle", "cycle_index", "cycle_number", "cycle_count", "cycle_no"})
_CLOCK_VALUE = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?")


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _text(value):
    return value.value if isinstance(value, Enum) else value


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    # Large Python integers are finite even when a float conversion overflows.
    if isinstance(value, Integral):
        return True
    try:
        return isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def coordinate_value_key(value):
    """Numeric equality only; never convert units, references or numeric text.

    Python's finite number equality/hash makes 10 and 10.0 the same coordinate,
    while preserving unequal large integers and explicit replicate dimensions.
    This key detects conflicts; it never deletes or merges an observation.
    """
    return ("number", value) if _finite_number(value) else ("literal", repr(value))


def _coordinate_name(value) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().casefold())


def has_numeric_value(observation) -> bool:
    """A scalar quantity only; a range/vector is not an independent source point."""
    fact = _get(observation, "value", {})
    value = _get(fact, "value")
    status = _text(_get(fact, "status", "unread"))
    return (_finite_number(value)
            and status in {"reported", "estimated", "explicit_absence", "inferred"})


def has_quantitative_value(observation) -> bool:
    """Include numeric ranges/vectors for provenance, without counting points.

    A numeric list can be a range, confidence interval or undivided vector. Its
    length therefore cannot establish curve/table extraction completeness.
    """
    if has_numeric_value(observation):
        return True
    fact = _get(observation, "value", {})
    value = _get(fact, "value")
    status = _text(_get(fact, "status", "unread"))
    return (isinstance(value, list) and bool(value)
            and all(_finite_number(item) for item in value)
            and status in {"reported", "estimated", "explicit_absence", "inferred"})


def has_usable_coordinates(observation, data_kind=None, *, observation_window=False) -> bool:
    """Require available coordinate values, not merely nonempty containers.

    This is an internal availability check. It does not certify units, temporal
    references or scientific comparability, which require original evidence.
    A time/cycle series additionally needs its explicit axis, not just a group
    label. Omitting data_kind preserves the general availability-only API.
    """
    coordinates = _get(observation, "coordinates", [])
    window_axis = False
    if observation_window and data_kind == "time_series":
        w = _get(observation, "time_window", {})
        end, unit, origin = (_get(w, field, {}) for field in ("end", "unit", "reference"))
        window_axis = (_get(w, "kind") in {"point", "endpoint", "time_point", "instantaneous", "plot_coordinate", "interval", "average"}
                       and _finite_number(_get(end, "value")) and bool(_get(end, "evidence_ids"))
                       and all(_text(_get(f, "status")) in {"reported", "estimated"} for f in (end, unit, origin))
                       and bool(_get(unit, "value")) and bool(_get(origin, "value")))
    if not coordinates:
        return window_axis
    required_axis_available = window_axis
    for coordinate in coordinates:
        fact = _get(coordinate, "fact", {})
        value = _get(fact, "value")
        status = _text(_get(fact, "status", "unread"))
        if status not in {"reported", "estimated", "explicit_absence"}:
            return False
        if isinstance(value, bool):
            return False
        if isinstance(value, Real) and not _finite_number(value):
            return False
        if not _finite_number(value) and (not isinstance(value, str) or not value.strip()):
            return False
        name = _coordinate_name(_get(coordinate, "name", ""))
        if data_kind == "time_series":
            required_axis_available |= ((name in TIME_COORDINATE_NAMES or name in DAY_COORDINATE_NAMES) and _finite_number(value))
            required_axis_available |= (name in CLOCK_COORDINATE_NAMES and isinstance(value, str)
                                        and _CLOCK_VALUE.fullmatch(value.strip()) is not None)
        elif data_kind == "cycle_series":
            # An explicit day-only basis cannot authenticate repeated cycles.
            basis = _coordinate_name(_get(_get(coordinate, "basis", {}), "value", ""))
            required_axis_available |= (name in CYCLE_COORDINATE_NAMES and _finite_number(value)
                                        and basis not in DAY_COORDINATE_NAMES)
    return required_axis_available if data_kind in {"time_series", "cycle_series"} else True


def has_source_numeric_point(observation) -> bool:
    """Count extracted source values, never values generated by local analysis."""
    if not has_numeric_value(observation):
        return False
    origin = _text(_get(observation, "origin", "unresolved"))
    fact = _get(observation, "value", {})
    return (origin in {"reported_measurement", "author_calculation", "author_simulation", "plot_estimate"}
            and _text(_get(fact, "status")) in {"reported", "estimated", "explicit_absence"}
            and bool(_get(fact, "evidence_ids", [])))


def observation_value_kind(observation) -> str:
    """Origin wins over old ambiguous 'reported' status; no record is mutated."""
    if not has_quantitative_value(observation):
        return "not_quantified"
    origin = _text(_get(observation, "origin", "unresolved"))
    if origin == "plot_estimate":
        return "plot_estimate"
    if _text(_get(_get(observation, "value", {}), "status")) == "estimated":
        return "estimated"
    if origin in {"model_inference", "local_calculation"}:
        return origin
    return {"reported_measurement": "author_reported", "author_calculation": "author_calculation",
            "author_simulation": "author_simulation", "author_hypothesis": "author_hypothesis"}.get(origin, "unresolved")


def quantitative_summary(record) -> dict:
    """Summarize source inventory declarations, not coverage verified by humans."""
    observations = list(_get(record, "observations", []))
    observation_window = _get(record, "rule_version") == "0.9"
    by_id = {_get(item, "id"): item for item in observations}
    inventory = list(_get(record, "quantitative_coverage", []))
    kinds = Counter(observation_value_kind(item) for item in observations)
    states = Counter(_text(_get(item, "state", "unread")) for item in inventory)
    incomplete = []
    for item in inventory:
        state = _text(_get(item, "state", "unread"))
        # Intentionally excluded cited benchmarks/non-target panels are part
        # of the inventory, not evidence that our own study is incomplete.
        if state == "out_of_scope":
            continue
        identifiers = set(_get(item, "observation_ids", []))
        source_points = [by_id[oid] for oid in identifiers if oid in by_id and has_source_numeric_point(by_id[oid])]
        count = len(source_points)
        expected = _get(item, "expected_points")
        kind = _get(item, "data_kind")
        # These flags describe separate availability checks, not scientific
        # completeness. A zero-point numeric inventory remains limited below.
        point_count_matched = expected is not None and count == expected
        coordinates_available = (kind not in COORDINATE_SERIES_KINDS or
            (bool(source_points) and all(has_usable_coordinates(point, kind, observation_window=observation_window) for point in source_points)))
        item_limits = [text for text in _get(item, "limitations", []) if str(text).strip()]
        if state == "complete" and expected is None:
            item_limits.append("Declared complete but source point count is unknown; pointwise completeness is not established.")
        elif state == "complete" and count != expected:
            item_limits.append("Declared complete but extracted source point count differs from the expected count.")
        if state == "complete" and kind in NUMERIC_SERIES_KINDS and count == 0:
            item_limits.append("A numeric series with no extracted source points cannot establish quantitative completeness.")
        if kind in COORDINATE_SERIES_KINDS and any(not has_usable_coordinates(point, kind, observation_window=observation_window) for point in source_points):
            item_limits.append("Some extracted source points lack available time/cycle/condition/cell coordinates; restrict pointwise comparisons.")
        if state == "complete" and not item_limits:
            continue
        incomplete.append({"id": _get(item, "id"), "label": _get(item, "label"),
            "source_id": _get(item, "source_id"), "state": state,
            "data_kind": kind, "expected_points": expected,
            "quantified_points": count,
            "point_count_matched": point_count_matched,
            "coordinates_available": coordinates_available,
            "reason": _get(item, "reason"), "limitations": item_limits})
    limitations = []
    if not inventory:
        limitations.append("No quantitative figure/table inventory: page-reading coverage does not establish curve/cell completeness.")
    if incomplete:
        limitations.append("Some figure/table series retain quantitative coverage or analysis limitations; restrict pointwise comparisons to explicitly supported source data.")
    if kinds["plot_estimate"] or kinds["estimated"]:
        limitations.append("Estimated values must remain distinguishable from author-stated numbers; they are not raw experimental data.")
    return {"has_quantitative_inventory": bool(inventory), "inventory_entries": len(inventory),
        "coverage_states": dict(states), "incomplete_series": incomplete,
        "value_kinds": dict(kinds), "estimated_values": kinds["plot_estimate"] + kinds["estimated"],
        "author_values": sum(kinds[key] for key in ("author_reported", "author_calculation", "author_simulation")),
        "unquantified_observations": kinds["not_quantified"], "limitations": limitations,
        "scientific_validation": "not_established"}
