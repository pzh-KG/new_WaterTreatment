"""Apply an exact, source-bound textual replacement prescribed by one completed review."""
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

from .citation_audit import _parts, _prefix, _resolve
from .codex import code_identity
from .domain import ExtractionRecord
from .quantitative import quantitative_summary
from .reader_workflow import reading_checks
from .reading import SourceAnchor
from .retained_review import _adjudicated, _bound, _latest, _machine, _outcome, _public
from .retained_review_parent import read
from .storage import digest_json, hash_file, utc_now, write_new_bytes
from .workflow import runtime_versions


POLICY = "source-reviewer-verbatim-text-v1"
ORIGIN = "verified_source_reviewer_text_directive"
NOTE = "本版逐字采用已完成的局部原文审查给出的替换句。复核者审查的是上一稿，程序核验了本版的唯一字段差异和原始来源。"
TEXT_PATH = (r"(?:comparisons\[\d+\]\.counterevidence\[\d+\]\.value|"
             r"(?:observations|comparisons)\[\d+\]\.limitations\[\d+\])")
NUMBER = re.compile(r"(?<![\d.])[+-]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][+-]?\d+)?")
LABELED_SOURCE = re.compile(r"\b(?:tables?|tab\.?|figures?|figs?\.?)\s+S\d+\b", re.I)


def prescribed_edit(candidate, packet, report):
    """Only use a final reviewer decision, never instructions found in a source document."""
    blockers = [d for d in report.decisions if d.verdict == "requires_revision"]
    if len(blockers) != 1 or len(blockers[0].groups) != 1 or len(report.additional_issues) != 1:
        raise ValueError("Exactly one located reviewer directive is required")
    directive = report.additional_issues[0]
    prefix = re.match(rf"^Replace ({TEXT_PATH}) with ", directive)
    if not prefix:
        raise ValueError("The reviewer did not prescribe an exact permitted interpretive text replacement")
    path = prefix[1]
    literal = directive[prefix.end():]
    replacement, end = json.JSONDecoder().raw_decode(literal)
    tail = literal[end:]
    if (not isinstance(replacement, str) or not 10 <= len(replacement) <= 2000
            or any(ord(c) < 32 for c in replacement)
            or not re.fullmatch(r"\.? (?:Use|See|Cite) [^.!?\n]{1,350}\.", tail)):
        raise ValueError("The replacement or its source instruction is ambiguous")
    if re.search(r"\b(?:replace|change|correct|update|remove|add|also|must|unless|if)\b|\[\d+\]", tail, re.I):
        raise ValueError("A second or conditional instruction needs independent review")
    group_id = blockers[0].groups[0]
    group = next((f for f in packet["findings"] if f["id"] == group_id), {})
    paths = group.get("paths", [])
    parts = _parts(path)
    if not parts or len(paths) != 1 or not _parts(paths[0]) or not _prefix(_parts(paths[0]), parts):
        raise ValueError("The exact field is outside the blocking source disposition")
    data = candidate.model_dump(mode="json")
    old = _resolve(data, parts)
    if not isinstance(old, str) or not old.strip() or old == replacement:
        raise ValueError("The addressed field is not an existing, changed text claim")
    card_ids = list(dict.fromkeys(re.findall(r"\bS\d+\b", LABELED_SOURCE.sub("", tail))))
    cards = {c["id"]: c for c in packet["sources"]}
    if not card_ids or not set(card_ids) <= cards.keys() or not set(card_ids).intersection(blockers[0].source_ids):
        raise ValueError("The directive needs explicit packet sources including the blocking review's evidence")
    def numbers(text):
        return {Decimal(x) for x in NUMBER.findall(text.replace("−", "-"))}
    if not numbers(replacement) <= numbers(old):
        raise ValueError("A local text correction cannot introduce a new magnitude")
    percentages = re.findall(r"([+−-]?(?:\d*\.)?\d+)\s*%", replacement)
    source_numbers = set().union(*(numbers(cards[k]["text"]) for k in card_ids))
    if not {Decimal(x.replace("−", "-")) for x in percentages} <= source_numbers:
        raise ValueError("A prescribed percentage lacks direct text in its cited source cards")
    _resolve(data, parts[:-1])[parts[-1]] = replacement
    corrected = ExtractionRecord.model_validate(data)
    replay = corrected.model_dump(mode="json")
    _resolve(replay, parts[:-1])[parts[-1]] = old
    if replay != candidate.model_dump(mode="json"):
        raise ValueError("Applying the literal caused an additional serialized change")
    return corrected, {"field":path, "previous_text":old, "replacement":replacement, "native_directive":directive,
        "group":group_id, "owner_path":paths[0], "native_blocker":blockers[0].explanation,
        "source_cards":[{"id":k, "source_id":cards[k]["source"], "block_id":cards[k]["block"]} for k in card_ids]}


