"""Explicit, bounded development-only re-audit; never auto-publish to Neo4j."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from watertreatment.settings import load_settings
from watertreatment.storage import DataStore, hash_file, write_new_json
from watertreatment.web_model import reaudit
from watertreatment.web_records import WebRecords
from watertreatment.reaudit_budget import reaudit_time_budget


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[2] / "config/project.local.json")
    parser.add_argument("--record-id", action="append", required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--repair-audit-run", help="reuse a verified completed audit as repair guidance; final audit stays fresh")
    parser.add_argument("--resume-staged-run", help="recover a protocol-verified staged draft and start with fresh source audit")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live or not 1 <= len(set(args.record_id)) <= 2:
        parser.error("requires explicit --live and one or two existing development record IDs")
    settings = load_settings(args.config)
    if settings.rules_version != "0.6":
        parser.error("this pilot requires explicitly configured rules_version 0.6")
    request = args.request_file.read_text(encoding="utf-8-sig").strip()
    if not request or len(request) > 8000:
        parser.error("request must contain 1-8000 characters")
    records, store = WebRecords(settings), DataStore(settings.data_root)
    identifiers = list(dict.fromkeys(args.record_id))
    if (args.repair_audit_run or args.resume_staged_run) and len(identifiers) != 1:
        parser.error("recovery requires exactly one record")
    if args.repair_audit_run and args.resume_staged_run:
        parser.error("choose one recovery mechanism; staged drafts require fresh audit")
    for identifier in identifiers:
        detail = records.get(identifier)
        bundle = json.loads(Path(detail["sources_bundle"]).read_text(encoding="utf-8"))
        if not detail["summary"]["current"] or not store.has_development_exposure(
                bundle["paper_id"], [doc["source"]["sha256"] for doc in bundle["documents"]]):
            parser.error("pilot accepts only current records with recorded development exposure")
    time_budget = reaudit_time_budget(settings.codex.timeout_seconds)
    pilot = store.new_run("quantitative_reaudit_pilot", {
        "record_ids": identifiers, "request_sha256": hash_file(args.request_file),
        "rules_version": settings.rules_version, "model": settings.codex.model,
        "effort": settings.codex.reasoning_effort, "independent_validation": False,
        "human_validated": False, "graph_published": False,
        "max_papers": 2, "max_calls_per_paper": time_budget["max_model_turns"],
        "max_revisions_per_paper": time_budget["max_revisions"], "reaudit_time_budget": time_budget,
        "repair_audit_run_id": args.repair_audit_run, "resume_staged_run_id": args.resume_staged_run})
    shutil.copyfile(args.request_file, pilot / "request.txt")
    print(json.dumps({"pilot": str(pilot)}, ensure_ascii=False), flush=True)
    results = []
    def progress(message, **details):
        print(json.dumps({"stage": message, **details}, ensure_ascii=False), flush=True)
    for identifier in identifiers:
        try:
            result = reaudit(settings, records, identifier, request, progress=progress,
                            repair_audit_run_id=args.repair_audit_run, resume_staged_run_id=args.resume_staged_run)
        except Exception as error:
            result = {"incomplete": True, "error": f"{type(error).__name__}: {error}"}
        results.append({"record_id": identifier, "result": result})
        write_new_json(pilot / f"paper-{len(results):02d}.json", results[-1])
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        if result.get("incomplete") or result.get("revision", {}).get("accepted") is False:
            break
    write_new_json(pilot / "result.json", {"papers": results, "independent_validation": False,
        "human_validated": False, "graph_published": False})
    return int(any(item["result"].get("incomplete") or item["result"].get("revision", {}).get("accepted") is False for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
