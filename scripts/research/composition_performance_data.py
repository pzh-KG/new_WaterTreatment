"""Prepare traceable composition coordinates and observation-level performance overlays."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import trustworthiness
from sklearn.metrics.pairwise import cosine_similarity
from umap import UMAP

from plot_material_atlas import VARIANT_ROLES, verified_bytes
from plot_material_space import ORIGINS, clean, rates_unit, reported


ENDPOINTS = {
    "parent_concentration_removal", "pollutant_removal", "pollutant_removal_efficiency",
    "feed_parent_removal", "feed_parent_removal_percent", "degradation_efficiency",
    "author_reported_photocatalytic_removal", "tc_degradation_efficiency", "tc_removal_efficiency",
}
ELEMENTS = set(("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
                "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce "
                "Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn "
                "Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh "
                "Fl Mc Lv Ts Og").split())
WEIGHTS = {"constituent_identity": .5, "explicit_elements": .3, "component_names": .2}
SEED = 42


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def signature(material):
    parts = set()
    for component in material["components"]:
        if component["retention"] == "retained":
            sub = component["substance"]
            name = clean(reported(sub["name"])).casefold()
            formula = clean(reported(sub["formula"]))
            if name or formula:
                parts.add((name, formula))
    return tuple(sorted(parts))


def formula_elements(formula):
    # Count explicit elements only; MXene surface terminations have unspecified chemistry.
    body = re.sub(r"^(?:[αβγδ]|g)-", "", formula).removesuffix("Tx")
    body = re.sub(r"\)n$", ")", body)
    symbols = re.findall(r"[A-Z][a-z]?", body)
    rest = re.sub(r"[A-Z][a-z]?|[0-9.()]", "", body)
    return set(symbols) if not rest and set(symbols) <= ELEMENTS else set()


def jaccard(left, right):
    return len(left & right) / len(left | right)


def embed(signatures):
    n = len(signatures)
    if n < 4:
        raise ValueError("Four documented compositions are required for a two-dimensional map")
    exact, elements, names = [], [], []
    for sig in signatures:
        exact.append({(kind, value) for name, formula in sig
                      for kind, value in (("name", name), ("formula", formula)) if value})
        elements.append({element for _, formula in sig for element in formula_elements(formula)})
        names.append(" ".join(name for name, _ in sig if name))
    text_sim = np.zeros((n, n))
    if any(names):
        vectors = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4)).fit_transform(names)
        text_sim = cosine_similarity(vectors)
    dist = np.zeros((n, n))
    for i in range(n):
        for j in range(i):
            w = WEIGHTS["constituent_identity"]
            sim = w * jaccard(exact[i], exact[j])
            if elements[i] and elements[j]:
                w += WEIGHTS["explicit_elements"]
                sim += WEIGHTS["explicit_elements"] * jaccard(elements[i], elements[j])
            if names[i] and names[j]:
                w += WEIGHTS["component_names"]
                sim += WEIGHTS["component_names"] * text_sim[i, j]
            dist[i, j] = dist[j, i] = np.clip(1 - sim / w, 0, 1)
    k = min(8, n - 1)
    xy = UMAP(metric="precomputed", n_neighbors=k, n_components=2, min_dist=.6, init="random",
              n_jobs=1, random_state=SEED).fit_transform(dist).astype(float)
    if not np.isfinite(xy).all():
        raise ValueError("Nonfinite profile coordinates")
    xy -= xy.mean(axis=0)
    # A rigid rotation fits the page; distances and neighborhood membership are invariant.
    _, _, axes = np.linalg.svd(xy, full_matrices=False)
    xy = xy @ axes.T
    for col in range(2):
        if xy[np.argmax(np.abs(xy[:, col])), col] < 0:
            xy[:, col] *= -1
    nearest = [sorted((j for j in range(n) if j != i), key=lambda j: (dist[i,j], j))[:2] for i in range(n)]
    links = []
    for i, neighbors in enumerate(nearest):
        for j in neighbors:
            shared = exact[i] & exact[j]
            if j > i and i in nearest[j] and shared:
                links.append({"left": f"C{i+1:02d}", "right": f"C{j+1:02d}", "distance": float(dist[i,j]),
                              "shared_descriptors": sorted([kind, value] for kind, value in shared)})
    tk = min(5, (n - 1) // 2)
    return xy, {"weights": WEIGHTS, "seed": SEED, "n_neighbors": k, "min_dist": .6, "init": "random",
                "trustworthiness_k": tk, "trustworthiness": float(trustworthiness(
                    dist, xy, metric="precomputed", n_neighbors=tk)),
                "profiles_with_explicit_elements": sum(bool(e) for e in elements), "neighbor_links": links}


def irradiance_value(param):
    val, unit = reported(param["fact"]), clean(reported(param.get("unit")))
    unit = unit.replace(" ", "").replace("−", "-").replace("^", "").replace("·", "")
    factors = {"sun": 1, "suns": 1, "kW/m2": 1, "kWm-2": 1, "W/m2": .001, "Wm-2": .001,
               "mW/cm2": .01, "mWcm-2": .01}
    return round(val * factors[unit], 6) if number(val) and val >= 0 and unit in factors else None


def irradiance(obs, experiment):
    for level, pars in [
        ("observation", obs.get("coordinates", [])),
        ("experiment", [p for light in experiment["energy_inputs"] for p in light["parameters"]]),
    ]:
        found = [p for p in pars if re.search(r"irradian|light intensity|illumination intensity|sun intensity",
                                                clean(p["name"]).casefold())]
        if found:
            values = {irradiance_value(p) for p in found}
            if len(values) == 1 and None not in values:
                return values.pop(), level
            return None, "unresolved_" + level
    return None, "no_scalar_irradiance"


def elapsed_minutes(obs):
    win = obs.get("time_window") or {}
    end, unit = reported(win.get("end")), clean(reported(win.get("unit"))).casefold()
    units = {"min": 1, "minutes": 1, "h": 60, "hour": 60, "hours": 60, "s": 1 / 60}
    if win.get("kind") in {"elapsed", "outdoor_elapsed", "treatment", "endpoint"} and number(end) and unit in units:
        return end * units[unit]
    return None


@dataclass
class Cohort:
    records: dict
    materials: list
    profiles: list
    rows: list
    projection: dict
    input_sha: str


def load(source, selection):
    manifest = json.loads((source / "manifest.json").read_bytes())
    digest = manifest["outputs"]["materials.json"]["sha256"]
    data = json.loads(verified_bytes(source / "materials.json", digest))
    records, mats, groups, rows = {}, [], defaultdict(list), []
    by_record = defaultdict(list)
    for material in data["materials"]:
        if material["role"] in VARIANT_ROLES:
            by_record[material["record_id"]].append(material)
    for rec in data["records"]:
        rid = rec["record_id"]
        doc = json.loads(verified_bytes(Path(rec["record_path"]), rec["record_sha256"]))
        doi = clean(reported(rec["doi"])).casefold()
        exps = {e["id"]: e for e in doc["experiments"]}
        evid = {e["id"]: e for e in doc["evidence"]}
        records[rid] = {"meta": rec, "experiments": exps, "evidence": evid, "doi": doi,
                        "reference_associations": doc.get("reference_associations", [])}
        substances = {s["id"]: clean(reported(s["name"])).casefold() for s in doc["substances"]}
        streams = {s["id"]: s["kind"] for cfg in doc["configurations"] for s in cfg["streams"]}
        selected = {}
        for mat in by_record.pop(rid, []):
            if (mat["snapshot_id"], mat["paper_id"]) != (rec["snapshot_id"], rec["paper_id"]):
                raise ValueError("Material snapshot mismatch")
            sig = signature(mat)
            item = {"uid": mat["uid"], "material_id": mat["material_id"], "record_id": rid, "doi": doi,
                    "role": mat["role"], "name": mat["name"].get("value") or "", "signature": sig,
                    "reported_retained_amounts": sum(reported(c.get("amount")) is not None for c in
                                                   mat["components"] if c["retention"] == "retained")}
            mats.append(item)
            selected[mat["material_id"]] = item
            if sig:
                groups[sig].append(item)
        for obs in doc["observations"]:
            mid = obs.get("material_id")
            if mid not in selected:
                continue
            exp = exps[obs["experiment_id"]]
            if mid not in exp["material_ids"]:
                continue
            metric, purpose = obs["metric_kind"], exp["purpose"]
            target = substances.get(obs.get("substance_id"))
            stream = streams.get(obs.get("stream_id"))
            unit = reported(obs["unit"])
            if metric == "evaporation_rate" and purpose in {"evaporation", "coupled"} and rates_unit(unit):
                outcome = "evaporation"
            elif (metric in ENDPOINTS and purpose in {"catalysis", "coupled"}
                  and target in selection["targets"] and stream in {"feed", "reaction_liquid"}
                  and unit == "%" and any(t["substance_id"] == obs["substance_id"] for t in exp["targets"])):
                outcome = "removal"
            else:
                continue
            value, status = obs["value"].get("value"), obs["value"]["status"]
            accepted = (status == "reported" and obs["origin"] in ORIGINS)
            estimate = (status == "estimated" and obs["origin"] == "plot_estimate")
            if not (number(value) and (accepted or estimate)):
                continue
            sun, basis = irradiance(obs, exp)
            rows.append({"record_id": rid, "doi": doi, "material_id": mid, "uid": selected[mid]["uid"],
                         "material_name": selected[mid]["name"], "experiment_id": exp["id"],
                         "observation_id": obs["id"], "outcome": outcome, "target": target,
                         "stream": stream, "purpose": purpose, "metric_kind": metric, "unit": unit,
                         "value": value, "status": status, "origin": obs["origin"], "irradiance_sun": sun,
                         "irradiance_basis": basis, "endpoint_min": elapsed_minutes(obs), "raw": obs})
    if by_record or len({m["uid"] for m in mats}) != len(mats):
        raise ValueError("Duplicate or unbound material identity")
    aliases = {(a["doi"], a["material_id"]): a["label"] for a in selection.get("profile_labels", [])}
    keys = sorted(groups)
    xy, projection = embed(keys)
    profiles, membership = [], {}
    for index, (sig, coords) in enumerate(zip(keys, xy), 1):
        members = groups[sig]
        code = f"C{index:02d}"
        for m in members:
            m["profile"] = code
            membership[m["uid"]] = code
        names = {aliases[(m["doi"], m["material_id"])] for m in members if (m["doi"], m["material_id"]) in aliases}
        if len(names) > 1:
            raise ValueError(f"Conflicting display names for {code}")
        profiles.append({"id": code, "x": float(coords[0]), "y": float(coords[1]), "signature": sig,
                         "variants": len(members), "papers": len({m["doi"] for m in members}),
                         "label": next(iter(names), ""), "uids": [m["uid"] for m in members]})
    for r in rows:
        r["profile"] = membership.get(r["uid"])
    return Cohort(records, mats, profiles, rows, projection, digest)


def paired_series(cohort, case, series):
    record, = [r for r in cohort.records.values() if r["doi"] == case["doi"]]
    index = {(r["experiment_id"], r["observation_id"]): r for r in cohort.rows if r["doi"] == case["doi"]}
    result = []
    for eid in series["experiments"]:
        exp = record["experiments"][eid]
        for pair in exp["pairs"]:
            if pair["level"] not in {"P1", "P2"} or pair.get("differing_conditions"):
                continue
            a = index.get((eid, pair["left_observation_id"]))
            b = index.get((eid, pair["right_observation_id"]))
            if not a or not b or a["uid"] != b["uid"] or {a["outcome"], b["outcome"]} != {"evaporation", "removal"}:
                continue
            flux, removal = (a, b) if a["outcome"] == "evaporation" else (b, a)
            if removal["target"] != case["target"]:
                continue
            doses = [[reported(c["fact"]) for c in r["raw"].get("coordinates", [])
                      if c["name"] == series["coordinate"] and reported(c.get("unit")) == series["unit"]]
                     for r in (flux, removal)]
            if doses[0] != doses[1] or len(doses[0]) != 1 or not number(doses[0][0]):
                continue
            result.append({"dose": doses[0][0], "evaporation": flux, "removal": removal,
                           "pair": pair, "joint": exp["joint_evidence"]["level"]})
    result.sort(key=lambda r: r["dose"])
    if len(result) < 3 or len({r["dose"] for r in result}) != len(result):
        raise ValueError("A unique numeric operating point at each of at least three doses is required")
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reagent_reference(cohort, case, loading, reagent):
    spec = case["reagent"]
    record, = [r for r in cohort.records.values() if r["doi"] == case["doi"]]
    ref, = [a for a in record["reference_associations"] if a["id"] == spec["reference"]
            and a["relationship"] == "shared_reference" and not a["differing_conditions"]]
    shared, = [p for p in loading if p["removal"]["observation_id"] in ref["observation_ids"]]
    row = shared["removal"]
    if {p["removal"]["uid"] for p in reagent} != {row["uid"]}:
        raise ValueError("Reagent sweep must use the same material as its common reference")
    exp = record["experiments"][row["experiment_id"]]
    dose, = [reported(t["amount"]) for t in exp["reaction_inputs"]
             if t["substance_id"] == spec["reagent_id"] and reported(t["unit"]) == spec["unit"]]
    if not number(dose) or dose in {p["dose"] for p in reagent}:
        raise ValueError("Shared reference dose is ambiguous")
    return {"dose": dose, "removal": row, "association": ref, "loading": shared["dose"]}