@dataclass
class Applied:
    records: object
    run: Path
    plan: dict
    record: ExtractionRecord
    audit: object
    dossier: object
    proof: dict


def authenticate(settings, parent_run):
    run = Path(parent_run).resolve(strict=True)
    records, ancestor, candidate, plan, packet = _bound(settings, run)
    report, native = _public(records, run, plan, packet)
    terminal, dossier, audit = _outcome(run, ancestor, candidate, packet, report, native)
    candidate = _adjudicated(ancestor, candidate, packet, report)
    event = records.store.last_event(run.name, "candidate_completed") or {}
    if (terminal != read(run/"result.json") or terminal["stop_reason"] != "local_review_unresolved"
            or event.get("result_sha256") != hash_file(run/"result.json")
            or audit.model_dump(mode="json") != read(run/"model_review.json")
            or dossier.model_dump(mode="json") != read(run/"reading_dossier.corrected.v01.json")
            or candidate.model_dump(mode="json") != read(run/"candidate.json")):
        raise ValueError("The retained candidate, findings or registered result changed")
    corrected, edit = prescribed_edit(candidate, packet, report)
    blockers = [f for f in audit.findings if f.needs_revision or f.verdict in {"unsupported","not_checked"}]
    if (len(blockers) != 1 or blockers[0].record_paths != [edit["owner_path"]]
            or blockers[0].explanation != edit["native_blocker"] or audit.potential_omissions != [edit["native_directive"]]
            or terminal["reading_check_issues"] != [edit["native_directive"], edit["native_blocker"]]):
        raise ValueError("The signed result still contains obligations beyond this exact directive")
    proof = {"policy":POLICY, "parent_run":run.name, "parent_result_sha256":hash_file(run/"result.json"),
        "parent_candidate_sha256":hash_file(run/"candidate.json"), "native_review":native,
        "candidate_content_hash":digest_json(corrected.model_dump(mode="json")), "edit":edit}
    # Keep every independent finding. The controller records the precise
    # unresolved scientific limitation in the replacement, with its own origin.
    f = blockers[0]
    f.record_path, f.record_paths = edit["field"], [edit["field"]]
    f.needs_revision, f.severity, f.verdict = False, "minor", "unresolved"
    f.explanation = NOTE + " " + edit["replacement"]
    seen = {(a.source_id, a.block_id) for a in f.evidence}
    for card in edit["source_cards"]:
        key = card["source_id"], card["block_id"]
        if key not in seen:
            f.evidence.append(SourceAnchor(source_id=key[0], block_id=key[1]))
            seen.add(key)
    audit.potential_omissions = []
    audit.coverage_limitations = list(dict.fromkeys([*audit.coverage_limitations, NOTE, edit["replacement"]]))
    audit.review_scope_sha256 = digest_json(proof)
    _machine(settings, ancestor, corrected)
    if reading_checks(dossier, corrected, audit, ancestor.documents):
        raise ValueError("The corrected text failed reading/evidence consistency checks")
    return Applied(records, run, plan, corrected, audit, dossier, proof)


def _result(run, applied):
    return {"status":"candidate_audited_with_limitations", "stop_reason":"audit_completed",
        "review_mode":POLICY, "model_calls":0, "reading_tool_calls":0, "human_validated":False, "graph_published":False,
        "scientific_accuracy":"not_established", "dataset_role":applied.plan["cohort_role"],
        "model_rereviewed_corrected_json":False, "interpretation":NOTE, "reading_check_issues":[],
        "validation_error_count":0, "quantitative_coverage_summary":quantitative_summary(applied.record),
        "reviewer_directive_proof":applied.proof, "history":[{"candidate_sha256":hash_file(run/"candidate.json"),
            "version":1, "audit_origin":ORIGIN, "validation_errors":0, "needs_revision":False, "reading_check_issues":[]}]}


def _freeze(path, value):
    content = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"Retained artifact differs from the verified content: {path.name}")
    else:
        write_new_bytes(path, content)


def _event_once(db, run, name, payload):
    row = db.execute("SELECT payload FROM run_events WHERE run_id=? AND event=? ORDER BY id DESC LIMIT 1",
                     (run.name, name)).fetchone()
    if row is None:
        db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES(?,?,?,?)",
                   (run.name, name, json.dumps(payload), utc_now()))
    elif json.loads(row[0]) != payload:
        raise ValueError(f"The registered {name} receipt has changed")


