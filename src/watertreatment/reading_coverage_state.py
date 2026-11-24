"""Precisely separate correctable dossier dispositions from scientific defects.

These issues may remain pending while a bounded scientific repair is reviewed.
They still block final completion; this predicate is never reading evidence.
Identity, scope-outline, candidate, source-anchor and audit defects are excluded.
"""
from __future__ import annotations


def only_deferred_coverage_issues(issues):
    """Accept an explicit list containing only known dossier disposition gaps."""
    return isinstance(issues, list) and all(
        isinstance(issue, str) and (
            issue.startswith("dossier lacks explicit coverage disposition for ")
            or issue == "dossier coverage references unknown source/blocks."
        ) for issue in issues
    )
