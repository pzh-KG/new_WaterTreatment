"""Source adjudication of bounded native edits, including protected observation values."""
from collections import defaultdict
from copy import deepcopy
import json
import re

from pydantic import Field

from .citation_audit import _evidence_ids, _parts, _path, _prefix, _resolve
from .domain import ExtractionRecord, Fact, StrictModel
from .ownership_audit import _changes, _dependencies
from .retained_review_packet import FOCUSED_VERSIONS, LocalReview, _snapshot, reading_groups
from .scientific_revision_audit import _change_evidence_ids, _entities, _owner, _validate_evidence_additions
from .storage import digest_json
from .workflow import _image_manifest


class FinalCorrection(StrictModel):
    group: str
    path: str
    replacement_json: str = Field(description="Exact JSON for the replacement value, checked as the final value against the cited originals.")
    source_ids: list[str] = Field(min_length=1)
    explanation: str = Field(min_length=12)


class FocusedReview(LocalReview):
    final_corrections: list[FinalCorrection] = Field(description="Only precise, independently checked terminal field replacements. Use [] when none.")


def _primary(change):
    path = _parts(change["changed_path"])
    return (len(path)==3 and path[0]=="observations" and path[2] in {"value","unit"}
            and all(isinstance(change[k],dict) for k in ("old_value","new_value"))
            and any(change["old_value"].get(k)!=change["new_value"].get(k) for k in ("value","normalized")))


def _sources(change, old, new):
    refs = set()
    for side, data in (("old_value",old),("new_value",new)):
        refs.update(_change_evidence_ids(change,side))
        parts = _parts(change["changed_path"])
        if _primary(change):
            obs = _resolve(data,parts[:2])
            refs.update(link["evidence_id"] for link in obs.get("evidence_links",[]) if parts[2] in link["supported_fields"]
                        and link["support_type"] in {"direct","conflict"})
    return refs


def _excerpt(text, quotes):
    if len(text) <= 2400:
        return text
    flat = " ".join(text.split())
    windows = []
    for quote in quotes:
        words = " ".join(quote.split()).split() if isinstance(quote,str) else []
        match = " ".join(words[:min(7,len(words))])
        pos = flat.casefold().find(match.casefold()) if len(match)>=10 else -1
        if pos >= 0:
            windows.append((max(0,pos-130),min(len(flat),pos+min(350,len(quote))+140)))
    merged = []
    for low,high in sorted(windows):
        if merged and low <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1],high)
        else:
            merged.append([low,high])
    return " […] ".join(flat[lo:hi] for lo,hi in merged) if merged else flat[:520]


def _context(row):
    if not isinstance(row,dict):
        return _snapshot(row)
    if len(json.dumps(row,ensure_ascii=False)) < 5200:
        return _snapshot(row)
    fields = ("id","name","material_ids","material_id","experiment_id","configuration_id","substance_id","stream_id",
              "reported_group","metric","value","unit","origin","time_window","pairs","observation_ids","relationship",
              "held_conditions","changed_conditions","outcomes","limitations","evidence_ids","locator","scope","target_id")
    return _snapshot({k:row[k] for k in fields if k in row})


def _affected_arm_endpoints(delta, before, after):
    """Select declared peer groups only for a changed operating arm or measured endpoint."""
    groups = defaultdict(set)
    owners, materials = set(), set()
    endpoints = set()
    for item in delta:
        p = _parts(item["changed_path"])
        if not p or len(p) < 2:
            continue
        for side in (before,after):
            if p[0] == "materials" and p[1] < len(side["materials"]) and (len(p) == 2 or p[2] == "role"
                    or p[2] == "components" and (len(p) <= 4 or p[4] in {"substance_id","retention"})):
                materials.add(side["materials"][p[1]]["id"])
            if p[0] not in {"experiments","observations"} or p[1] >= len(side[p[0]]):
                continue
            row = side[p[0]][p[1]]
            if p[0] == "experiments" and (len(p) == 2 or p[2] in {
                    "arm_kind","comparison_group","material_ids","configuration_id","targets","reaction_inputs","energy_inputs","conditions"}):
                owners.add(row["id"])
            if p[0] == "observations" and (len(p) == 2 or p[2] in {
                    "material_id","experiment_id","substance_id","stream_id","endpoint_kind","removal_attribution","time_window"} or
                    (p[2] in {"value","unit"} and _primary(item))):
                endpoints.add(row["id"])
    for side in (before,after):
        by_id = {e["id"]:e.get("comparison_group") for e in side["experiments"] if e.get("comparison_group")}
        for e in side["experiments"]:
            if e.get("comparison_group") and (e["id"] in owners or materials.intersection(e.get("material_ids",[]))):
                owners.add(e["id"])
                groups.setdefault(e["comparison_group"], set())
        for o in side["observations"]:
            gid = by_id.get(o.get("experiment_id"))
            if gid and (o.get("experiment_id") in owners or o["id"] in endpoints):
                groups[gid].add((o.get("endpoint_kind"),o.get("substance_id"),o.get("metric_kind")))
    return groups


