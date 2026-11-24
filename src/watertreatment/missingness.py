"""Targeted consistency feedback; missing values are never inferred or filled.

Claimed reading of a final material's preparation does not establish its retained
composition.  It does make an unread retained-amount field worth an explicit
disposition.  This is a warning to consult the source, not evidence of absence.
"""
from __future__ import annotations

from .domain import DESIGN_VERSIONS

from .domain import FINAL_ROLES, FactStatus, Retention


def missingness_issues(record):
    from .validation import ValidationIssue

    # Existing archives keep their original validation/serialization contract.
    if record.rule_version not in DESIGN_VERSIONS or record.design_scope is None:
        return []
    preparation_checked = any(check.area == "preparation_structure" and check.state == "checked"
                              for check in record.design_scope.core_checks)
    if not preparation_checked:
        return []
    issues = []
    for mi, material in enumerate(record.materials):
        if material.role not in FINAL_ROLES:
            continue
        for ci, component in enumerate(material.components):
            if (component.retention not in {Retention.RETAINED, Retention.PRELOADED_CONSUMABLE}
                    or not (component.evidence_ids or material.evidence_ids)
                    or component.amount.status != FactStatus.UNREAD):
                continue
            issues.append(ValidationIssue(severity="warning", code="COMPONENT_AMOUNT_READING_STATE",
                path=f"materials[{mi}].components[{ci}].amount",
                message="Preparation/structure is marked checked and this retained component has source evidence, "
                    "but its actual amount remains unread. Inspect the source and distinguish not_reported, "
                    "read_unquantified, unavailable and genuinely unread; preserve unread with an explicit reason "
                    "when reading remains incomplete. Do not substitute precursor/suspension charge, infer a "
                    "retained fraction, fill zero, or automatically convert all nulls to not_reported."))
    return issues
