"""Compact transport feedback; complete machine findings remain an audit artifact.

Errors are never sampled. Repeated warnings are indexed by code and exact path,
with full details available through a controller-registered retrieval tool.
This changes presentation, not validation, admission or scientific meaning.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy


def compact_validation_feedback(issues: list[dict], artifact: dict) -> dict:
    groups = {}
    for issue in issues:
        if issue["severity"] == "error":
            continue
        key = (issue["severity"], issue["code"])
        group = groups.setdefault(key, {"severity": key[0], "code": key[1], "count": 0,
                                       "affected_paths": [], "examples": []})
        group["count"] += 1
        path = issue.get("path")
        if path not in group["affected_paths"]:
            group["affected_paths"].append(path)
        if len(group["examples"]) < 2:
            group["examples"].append(deepcopy(issue))
    return {"errors": [deepcopy(i) for i in issues if i["severity"] == "error"],
            "warning_groups": list(groups.values()),
            "issue_counts": dict(Counter(i["severity"] for i in issues)),
            "validation_artifact": deepcopy(artifact),
            "all_errors_inlined": True, "all_warning_details_inlined": False,
            "feedback_notice": "All errors are included verbatim. Non-error groups list every affected path but "
                "only two example issues per code. Full findings are preserved in the immutable validation artifact; "
                "use wt_validation_issues with its validation_id to inspect exact details. Grouping does not waive "
                "warnings, prove missing source information, or establish scientific validity. Honest partial-series "
                "warnings require scope-appropriate limitations, not forced point digitization.",
            "scientific_accuracy": "not_established"}
