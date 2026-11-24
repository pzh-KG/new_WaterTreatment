"""Export a source-bound, read-only material-map input from explicit configuration.

This research view preserves reported labels and missingness. It does not infer
composition, merge variants, rank performance, or establish scientific validity.
No Neo4j connection is made unless --verify-live is explicitly supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from watertreatment.domain import ExtractionRecord
from watertreatment.settings import load_settings

VERSION = "material-mapping-export-v1"
FINAL_ROLES = {"primary_final", "optimized_variant", "final_component"}
PUBLICATIONS_QUERY = """
SELECT p.paper_id,p.record_id,p.snapshot_id,p.published_at,
       r.paper_id AS record_paper_id,r.run_id,r.record_path,r.record_hash,r.metadata,
       c.record_id AS latest_record_id,s.paper_id AS changed_paper_id,
       s.reason AS source_change
FROM publications p JOIN records r ON r.id=p.record_id
LEFT JOIN current_records c ON c.paper_id=p.paper_id
LEFT JOIN source_changes s ON s.paper_id=p.paper_id
ORDER BY p.paper_id
"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_hash(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def data_path(value, root):
    path = Path(value).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("input_path_outside_data_root_or_not_file")
    return path


def compact(value):
    """Remove empty metadata only; retain False, zero, all statuses and wording."""
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if v is not None and v != [] and v != {}}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


def taxonomy_labels(path):
    taxonomy = json.loads(path.read_text(encoding="utf-8-sig"))
    return {(axis["id"], term["id"]): term["label"]
            for axis in taxonomy["axes"] for term in axis["terms"]}


def material_row(material, record, binding, labels):
    substances = {item["id"]: item for item in record["substances"]}
    item = {"uid": json.dumps([binding["record_id"], material["id"]], ensure_ascii=False,
                              separators=(",", ":")),
            "paper_id": binding["paper_id"], "record_id": binding["record_id"],
            "snapshot_id": binding["snapshot_id"], "material_id": material["id"],
            "name": material["name"], "family_id": material["family_id"],
            "role": material["role"], "classification": [], "components": [],
            "state": material["state"], "morphology": material["morphology"],
            "evidence_ids": material["evidence_ids"]}
    for classification in material["classification"]:
        entry = dict(classification)
        value = entry["label"].get("value")
        # Only exact dictionary IDs are display-translated. Unknown/source labels
        # and Fact.normalized are never used to manufacture a category.
        if isinstance(value, str) and (entry["axis"], value) in labels:
            entry["display_label"] = labels[entry["axis"], value]
        item["classification"].append(entry)
    for component in material["components"]:
        entry = dict(component)
        substance = substances.get(component["substance_id"])
        if substance is None:
            raise ValueError("material_component_references_missing_substance")
        entry["substance"] = {key: substance[key] for key in ("id", "name", "formula", "evidence_ids")}
        item["components"].append(entry)
    assigned = {a["observation_id"] for a in (record.get("design_scope") or {}).get("observation_assignments", [])
                if a["purpose"] == "material_feature"}
    # Retain the complete observation and its experiment context. This is
    # navigation data, not permission to compare unnormalized quantities.
    experiments = {experiment["id"]: experiment for experiment in record["experiments"]}
    item["feature_observations"] = [
        {"observation": observation, "experiment": experiments.get(observation["experiment_id"]),
         "use": "source_navigation_only_comparability_not_established"}
        for observation in record["observations"]
        if observation["id"] in assigned and observation.get("material_id") == material["id"]]
    return compact(item)


