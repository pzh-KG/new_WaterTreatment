"""Bind reused local stream labels to their explicit apparatus, without changing measured facts."""
from collections import Counter, defaultdict
from urllib.parse import quote

from .storage import digest_json


def bind_stream_identifiers(record):
    if record.rule_version != "0.9":
        return None
    owners = defaultdict(list)
    for config in record.configurations:
        for stream in config.streams:
            owners[stream.id].append(config.id)
    duplicated = {name: ids for name, ids in owners.items() if len(set(ids)) > 1 and len(ids) == len(set(ids))}
    if not duplicated or len({c.id for c in record.configurations}) != len(record.configurations):
        return None
    used = set(owners)
    aliases = {}
    for name, ids in sorted(duplicated.items()):
        for cid in sorted(ids):
            candidate = "stream:" + quote(cid, safe="") + ":" + quote(name, safe="")
            while candidate in used:
                candidate = "@" + candidate
            aliases[(cid, name)] = candidate
            used.add(candidate)
    before = digest_json(record.model_dump(mode="json"))
    counts = Counter()

    def resolve(cid, sid, kind):
        if (cid, sid) in aliases:
            counts[kind] += 1
            return aliases[cid, sid]
        return sid

    for config in record.configurations:
        for stream in config.streams:
            stream.id = resolve(config.id, stream.id, "definitions")
            stream.upstream_stream_ids = [resolve(config.id, sid, "upstream") for sid in stream.upstream_stream_ids]
    experiment_counts = Counter(e.id for e in record.experiments)
    config_by_exp = {e.id: e.configuration_id for e in record.experiments if experiment_counts[e.id] == 1}
    for e in record.experiments:
        for r in e.reaction_inputs:
            if r.stream_id is not None:
                r.stream_id = resolve(e.configuration_id, r.stream_id, "reaction_inputs")
    for o in record.observations:
        if o.stream_id is not None:
            o.stream_id = resolve(config_by_exp.get(o.experiment_id), o.stream_id, "observations")
    return {"policy": "explicit-configuration-stream-identifiers-v1", "before_sha256": before,
            "after_sha256": digest_json(record.model_dump(mode="json")), "references": dict(counts),
            "aliases": [{"configuration_id": cid, "source_label": sid, "record_id": new} for (cid, sid), new in aliases.items()],
            "scientific_values_changed": False}