def named_residual(parent):
    """Identify one public, explicitly located blocker shared by every negative disposition."""
    if parent.plan.get("workflow")!="retained-issue-source-review-v1":
        return None
    answers=list(parent.run.glob("calls/*/answer.json"))
    if len(answers)!=1:
        return None
    report=json.loads(answers[0].read_bytes())
    issues=report["additional_issues"]
    if len(issues)!=1:
        return None
    negative=[r for r in report["decisions"] if r["verdict"]=="requires_revision"]
    ids={g for r in negative for g in r["groups"]}
    named=set(re.findall(r"\b[AFCDL]\d+\b",issues[0]))
    paths=re.findall(r"\b(?:experiments|observations|materials|comparisons|configurations|quantitative_coverage)\[\d+\](?:\.[a-z_][a-z_0-9]*(?:\[\d+\])?)+",issues[0])
    if len(paths)!=1 or not ids or ids!=named or not _parts(paths[0]):
        return None
    return {"path":paths[0],"text":issues[0],"native_group_ids":sorted(ids),
            "inherited_judgments":[r["explanation"] for r in negative]}


def _residual_finding(parent, old, new):
    remaining=named_residual(parent)
    if not remaining:
        raise ValueError("No single explicitly named residual in the signed prior review")
    path=_parts(remaining["path"])
    expected=deepcopy(old)
    replacement=_resolve(new,path)
    _resolve(expected,path[:-1])[path[-1]]=replacement
    if expected!=new or old==new:
        raise ValueError("The proposed change extends outside the signed residual field")
    refs=_evidence_ids(_resolve(old,path)) | _evidence_ids(replacement)
    anchors=[{"source_id":e.source_id,"block_id":e.block_id,"quote":e.quote,
              "figure_or_table":e.locator.figure} for e in parent.candidate.evidence if e.id in refs]
    return {"id":"F1","paths":[remaining["path"]],"evidence":anchors,**remaining}


