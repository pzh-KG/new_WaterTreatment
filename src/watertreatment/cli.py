"""Small explicit CLI. Default operations are local and do not call models."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from .codex import CodexClient, ModelCallError, strict_schema
from .settings import load_settings
from .storage import DataStore, hash_file, write_new_json
from .workflow import discover, execute_plan, import_paper, prepare, runtime_versions


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="wt", description="Evidence-grounded evaporation/catalysis literature workspace")
    result.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[2] / "config/project.local.json")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the isolated data store")
    commands.add_parser("doctor", help="check local setup without model or database calls")
    database_check = commands.add_parser("neo4j-check", help="check the explicitly configured business database; no graph writes")
    database_check.add_argument("--live", action="store_true", help="allow this read-only database connection")
    model_check = commands.add_parser("codex-check", help="one synthetic text/image/JSON model call; no scientific records")
    model_check.add_argument("--live", action="store_true")
    listing = commands.add_parser("discover", help="list paper folders; does not read scientific content")
    listing.add_argument("--limit", type=int, default=20, help="display limit; 0 displays all")
    ingest = commands.add_parser("import", help="snapshot and parse one paper folder")
    ingest.add_argument("folder", type=Path)
    ingest.add_argument("--paper-id")
    ingest.add_argument("--roles", type=Path, help="JSON mapping filenames to main_article/supporting_information")
    ingest.add_argument("--families", type=Path, help="JSON mapping explicitly related derivatives to source-family IDs")
    ingest.add_argument("--render", action="store_true", help="render all PDF pages locally for subsequent visual reading")
    plan = commands.add_parser("prepare", help="prepare all source inputs and prompts; no model call")
    plan.add_argument("bundle", type=Path, help="sources.json from an import run")
    plan.add_argument("--cohort-role", choices=["development", "heldout", "unassigned"], default="unassigned")
    plan.add_argument("--model", help="freeze the intended model into the plan without making a call")
    plan.add_argument("--reuse-dossier", type=Path, help="reuse an intact Sol MAX comprehension dossier for identical non-heldout sources")
    candidate_source = plan.add_mutually_exclusive_group()
    candidate_source.add_argument("--reuse-candidate", type=Path, help="resume a completed non-heldout candidate with verified lineage; requires --reuse-dossier")
    candidate_source.add_argument("--reuse-staged-candidate", type=Path, help="authenticate a failed revision checkpoint and inherit its completed full baseline; requires --reuse-dossier")
    candidate_source.add_argument("--reuse-completed-continuation-candidate", type=Path,
        help="authenticate a completed targeted repair awaiting its first source audit; requires --reuse-dossier, no repeated pre-repair")
    candidate_source.add_argument("--reuse-identity-corrected-run", type=Path,
        help="reuse completed raw reading/extraction outputs with explicit source-bound system ID correction; no inherited audit or reset budget")
    plan.add_argument("--reuse-source-audit", action="store_true", help="inherit a completed full audit for a bounded recheck; automatic for --reuse-staged-candidate")
    plan.add_argument("--continuation-repair", action="append", choices=["shared_references", "missingness"], default=[],
                      help="freeze a targeted repair before rechecking the staged candidate; repeat for both options")
    run = commands.add_parser("extract", help="execute an immutable plan with explicitly enabled Codex calls")
    run.add_argument("plan", type=Path)
    run.add_argument("--live", action="store_true")
    run.add_argument("--model", help="explicit model identifier; no automatic model substitution")
    run.add_argument("--no-model-review", action="store_true")
    addendum = commands.add_parser("prepare-audit-addendum",
        help="freeze a small source-disposition supplement to a completed but incomplete audit; no model call")
    addendum.add_argument("parent_run", type=Path)
    addendum.add_argument("--timeout-seconds", type=int, default=1200,
        help="upper bound within the parent's remaining budget; never extends that budget")
    finish_addendum = commands.add_parser("complete-audit-addendum",
        help="execute one source-bound supplement without editing or re-extracting the candidate")
    finish_addendum.add_argument("plan", type=Path)
    finish_addendum.add_argument("--live", action="store_true")
    context_revalidation = commands.add_parser("revalidate-coverage-context",
        help="Locally revalidate a completed coverage report with authenticated equation parent quotes; no model/graph calls")
    context_revalidation.add_argument("parent_run", type=Path)
    coverage_plan = commands.add_parser("prepare-coverage-correction",
        help="freeze a source-bound reading-ledger correction; preserve the completed scientific candidate")
    coverage_plan.add_argument("parent_run", type=Path)
    coverage_plan.add_argument("--timeout-seconds", type=int, default=900)
    coverage_run = commands.add_parser("complete-coverage-correction",
        help="complete one bounded reading-ledger correction without re-extraction or graph writes")
    coverage_run.add_argument("plan", type=Path)
    coverage_run.add_argument("--live", action="store_true")
    partial_plan = commands.add_parser("prepare-partial-audit-completion",
        help="freeze only the missing source deliveries and report dispositions of a completed partial audit")
    partial_plan.add_argument("parent_run", type=Path)
    partial_plan.add_argument("--timeout-seconds", type=int, default=600)
    partial_run = commands.add_parser("complete-partial-audit",
        help="complete one source-bound supplement within the parent's remaining budget")
    partial_run.add_argument("plan", type=Path)
    partial_run.add_argument("--live", action="store_true")
    retained_plan = commands.add_parser("prepare-retained-review", help="freeze a source-bound repair proposal for the unresolved portion of a delivered review")
    retained_plan.add_argument("parent_run", type=Path)
    retained_plan.add_argument("proposal", type=Path)
    retained_plan.add_argument("--seconds", type=int, default=900)
    retained_plan.add_argument("--authorized-extra-seconds", type=int, default=0,
        help="explicit one-off authority for one additional local turn, bounded to 900 seconds")
    retained_plan.add_argument("--authorized-extra-call", action="store_true",
        help="authorize one extra turn within the remaining time, without extending the paper's time limit")
    retained_run = commands.add_parser("complete-retained-review", help="execute the prepared issues-only independent review")
    retained_run.add_argument("run", type=Path)
    retained_run.add_argument("--live", action="store_true")
    retained_finish = commands.add_parser("finalize-retained-review", help="replay the already delivered native response without a model call")
    retained_finish.add_argument("run", type=Path)
    directive = commands.add_parser("apply-reviewer-text", help="apply one exact, authenticated textual remedy from a completed source review, with zero model calls")
    directive.add_argument("parent_run", type=Path)
    validate = commands.add_parser("validate", help="check record schema, references and scientific consistency")
    validate.add_argument("record", type=Path)
    validate.add_argument("--bundle", type=Path)
    projection = commands.add_parser("project", help="export a graph projection locally; never connects to Neo4j")
    projection.add_argument("record", type=Path)
    projection.add_argument("--bundle", type=Path)
    graph_load = commands.add_parser("graph-load", help="load a registered validated candidate snapshot into the configured database")
    graph_load.add_argument("record", type=Path)
    graph_load.add_argument("--bundle", type=Path)
    graph_load.add_argument("--live", action="store_true")
    graph_load.add_argument("--rollback", action="store_true", help="test data writes and roll back; no schema changes")
    commands.add_parser("schema", help="print the strict extraction JSON schema")
    commands.add_parser("runs", help="list registered runs")
    web = commands.add_parser("web", help="start the local literature workbench; no model runs at startup")
    web.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    return result


def main(argv: list[str] | None = None) -> int:
    # Windows subprocess pipes otherwise inherit a legacy ANSI code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "schema":
            from .domain import ExtractionRecord
            _print(strict_schema(ExtractionRecord.model_json_schema()))
            return 0
        settings = load_settings(args.config)
        store = DataStore(settings.data_root)
        if args.command == "web":
            if not 1024 <= args.port <= 65535:
                raise ValueError("web port must be between 1024 and 65535")
            import uvicorn
            from .web_api import create_app
            uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
        elif args.command == "init":
            store.initialize()
            _print({"status": "initialized", "data_root": settings.data_root, "model_calls": 0})
        elif args.command == "doctor":
            from .neo4j_connection import public_status
            _print({"project_root": settings.project_root, "data_root": settings.data_root, "store_initialized": (settings.data_root / "store.json").is_file(), "runtime": runtime_versions(), "codex_executable": settings.codex.executable or shutil.which("codex"), "model_configured": settings.codex.model, "reasoning_effort": settings.codex.reasoning_effort, "source_roots": [{"path": p, "available": p.is_dir()} for p in settings.source_roots], "taxonomy_available": settings.taxonomy_path.is_file(), "vision_available": importlib.util.find_spec("pypdfium2") is not None, "model_calls": 0, "neo4j": public_status(settings.neo4j)})
        elif args.command == "neo4j-check":
            from .neo4j_connection import check_connection
            if not args.live:
                raise ValueError("no database connection attempted; specify --live for the read-only Neo4j check")
            _print(check_connection(settings.neo4j))
        elif args.command == "codex-check":
            from .model_check import check_model
            path = check_model(settings, live=args.live)
            _print({"run": path, "status": "synthetic_check_passed", "model": settings.codex.model,
                    "reasoning_effort": settings.codex.reasoning_effort, "scientific_accuracy": "not_established"})
        elif args.command == "discover":
            if args.limit < 0:
                raise ValueError("limit must be nonnegative")
            entries = discover(settings)
            _print({"total_folders": len(entries), "displayed": entries[:args.limit] if args.limit else entries, "scientific_screening": "not_performed"})
        elif args.command == "import":
            roles = json.loads(args.roles.read_text(encoding="utf-8-sig")) if args.roles else None
            families = json.loads(args.families.read_text(encoding="utf-8-sig")) if args.families else None
            path = import_paper(settings, args.folder, paper_id=args.paper_id, render=args.render, source_roles=roles, source_families=families)
            _print({"run": path, "source_bundle": path / "sources.json", "model_calls": 0})
        elif args.command == "prepare":
            if args.model:
                settings.codex.model = args.model
            path = prepare(settings, args.bundle, cohort_role=args.cohort_role, reuse_dossier=args.reuse_dossier,
                           reuse_candidate=args.reuse_candidate, reuse_source_audit=args.reuse_source_audit,
                           reuse_staged_candidate=args.reuse_staged_candidate, continuation_repairs=args.continuation_repair,
                           reuse_completed_continuation_candidate=args.reuse_completed_continuation_candidate,
                           reuse_identity_corrected_run=args.reuse_identity_corrected_run)
            _print({"run": path, "plan": path / "plan.json", "model_calls": 0})
        elif args.command == "prepare-retained-review":
            from .retained_review import prepare as prepare_retained_review
            _print({"run": prepare_retained_review(settings, args.parent_run, args.proposal, seconds=args.seconds,
                extra_seconds=args.authorized_extra_seconds, extra_calls=int(args.authorized_extra_call))})
        elif args.command == "complete-retained-review":
            from .retained_review import execute
            _print(execute(settings, args.run, live=args.live))
        elif args.command == "finalize-retained-review":
            from .retained_review import finalize
            _print(finalize(settings, args.run))
        elif args.command == "apply-reviewer-text":
            from .reviewer_text import apply as apply_reviewer_text
            _print({"run":apply_reviewer_text(settings, args.parent_run), "model_calls":0})
        elif args.command == "prepare-audit-addendum":
            from .audit_addendum_workflow import prepare_audit_addendum
            path = prepare_audit_addendum(settings, args.parent_run, timeout_seconds=args.timeout_seconds)
            _print({"plan": path, "model_calls": 0})
        elif args.command == "complete-audit-addendum":
            from .app_server import AppServerClient
            from .audit_addendum_workflow import execute_audit_addendum
            if not args.live:
                raise ValueError("no model invoked; specify --live to execute the frozen addendum plan")
            frozen = json.loads(args.plan.read_text(encoding="utf-8"))
            from .settings import CodexSettings
            config = CodexSettings.model_validate(frozen["model_settings"])
            client = AppServerClient(config, live=True,
                total_timeout_seconds=frozen["reading_settings"]["total_timeout_seconds"])
            path = execute_audit_addendum(settings, args.plan, client=client)
            outcome = json.loads((path / "result.json").read_text(encoding="utf-8"))
            _print({"run": path, "status": outcome["status"], "human_validated": False})
        elif args.command == "revalidate-coverage-context":
            from .coverage_context_revalidation import revalidate_context
            path = revalidate_context(settings, args.parent_run)
            _print({"run": path, "status": "candidate_audited_with_limitations",
                    "model_calls": 0, "human_validated": False, "graph_published": False})
        elif args.command == "prepare-coverage-correction":
            from .coverage_correction_workflow import prepare as prepare_coverage
            path = prepare_coverage(settings, args.parent_run, timeout_seconds=args.timeout_seconds)
            _print({"plan": path, "model_calls": 0, "graph_published": False})
        elif args.command == "complete-coverage-correction":
            from .app_server import AppServerClient
            from .coverage_correction_workflow import execute as execute_coverage
            from .settings import CodexSettings
            if not args.live:
                raise ValueError("no model invoked; specify --live to execute the frozen coverage correction")
            frozen = json.loads(args.plan.read_text(encoding="utf-8"))
            config = CodexSettings.model_validate(frozen["model_settings"])
            client = AppServerClient(config, live=True,
                total_timeout_seconds=frozen["reading_settings"]["total_timeout_seconds"])
            path = execute_coverage(settings, args.plan, client=client)
            outcome = json.loads((path / "result.json").read_text(encoding="utf-8"))
            _print({"run": path, "status": outcome["status"], "human_validated": False,
                    "graph_published": False})
        elif args.command == "prepare-partial-audit-completion":
            from .partial_audit_completion_workflow import prepare as prepare_partial
            path = prepare_partial(settings, args.parent_run, timeout_seconds=args.timeout_seconds)
            _print({"plan": path, "model_calls": 0, "graph_published": False})
        elif args.command == "complete-partial-audit":
            from .app_server import AppServerClient
            from .partial_audit_completion_workflow import execute as execute_partial
            from .settings import CodexSettings
            if not args.live:
                raise ValueError("no model invoked; specify --live to execute the frozen partial audit completion")
            frozen = json.loads(args.plan.read_text(encoding="utf-8"))
            config = CodexSettings.model_validate(frozen["model_settings"])
            client = AppServerClient(config, live=True,
                total_timeout_seconds=frozen["reading_settings"]["total_timeout_seconds"])
            path = execute_partial(settings, args.plan, client=client)
            outcome = json.loads((path / "result.json").read_text(encoding="utf-8"))
            _print({"run": path, "status": outcome["status"], "human_validated": False,
                    "graph_published": False})
        elif args.command == "extract":
            if not args.live:
                raise ValueError("no model invoked; specify --live and an explicit --model to execute")
            config = settings.codex.model_copy(update={"model": args.model or settings.codex.model})
            frozen = json.loads(args.plan.read_text(encoding="utf-8"))
            if frozen.get("workflow") == "active_reading":
                from .app_server import AppServerClient
                client = AppServerClient(config, live=True, total_timeout_seconds=settings.reading.total_timeout_seconds)
            else:
                client = CodexClient(config, live=True)
            path = execute_plan(settings, args.plan, client=client, verify=not args.no_model_review)
            result = json.loads((path / "result.json").read_text(encoding="utf-8"))
            _print({"run": path, "status": result["status"], "human_validated": False})
        elif args.command in {"validate", "project", "graph-load"}:
            if args.command == "graph-load" and not args.live:
                raise ValueError("no database connection attempted; specify --live for graph loading")
            from .domain import ExtractionRecord
            from .validation import validate_record
            from .workflow import _read_bundle, validate_plan_metadata
            record_path = args.record.resolve(strict=True)
            record = ExtractionRecord.model_validate_json(record_path.read_bytes())
            documents = []
            plan = None
            run = record_path.parent
            registered = (store.last_event(run.name, "prepared")
                          if record_path.name.casefold() == "candidate.json" and run.parent == settings.data_root / "runs"
                          else None)
            if registered is not None:
                plan_path = run / "plan.json"
                if not plan_path.is_file() or hash_file(plan_path) != registered.get("plan_sha256"):
                    raise ValueError("registered candidate plan is missing or changed")
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                if plan.get("run_id") != run.name:
                    raise ValueError("registered candidate plan belongs to another run")
                bundle_path = (run / "sources.json").resolve(strict=True)
                if args.bundle and args.bundle.resolve(strict=True) != bundle_path:
                    raise ValueError("registered candidate must use its own run source bundle")
                expected = [item["sha256"] for item in plan.get("input_paths", [])
                            if Path(item["path"]).resolve() == bundle_path]
                if len(expected) != 1 or hash_file(bundle_path) != expected[0]:
                    raise ValueError("registered candidate source bundle is unregistered or changed")
                _, documents = _read_bundle(bundle_path)
            elif args.bundle:
                _, documents = _read_bundle(args.bundle.resolve(strict=True))
            if args.command == "graph-load" and plan is None:
                raise ValueError("graph loading requires a registered candidate.json and its frozen source plan")
            issues = validate_record(record, source_documents=documents)
            if plan is not None:
                issues.extend(validate_plan_metadata(record, plan, documents, store=store))
            errors = [issue for issue in issues if issue.severity == "error"]
            if args.command == "validate":
                _print({"schema_valid": True, "issues": [i.model_dump(mode="json") for i in issues], "scientific_accuracy": "not_established"})
                return 1 if errors else 0
            if errors:
                _print({"status": "projection_blocked", "issues": [i.model_dump(mode="json") for i in errors]})
                return 1
            from .projection import project_record, render_cypher
            graph = project_record(record, source_documents=documents)
            with store.connection():
                pass
            directory = settings.data_root / "exports" / ("graph-" + uuid.uuid4().hex[:12])
            directory.mkdir(parents=True)
            write_new_json(directory / "graph.json", graph.model_dump(mode="json"))
            (directory / "graph.cypher").write_text(render_cypher(graph), encoding="utf-8")
            if args.command == "graph-load":
                from .graph_store import load_graph
                write_new_json(directory / "record.json", record.model_dump(mode="json"))
                store.event(run.name, "graph_load_started", {"record_sha256": hash_file(directory / "record.json"), "origin_record": str(record_path), "export": str(directory), "rollback": args.rollback})
                try:
                    result = load_graph(settings.neo4j, graph, source_record=directory / "record.json", live=True, rollback=args.rollback)
                    write_new_json(directory / "load-result.json", result)
                    store.event(run.name, "graph_load_completed", result)
                except Exception:
                    store.event(run.name, "graph_load_failed", {"export": str(directory)})
                    raise
                _print({"export": directory, **result})
                return 0
            _print({"export": directory, "neo4j_connected": False, "scientific_validation": "not_established"})
        elif args.command == "runs":
            _print(store.runs())
        return 0
    except (ValueError, OSError, RuntimeError, ValidationError, ModelCallError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        return 2

