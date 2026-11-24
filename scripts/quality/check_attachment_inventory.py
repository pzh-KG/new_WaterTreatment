"""Check only a frozen user-verified collection; no extraction or graph operation.

Runs the same classifier as the workbench, retaining a SQLite backup and a
timestamped identification report. Limited samples are never reading coverage.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import time

from watertreatment.collection_policy import load_collection_policy
from watertreatment.codex import code_identity
from watertreatment.settings import load_settings
from watertreatment.storage import DataStore, hash_file, utc_now, write_new_json
from watertreatment.web_library import WebLibrary, CLASSIFIER_VERSION


def check(config: Path, output: Path):
    settings = load_settings(config)
    output = output.resolve()
    if not output.is_relative_to(settings.data_root):
        raise ValueError("identification reports must stay in the configured data root")
    policy = load_collection_policy(settings)
    if policy is None:
        raise ValueError("an explicit verified collection is required; no default whole-library scan")
    output.mkdir(parents=True, exist_ok=False)
    database = settings.data_root / "workbench/library.sqlite3"
    if database.exists():
        with sqlite3.connect(database) as original, sqlite3.connect(output / "library.before.sqlite3") as backup:
            original.backup(backup)
    store = DataStore(settings.data_root)
    before_runs = store.runs() if (settings.data_root / "store.json").is_file() else []
    code_before = code_identity(settings.project_root)
    write_new_json(output / "before.json", {"at": utc_now(), "config_sha256": hash_file(config),
        "policy_path": str(policy.path), "policy_sha256": policy.manifest_sha256,
        "registered_run_ids": [r["run_id"] for r in before_runs], "classifier_version": CLASSIFIER_VERSION,
        "code_identity": code_before})
    library = WebLibrary(settings)
    started = time.monotonic()

    def progress(paper, completed, total):
        item = {"at": utc_now(), "completed": completed, "total": total,
                "folder": paper["folder_name"], "status": paper["attachment_status"]}
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        if completed % 25 == 0 or completed == total:
            print(json.dumps(item, ensure_ascii=False), flush=True)

    try:
        papers = library.scan([str(folder) for folder in policy.entries], on_progress=progress)
        if len(papers) != len(policy.entries):
            raise ValueError("some verified folders no longer contain identifiable document files")
        after_runs = store.runs() if before_runs or (settings.data_root / "store.json").is_file() else []
        if before_runs != after_runs:
            raise ValueError("registered run inventory changed during identification; inspect concurrent activity")
        if code_before != code_identity(settings.project_root):
            raise ValueError("production code changed during identification; retain this run and recheck frozen code")
        intact = all(policy.match(Path(p["folder"]), p["files"]) is not None for p in papers)
        if not intact:
            raise ValueError("verified document inventory changed; keep this failed check and prepare a new snapshot")
        summary = {"papers": len(papers), "files": sum(len(p["files"]) for p in papers),
            "attachment_status": dict(Counter(p["attachment_status"] for p in papers)),
            "si_status": dict(Counter(p["supporting_information_state"] for p in papers)),
            "roles": dict(Counter(f["role"] for p in papers for f in p["files"])),
            "readability": dict(Counter(f.get("readability", "unknown") for p in papers for f in p["files"])),
            "language_hints": dict(Counter(f["signals"].get("language_hint", "undetermined") for p in papers for f in p["files"])),
            "single_document_rule": sum(f.get("role_origin") == "single_document_rule" for p in papers for f in p["files"]),
            "elapsed_seconds": round(time.monotonic()-started, 3), "source_hashes_unchanged": intact,
            "registered_runs_unchanged": True, "model_calls": 0, "neo4j_accessed": False,
            "scientific_accuracy": "not_assessed", "reading_scope": "bounded_attachment_identification_only"}
        write_new_json(output / "recognition-results.json", {"summary": summary, "papers": papers})
        write_new_json(output / "summary.json", summary)
        lines = ["文献附件自动识别检查", "", "本次仅抽样识别正文/SI，不是全文阅读或科学抽取。", "",
                 json.dumps(summary, ensure_ascii=False, indent=2), "", "逐篇结果："]
        for paper in sorted(papers, key=lambda p: (p["folder_number"] or 0, p["folder_name"])):
            lines.append(f"{paper['folder_name']} | {paper['attachment_status']} | SI: {paper['supporting_information_state']}")
            for file in paper["files"]:
                lines.append(f"  {file['name']} | {file['role']} | {file['status']} | {file.get('role_origin', 'sample_classifier')}")
                if paper["attachment_status"] != "ready":
                    lines.extend("    " + reason for reason in file["reasons"])
        (output / "recognition-report.txt").write_text("\n".join(lines)+"\n", encoding="utf-8")
        print(json.dumps({"output": str(output), "summary": summary}, ensure_ascii=False), flush=True)
    except BaseException as error:
        write_new_json(output / "failure.json", {"at": utc_now(), "type": type(error).__name__,
            "message": str(error), "elapsed_seconds": round(time.monotonic()-started, 3)})
        raise


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.config.resolve(strict=True), args.output)