def build(parent, proposed, extra_source_keys=(), *, version=3):
    if version not in FOCUSED_VERSIONS:
        raise ValueError("Unknown focused review format")
    old, new = (x.model_dump(mode="json") for x in (parent.before,proposed))
    if version == 6 or (version == 9 and parent.receipt.get("baseline", {}).get("repaired_baseline_checks")):
        from .repaired_baseline import scoped_changes
        delta = scoped_changes(parent, proposed, review_quote_extensions=version == 9)
    else:
        delta = _changes(old,new,adjudicate_outcomes=True,review_source_edits=version >= 4,
                         review_quote_extensions=version == 9)
    if not delta or (version == 3 and len(delta) > 180):
        raise ValueError("Expected a nonempty bounded native change set")
    _validate_evidence_additions(parent.before,proposed,delta)
    old_entities,entities = (_entities(x,include_evidence=True) for x in (old,new))
    claims, primary, owner_names = defaultdict(list), {}, set()
    evidence = {e.id:e for e in proposed.evidence}
    ref_ids, priority = set(), defaultdict(int)
    findings = ([_residual_finding(parent,old,new)] if version==7 else
                [{"id":f"F{i}",**f} for i,f in enumerate(parent.findings,1)])
    if version >= 5:
        for finding in findings:
            ref_ids.update(finding.get("required_evidence_ids",[]))
    source_edits, consumers = {}, {}
    inventory={(d.source.source_id,b.block_id):(d,b) for d in parent.documents for b in d.blocks}
    for c in delta:
        path = _parts(c["changed_path"])
        kind = c["kind"]
        if kind in {"coverage_note","empty_source_block"}:
            row = new["coverage"][path[1]]
            source_edits[c["changed_path"]] = {(row["source_id"], block) for block in row["block_ids"]}
        else:
            owner_names.add(_owner(path,entities))
        if kind in {"evidence_relocation", "located_quote_addition", "literal_quote_extension"}:
            refs = [c["old_value"],c["new_value"]]
            source_edits[c["changed_path"]] = {(e["source_id"],e["block_id"]) for e in refs}
            if kind in {"located_quote_addition", "literal_quote_extension"}:
                from .validation import _text
                destination = inventory.get((refs[1]["source_id"], refs[1]["block_id"]))
                if not destination or _text(refs[1]["quote"]) not in _text(destination[1].text or ""):
                    raise ValueError("Added quotation must occur in its unchanged, registered original block.")
            eid = refs[0]["id"]
            if kind == "literal_quote_extension":
                ref_ids.add(eid)
            affected_refs = [key for key in sorted(entities) if key[0] != "evidence" and
                             any(eid in _evidence_ids(side.get(key)) for side in (old_entities,entities))]
            if not affected_refs:
                raise ValueError("Relocated evidence has no identifiable scientific consumer")
            owner_names.update(affected_refs)
            consumers[c["changed_path"]] = [_path(key) for key in affected_refs]
        ids = _sources(c,old,new)
        ref_ids.update(ids)
        if _primary(c):
            primary[c["changed_path"]] = ids
            for eid in ids:
                e=evidence[eid]
                priority[(e.source_id,e.block_id)] += 100
        matching=[f["id"] for f in findings if any(_parts(p) and
                  (_prefix(_parts(p),path) or _prefix(path,_parts(p))) for p in f.get("paths",[]))]
        key = (c["changed_path"] if _primary(c) or c["changed_path"] in source_edits else
               (path[0],tuple(matching) or tuple(t for t in path[1:] if isinstance(t,str))[:1]))
        if version >= 6 and c["changed_path"] not in source_edits:
            if _primary(c):
                key = ("direct value sources",tuple(sorted(ids)))
            elif matching:
                key = ("review findings",tuple(matching))
            else:
                key = ("affected fields",path[0])
        claims[key].append([c["changed_path"],_snapshot(c["old_value"]),_snapshot(c["new_value"])])
    changes = []
    for batch in claims.values():
        for i in range(0,len(batch),12):
            changes.append({"id":f"C{len(changes)+1}","changes":batch[i:i+12]})
    if version >= 4 and (len(changes) > 64 or len(owner_names) > 180):
        raise ValueError("Focused review exceeds the bounded target groups or affected entities; split the revision.")
    # Include genuinely affected relations and their old/new paired endpoints.
    # A material or experiment name alone never establishes a physical pairing.
    affected,_,links = _dependencies(old_entities,entities,owner_names,set())
    related = [key for key in sorted(affected) if key[0] in {"comparisons","reference_associations"}]
    affected_obs = {entities[k]["id"] for k in affected if k[0]=="observations"}
    linked = {}
    for key,row in entities.items():
        if key[0]!="experiments":
            continue
        pairs = [p for item in (old_entities.get(key,{}),row) for p in item.get("pairs",())
                 if {p["left_observation_id"],p["right_observation_id"]} & affected_obs]
        if pairs:
            linked[_path(key)] = list({digest_json(p):_snapshot(p) for p in pairs}.values())
    dependency_paths = list(dict.fromkeys([*map(_path,related),*linked]))
    for i in range(0,len(dependency_paths),8):
        batch = dependency_paths[i:i+8]
        findings.append({"id":f"D{i//8+1}","paths":batch,"text":"Check how the scoped edits affect these named comparisons, shared references and paired endpoints; retain documented boundaries.","evidence":[]})
    # A separately owned dark/no-dose or material control may have no registered
    # comparison edge. Preserve its measured endpoint and supply route in the
    # same bounded review when the explicit comparison group is affected.
    if proposed.rule_version == "0.9":
        group_metrics = _affected_arm_endpoints(delta, old, new)
        peers = {key for key in affected - owner_names if key[0] == "experiments"
                 and entities[key].get("comparison_group") in group_metrics and entities[key].get("arm_kind") in {"control", "treatment"}}
        outcomes = {key for key in affected - owner_names if key[0] == "observations" and any(
            (entities[key].get("endpoint_kind"),entities[key].get("substance_id"),entities[key].get("metric_kind")) in group_metrics[entities[peer]["comparison_group"]]
            for peer in links[key] & peers)}
        for peer in sorted(peers):
            members = [peer, *(key for key in sorted(outcomes) if peer in links[key])]
            for offset in range(0,len(members),8):
                group = members[offset:offset+8]
                ids = {identifier for key in group for identifier in entities[key].get("evidence_ids", [])}
                ref_ids.update(ids)
                paths = [_path(key) for key in group if key != peer]
                if peer in group:
                    paths[:0] = [_path((*peer, name)) for name in ("reported_group","material_ids","reaction_inputs","energy_inputs","targets","conditions")]
                anchors = [{"source_id":evidence[i].source_id,"block_id":evidence[i].block_id,
                            "figure_or_table":evidence[i].locator.figure} for i in sorted(ids)]
                findings.append({"id":f"A{len(findings)+1}", "paths":paths,
                                 "text":"Verify the registered arm, operating inputs and related control endpoints against their original labels and source conditions.",
                                 "evidence":anchors})
    for path, names in consumers.items():
        for i in range(0,len(names),8):
            findings.append({"id":f"L{len(findings)+1}", "paths":names[i:i+8],
                             "text":f"Check each named consumer of {path} against the original evidence, both locators if changed, and the exact proposed quote if added. Restrict unsupported claims." if proposed.rule_version == "0.9" else f"Check each named consumer of relocated {path} against both original locators. Restrict unsupported claims.",
                             "evidence":[], "locator_change":path})
    locations = set(map(tuple,extra_source_keys))
    if version==7:
        for key in locations:
            priority[key]+=1
    for keys in source_edits.values():
        locations.update(keys)
        for key in keys:
            priority[key] += 5
    for e in (evidence[i] for i in ref_ids):
        locations.add((e.source_id,e.block_id))
    for f in findings:
        for a in f["evidence"]:
            key = a["source_id"],a["block_id"]
            locations.add(key)
            priority[key] += 2 if a.get("figure_or_table") else 1
    if not locations:
        raise ValueError("No original sources traceable for the focused proposal")
    renders = _image_manifest(parent.bundle)
    images, cards, directory, native = [], [], {}, {}
    all_evidence=defaultdict(list)
    for e in proposed.evidence:
        all_evidence[(e.source_id,e.block_id)].append(e)
    for key in sorted(locations,key=lambda k:(-priority[k],k)):
        doc,block=inventory[key]
        found=all_evidence[key]
        card_id=f"S{len(cards)+1}"
        directory[key]=card_id
        quotes=[e.quote for e in found if e.id in ref_ids and e.quote]
        quotes.extend(a["quote"] for f in findings for a in f["evidence"] if (a["source_id"],a["block_id"])==key and a.get("quote"))
        card={"id":card_id,"source":key[0],"block":key[1],"kind":block.kind,
              "text":_excerpt(block.text,quotes),"page":block.location.physical_page}
        matches=[v["path"] for v in renders if v["source_id"]==key[0] and (v.get("block_id")==key[1] or
                 doc.source.format=="pdf" and v.get("physical_page")==block.location.physical_page)]
        if matches:
            if len(set(matches)) != 1:
                raise ValueError("Ambiguous original rendering for a localized source")
            native[card_id]=matches[0]
            if matches[0] in images or (priority[key]>0 and len(images)<12):
                if matches[0] not in images:
                    images.append(matches[0])
                card["image"]=images.index(matches[0])+1
        cards.append(card)
    literature = {eid:{"card":directory[(evidence[eid].source_id,evidence[eid].block_id)],
                       "locator":_snapshot(evidence[eid].locator.model_dump(mode="json")),"quote":evidence[eid].quote}
                  for eid in sorted(ref_ids)}
    targets={}
    changed_groups={row[0]:g["id"] for g in changes for row in g["changes"]}
    for f in findings:
        for p in f.get("paths",[]):
            try:
                if p in changed_groups:
                    targets[p]={"exact_change":changed_groups[p]}
                else:
                    targets[p]=linked[p] if p in linked and f["id"].startswith("D") else _context(_resolve(new,_parts(p)))
            except (ValueError,TypeError,KeyError,IndexError):
                continue  # Historical element may have been deleted within the explicitly changed parent collection.
    _,reading=reading_groups(parent)
    packet={"version":version,"candidate_hash":digest_json(new),"prior_candidate_hash":digest_json(old),"title":proposed.paper.title.value,
            "changes":changes,"findings":findings,"sources":cards,"current_targets":targets,"reading_groups":reading,
            "inherited_references":{},"evidence_directory":literature,"source_order":[d.source.source_id for d in parent.documents],
            "primary_source_cards":{p:sorted({literature[e]["card"] for e in ids}) for p,ids in primary.items()}}
    if version >= 4:
        if proposed.rule_version == "0.9":
            from .review_transport import POLICY as WIRE_POLICY
            packet["prompt_transport"] = WIRE_POLICY
        packet["required_group_sources"] = {
            changed_groups[path]: sorted(directory[key] for key in keys) for path,keys in source_edits.items()}
        for finding in findings:
            if finding.get("locator_change"):
                packet["required_group_sources"][finding["id"]] = packet["required_group_sources"][changed_groups[finding["locator_change"]]]
            if version >= 5 and finding.get("required_evidence_ids"):
                needed = finding["required_evidence_ids"]
                packet["required_group_sources"][finding["id"]] = sorted({literature[eid]["card"] for eid in needed})
                for eid in needed:
                    packet["inherited_references"][eid] = {"evidence":_snapshot(evidence[eid].model_dump(mode="json"))}
        from .review_transport import compact_review
        if len(json.dumps(compact_review(packet),ensure_ascii=False,separators=(",",":")).encode("utf-8")) > 650_000:
            raise ValueError("Focused review exceeds its source-packet bound; split the revision.")
    packet["packet_hash"]=digest_json(packet)
    return packet,images,native


