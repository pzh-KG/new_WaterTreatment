"""Replay source-tool delivery for a bounded review, never scientific validation."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .bounded_revision_audit import POLICY as BOUNDED_POLICY
from .scientific_revision_audit import POLICY as SCIENTIFIC_POLICY
from .audit_addendum import POLICY as ADDENDUM_POLICY
from .coverage_correction import POLICY as COVERAGE_POLICY
from .domain import EvidenceLocator
from .sources import SourceDocument
from .storage import digest_json, hash_file
from .word_review_links import LINKED_POLICY, AMENDMENT as LINKED_AMENDMENT, linked_structure


POLICY = "bounded-source-delivery-v1"
PROGRESS_POLICY = "bounded-source-delivery-progress-v1"
UNION_POLICY = "bounded-source-delivery-union-v1"
PARTIAL_COMPLETION_POLICY = "bounded-partial-audit-completion-v1"
NATIVE_AMENDMENT_POLICY = "native-word-source-requirement-amendment-v1"
NATIVE_REQUIREMENT_POLICY = "native-word-structure-v1"
SOURCE_TOOLS = {"wt_read_blocks", "wt_page_text", "wt_view"}
REVIEW_POLICIES = frozenset({BOUNDED_POLICY, SCIENTIFIC_POLICY, ADDENDUM_POLICY, COVERAGE_POLICY,
                           PARTIAL_COMPLETION_POLICY})


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class _ReviewSources:
    """One immutable-source verification pass. No cache survives a proof replay."""
    def __init__(self, run):
        self.run = Path(run).resolve(strict=True)
        bundle = _json(self.run / "sources.json")
        plan_path = self.run / "plan.json"
        plan = _json(plan_path) if plan_path.is_file() else {}
        self.compiler_version = plan.get("scientific_scope_limits", {}).get("compiler_version")
        if plan.get("review_visual_recovery"):
            from .review_visuals import review_visuals
            bundle, _, _ = review_visuals(self.run, bundle, create=False, proof=plan["review_visual_recovery"])
        self.documents = {}
        self.blocks = {}
        self.verified = set()
        self.tables = {}
        self.registered = {}
        for raw in bundle["documents"]:
            doc = SourceDocument.model_validate(raw)
            sid = doc.source.source_id
            blocks = {b.block_id: b for b in doc.blocks}
            if sid in self.documents or len(blocks) != len(doc.blocks):
                raise ValueError("source review bundle contains duplicate source or block IDs")
            self.documents[sid], self.blocks[sid] = doc, blocks
        for rendering in bundle.get("renderings", []):
            self.registered.setdefault(rendering["source_id"], set()).update(
                i["block_id"] for i in rendering.get("images", []) if "path" in i)

    def document(self, sid):
        doc = self.documents[sid]
        if sid not in self.verified:
            _source_path(self.run, doc)
            self.verified.add(sid)
        return doc

    def structure(self, sid, bid, anchors, *, linked, use_document_order=False):
        doc = self.document(sid)
        target = self.blocks[sid][bid]
        table_key = sid, target.location.part, target.location.table_index
        if (doc.source.format == "docx" and target.kind in {"table", "table_cell", "paragraph", "equation"}
                and target.location.table_index is not None):
            if table_key not in self.tables:
                self.tables[table_key] = _native_table(doc, target, self.registered.get(sid, set()))
            return self.tables[table_key]
        if linked:
            return linked_structure(doc, target, anchors, self.registered.get(sid, set()),
                                    lambda table: self.structure(sid, table, (), linked=False),
                                    use_document_order=use_document_order)
        return None


def _source_path(run, doc):
    path = Path(doc.source.path).resolve(strict=True)
    if not path.is_relative_to(Path(run).resolve().parent.parent) or hash_file(path) != doc.source.sha256:
        raise ValueError("source review original escaped its data root or changed")
    return path


def _native_table(doc, target, registered):
    """Describe an actual XML table, never substitute a caption for a figure."""
    family = [b for b in doc.blocks if b.location.part == target.location.part
              and b.location.table_index == target.location.table_index]
    tables = [b for b in family if b.kind == "table"]
    if len(tables) != 1:
        raise ValueError("native Word source requirement needs one exact parent table")
    images = [b.block_id for b in family if b.kind == "image"]
    if any(bid not in registered for bid in images):
        raise ValueError("native table contains an unavailable image; XML cannot replace its visual evidence")
    return {"representation": "native_word_table_xml", "parent_table_block_id": tables[0].block_id,
            "part": target.location.part, "table_index": target.location.table_index,
            "block_ids": [b.block_id for b in family], "full_view_block_ids": images,
            "visual_status": "native_table_has_no_registered_page_view",
            "limitations": ["Native Word table text, physical XML cells and equations can be delivered; "
                            "no rendered Word table/page layout is claimed. Empty containers alone are insufficient. "
                            "Any embedded image requires its own full registered occurrence view."]}


def build_native_word_requirement_amendment(run, scope, requirements, *, linked=False):
    """Explicit source-kind correction; old scopes and proofs stay unchanged."""
    sources = _ReviewSources(run)
    original = review_requirements(run, scope, "native-word-amendment", _sources=sources)
    lookup = {(item["source_id"], item["block_id"]): item for item in original}
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("native source amendment requires explicit original requirements")
    substitutions, seen = [], set()
    for item in requirements:
        key = item["source_id"], item["block_id"]
        if key in seen or lookup.get(key) != item or not item["requires_full_view"]:
            raise ValueError("native source amendment does not name an exact original visual requirement")
        seen.add(key)
        anchors = [a for a in scope["source_anchors"] if a["source_id"] == key[0]]
        structure = sources.structure(*key, anchors, linked=linked)
        if structure is None:
            raise ValueError("only a native Word table/cell/paragraph/equation can amend an impossible table view")
        substitutions.append({"original_requirement": item, "native_word_structure": structure,
                              "replacement_requires_full_view": False})
    amendment = {"policy": LINKED_AMENDMENT if linked else NATIVE_AMENDMENT_POLICY, "review_scope_sha256": scope["scope_sha256"],
                 "source_bundle_sha256": hash_file(Path(run) / "sources.json"), "substitutions": substitutions,
                 "scientific_validation": "not_established",
                 "interpretation": "Source-kind correction, not a claim that missing views were performed. "
                 "Legacy requirements are retained; complete native table XML/text delivery and actual embedded "
                 "image views replace only the impossible table-page view obligation."}
    if linked:
        amendment["interpretation"] = "The original failed delivery stands. Check the exact caption with its registered figure occurrence or adjacent native XML table. Text alone cannot establish visual delivery."
    amendment["amendment_sha256"] = digest_json(amendment)
    return amendment


def review_requirements(run, scope, call_id, *, _sources=None):
    if (not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", call_id)
            or not isinstance(scope, dict) or scope.get("policy") not in REVIEW_POLICIES
            or scope.get("scope_sha256") != digest_json({k: v for k, v in scope.items() if k != "scope_sha256"})):
        raise ValueError("source review requires a bound call and exact bounded-revision scope")
    sources = _sources or _ReviewSources(run)
    linked = scope.get("source_requirement_policy") == LINKED_POLICY
    anchors_by_source = {}
    for a in [*scope["source_anchors"], *scope.get("inherited_source_anchors", []), *scope.get("word_figure_witnesses", [])]:
        anchors_by_source.setdefault(a["source_id"], []).append(a)
    requirements = {}
    for anchor in scope["source_anchors"]:
        sid, bid = anchor["source_id"], anchor["block_id"]
        doc = sources.document(sid)
        block = sources.blocks[sid].get(bid)
        if block is None:
            raise ValueError("source review anchor is not one registered source block")
        locator = EvidenceLocator.model_validate(anchor.get("locator", {}))
        if locator.physical_page is not None and locator.physical_page != block.location.physical_page:
            raise ValueError("source review visual locator disagrees with source block")
        visual = bool(locator.figure or locator.panel or locator.table_index is not None
            or block.kind in {"image", "table", "table_cell"}
            or re.search(r"\b(?:fig(?:ure)?s?\.?|table)\s*[A-Za-z]?\d", " ".join(
                str(value or "") for value in (locator.section, locator.description)), re.I))
        if scope["policy"] == COVERAGE_POLICY:
            # Word XML table paragraphs/equation fragments are text dispositions,
            # not a claim that a reconstructed Word page was visually inspected.
            visual = block.kind in {"image", "pdf_page"}
        elif (scope.get("scope_limits", {}).get("compiler_version") in {2, 3, 4, 5, 6}
                and anchor.get("origin") == "prior_audit_blocker"
                and doc.source.format == "pdf"):
            visual = True
        key = sid, bid
        item = requirements.setdefault(key, {"source_id": sid, "block_id": bid,
            "source_sha256": doc.source.sha256, "physical_page": block.location.physical_page,
            "requires_full_view": False, "evidence_ids": []})
        item["requires_full_view"] |= visual
        item["evidence_ids"].append(anchor["evidence_id"])
        if (scope["policy"] == PARTIAL_COMPLETION_POLICY
                or scope.get("source_requirement_policy") in {NATIVE_REQUIREMENT_POLICY, LINKED_POLICY}):
            structure = sources.structure(
                sid, bid, anchors_by_source[sid], linked=linked and item["requires_full_view"],
                use_document_order=scope.get("scope_limits", {}).get("compiler_version", sources.compiler_version) == 6)
            if structure is not None:
                item["requires_full_view"] = False
                item["native_word_structure"] = structure
    if not requirements and scope["policy"] != PARTIAL_COMPLETION_POLICY:
        raise ValueError("bounded source review has no required original source blocks")
    for item in requirements.values():
        item["evidence_ids"] = sorted(set(item["evidence_ids"]))
        sid, bid = item["source_id"], item["block_id"]
        if (linked and item["requires_full_view"] and sources.documents[sid].source.format == "docx"
                and bid not in sources.registered.get(sid, set())):
            item["unresolved_word_visual"] = "A unique registered figure occurrence or adjacent native table could not be bound to this text anchor."
    return [requirements[key] for key in sorted(requirements)]


def delivery_payload(run, folder, tool, arguments, data, *, _sources=None):
    """Derive the receipt from actual successful tool output, not model claims."""
    from .reading_tools import ReadArgs, PageTextArgs, ViewArgs
    if tool not in SOURCE_TOOLS:
        return None
    if data.get("mode") == "cached_source_reference":
        # Repeated-request metadata contains no newly delivered source content.
        # Historical receipts without this marker keep their original replay.
        return None
    args = {"wt_read_blocks": ReadArgs, "wt_page_text": PageTextArgs, "wt_view": ViewArgs}[tool].model_validate(arguments)
    sources = _sources or _ReviewSources(run)
    doc = sources.document(args.source_id)
    base = {"tool": tool, "source_id": args.source_id, "source_sha256": doc.source.sha256}
    if tool == "wt_read_blocks":
        if not 0 < args.end_order - args.start_order + 1 <= 20 or args.end_order > len(doc.blocks):
            raise ValueError("source delivery block range is invalid")
        expected = [b.model_dump(mode="json") for b in doc.blocks[args.start_order - 1:args.end_order]]
        if data.get("blocks") != expected:
            raise ValueError("source delivery blocks differ from the frozen source representation")
        return {**base, "kind": "text", "block_ids": [b["block_id"] for b in expected if b["text"].strip()],
                "content_sha256": digest_json(expected)}
    if tool == "wt_page_text":
        expected = [b.block_id for b in doc.blocks if b.location.physical_page == args.physical_page]
        if (doc.source.format != "pdf" or data.get("source_id") != args.source_id
                or data.get("physical_page") != args.physical_page or not expected
                or data.get("canonical_block_ids") != expected or data.get("mode") != args.mode
                or not isinstance(data.get("text"), str)):
            raise ValueError("source page-text delivery does not match the registered page")
        return {**base, "kind": "text", "block_ids": expected if data["text"].strip() else [],
                "physical_page": args.physical_page, "content_sha256": digest_json(data["text"])}
    expected = ([b.block_id for b in doc.blocks if b.location.physical_page == args.physical_page]
                if args.physical_page is not None else [args.block_id])
    if args.physical_page is not None:
        if doc.source.format != "pdf":
            raise ValueError("a physical-page source view requires a PDF")
    else:
        if (doc.source.format != "docx" or args.block_id not in sources.blocks[args.source_id]
                or args.block_id not in sources.registered.get(args.source_id, set())):
            raise ValueError("a Word source view requires a registered image occurrence")
    rectangle = args.rectangle or [0, 0, 1, 1]
    image = Path(data["image_path"]).resolve(strict=True)
    if (data.get("source_id") != args.source_id or data.get("source_sha256") != doc.source.sha256
            or data.get("physical_page") != args.physical_page or data.get("block_ids") != expected
            or data.get("rectangle") != rectangle or data.get("scale") != args.scale or not expected
            or image != (Path(folder) / "view.png").resolve()
            or hash_file(image) != data.get("image_sha256")):
        raise ValueError("source visual delivery or retained image changed")
    return {**base, "kind": "full_view" if rectangle == [0, 0, 1, 1] else "crop_view",
            "block_ids": expected, "physical_page": args.physical_page, "rectangle": rectangle,
            "image_path": str(image), "image_sha256": data["image_sha256"]}


def review_navigation(requirements, documents=None):
    """Lossless dictionary for recurring table definitions in the model prompt."""
    structures, indexes, entries = [], {}, []
    for requirement in requirements:
        entry = dict(requirement)
        native = entry.pop("native_word_structure", None)
        if native is not None:
            key = (entry["source_id"], digest_json(native))
            if key not in indexes:
                indexes[key] = len(structures)
                structures.append(native)
            entry["native_structure_index"] = indexes[key]
        entries.append(entry)
    navigation = {"required_current_source_deliveries": entries, "native_structures": structures}
    if documents is None:
        return navigation
    # A single delivered range or original full page can satisfy many anchors. The
    # verifier still reconstructs the coverage of each from this turn's actual bytes.
    from .reading_budget import PAGE_SIZE
    by_source, visuals = {}, set()
    for row in requirements:
        sid = row["source_id"]
        native = row.get("native_word_structure") or {}
        by_source.setdefault(sid, set()).update(native.get("block_ids", []))
        for bid in native.get("full_view_block_ids", []):
            visuals.add((sid, "block_id", bid))
        if row["requires_full_view"]:
            page = row.get("physical_page")
            visuals.add((sid, "physical_page", page) if documents[sid].source.format == "pdf" and page is not None
                        else (sid, "block_id", row["block_id"]))
    for row in requirements:
        sid, bid = row["source_id"], row["block_id"]
        if (row.get("native_word_structure") or (sid, "block_id", bid) in visuals
                or (sid, "physical_page", row.get("physical_page")) in visuals):
            continue
        by_source[sid].add(bid)
    reads = []
    for sid, block_ids in by_source.items():
        blocks = documents[sid].blocks
        ordered = sorted(b.order for b in blocks if b.block_id in block_ids)
        extra_bytes = 0
        for order in ordered:
            last = reads[-1] if reads else None
            if last and last["source_id"] == sid and order - last["start_order"] < PAGE_SIZE:
                # Small gaps in Word cell/paragraph order are cheap to include in one read.
                # A long intervening source or a large metadata block gets its own boundary.
                gap_bytes = sum(len(b.model_dump_json().encode("utf-8"))
                                for b in blocks[last["end_order"]:order - 1])
                if extra_bytes + gap_bytes <= 4096:
                    last["end_order"] = order
                    extra_bytes += gap_bytes
                    continue
            reads.append({"source_id": sid, "start_order": order, "end_order": order})
            extra_bytes = 0
    navigation["suggested_wt_read_blocks"] = reads
    navigation["suggested_full_wt_views"] = [{"source_id": sid, field: value, "scale": 1}
                                              for sid, field, value in sorted(visuals)]
    navigation["usage"] = ("Read shared blocks once and inspect each required original full view once. "
                           "Zoom further when the measurements require it. These suggested calls produce no evidence.")
    return navigation


def partial_source_review_progress(run, scope, receipts, call_id):
    """Replay every supplied receipt and describe gaps without approving them.

    Empty or incomplete delivery is representable. Invalid/changed receipts are
    errors, not missing coverage, and a partial result is never a complete proof.
    """
    run = Path(run).resolve(strict=True)
    sources = _ReviewSources(run)
    requirements = review_requirements(run, scope, call_id, _sources=sources)
    if not isinstance(receipts, list):
        raise ValueError("source review receipts must be an explicit list")
    delivered, full_views, structured, checked, seen = set(), set(), set(), [], set()
    for receipt in receipts:
        if set(receipt) != {"request_path", "request_sha256", "result_path", "result_sha256"}:
            raise ValueError("source tool receipt has unexpected fields")
        request_path, result_path = (Path(receipt[k]).resolve(strict=True) for k in ("request_path", "result_path"))
        folder = request_path.parent
        if (folder.parent != run / "reading_tools" or not re.fullmatch(r"tool-\d{4,}", folder.name)
                or request_path.name != "request.json" or result_path != folder / "result.json"
                or str(request_path) in seen or hash_file(request_path) != receipt["request_sha256"]
                or hash_file(result_path) != receipt["result_sha256"]):
            raise ValueError("source tool receipt changed, repeated or escaped the current run")
        seen.add(str(request_path))
        request, result = _json(request_path), _json(result_path)
        data = result.get("data", {})
        if (request.get("source_review_call") != call_id or data.get("source_review_call") != call_id
                or result.get("success") is not True or request.get("tool") not in SOURCE_TOOLS):
            raise ValueError("source receipt is unsuccessful or belongs to another model call")
        expected = delivery_payload(run, folder, request["tool"], request["arguments"], data, _sources=sources)
        if expected is None or data.get("source_delivery") != expected:
            raise ValueError("source receipt does not describe its actual delivered content")
        keys = {(expected["source_id"], block) for block in expected["block_ids"]}
        if expected["kind"] in {"text", "full_view"}:
            delivered.update(keys)
        if expected["kind"] == "full_view":
            full_views.update(keys)
        if request["tool"] == "wt_read_blocks":
            # Complete original blocks were authenticated, including empty XML
            # containers. Their delivery does not imply any image was viewed.
            structured.update((expected["source_id"], b["block_id"]) for b in data["blocks"])
        checked.append(dict(receipt))
    for item in requirements:
        native = item.get("native_word_structure")
        if native and all((item["source_id"], bid) in structured for bid in native["block_ids"]):
            delivered.add((item["source_id"], item["block_id"]))
    missing = [r for r in requirements
               if (r["source_id"], r["block_id"]) not in delivered]
    unviewed = [r for r in requirements
                if r["requires_full_view"] and (r["source_id"], r["block_id"]) not in full_views]
    native_incomplete = [r for r in requirements if r.get("native_word_structure") and (
        any((r["source_id"], bid) not in structured for bid in r["native_word_structure"]["block_ids"])
        or any((r["source_id"], bid) not in full_views for bid in r["native_word_structure"]["full_view_block_ids"]))]
    pairs = lambda values: [{"source_id": sid, "block_id": bid} for sid, bid in sorted(values)]
    progress = {"policy": PROGRESS_POLICY, "call_id": call_id, "review_scope_sha256": scope["scope_sha256"],
                "source_bundle_sha256": hash_file(run / "sources.json"), "requirements": requirements,
                "tool_receipts": checked, "delivered": pairs(delivered), "full_views": pairs(full_views),
                "structured_blocks": pairs(structured), "missing_blocks": missing, "missing_full_views": unviewed,
                "missing_native_structures": native_incomplete,
                "complete": not missing and not unviewed and not native_incomplete, "scientific_validation": "not_established",
                "interpretation": "Verified source-tool delivery progress only. Partial delivery is not a completed "
                "source review; no scientific conclusion or model-turn completion is certified."}
    progress["progress_sha256"] = digest_json(progress)
    return progress


def verify_partial_source_review_progress(run, scope, progress, call_id):
    try:
        return progress == partial_source_review_progress(run, scope, progress["tool_receipts"], call_id)
    except (ValueError, OSError, TypeError, KeyError, AttributeError):
        return False


def build_source_review_proof(run, scope, receipts, call_id):
    """Require complete delivery, preserving the original v1 proof contract."""
    progress = partial_source_review_progress(run, scope, receipts, call_id)
    if not receipts and (progress["requirements"] or scope["policy"] != PARTIAL_COMPLETION_POLICY):
        raise ValueError("bounded review did not re-read any required original sources")
    if not progress["complete"]:
        names = lambda key: [f"{r['source_id']}/{r['block_id']}" for r in progress[key]]
        raise ValueError("bounded source review delivery incomplete; unread blocks=" + repr(names("missing_blocks"))
                         + "; missing full-page/occurrence views=" + repr(names("missing_full_views"))
                         + "; incomplete native structures=" + repr(names("missing_native_structures")))
    proof = {"policy": POLICY, "call_id": call_id, "review_scope_sha256": scope["scope_sha256"],
             "source_bundle_sha256": progress["source_bundle_sha256"], "requirements": progress["requirements"],
             "tool_receipts": progress["tool_receipts"], "scientific_validation": "not_established",
             "interpretation": "Original text/image delivery was rechecked; this does not prove comprehension or correctness."}
    proof["proof_sha256"] = digest_json(proof)
    return proof


def build_source_review_union_proof(run, scope, parts, *, requirement_amendment=None):
    """Verify separate call/run deliveries jointly satisfy the original scope.

    Each part has exactly run, scope, call_id, progress. No receipt is renamed or
    reassigned to another call. This proves source delivery only: the caller must
    separately authenticate the completed model reports and scientific findings.
    """
    run = Path(run).resolve(strict=True)
    requirements = review_requirements(run, scope, "source-review-union")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 8:
        raise ValueError("source delivery union requires 1..8 explicit call parts")
    documents_hash = digest_json(_json(run / "sources.json")["documents"])
    checked, seen, delivered, full_views, structured = [], set(), set(), set(), set()
    for part in parts:
        if not isinstance(part, dict) or set(part) != {"run", "scope", "call_id", "progress"}:
            raise ValueError("source delivery union part has unexpected fields")
        part_run = Path(part["run"]).resolve(strict=True)
        if part_run.parent != run.parent:
            raise ValueError("source delivery union part is outside the original run store")
        key = str(part_run), part["call_id"]
        if key in seen:
            raise ValueError("source delivery union repeats a model call")
        seen.add(key)
        if digest_json(_json(part_run / "sources.json")["documents"]) != documents_hash:
            raise ValueError("source delivery union has different original source representations")
        progress = partial_source_review_progress(part_run, part["scope"],
            part["progress"]["tool_receipts"], part["call_id"])
        if progress != part["progress"]:
            raise ValueError("source delivery union progress does not match its original receipts")
        delivered.update((item["source_id"], item["block_id"]) for item in progress["delivered"])
        full_views.update((item["source_id"], item["block_id"]) for item in progress["full_views"])
        structured.update((item["source_id"], item["block_id"]) for item in progress["structured_blocks"])
        checked.append({"run": str(part_run), "scope": part["scope"], "call_id": part["call_id"], "progress": progress})
    replacements = {}
    if requirement_amendment is not None:
        expected_amendment = build_native_word_requirement_amendment(run, scope,
            [item["original_requirement"] for item in requirement_amendment["substitutions"]],
            linked=requirement_amendment.get("policy") == LINKED_AMENDMENT)
        if requirement_amendment != expected_amendment:
            raise ValueError("native source requirement amendment changed or lacks exact source-kind support")
        for item in expected_amendment["substitutions"]:
            req, native = item["original_requirement"], item["native_word_structure"]
            key = req["source_id"], req["block_id"]
            if (all((req["source_id"], bid) in structured for bid in native["block_ids"])
                    and all((req["source_id"], bid) in full_views for bid in native["full_view_block_ids"])):
                replacements[key] = native
                delivered.add(key)
    native_incomplete = []
    for req in requirements:
        native = req.get("native_word_structure")
        if native is None:
            continue
        if (all((req["source_id"], bid) in structured for bid in native["block_ids"])
                and all((req["source_id"], bid) in full_views for bid in native["full_view_block_ids"])):
            delivered.add((req["source_id"], req["block_id"]))
        else:
            native_incomplete.append(req)
    missing = [r for r in requirements if (r["source_id"], r["block_id"]) not in delivered]
    unviewed = [r for r in requirements if r["requires_full_view"]
                and (r["source_id"], r["block_id"]) not in full_views
                and (r["source_id"], r["block_id"]) not in replacements]
    if missing or unviewed or native_incomplete:
        raise ValueError("source delivery union remains incomplete; unread blocks=" + repr(missing)
                         + "; missing full-page/occurrence views=" + repr(unviewed)
                         + "; incomplete native structures=" + repr(native_incomplete))
    proof = {"policy": UNION_POLICY, "review_scope_sha256": scope["scope_sha256"],
             "source_bundle_sha256": hash_file(run / "sources.json"), "requirements": requirements,
             "parts": checked, "scientific_validation": "not_established",
             "interpretation": "Original source delivery is complete across separately replayed calls. "
             "Earlier delivery is inherited, not newly read. Model completion and scientific audit conclusions "
             "require separate authentication; this is not scientific validation."}
    if requirement_amendment is not None:
        proof["source_requirement_amendment"] = requirement_amendment
    proof["proof_sha256"] = digest_json(proof)
    return proof


def verify_source_review_union(run, scope, proof):
    try:
        return proof == build_source_review_union_proof(run, scope, proof["parts"],
            requirement_amendment=proof.get("source_requirement_amendment"))
    except (ValueError, OSError, TypeError, KeyError, AttributeError):
        return False


def verify_source_review(run, scope, proof, call_id):
    """Admission-time replay, including original sources and image/content hashes."""
    try:
        return proof == build_source_review_proof(run, scope, proof["tool_receipts"], call_id)
    except (ValueError, OSError, TypeError, KeyError, AttributeError):
        return False
