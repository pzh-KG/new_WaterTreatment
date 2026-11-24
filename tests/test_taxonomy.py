from pathlib import Path

import pytest
from pydantic import ValidationError

from watertreatment.taxonomy import (
    ScopedDefinition, Taxonomy, TaxonomyAssignment, TaxonomyAxis, TaxonomyTerm,
    load_taxonomy, resolve_abbreviation, validate_assignments,
)


@pytest.fixture
def taxonomy():
    return load_taxonomy(Path(__file__).resolve().parents[1] / "config" / "taxonomy.v0.5.json")


def test_final_chemistry_and_origin_do_not_imply_each_other(taxonomy):
    assert taxonomy.resolve_term("origin", "mof_derived").status == "known"
    assert taxonomy.resolve_term("chemistry", "mof_derived").status == "unknown"
    assert taxonomy.resolve_term("chemistry", "biomass_derived").term_id is None
    assert taxonomy.resolve_term("form", "nanofluid").status == "known"


def test_unknown_material_is_preserved_and_extension_is_versioned(taxonomy):
    result = taxonomy.resolve_term("chemistry", "new fictional phase")
    assert result.status == "unknown" and result.raw_label == "new fictional phase"
    new = taxonomy.extended(
        "chemistry", TaxonomyTerm(id="fictional_phase", label="new fictional phase", parent="inorganic_compound"),
        version="0.6-test",
    )
    assert new.resolve_term("chemistry", "new fictional phase").term_id == "fictional_phase"
    assert taxonomy.resolve_term("chemistry", "new fictional phase").status == "unknown"
    with pytest.raises(ValueError, match="new taxonomy version"):
        taxonomy.extended("chemistry", TaxonomyTerm(id="x", label="x"), version="0.5")


def test_multiaxis_multilabel_assignments_require_evidence(taxonomy):
    assignments = [TaxonomyAssignment(
        subject_id="synthetic-composite", axis_id="chemistry", term_id=term,
        raw_label=term, taxonomy_version="0.5", basis="reported", evidence_refs=("source:p2",),
    ) for term in ("oxide", "polymer", "carbon")]
    assert validate_assignments(assignments, taxonomy) == []
    bad = assignments[0].model_copy(update={"taxonomy_version": "old", "evidence_refs": ()})
    assert len(validate_assignments([bad], taxonomy)) == 2
    unknown = TaxonomyAssignment(subject_id="new", axis_id="chemistry", raw_label="unresolved phase", taxonomy_version="0.5", basis="unknown")
    assert validate_assignments([unknown], taxonomy) == []


@pytest.mark.parametrize("abbreviation", ["MB", "PS"])
def test_bare_abbreviation_never_resolves_globally(abbreviation):
    assert resolve_abbreviation(abbreviation, paper_id="paper-A", scope_id="run", definitions=[]).status == "unknown"


def test_definition_is_scoped_to_paper_and_experimental_context():
    definitions = [
        ScopedDefinition(abbreviation="PS", canonical_name="polystyrene", paper_id="paper-A", scope_id="support", evidence_refs=("p1",)),
        ScopedDefinition(abbreviation="PS", canonical_name="persulfate", paper_id="paper-A", scope_id="reaction", evidence_refs=("p3",)),
        ScopedDefinition(abbreviation="PS", canonical_name="fictional unrelated substance", paper_id="paper-B", scope_id="reaction", evidence_refs=("p5",)),
    ]
    result = resolve_abbreviation("PS", paper_id="paper-A", scope_id="reaction", definitions=definitions)
    assert result.canonical_name == "persulfate" and result.evidence_refs == ("p3",)
    assert resolve_abbreviation("PS", paper_id="paper-A", scope_id="unread", definitions=definitions).status == "unknown"
    definitions.append(ScopedDefinition(abbreviation="PS", canonical_name="ambiguous alternative", paper_id="paper-A", scope_id="reaction", evidence_refs=("p7",)))
    assert resolve_abbreviation("PS", paper_id="paper-A", scope_id="reaction", definitions=definitions).status == "ambiguous"


def test_invalid_dictionary_hierarchy_and_duplicates_are_rejected():
    with pytest.raises(ValidationError, match="Unknown parent"):
        TaxonomyAxis(id="chemical", label="chemical", terms=(TaxonomyTerm(id="x", label="x", parent="missing"),))
    with pytest.raises(ValidationError, match="Cyclic"):
        TaxonomyAxis(id="chemical", label="chemical", terms=(
            TaxonomyTerm(id="x", label="x", parent="y"), TaxonomyTerm(id="y", label="y", parent="x"),
        ))
    axis = TaxonomyAxis(id="x", label="x", terms=())
    with pytest.raises(ValidationError, match="Duplicate axis"):
        Taxonomy(version="test", axes=(axis, axis))
    with pytest.raises(ValidationError, match="collides"):
        TaxonomyAxis(id="x", label="x", terms=(
            TaxonomyTerm(id="first", label="second"), TaxonomyTerm(id="second", label="other"),
        ))


def test_unknown_axis_is_a_schema_error_not_a_new_chemical_class(taxonomy):
    with pytest.raises(ValueError, match="Unknown taxonomy axis"):
        taxonomy.resolve_term("misspelled_axis", "oxide")