def _check_comparison_operands(data, comparison, cited, previous, replacement):
    """New magnitudes in interpretive prose must repeat cited, owned source values with units."""
    from decimal import Decimal
    from unicodedata import normalize
    from .reviewer_text import NUMBER

    def plain(text):
        return normalize("NFKC", text).replace("−", "-")

    new_text = plain(replacement)
    known = {Decimal(m) for m in NUMBER.findall(plain(previous))}
    anchors = {e["id"] for e in data["evidence"] if (e["source_id"], e["block_id"]) in cited}
    units = defaultdict(set)
    member_ids = set(comparison.get("observation_ids", []))
    for row in data.get("observations", []):
        if row["id"] not in member_ids or row["origin"] not in {"reported_measurement", "author_calculation"}:
            continue
        value, unit = row["value"], row["unit"]
        if (value.get("status") != "reported" or type(value.get("value")) not in {int, float}
                or unit.get("status") != "reported" or not isinstance(unit.get("value"), str)
                or not unit["value"].strip() or not anchors.intersection(value.get("evidence_ids", []))
                or not anchors.intersection(unit.get("evidence_ids", []))):
            continue
        units[Decimal(str(value["value"]))].add(plain(unit["value"]).strip())
    for match in NUMBER.finditer(new_text):
        number = Decimal(match.group())
        if number in known:
            continue
        tail = new_text[match.end():].lstrip()
        if not any(re.match(re.escape(unit) + r"(?=$|[\s),.;:\]])", tail) for unit in units[number]):
            raise ValueError("A new magnitude in comparison text needs a source-linked comparison operand with its unit")


