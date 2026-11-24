"""Build a source-bound case table and an outcome-blind material–treatment UMAP."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import trustworthiness
from sklearn.metrics.pairwise import cosine_similarity
from umap import UMAP

from composition_performance_data import (ENDPOINTS, clean, elapsed_minutes, formula_elements, irradiance,
                                         jaccard, number, rates_unit, reported, signature, verified_bytes)
from plot_material_space import ORIGINS


PARENT = ENDPOINTS | {"uv_based_degradation_efficiency", "parent_removal_efficiency",
                      "rhb_parent_concentration_removal"}
WEIGHTS = {"material": .40, "pollutant": .18, "activation": .18, "light": .09, "water": .10, "assay": .05}


def scalar(obs):
    value = obs["value"]
    ok = ((value["status"] == "reported" and obs["origin"] in ORIGINS)
          or (value["status"] == "estimated" and obs["origin"] == "plot_estimate"))
    return value.get("value") if ok and number(value.get("value")) else None


def value_unit(items, pattern):
    fields = [q for q in items if re.fullmatch(pattern, clean(q["name"]).casefold().replace("_", " "))]
    values = [(reported(q["fact"]), clean(reported(q.get("unit")))) for q in fields]
    if not values or not all(number(v) for v,_ in values):
        return None, ""
    unique = set(values)
    return unique.pop() if len(unique) == 1 else (None, "")


def conditions(material, exp, obs, target, case, paper, substance):
    pars, coords = exp["conditions"], obs.get("coordinates", [])
    sun, sun_basis = irradiance(obs, exp)
    energy = [str(q["kind"]).casefold() for q in exp["energy_inputs"]]
    light = ("dark" if sun == 0 or "dark" in energy else "illuminated"
             if sun is not None or any(re.search("light|solar|irrad|illum", e) for e in energy) else None)
    matrix_raw = [reported(q["fact"]) for q in (coords + pars)
                  if clean(q["name"]).casefold().replace("_"," ") in {"water matrix","feed matrix","matrix"}
                  and isinstance(reported(q["fact"]), str)]
    matrix = None
    if matrix_raw:
        m = matrix_raw[0].lower()
        if "river" in m:
            matrix = "River water"
        elif m in {"freshwater","seawater"}:
            synthetic = any(re.search("simulated|synthetic",s,re.I) for s in matrix_raw)
            matrix = ("Simulated "+m) if synthetic else m.capitalize()
        elif "brine" in m or "synthetic saline" in m:
            matrix = "Synthetic brine"
        elif any(w in m for w in ("deionized","ultrapure","non-saline model")):
            matrix = "Model water"
    sal, sal_unit = value_unit(pars, "nacl (salinity|concentration)")
    sal = sal if sal_unit in {"wt%", "wt.%", "wt %"} else None
    temp, temp_unit = value_unit(pars, "bulk reaction temperature|reaction temperature")
    temp = temp if temp_unit in {"°C", "℃"} else None
    recipe, recipe_unit = None, ""
    recipe_key = paper.get("recipe_parameter")
    if recipe_key:
        prep = [p for step in material["preparation"] for p in step["parameters"]]
        recipe, recipe_unit = value_unit(prep, re.escape(recipe_key.replace("_"," ").casefold()))
    concentration, cunit = (reported(target["concentration"]), clean(reported(target.get("unit")))) if target else (None,"")
    cv, cu = value_unit(coords, "initial tc concentration|initial concentration")
    if cv is not None:
        concentration, cunit = cv, cu
    if not number(concentration) or cunit.replace(" ","").replace("−","-") not in {"mgL-1","mg/L"}:
        concentration = None
    ox = [q for q in exp["reaction_inputs"] if re.search("oxidant",q["role"],re.I)
          and substance.get(q["substance_id"]) and re.search("peroxymonosulfate|hydrogen peroxide",
                                                            substance[q["substance_id"]],re.I)]
    reagent, dose, dose_unit = None, None, ""
    if len(ox) == 1:
        q = ox[0]
        reagent = "PMS" if "peroxymonosulfate" in substance[q["substance_id"]].lower() else "H₂O₂"
        dose, dose_unit = reported(q["amount"]), clean(reported(q.get("unit")))
        v, u = value_unit(coords, "pms concentration|external pms reagent amount")
        if v is not None:
            dose, dose_unit = v, u
        if not number(dose) or not dose_unit:
            dose, dose_unit = None, ""
    return {"target_mg_l": concentration, "route": case["route"], "reagent": reagent, "dose": dose,
            "dose_unit": dose_unit, "sun": sun, "sun_basis": sun_basis, "light": light,
            "matrix": matrix, "matrix_source": matrix_raw, "salt_wt": sal, "temp_c": temp,
            "time_min": elapsed_minutes(obs), "recipe": recipe, "recipe_unit": recipe_unit,
            "recipe_key": recipe_key if recipe is not None else None}


def bind_pair(anchor, exp, obs, target, stream):
    """A candidate pair can contribute a single endpoint if the other one is conflicted."""
    rate, removal, binding = None, None, None
    if anchor["metric_kind"] == "evaporation_rate" and rates_unit(reported(anchor["unit"])):
        rate = anchor
    elif (anchor["metric_kind"] in PARENT and reported(anchor["unit"]) == "%" and target
          and stream in {"feed","reaction_liquid"}):
        removal = anchor
        matches = []
        for pair in exp["pairs"]:
            ids = {pair["left_observation_id"], pair["right_observation_id"]}
            if anchor["id"] not in ids or pair["level"] not in {"P1","P2"} or pair["differing_conditions"]:
                continue
            other, = ids - {anchor["id"]}
            o = obs[other]
            if (o["experiment_id"] == exp["id"] and o.get("material_id") == anchor["material_id"]
                    and o["metric_kind"] == "evaporation_rate"):
                matches.append((o,pair))
        if len(matches) > 1:
            raise ValueError(f"Multiple eligible pair owners for {anchor['id']}")
        if matches:
            rate, binding = matches[0]
    else:
        raise ValueError(f"Unsupported case anchor {anchor['id']}")
    return rate, removal, binding


def load_cases(source, cfg):
    manifest = json.loads((source/"manifest.json").read_bytes())
    sha = manifest["outputs"]["materials.json"]["sha256"]
    if sha != cfg["source_snapshot_sha256"]:
        raise ValueError("Selection is bound to a different material export")
    payload = json.loads(verified_bytes(source/"materials.json", sha))
    catalog = {clean(reported(r["doi"])).casefold():r for r in payload["records"]}
    rows, papers = [], {}
    for paper in cfg["studies"]:
        doi = paper["doi"]
        rec = catalog[doi]
        if paper["record_sha256"] != rec["record_sha256"]:
            raise ValueError("Selected record has changed")
        d = json.loads(verified_bytes(Path(rec["record_path"]),rec["record_sha256"]))
        mats = {m["id"]:m for m in d["materials"]}
        exps = {e["id"]:e for e in d["experiments"]}
        obs = {o["id"]:o for o in d["observations"]}
        subs = {s["id"]:s for s in d["substances"]}
        names = {k:clean(reported(v["name"])) for k,v in subs.items()}
        streams = {s["id"]:s["kind"] for c in d["configurations"] for s in c["streams"]}
        ev = {e["id"]:e for e in d["evidence"]}
        papers[doi] = {"record": rec, "comparisons": {c["id"]:c for c in d["comparisons"]},
                       "references": d.get("reference_associations",[])}
        for case in paper["arms"]:
            a = obs[case["anchor"]]
            e, m = exps[a["experiment_id"]], mats[a["material_id"]]
            if m["id"] not in e["material_ids"] or scalar(a) is None:
                raise ValueError("Selected observation lacks material ownership or a numeric outcome")
            sid = a.get("substance_id")
            target = next((t for t in e["targets"] if sid and sid == t["substance_id"]), None)
            pollutant = names[sid].casefold() if target else None
            if not pollutant and (e["targets"] or case["route"] != "Brine test"):
                raise ValueError("Ambiguous target at selected anchor")
            st = streams.get(a.get("stream_id"))
            rate, removal, pair = bind_pair(a,e,obs,target,st)
            v = scalar(rate) if rate and rates_unit(reported(rate["unit"])) else None
            u = scalar(removal) if removal else None
            is_paired = bool(pair and v is not None and v>=0 and u is not None
                             and "signed" not in clean(reported(rate["metric"])).casefold())
            sig = signature({"components":[{**c,"substance":subs[c["substance_id"]]} for c in m["components"]]})
            if not sig:
                raise ValueError("Material lacks documented retained components")
            context = conditions(m,e,a,target,case,paper,names)
            used = set(e["evidence_ids"] + a["evidence_ids"] + (rate or {}).get("evidence_ids", [])
                       + (pair or {}).get("evidence_ids",[]) + m["evidence_ids"])
            row = {"key":case["key"],"doi":doi,"record_id":rec["record_id"],"snapshot_id":rec["snapshot_id"],
                   "material":m["id"],"material_name":reported(m["name"]) or m["id"],"role":m["role"],
                   "study_label":paper["label"],"experiment":e["id"],"anchor":a["id"],"signature":sig,
                   "target":pollutant, "target_class":cfg["target_classes"][pollutant] if pollutant else "Brine",
                   "flux":v,"removal":u,"flux_status":rate["value"]["status"] if rate else None,
                   "flux_unit_status":rate["unit"]["status"] if rate else None,
                   "removal_status":removal["value"]["status"] if removal else None,"paired":is_paired,
                   "paired60":is_paired and context["time_min"] == 60,"pair_level":pair["level"] if pair else None,
                   "flux_id":rate["id"] if rate else None,"removal_id":removal["id"] if removal else None,
                   "stream":st,**context,
                   "source":{"observation":a,"flux_observation":rate,"experiment":e,"pair":pair,
                             "material":m,"evidence":{i:ev[i] for i in sorted(used)}}}
            rows.append(row)
    if len({r["key"] for r in rows}) != len(rows) or len({(r["doi"],r["anchor"]) for r in rows}) != len(rows):
        raise ValueError("Duplicate case or owner")
    return rows, papers


def check_contrasts(rows, papers, cfg):
    index = {r["key"]:r for r in rows}
    for c in cfg["material_paths"]+cfg["operating_contrasts"]:
        cases = [index[k] for k in c["arms"]]
        doi, = {r["doi"] for r in cases}
        comparison = papers[doi]["comparisons"][c["comparison"]]
        if len({r["target"] for r in cases}) > 1:
            raise ValueError("Contrast cannot cross pollutants")
        for r in cases:
            ids = {v for v in (r["flux_id"],r["removal_id"]) if v}
            if not ids.issubset(comparison["observation_ids"]):
                raise ValueError(f"{r['key']} is outside the source-owned comparison")
        if c in cfg["material_paths"] and not all(r["paired60"] for r in cases):
            raise ValueError("Material contrast requires two outcomes on each 60-minute arm")
        if "focus" in c and not set(c["focus"]).issubset(c["arms"]):
            raise ValueError("Displayed change must stay inside the cited comparison")
        if c in cfg["operating_contrasts"] and len({r["material"] for r in cases}) != 1:
            raise ValueError("Operating contrast changed the material")


def average(parts):
    known = [(w,v) for w,v in parts if v is not None]
    return sum(w*v for w,v in known)/sum(w for w,_ in known) if known else None


def categorical(a,b):
    return float(a!=b) if a is not None and b is not None else None


def ingredient_distances(sigs):
    names = [" ".join(s for s,_ in sig if s) for sig in sigs]
    vectors = TfidfVectorizer(analyzer="char_wb",ngram_range=(2,4)).fit_transform(names)
    text = cosine_similarity(vectors)
    exact = [{(k,v) for a,b in sig for k,v in (("name",a),("formula",b)) if v} for sig in sigs]
    elem = [{k for _,f in sig for k in formula_elements(f)} for sig in sigs]
    result = {}
    for i,a in enumerate(sigs):
        for j,b in enumerate(sigs):
            parts = [(.5,1-jaccard(exact[i],exact[j])),(.2,1-text[i,j] if names[i] and names[j] else None),
                     (.3,1-jaccard(elem[i],elem[j]) if elem[i] and elem[j] else None)]
            result[a,b] = float(np.clip(average(parts),0,1))
    return result


def fit(rows):
    # IDs, observations, evidence quality and performance never enter the distances.
    fields = ["signature","recipe_key","recipe_unit","recipe","target","target_class","target_mg_l",
              "route","reagent","dose_unit","dose","light","sun","matrix","salt_wt","temp_c","time_min"]
    signatures = defaultdict(list)
    for r in rows:
        recipe_scope = r["doi"] if r["recipe_key"] else None
        values = [r[f] for f in fields] + [recipe_scope]
        signatures[json.dumps(values, ensure_ascii=False)].append(r)
    groups = [signatures[k] for k in sorted(signatures)]
    reps = [g[0] for g in groups]
    n = len(reps)
    chem = ingredient_distances(sorted({r["signature"] for r in reps}))
    ranges = defaultdict(list)

    def domain(r,f):
        if f=="recipe":
            return r["doi"],r["recipe_key"],r["recipe_unit"]
        if f=="dose":
            return r["reagent"],r["dose_unit"]
        if f=="target_mg_l":
            return r["target"]
        return None

    for r in reps:
        for f in ("recipe","dose","target_mg_l","sun","salt_wt","temp_c","time_min"):
            if r[f] is not None:
                ranges[f,domain(r,f)].append(r[f])

    def delta(a,b,f):
        context = domain(a,f)
        if a[f] is None or b[f] is None or context != domain(b,f):
            return None
        span = np.ptp(ranges[f,context])
        return abs(a[f]-b[f])/float(span) if span else 0.0

    matrices = {key:np.full((n,n),np.nan) for key in WEIGHTS}
    for i,a in enumerate(reps):
        for j,b in enumerate(reps[:i]):
            blocks = {
                "material":average([(.76,chem[a["signature"],b["signature"]]),(.24,delta(a,b,"recipe"))]),
                "pollutant":average([(.52,categorical(a["target"],b["target"])),(.32,categorical(a["target_class"],b["target_class"])),
                                     (.16,delta(a,b,"target_mg_l"))]),
                "activation":average([(.7,categorical(a["route"] if a["route"]!="Route unresolved" else None,
                                                       b["route"] if b["route"]!="Route unresolved" else None)),
                                      (.3,delta(a,b,"dose"))]),
                "light":average([(.75,delta(a,b,"sun")),(.25,categorical(a["light"],b["light"]))]),
                "water":average([(.5,categorical(a["matrix"],b["matrix"])),(.5,delta(a,b,"salt_wt"))]),
                "assay":average([(.5,delta(a,b,"temp_c")),(.5,delta(a,b,"time_min"))])}
            for field,val in blocks.items():
                if val is not None:
                    matrices[field][i,j] = matrices[field][j,i] = val

    def distances(weights):
        measured = sum(w*np.isfinite(matrices[k]) for k,w in weights.items())
        num = sum(w*np.nan_to_num(matrices[k]) for k,w in weights.items())
        return np.divide(num,measured,out=np.zeros_like(num),where=measured>0), measured

    dist, common = distances(WEIGHTS)
    k = min(9,n-1)
    xy = UMAP(metric="precomputed", n_neighbors=k, min_dist=.55, n_components=2, init="random",
              random_state=42, n_jobs=1).fit_transform(dist).astype(float)
    if not np.isfinite(xy).all():
        raise ValueError("Invalid mixture projection")
    xy -= xy.mean(axis=0)
    _,_,rotation = np.linalg.svd(xy, full_matrices=False)
    xy = xy @ rotation.T
    for col in range(2):
        if xy[np.argmax(np.abs(xy[:,col])),col] < 0:
            xy[:,col] *= -1
    for g,(x,y) in zip(groups,xy):
        for r in g:
            r.update(x=float(x),y=float(y),shared_position=len(g))
    kn = min(5,(n-1)//2)
    nearest = lambda m: [set(np.argsort(np.where(np.arange(n)==i,np.inf,m[i]),kind="stable")[:kn]) for i in range(n)]
    reference = nearest(dist)
    checks = {}
    for w in (.30,.50):
        alt = {k:(w if k=="material" else v*(1-w)/(1-WEIGHTS["material"])) for k,v in WEIGHTS.items()}
        alt_near = nearest(distances(alt)[0])
        checks[str(w)] = round(float(np.mean([len(a & b)/kn for a,b in zip(reference,alt_near)])),3)
    return {"features":fields,"groups":n,"weights":WEIGHTS,"n_neighbors":k,"min_dist":.55,"seed":42,
            "k":kn,"trustworthiness":float(trustworthiness(dist,xy,metric="precomputed",n_neighbors=kn)),
            "neighbor_retention_at_alternative_material_weights":checks,
            "min_comparable_weight":float(common[np.triu_indices(n,1)].min())}
