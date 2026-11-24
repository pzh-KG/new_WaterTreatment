"""Versioned, multi-axis labels; never infer chemical identity from a bare acronym."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaxonomyTerm(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    parent: str | None = None
    description: str = ""


class TaxonomyAxis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    terms: tuple[TaxonomyTerm, ...]

    @model_validator(mode="after")
    def validate_tree(self) -> TaxonomyAxis:
        by_id = {term.id: term for term in self.terms}
        if len(by_id) != len(self.terms):
            raise ValueError(f"Duplicate term ID in {self.id}")
        if len({term.label for term in self.terms}) != len(self.terms):
            raise ValueError(f"Duplicate term label in {self.id}")
        for term in self.terms:
            if term.label in by_id and term.label != term.id:
                raise ValueError(f"Term label collides with another ID in {self.id}")
        for term in self.terms:
            visited = {term.id}
            parent = term.parent
            while parent is not None:
                if parent not in by_id:
                    raise ValueError(f"Unknown parent {parent} in {self.id}")
                if parent in visited:
                    raise ValueError(f"Cyclic taxonomy in {self.id}")
                visited.add(parent)
                parent = by_id[parent].parent
        return self


class TermResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    axis_id: str
    raw_label: str
    status: Literal["known", "unknown"]
    term_id: str | None = None
    taxonomy_version: str


class Taxonomy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = Field(min_length=1)
    description: str = ""
    axes: tuple[TaxonomyAxis, ...]

    @model_validator(mode="after")
    def unique_axes(self) -> Taxonomy:
        if len({axis.id for axis in self.axes}) != len(self.axes):
            raise ValueError("Duplicate axis ID")
        return self

    def axis(self, axis_id: str) -> TaxonomyAxis:
        for axis in self.axes:
            if axis.id == axis_id:
                return axis
        raise ValueError(f"Unknown taxonomy axis: {axis_id}")

    def resolve_term(self, axis_id: str, label: str) -> TermResolution:
        """Exact controlled label/ID lookup, not material-name classification.

        An unknown label is preserved. No formula parsing, fuzzy matching,
        acronym expansion, or inference from precursor/source is performed.
        """
        axis = self.axis(axis_id)
        term = next((t for t in axis.terms if label.strip() in (t.id, t.label)), None)
        return TermResolution(
            axis_id=axis_id, raw_label=label,
            status="known" if term else "unknown",
            term_id=term.id if term else None, taxonomy_version=self.version,
        )

    def extended(self, axis_id: str, term: TaxonomyTerm, *, version: str) -> Taxonomy:
        """Return a validated new dictionary; do not mutate old classifications."""
        if not version.strip() or version == self.version:
            raise ValueError("An extension requires a new taxonomy version")
        axis = self.axis(axis_id)
        replacement = TaxonomyAxis(id=axis.id, label=axis.label, terms=(*axis.terms, term))
        return Taxonomy(
            version=version, description=self.description,
            axes=tuple(replacement if a.id == axis_id else a for a in self.axes),
        )


class TaxonomyAssignment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    subject_id: str = Field(min_length=1)
    axis_id: str
    term_id: str | None = None
    raw_label: str = Field(min_length=1)
    taxonomy_version: str
    basis: Literal["reported", "normalized", "inferred", "unknown"]
    evidence_refs: tuple[str, ...] = ()


def load_taxonomy(path: str | Path) -> Taxonomy:
    return Taxonomy.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def validate_assignments(
    assignments: Sequence[TaxonomyAssignment], taxonomy: Taxonomy,
) -> list[str]:
    """Return structural issues only; valid labels do not verify scientific truth."""
    issues: list[str] = []
    for index, assignment in enumerate(assignments):
        prefix = f"assignment[{index}]"
        if assignment.taxonomy_version != taxonomy.version:
            issues.append(f"{prefix}: taxonomy version mismatch")
        try:
            axis = taxonomy.axis(assignment.axis_id)
        except ValueError:
            issues.append(f"{prefix}: unknown axis {assignment.axis_id}")
            continue
        if assignment.term_id is not None and assignment.term_id not in {t.id for t in axis.terms}:
            issues.append(f"{prefix}: unknown term {assignment.term_id}")
        if assignment.basis in ("reported", "normalized") and not assignment.evidence_refs:
            issues.append(f"{prefix}: source evidence required for {assignment.basis}")
        if assignment.basis == "unknown" and assignment.term_id is not None:
            issues.append(f"{prefix}: unknown assignment must not assert a term")
    return issues


class ScopedDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    abbreviation: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class IdentityResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    abbreviation: str
    paper_id: str
    scope_id: str
    status: Literal["resolved", "unknown", "ambiguous"]
    canonical_name: str | None = None
    candidates: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


def resolve_abbreviation(
    abbreviation: str, *, paper_id: str, scope_id: str,
    definitions: Sequence[ScopedDefinition],
) -> IdentityResolution:
    """Only explicit definitions in this exact paper AND scope may resolve a name."""
    matches = [d for d in definitions if (
        d.abbreviation == abbreviation and d.paper_id == paper_id and d.scope_id == scope_id
    )]
    names = tuple(sorted({d.canonical_name for d in matches}))
    status = "unknown" if not names else "resolved" if len(names) == 1 else "ambiguous"
    return IdentityResolution(
        abbreviation=abbreviation, paper_id=paper_id, scope_id=scope_id,
        status=status, canonical_name=names[0] if len(names) == 1 else None,
        candidates=names, evidence_refs=tuple(sorted({r for d in matches for r in d.evidence_refs})),
    )