def _condition_withdrawal(original, value, correction, path, companions):
    """Withdraw only documented claims, with a signed qualification on this same reference."""
    if not (len(path) == 3 and path[0] == "reference_associations"
            and path[2] in {"matched_conditions", "differing_conditions"}):
        return False
    if (not isinstance(original, list) or not isinstance(value, list)
            or not all(isinstance(v, str) and v.strip() for v in original + value)
            or len(set(original)) != len(original) or len(value) >= len(original)
            or value != [v for v in original if v in value]):
        raise ValueError("A condition withdrawal may only retain an ordered subset of the reviewed assertions")
    for other in companions:
        parts = _parts(other.path)
        if (other.group == correction.group and parts[:2] == tuple(path[:2])
                and len(parts) == 4 and parts[2] == "limitations" and type(parts[3]) is int
                and set(other.source_ids).intersection(correction.source_ids)):
            value_note = json.loads(other.replacement_json)
            if isinstance(value_note, str) and value_note.strip():
                # The companion is adjudicated independently in the same atomic result.
                return True
    raise ValueError("Withdrawing a comparison condition requires a source-reviewed limitation on the same reference")


def _check_dependency_text(data, packet, finding, path, correction, value, *, companions=()):
    """A named relation's visible wording can be adjudicated independently of its edges."""
    owner = _path(path[:2])
    leaf = path[2:]
    is_rationale = list(leaf) == ["rationale"] and path[0] == "reference_associations"
    is_limitation = len(leaf) == 2 and leaf[0] == "limitations" and type(leaf[1]) is int
    is_comparison_text = (path[0] == "comparisons" and len(leaf) == 4
        and leaf[0] in {"held_conditions", "changed_conditions", "outcomes"}
        and type(leaf[1]) is int and list(leaf[2:]) == ["fact", "value"])
    is_interpretation = (path[0] == "comparisons" and len(leaf) == 3
        and leaf[0] in {"counterevidence", "confounders"} and type(leaf[1]) is int and leaf[2] == "value")
    is_pair_note = (path[0]=="experiments" and len(leaf)==3 and leaf[0]=="pairs"
                    and type(leaf[1]) is int and leaf[-1]=="note")
    original = _resolve(data, path)
    withdrawal = _condition_withdrawal(original, value, correction, path, companions)
    textual = (isinstance(value, str) and isinstance(original, str)
               and (is_pair_note or (path[0] in {"reference_associations", "comparisons"}
                    and (is_rationale or is_limitation or is_comparison_text or is_interpretation))))
    if owner not in finding["paths"] or not (withdrawal or textual):
        raise ValueError("A dependency correction may clarify only explicitly reviewed relation text")
    shown = packet["current_targets"].get(owner)
    if is_pair_note:
        pair = _resolve(data,path[:-1])
        if not isinstance(shown,list) or _snapshot(pair) not in shown:
            raise ValueError("The reviewer did not receive the exact original pair and its explanation")
        evidence_owner = pair
    elif not isinstance(shown, dict) or _resolve(shown, leaf) != _resolve(data, path):
        raise ValueError("The reviewer did not receive the exact original relation text")
    else:
        evidence_owner = _resolve(data, path[:-1] if is_comparison_text or is_interpretation else path[:2])
    cited = {(s["source"], s["block"]) for s in packet["sources"] if s["id"] in correction.source_ids}
    if (is_comparison_text or is_interpretation) and evidence_owner.get("normalized") is not None:
        raise ValueError("A dependency wording correction cannot change a normalized quantity")
    if is_interpretation:
        if evidence_owner.get("raw") or evidence_owner.get("alternatives"):
            raise ValueError("Comparison wording with a verbatim source or alternatives needs a separate review")
        _check_comparison_operands(data, _resolve(data, path[:2]), cited, _resolve(data, path), value)
    own_ids = set(evidence_owner.get("evidence_ids", []))
    if not any(e["id"] in own_ids and (e["source_id"], e["block_id"]) in cited for e in data["evidence"]):
        raise ValueError("A dependency text correction must cite the relation's own original source")