def evidence_ids(value):
    found = set()
    if isinstance(value, dict):
        found.update(value.get("evidence_ids", []))
        for nested in value.values():
            found.update(evidence_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(evidence_ids(nested))
    return found


def read_record(row, root, labels):
    path = data_path(row["record_path"], root)
    record_bytes = path.read_bytes()
    if hashlib.sha256(record_bytes).hexdigest() != row["record_hash"]:
        raise ValueError("record_sha256_mismatch")
    record = ExtractionRecord.model_validate_json(record_bytes).model_dump(mode="json")
    if row["paper_id"] != row["record_paper_id"] or record["paper"]["id"] != row["paper_id"]:
        raise ValueError("paper_identity_mismatch")
    for entity in ("materials", "substances", "observations", "evidence"):
        ids = [item["id"] for item in record[entity]]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_record_entity_ids")
    metadata = json.loads(row["metadata"])
    run_id = row["run_id"]
    if not isinstance(run_id, str) or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("invalid_run_identity")
    bundle_path = data_path(root / "runs" / run_id / "sources.json", root)
    bundle_bytes = bundle_path.read_bytes()
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    if bundle_hash != metadata.get("source_hash"):
        raise ValueError("source_bundle_sha256_mismatch")
    bundle = json.loads(bundle_bytes.decode("utf-8-sig"))
    if bundle.get("paper_id") != row["paper_id"]:
        raise ValueError("source_bundle_paper_identity_mismatch")
    sources = []
    for document in bundle.get("documents", []):
        source = document["source"]
        original = data_path(source["path"], root)
        if file_hash(original) != source["sha256"]:
            raise ValueError("source_document_sha256_mismatch")
        sources.append({"source_id": source["source_id"], "role": source["role"],
                        "path": str(original), "sha256": source["sha256"]})
    if not sources:
        raise ValueError("source_bundle_has_no_documents")
    binding = {key: row[key] for key in ("paper_id", "record_id", "snapshot_id", "run_id", "published_at")}
    binding.update(record_path=str(path), record_sha256=row["record_hash"],
                   record_fingerprint=digest(record), schema_version=record["schema_version"],
                   rule_version=record["rule_version"], sources=sources,
                   source_bundle={"path": str(bundle_path), "sha256": bundle_hash},
                   title=record["paper"]["title"], doi=record["paper"]["doi"],
                   dataset_role=record["paper"]["dataset_role"])
    materials = [material_row(m, record, binding, labels) for m in record["materials"] if m["role"] in FINAL_ROLES]
    required_evidence = evidence_ids(materials) | evidence_ids(binding["title"]) | evidence_ids(binding["doi"])
    known_evidence = {e["id"]: e for e in record["evidence"]}
    if not required_evidence <= set(known_evidence):
        raise ValueError("material_fact_references_missing_evidence")
    # Locators are included once per record. Quotes and full snapshots stay in
    # the authoritative record rather than inflating the inline map dataset.
    binding["evidence"] = [{"id": e["id"], "source_id": e["source_id"], "block_id": e["block_id"],
                            "locator": e["locator"]} for eid in sorted(required_evidence)
                           for e in [known_evidence[eid]]]
    return compact(binding), materials


def verify_live(settings, bindings):
    from watertreatment import neo4j_connection as connection
    config = settings.neo4j
    if not config.enabled:
        raise ValueError("live_verification_requires_enabled_explicit_neo4j_config")
    expected = {b["snapshot_id"]: b for b in bindings}
    if len(expected) != len(bindings):
        raise ValueError("duplicate_snapshot_identity")
    try:
        with connection._driver_factory(config.uri, auth=(config.username, connection._password(config)),
                connection_timeout=config.timeout_seconds,
                connection_acquisition_timeout=config.timeout_seconds,
                max_transaction_retry_time=config.timeout_seconds) as driver:
            with driver.session(database=config.database, default_access_mode="READ") as session:
                with session.begin_transaction(timeout=config.timeout_seconds) as transaction:
                    actual = transaction.run("CALL db.info() YIELD name RETURN name AS database").single(strict=True)
                    if actual is None or actual["database"] != config.database:
                        raise ValueError("database_identity_mismatch")
                    rows = transaction.run(
                        "MATCH (s:WTProjection) WHERE s.id IN $ids "
                        "RETURN s.id AS snapshot_id,s.paper_id AS paper_id,"
                        "s.record_fingerprint AS record_fingerprint",
                        ids=list(expected)).data()
                    if len(rows) != len(expected) or {r["snapshot_id"] for r in rows} != set(expected):
                        raise ValueError("active_snapshot_scope_mismatch")
                    for row in rows:
                        binding = expected[row["snapshot_id"]]
                        if any(row[key] != binding[key] for key in ("paper_id", "record_fingerprint")):
                            raise ValueError("active_snapshot_record_identity_mismatch")
        return {"performed": True, "actual_database": config.database,
                "database_identity_confirmed": True, "matched_active_snapshots": len(expected),
                "persistent_graph_write": False}
    except Exception:
        # Driver exceptions may reveal endpoint or credential information.
        raise ValueError("configured_database_or_active_snapshot_verification_failed") from None


def git_identity():
    def command(arguments):
        result = subprocess.run(["git", "-C", str(PROJECT_ROOT), *arguments],
                                capture_output=True, text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        return {"head": command(["rev-parse", "HEAD"]),
                "status_porcelain": command(["status", "--porcelain=v1"])}
    except (OSError, subprocess.TimeoutExpired):
        return {"head": None, "status_porcelain": None, "available": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify-live", action="store_true")
    arguments = parser.parse_args()
    settings = load_settings(arguments.config)
    destination = arguments.output_dir.resolve()
    exports = (settings.data_root / "exports").resolve()
    if not arguments.output_dir.is_absolute() or destination == exports or not destination.is_relative_to(exports):
        raise ValueError("output_dir_must_be_an_explicit_child_of_configured_data_root_exports")
    if destination.exists():
        raise ValueError("output_dir_already_exists")
    labels = taxonomy_labels(settings.taxonomy_path)
    database_path = data_path(settings.data_root / "workbench" / "records.sqlite3", settings.data_root)
    with sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=30) as catalog:
        catalog.row_factory = sqlite3.Row
        catalog.execute("PRAGMA query_only=ON")
        catalog.execute("BEGIN")
        rows = [dict(row) for row in catalog.execute(PUBLICATIONS_QUERY)]
        # Preserve orphaned publication failures instead of silently losing them
        # through the required publications/records inner join.
        orphaned = [dict(row) for row in catalog.execute(
            "SELECT p.paper_id,p.record_id,p.snapshot_id FROM publications p "
            "LEFT JOIN records r ON r.id=p.record_id WHERE r.id IS NULL ORDER BY p.paper_id")]
    excluded = [{**row, "reasons": ["publication_record_missing"]} for row in orphaned]
    bindings, materials = [], []
    for row in rows:
        reasons = []
        if row["record_id"] != row["latest_record_id"]:
            reasons.append("published_record_is_not_current")
        if row["changed_paper_id"] is not None:
            reasons.append("source_changed")
        if not isinstance(row["snapshot_id"], str) or not row["snapshot_id"]:
            reasons.append("snapshot_identity_missing")
        if not reasons:
            try:
                binding, extracted = read_record(row, settings.data_root, labels)
            except (ValueError, OSError, KeyError, TypeError) as error:
                reasons.append("record_or_source_integrity_failure")
                # Preserve the failure class; paths and detailed traceback remain
                # inspectable through source record provenance, not model output.
                row["export_error_type"] = type(error).__name__
            else:
                bindings.append(binding)
                materials.extend(extracted)
        if reasons:
            excluded.append({key: row.get(key) for key in ("paper_id", "record_id", "snapshot_id", "latest_record_id",
                                                          "source_change", "export_error_type")} | {"reasons": reasons})
    verification = verify_live(settings, bindings) if arguments.verify_live else {
        "performed": False, "scope_basis": "local_workbench_publication_pointers", "persistent_graph_write": False}
    created = datetime.now(timezone.utc).isoformat()
    payload = {"version": VERSION, "created_at": created, "scope": "current_nonstale_workbench_publications",
               "scientific_validation": "not_established", "records": bindings, "materials": materials}
    counts = Counter(m["role"] for m in materials)
    axes = Counter(c["axis"] for m in materials for c in m.get("classification", []))
    inventory = {"version": VERSION, "created_at": created,
                 "publications_examined": len(rows) + len(orphaned), "included_records": len(bindings),
                 "excluded_records": len(excluded), "materials": len(materials),
                 "material_roles": dict(counts), "classification_entries_by_axis": dict(axes),
                 "materials_with_feature_observations": sum(bool(m.get("feature_observations")) for m in materials),
                 "selected_publications": [{key: b[key] for key in ("paper_id", "record_id", "snapshot_id")} for b in bindings],
                 "excluded_publications": excluded, "live_verification": verification,
                 "limitations": ["Reported labels are preserved; no labels or absences are inferred.",
                     "Material variants remain separate; no aliases or formula identities are merged.",
                     "Preloaded consumables and unresolved components are not retained composition.",
                     "Performance is not a material feature; no numerical ranking or performance pairing is performed.",
                     "Hash and schema checks do not establish scientific correctness."]}
    outputs = {"materials.json": payload, "inventory.json": inventory}
    manifest = {"version": VERSION, "created_at": created, "script": {"path": str(Path(__file__).resolve()),
                "sha256": file_hash(Path(__file__))}, "git": git_identity(),
                "config": {"path": str(arguments.config.resolve()), "sha256": file_hash(arguments.config)},
                "taxonomy": {"path": str(settings.taxonomy_path), "sha256": file_hash(settings.taxonomy_path)},
                "catalog": {"path": str(database_path), "access": "sqlite_mode_ro_query_only",
                            "publication_selection_sha256": digest(rows), "orphaned_publications_sha256": digest(orphaned)},
                "records": [{key: b[key] for key in ("paper_id", "record_id", "snapshot_id", "record_path",
                             "record_sha256", "record_fingerprint", "source_bundle", "sources")} for b in bindings],
                "live_verification": verification, "model_calls": 0, "persistent_graph_write": False,
                "outputs": {name: {"sha256": hashlib.sha256(encoded(value)).hexdigest(), "bytes": len(encoded(value))}
                            for name, value in outputs.items()}}
    destination.mkdir(parents=True, exist_ok=False)
    for name, value in {**outputs, "manifest.json": manifest}.items():
        with (destination / name).open("xb") as output:
            output.write(encoded(value))
    print(json.dumps({"output_dir": str(destination), "records": len(bindings), "materials": len(materials),
                      "excluded_records": len(excluded), "materials_bytes": manifest["outputs"]["materials.json"]["bytes"],
                      "live_verified": verification["performed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
