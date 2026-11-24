"""Explicit 1-3 paper DEVELOPMENT pilot. No directory-wide batch discovery.

Usage: python scripts/quality/run_reading_pilot.py --live --bundle SOURCES_JSON ...
Each run freezes current inputs and settings. Failed runs remain inspectable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from watertreatment.app_server import AppServerClient
from watertreatment.settings import load_settings
from watertreatment.storage import DataStore, write_new_json
from watertreatment.workflow import execute_plan, prepare

COMPLETED_STATUSES = {"candidate_model_audited", "candidate_audited_with_limitations"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[2] / "config/project.local.json")
    parser.add_argument("--bundle", action="append", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reuse-dossier", type=Path, help="resume one development paper from completed comprehension notes")
    parser.add_argument("--reuse-candidate", type=Path, help="resume a completed versioned candidate; requires --reuse-dossier")
    args = parser.parse_args()
    if not args.live or not 1 <= len(args.bundle) <= 3:
        parser.error("requires explicit --live and 1-3 development bundles")
    if (args.reuse_dossier or args.reuse_candidate) and len(args.bundle) != 1:
        parser.error("resumption requires exactly one bundle")
    if args.reuse_candidate and not args.reuse_dossier:
        parser.error("--reuse-candidate requires --reuse-dossier")
    settings = load_settings(args.config)
    if not settings.reading.enabled:
        parser.error("enable the active reading workflow first")
    # Existing development exposure is required: reserved validation is never
    # silently converted to development by this convenience runner.
    store = DataStore(settings.data_root)
    for path in args.bundle:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if not store.has_development_exposure(bundle["paper_id"], [d["source"]["sha256"] for d in bundle["documents"]]):
            parser.error("pilot accepts only papers already registered as development")
    pilot = store.new_run("development_reading_pilot", {"source_bundles": [str(p.resolve()) for p in args.bundle],
                                                        "human_validated": False, "model": settings.codex.model})
    print(json.dumps({"pilot": str(pilot)}, ensure_ascii=False), flush=True)
    results = []
    for path in args.bundle:
        run = prepare(settings, path, cohort_role="development", reuse_dossier=args.reuse_dossier, reuse_candidate=args.reuse_candidate)
        print(json.dumps({"paper_run": str(run), "status": "started"}, ensure_ascii=False), flush=True)
        client = AppServerClient(settings.codex, live=True, total_timeout_seconds=settings.reading.total_timeout_seconds)
        original = run
        try:
            try:
                execute_plan(settings, run / "plan.json", client=client)
            except ValueError as error:
                from watertreatment.retained_review import complete_failed_revision
                def report(message, **details):
                    print(json.dumps({"message": message, **details}, ensure_ascii=False), flush=True)
                run = complete_failed_revision(settings, run, error, progress=report)
            result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        except Exception as error:
            result = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        results.append({"run": str(run), "result": result})
        if run != original:
            results[-1]["parent_run"] = str(original)
        write_new_json(pilot / f"paper-{len(results):02d}.json", results[-1])
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        if result["status"] not in COMPLETED_STATUSES:
            break  # Keep bounded/incomplete work visible before spending more calls.
    write_new_json(pilot / "result.json", {"papers": results, "independent_validation": False,
        "human_validated": False, "planned_paper_count": len(args.bundle), "attempted_paper_count": len(results),
        "completed_paper_count": sum(r["result"]["status"] in COMPLETED_STATUSES for r in results)})
    return 1 if any(r["result"]["status"] not in COMPLETED_STATUSES for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