def _supplemental_note_sources(data, packet, report, correction, decision, groups):
    """A field-level signed note can cite the same experiment's already adjudicated method."""
    path = _parts(correction.path)
    if (len(path) < 4 or path[0] != "experiments" or path[-1] != "note"
            or correction.group not in groups):
        return False
    fact = _resolve(data,path[:-1])
    if not isinstance(fact,dict) or "status" not in fact or "value" not in fact:
        return False
    directory = {(c["source"],c["block"]):c["id"] for c in packet["sources"]}
    cards = {e["id"]:directory.get((e["source_id"],e["block_id"])) for e in data["evidence"]}
    common = set(correction.source_ids) & set(decision.source_ids)
    if not common.intersection(cards.get(k) for k in fact.get("evidence_ids",[])):
        return False
    method_cards = {cards.get(eid) for eid in _evidence_ids(_resolve(data,path[:2]).get("conditions",[]))}
    extra = set(correction.source_ids) - set(decision.source_ids)
    if not extra <= method_cards:
        return False
    signed = set()
    for sibling in report.final_corrections:
        other = _parts(sibling.path)
        if (other == path or other[:2] != path[:2] or
                tuple(x for x in other[2:] if isinstance(x,str)) != tuple(x for x in path[2:] if isinstance(x,str))):
            continue
        row = next((r for r in report.decisions if sibling.group in r.groups),None)
        if (row and row.verdict != "requires_revision" and sibling.group in groups
                and any(_prefix(_parts(c[0]),other) for c in groups[sibling.group]["changes"])
                and set(sibling.source_ids) <= set(row.source_ids)):
            signed.update(sibling.source_ids)
    return extra <= signed


def _reviewed_inactive_setting(data, packet, correction, path, original, value, report):
    """Adjudicate qualitative absence without introducing or dropping measured quantities."""
    if (len(path) != 7 or path[0] != "experiments" or path[2] != "energy_inputs"
            or path[4] != "parameters" or path[6] not in {"fact", "unit"}
            or not all(type(path[i]) is int for i in (1, 3, 5))
            or not isinstance(original, dict) or not isinstance(value, dict)
            or len(correction.replacement_json) > 5000):
        return None
    energy = _resolve(data, path[:4])
    shown = packet.get("current_targets", {}).get(_path(path[:3]))
    if (not isinstance(shown, list) or path[3] >= len(shown) or shown[path[3]] != _snapshot(energy)
            or energy["source"]["status"] != "explicit_absence" or energy["source"]["value"] is not None):
        return None
    parameter = _resolve(data, path[:6])
    magnitude = parameter["fact"]["value"]
    if magnitude is not None and (type(magnitude) not in {int, float} or magnitude != 0):
        return None
    if any(original[key] for key in ("alternatives", "normalization_reason")) or any(
            original[key] is not None for key in ("normalized", "raw")):
        return None
    if path[-1] == "unit" and magnitude is not None:
        # A retained numerical magnitude also needs its original unit.
        magnitude_path = _path((*path[:-1], "fact"))
        peers = [item for item in report.final_corrections if item.path == magnitude_path]
        if len(peers) != 1:
            return None
        peer = peers[0]
        replacement = _reviewed_inactive_setting(data, packet, peer, _parts(peer.path),
                                                  parameter["fact"], json.loads(peer.replacement_json), report)
        if replacement is None:
            return None
    try:
        qualified = Fact.model_validate(value).model_dump(mode="json")
    except ValueError:
        return None
    refs = original["evidence_ids"]
    if (qualified["status"] != "not_reported" or qualified["value"] is not None or not qualified["note"]
            or not refs or qualified["evidence_ids"] != refs or not set(refs) <= set(energy["source"]["evidence_ids"])
            or any(qualified[k] != original[k] for k in ("raw", "normalized", "normalization_reason", "alternatives"))):
        return None
    anchors = {(card["source"], card["block"]) for card in packet["sources"] if card["id"] in correction.source_ids}
    shared = {ref["id"] for ref in data["evidence"] if (ref["source_id"], ref["block_id"]) in anchors}
    return qualified if set(refs) <= shared and qualified != original else None