def apply(settings, parent_run, *, expected_proof=None):
    a = authenticate(settings, parent_run)
    if expected_proof is not None and expected_proof != digest_json(a.proof):
        raise ValueError("The queued reviewer remedy or its evidence changed")
    store = a.records.store
    fields = {k:a.plan[k] for k in ("paper_id","cohort_role","rule_version","schema_version","extraction_policy")}
    identity = {**fields, "operation":POLICY, "workflow":POLICY, "parent_run":a.run.name}
    # Serialize the claim separately. A worker interrupted after claiming the run
    # can be retried against the same immutable directory and source receipt.
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM runs WHERE kind='extraction'")]
        siblings = []
        for row in rows:
            manifest = read(row["manifest_path"])
            if manifest.get("operation") == POLICY and manifest.get("parent_run") == a.run.name:
                if any(manifest.get(k) != v for k, v in identity.items()):
                    raise ValueError("The registered derivative has a different research identity")
                siblings.append(Path(row["manifest_path"]).parent)
        if len(siblings) > 1:
            raise ValueError("Multiple derivatives claim the same verified instruction")
        if siblings:
            run = siblings[0]
        else:
            _latest(settings, a, rows=rows)
            run = store.new_run_in_transaction(db, "extraction", identity)
    if store.last_event(run.name, "candidate_completed"):
        if not verify(settings, run, a.record, a.audit):
            raise ValueError("The registered derivative no longer matches its native review")
        return run
    files = {"sources.json":(a.run/"sources.json").read_bytes(),
             "reading_dossier.json":(a.run/"reading_dossier.corrected.v01.json").read_bytes(),
             "candidate.json":a.record.model_dump(mode="json"), "model_review.json":a.audit.model_dump(mode="json"),
             "reviewer_directive.json":a.proof}
    runtime, code = runtime_versions(), code_identity(settings.project_root)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.row_factory = sqlite3.Row
        _latest(settings, a, exclude=run.name, rows=[dict(row) for row in db.execute("SELECT * FROM runs")])
        for name, value in files.items():
            _freeze(run/name, value)
        plan = {**fields, "version":1, "run_id":run.name, "workflow":POLICY, "parent_run":a.run.name,
            "model_settings":a.plan["model_settings"], "model_execution":"none_inherited_source_review",
            "max_calls":0, "reading_settings":{"enabled":False,"max_tool_calls":0}, "workdir":str(run),
            "runtime":runtime, "code_identity":code,
            "manifest_sha256":hash_file(run/"manifest.json"),
            "input_paths":[{"path":str(run/name),"sha256":hash_file(run/name)} for name in files]}
        _freeze(run/"plan.json", plan)
        _event_once(db, run, "prepared", {"plan_sha256":hash_file(run/"plan.json")})
        _freeze(run/"result.json", _result(run,a))
        _event_once(db, run, "candidate_completed", {"status":"candidate_audited_with_limitations","model_calls":0,
                    "result_sha256":hash_file(run/"result.json")})
    return run


def verify(settings, run, record, audit):
    from .web_records import WebRecords
    try:
        w = WebRecords(settings)
        run = w._safe_data_path(run)
        plan, _, _, _ = w._source(run.name)
        manifest = read(run/"manifest.json")
        expected_inputs = {str(run/name) for name in ("sources.json", "reading_dossier.json", "candidate.json",
                                                      "model_review.json", "reviewer_directive.json")}
        if (run.parent != (settings.data_root/"runs").resolve() or plan["workflow"] != POLICY
                or manifest.get("operation") != POLICY or manifest.get("workflow") != POLICY
                or manifest.get("parent_run") != plan["parent_run"] or manifest["kind"] != "extraction"
                or manifest["run_id"] != run.name or plan.get("workdir") != str(run)
                or len(plan["input_paths"]) != len(expected_inputs)
                or {item["path"] for item in plan["input_paths"]} != expected_inputs
                or plan["manifest_sha256"] != hash_file(run/"manifest.json") or plan["max_calls"] != 0
                or plan["model_execution"] != "none_inherited_source_review" or (run/"calls").exists()):
            return False
        parent_run = w._safe_data_path(settings.data_root/"runs"/plan["parent_run"])
        if parent_run.parent != run.parent or read(parent_run/"manifest.json")["created_at"] >= manifest["created_at"]:
            return False
        a = authenticate(settings, parent_run)
        event = w.store.last_event(run.name,"candidate_completed") or {}
        return (event.get("result_sha256") == hash_file(run/"result.json") and event.get("model_calls") == 0
            and all(manifest[k] == plan[k] == a.plan[k] for k in ("paper_id","cohort_role","rule_version","schema_version","extraction_policy"))
            and plan["model_settings"] == a.plan["model_settings"]
            and hash_file(run/"sources.json") == hash_file(a.run/"sources.json")
            and a.proof == read(run/"reviewer_directive.json") and a.record == record and a.audit == audit
            and read(run/"candidate.json") == a.record.model_dump(mode="json") and read(run/"model_review.json") == a.audit.model_dump(mode="json")
            and read(run/"reading_dossier.json") == a.dossier.model_dump(mode="json")
            and read(run/"result.json") == _result(run,a))
    except (ValueError, KeyError, IndexError, TypeError, OSError, AttributeError):
        return False