def correction_result(record, packet, report):
    data=record.model_dump(mode="json")
    findings={f["id"]:f for f in packet["findings"]}
    changes={g["id"]:g for g in packet["changes"]}
    outcomes={group:row for row in report.decisions for group in row.groups}
    for group, cards in packet.get("required_group_sources", {}).items():
        if group not in outcomes or not set(cards).issubset(outcomes[group].source_ids):
            raise ValueError("Source-note or locator adjudication omitted a bound original for "+group)
    applied=[]
    reviewed_data = record.model_dump(mode="json")
    for group in packet["changes"]:
        for p,_,_ in group["changes"]:
            expected=set(packet["primary_source_cards"].get(p,[]))
            row=outcomes.get(group["id"])
            if expected and (row is None or not expected.intersection(row.source_ids)):
                raise ValueError("Revised observation lacks a disposition citing its own original source: "+p)
    for correction in report.final_corrections:
        row=outcomes.get(correction.group)
        if (row is None or row.verdict=="requires_revision"
                or (not set(correction.source_ids)<=set(row.source_ids)
                    and not _supplemental_note_sources(data,packet,report,correction,row,changes))):
            raise ValueError("A final correction requires an affirmative source-bound disposition")
        paths=([c[0] for c in changes[correction.group]["changes"]] if correction.group in changes
               else findings.get(correction.group,{}).get("paths",[]))
        path=_parts(correction.path)
        coverage_note = (len(path)==3 and path[0]=="coverage" and type(path[1]) is int and path[2]=="note"
                         and correction.path in paths and packet.get("required_group_sources",{}).get(correction.group)
                         and any(s in packet["required_group_sources"][correction.group] for s in correction.source_ids))
        if (not path or (not coverage_note and path[0] not in {"materials","experiments","observations","comparisons","quantitative_coverage","design_scope","extensions","configurations","reference_associations","substances"})
                or not any(_parts(p) and _prefix(_parts(p),path) for p in paths)
                or any(_prefix(prior,path) or _prefix(path,prior) for prior in applied)):
            raise ValueError("Corrective field is outside the adjudicated target or overlaps another correction")
        original=_resolve(data,path)
        value=json.loads(correction.replacement_json)
        pair_text = (len(path)>=5 and path[0]=="experiments" and path[2]=="pairs" and type(path[3]) is int
                     and (path[4:]==("note",) or (len(path)==6 and path[4] in {"matched_conditions","unresolved_conditions","differing_conditions"}
                         and type(path[5]) is int)) and isinstance(original,str) and isinstance(value,str))
        if pair_text:
            if path[4] == "differing_conditions" and not value.strip():
                raise ValueError("A reviewed condition difference must retain a substantive description")
            pair=_resolve(data,path[:4])
            anchors={(s["source"],s["block"]) for s in packet["sources"] if s["id"] in correction.source_ids}
            if not any(e["id"] in pair["evidence_ids"] and (e["source_id"],e["block_id"]) in anchors for e in data["evidence"]):
                raise ValueError("Pair wording requires an original cited by the exact pair")
        if correction.group.startswith("D"):
            _check_dependency_text(data, packet, findings[correction.group], path, correction, value,
                                   companions=report.final_corrections)
        qualified = _reviewed_inactive_setting(reviewed_data, packet, correction, path, original, value, report)
        if qualified is not None:
            _resolve(data,path[:-1])[path[-1]] = qualified
            applied.append(path)
            continue
        if (value==original or len(correction.replacement_json)>5000 or isinstance(original,dict) or isinstance(value,dict)
                or any(isinstance(t,str) and (t=="id" or t.endswith(("_id","_ids")) or t=="source_ids"
                    or (t=="pairs" and not pair_text)
                    or (t=="coverage" and not coverage_note)) for t in path)):
            raise ValueError("Terminal corrections cannot silently change scientific identity or replace a whole scientific object")
        _resolve(data,path[:-1])[path[-1]]=value
        applied.append(path)
    corrected=ExtractionRecord.model_validate(data)
    if corrected.model_dump(mode="json")!=data:
        raise ValueError("Terminal review changed additional serialized fields")
    return corrected


def _pending_missing_setting(data, packet, correction, path, original, value):
    """Keep source-bound operating conditions as proposals even when terminal edits are unsafe."""
    if (len(path) < 4 or path[0] != "experiments" or not isinstance(original, dict)
            or not isinstance(value, dict) or not value.keys() <= original.keys()):
        return False
    owner = _resolve(data, path[:-1])
    if path[2] == "reaction_inputs":
        if (len(path) != 5 or path[4] not in {"unit", "basis", "timing"}
                or owner["amount"]["value"] is not None):
            return False
    elif not (isinstance(owner, dict) and set(owner) == {"name", "fact", "unit", "basis"}
              and path[-1] in {"fact", "unit", "basis"}):
        return False
    try:
        fact = Fact.model_validate(value)
    except ValueError:
        return False
    if (fact.status.value not in {"not_reported", "ambiguous", "not_available"} or fact.value is not None
            or fact.normalized is not None or fact.alternatives or fact.raw or fact.normalization_reason
            or not fact.evidence_ids or not fact.note or len(correction.replacement_json) > 5000):
        return False
    anchors = {(card["source"], card["block"]) for card in packet.get("sources", [])
               if card["id"] in correction.source_ids}
    supported = {ref["id"] for ref in data["evidence"] if (ref["source_id"], ref["block_id"]) in anchors}
    return set(fact.evidence_ids) <= supported and fact.model_dump(mode="json") != original


def pending_reference_wording(record, packet, report):
    """Separate a proposed change to matching criteria for independent adjudication."""
    data = record.model_dump(mode="json")
    findings = {f["id"]: f for f in packet["findings"]}
    decisions = {g: row for row in report.decisions for g in row.groups}
    sources = {c["id"]: (c["source"], c["block"]) for c in packet["sources"]}
    evidence = {e["id"]: (e["source_id"], e["block_id"]) for e in data["evidence"]}
    pending = []
    paths = [_parts(c.path) for c in report.final_corrections]
    for i, correction in enumerate(report.final_corrections):
        path = paths[i]
        if not (correction.group.startswith("D") and len(path) == 4 and path[0] == "reference_associations"
                and type(path[1]) is int and path[2] in {"matched_conditions", "unresolved_conditions", "differing_conditions"}
                and type(path[3]) is int):
            continue
        owner = _path(path[:2])
        finding = findings.get(correction.group, {})
        decision = decisions.get(correction.group)
        shown = packet["current_targets"].get(owner)
        original = _resolve(data, path)
        value = json.loads(correction.replacement_json)
        if (owner not in finding.get("paths", []) or not isinstance(shown, dict) or path[2] not in shown
                or _resolve(shown, path[2:]) != original or not isinstance(original, str)
                or not isinstance(value, str) or not value.strip() or len(correction.replacement_json) > 5000
                or value == original or not decision or decision.verdict == "requires_revision"
                or not correction.source_ids or not set(correction.source_ids) <= set(decision.source_ids)
                or any(j != i and (_prefix(other, path) or _prefix(path, other)) for j, other in enumerate(paths))):
            return None
        anchors = {evidence[k] for k in _resolve(data, path[:2])["evidence_ids"] if k in evidence}
        if any(sources.get(k) not in anchors for k in correction.source_ids):
            return None
        pending.append(correction)
    if not pending:
        return None
    applied = [c for c in report.final_corrections if c not in pending]
    checked = correction_result(record, packet, report.model_copy(update={"final_corrections": applied}))
    return checked, pending


def pending_structured_corrections(record, packet, report):
    """Retain an entire nonterminal proposal batch for a further independent source review."""
    if not report.final_corrections:
        return []
    data=record.model_dump(mode="json")
    groups={r["id"]:r for r in packet["changes"]+packet["findings"]}
    decisions={g:row for row in report.decisions for g in row.groups}
    structured=False
    scoped_settings=[]
    ordinary=[]
    for c in report.final_corrections:
        path=_parts(c.path)
        group=groups.get(c.group,{})
        allowed=[*group.get("paths",[]),*[row[0] for row in group.get("changes",[])]]
        row=decisions.get(c.group)
        if (not path or not any(_parts(p) and _prefix(_parts(p),path) for p in allowed)
                or not row or row.verdict=="requires_revision" or not c.source_ids
                or not set(c.source_ids)<=set(row.source_ids)):
            return []
        original=_resolve(data,path)
        value=json.loads(c.replacement_json)
        if (isinstance(original,dict) and isinstance(value,dict) and path[0] in {"experiments","observations"}
                and path[-1] in {"time_window","reference"} and value and value.keys()<=original.keys()):
            structured=True
        if _pending_missing_setting(data, packet, c, path, original, value):
            scoped_settings.append(c)
        else:
            ordinary.append(c)
    if not structured:
        if not scoped_settings:
            return []
        # A mixed proposal is indivisible. Recheck each remaining scalar field's
        # ordinary scope and identity rules before offering the batch for review.
        try:
            correction_result(record, packet, report.model_copy(update={"final_corrections": ordinary}))
        except ValueError:
            return []
    # Check primary values and required original locators even when no proposed
    # terminal field can be applied. Every pending edit is re-exposed as a blocker.
    correction_result(record,packet,report.model_copy(update={"final_corrections":[]}))
    return report.final_corrections
