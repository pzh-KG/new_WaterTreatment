"""Analyse study-level recurrence and comparable dual responses on a fixed composition UMAP."""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
import json
from pathlib import Path
import re

from composition_performance_data import (clean, elapsed_minutes, embed, irradiance, irradiance_value,
                                         number, rates_unit, reported, signature, verified_bytes)
from design_rule_data import PARENT, scalar
from plot_material_atlas import VARIANT_ROLES


DUAL_WATER = "Dual-role phase + separate water-supply component"
FE_ABSORBER = "Fe catalytic phase + separate carbon/polymer absorber"
SULFIDE_HOST = "Metal sulfide + polymer/fibre framework"
MOTIFS = [DUAL_WATER, FE_ABSORBER, SULFIDE_HOST]
UNASSIGNED = "Unresolved arm"
CTRL = "Control"
ORGANIC = {"Phenolics","Dyes","Pharmaceuticals","Other organics"}


def norm(x):
    return clean(x).casefold()


def material_features(m, subs, cfg):
    parts, families, roles = [], set(), []
    for c in m["components"]:
        if c["retention"] != "retained":
            continue
        sub = subs[c["substance_id"]]
        name = norm(reported(sub["name"]))
        formula = clean(reported(sub.get("formula")))
        if not name and not formula:
            continue
        family = cfg["component_family"].get(name)
        if family:
            families.add(family)
        text = " | ".join(norm(reported(v)).replace("_"," ") for v in c["roles"] if reported(v))
        flags = {
            "heat":bool(re.search(r"photothermal|photoabsorber|solar absorb|solar absorption|heat generation",text)),
            "cat":bool(re.search(r"cataly|activator|electrode reaction",text)),
            "water":bool(re.search(r"water[- ](?:supply|transport|uptake)|capillary water",text)),
            "iron":name in cfg["iron_components"],"carbon":family in cfg["carbon_absorber_families"]}
        parts.append({"name":name,"formula":formula,"family":family,"roles":text,**flags})
        roles.append(flags)
    tags = {" + ".join(x) for x in combinations(sorted(families),2)}
    motifs = set()
    for i,a in enumerate(roles):
        for j,b in enumerate(roles):
            if i==j:
                continue
            if a["heat"] and a["cat"] and b["water"]:
                motifs.add(DUAL_WATER)
            if a["iron"] and a["cat"] and b["carbon"] and b["heat"]:
                motifs.add(FE_ABSORBER)
    if "Metal sulfide" in families and families.intersection(cfg["framework_families"]):
        motifs.add(SULFIDE_HOST)
    return parts,tags,motifs


def parameter_value(entries, predicate):
    found=[v for v in entries if predicate(norm(v["name"]).replace("_"," "))]
    if not found:
        return None
    values=[(reported(v["fact"]),clean(reported(v.get("unit")))) for v in found]
    if not all(number(a) for a,_ in values) or len(set(values))!=1:
        return (None,"",found)
    return (*values[0],found)


def salinity(obs,e,conf):
    def relevant(n):
        return (bool(re.search(r"salinity|nacl (?:feed )?(?:mass fraction|concentration|setting)|^salt concentration",n))
                and not re.search(r"recipe|stock|prepar|threshold|maximum|minimum",n))
    for source,pars in [("observation",obs.get("coordinates",[])),("experiment",e["conditions"]),
                        ("configuration",conf.get("parameters",[]))]:
        found=parameter_value(pars,relevant)
        if found is None:
            continue
        v,u,fields=found
        if v is None:
            return None,None,"unresolved_"+source
        unit=u.replace(" ","").replace("−","-").casefold()
        if unit in {"wt%","wt.%","mass%","%w/w","w/w%"}:
            factor=1
        elif unit in {"gkg-1","g/kg"}:
            factor=.1
        else:
            return None,None,"incompatible_unit_"+source
        # Unknown brine chemistry cannot silently become NaCl.
        text=" ".join(norm(p["name"])+" "+norm(reported(p.get("basis"))) for p in fields)
        text+=" "+" ".join(norm(reported(p["fact"])) for p in e["conditions"]
                           if norm(p["name"]).replace("_"," ") in {"matrix","feed matrix","water matrix"})
        basis="NaCl" if "nacl" in text or "sodium chloride" in text else "Unspecified salts"
        return round(v*factor,5),basis,source
    return None,None,"not_resolved"


def water_matrix(obs,e):
    for source,items in (("observation",obs.get("coordinates",[])),("experiment",e["conditions"])):
        fields=[q for q in items if norm(q["name"]).replace("_"," ") in {"water matrix","feed matrix","matrix","source matrix","feed water"}]
        for q in fields:
            val=reported(q["fact"])
            if not isinstance(val,str):
                continue
            s=norm(val)
            if "separate" in s and "freshwater" in s and "seawater" in s:
                return None,"mixed_"+source
            if any(k in s for k in ("deionized","deionised","ultrapure","non-saline model-water")):
                if any(norm(p["name"]).replace("_"," ") in {"added salt","ion label","added ion","inorganic anion"}
                       for p in obs.get("coordinates",[])):
                    return "Model water with added solute","observation_intervention"
                if "unless named matrix intervention" in s and any("matrix" in norm(p["name"]) or "addition" in norm(p["name"])
                        for p in obs.get("coordinates",[])):
                    return None,"matrix_intervention_unresolved"
                return "Defined model water",source
            for term,label in [("seawater","Seawater"),("river","River water"),("lake water","Lake water"),
                               ("brine","Brine"),("saline","Saline water"),("freshwater","Freshwater"),
                               ("coking","Industrial water"),("tap water","Tap water")]:
                if term in s:
                    return label,source
            return None,"unclassified_"+source
    return None,"not_resolved"


def light(obs,e,conf,study):
    sun,basis=irradiance(obs,e)
    if sun is not None or basis.startswith("unresolved"):
        return sun,basis
    explicit=[p for p in e["conditions"] if norm(p["name"]).replace("_"," ") in
              {"irradiance","illumination","incident light intensity","solar irradiance","irradiation intensity"}
              and irradiance_value(p) is not None]
    if explicit:
        values={irradiance_value(p) for p in explicit}
        return (values.pop(),"documented_experiment") if len(values)==1 else (None,"unresolved_experiment")
    energy={norm(x["kind"]) for x in e["energy_inputs"]}
    if energy=={"dark"}:
        return 0.0,"explicit_dark"
    if conf.get("id") in study["configuration_irradiance"]:
        fields=[p for p in conf["parameters"] if "irradian" in norm(p["name"])]
        vals={irradiance_value(p) for p in fields}
        if len(vals)==1 and None not in vals:
            return vals.pop(),"documented_configuration"
    return None,basis


def route(obs,e,study,sun):
    family=study["reported_route_family"]
    if e["id"] in study["controls"]:
        return CTRL,"documented_control"
    if "identity unresolved" in family:
        return UNASSIGNED,"oxidant_identity_conflict"
    coords=obs.get("coordinates",[])
    for p in coords:
        n=norm(p["name"]).replace("_"," ")
        v=reported(p["fact"])
        if re.search(r"(?:h2o2|pms|pds|oxidant).*(?:add|dose|concentr|present)|^(?:voltage|applied bias)$",n):
            if v is False or (number(v) and v==0):
                return CTRL,"observation_zero_input"
    if e["id"] in study["mixed_or_uncertain_protocols"]:
        return UNASSIGNED,"mixed_or_unconfirmed_protocol"
    if family=="Photocatalysis" or family=="Thermocatalytic oxidation":
        if sun==0:
            return CTRL,"dark_control"
        if e["id"] in study.get("illumination_protocols", {}):
            return family, "source_reviewed_illumination_protocol"
        if sun is not None or any(re.search("light|solar|irradi|illum",norm(t["kind"])) for t in e["energy_inputs"]):
            return family,"light_protocol_in_original_study"
        return UNASSIGNED,"light_regime_unresolved"
    if family=="Electro-oxidation":
        if any(re.search("electr|bias|potential",norm(t["kind"])) for t in e["energy_inputs"]):
            return family,"electrical_input"
        return UNASSIGNED,"electrical_input_unresolved"
    if family=="Electro-Fenton":
        if any(q["source"]=="in_situ" and re.search("oxidant|peroxide",norm(q["role"])) for q in e["reaction_inputs"]):
            return family,"in_situ_electrochemical_oxidant"
        if any("electr" in norm(t["kind"]) for t in e["energy_inputs"]):
            return family,"electrical_protocol_in_original_study"
        return UNASSIGNED,"operating_route_unresolved"
    sources={"In-situ peroxide":"in_situ","PMS":"external","PDS":"external","External peroxide":"external"}
    candidates=[q for q in e["reaction_inputs"] if re.search("oxidant",norm(q["role"]))
                and not re.search("activator|catalyst|precursor|substrate",norm(q["role"]))]
    for q in candidates:
        dose=reported(q["amount"])
        if q["source"]=="explicit_none" or (number(dose) and dose==0):
            return CTRL,"oxidant_absent"
        if isinstance(dose,list) and 0 in dose:
            return UNASSIGNED,"mixed_oxidant_doses"
    if any(q["source"]==sources.get(family) for q in candidates):
        return family,"documented_reagent_source"
    return UNASSIGNED,"reagent_source_unresolved"


def outcome(obs,stream,organic):
    kind=obs["metric_kind"]
    unit=reported(obs.get("unit"))
    v=scalar(obs)
    if kind=="evaporation_rate":
        if not rates_unit(unit):
            return "Flux unit unresolved",None
        if v is not None and (v<0 or "signed" in norm(reported(obs["metric"]))):
            return "Signed field rate",v
        return "Evaporation flux",v
    if not organic:
        return "Other endpoint",None
    if stream=="condensate":
        return "Condensate endpoint",None
    if stream not in {"feed","reaction_liquid"}:
        return "Stream unresolved",None
    if kind in PARENT|{"phenol_removal","removal_efficiency"} and unit=="%":
        return "Liquid parent removal",v
    if kind=="parent_concentration_ratio" and norm(unit) in {"dimensionless","1","ratio","-"}:
        # Preserve the original ratio; it only describes coverage, never a new P1/P2 link.
        return "Liquid parent ratio",v
    return "Other liquid endpoint",None


def treatment_minutes(obs):
    value=elapsed_minutes(obs)
    if value is not None:
        return value
    window=obs.get("time_window") or {}
    kind=norm(window.get("kind")).replace("_"," ")
    ref=norm(reported(window.get("reference")))
    explicit={"reaction elapsed","plotted reaction time","irradiation endpoint","reported irradiation window",
              "end of described reaction window","plotted endpoint"}
    if kind not in explicit and not (kind=="point" and re.search(r"elapsed.*(?:irradiation|illumination|reaction|treatment)",ref)):
        return None
    factors={"min":1,"minutes":1,"h":60,"hours":60,"s":1/60}
    end=reported(window.get("end"))
    unit=norm(reported(window.get("unit")))
    return end*factors[unit] if number(end) and unit in factors else None


def read_cohort(source,cfg):
    manifest=json.loads((source/"manifest.json").read_bytes())
    sha=manifest["outputs"]["materials.json"]["sha256"]
    if sha!=cfg["materials_sha256"]:
        raise ValueError("Curation is bound to another material snapshot")
    payload=json.loads(verified_bytes(source/"materials.json",sha))
    mats,studies,rows,pairs={}, {}, [], []
    for rec in sorted(payload["records"],key=lambda r:clean(reported(r["doi"])).casefold()):
        doi=norm(reported(rec["doi"])); study=cfg["papers"][doi]
        if study["record_sha256"]!=rec["record_sha256"]:
            raise ValueError("Study record no longer matches the annotations")
        doc=json.loads(verified_bytes(Path(rec["record_path"]),rec["record_sha256"]))
        subs={s["id"]:s for s in doc["substances"]}
        exps={e["id"]:e for e in doc["experiments"]}
        confs={c["id"]:c for c in doc["configurations"]}
        obs={o["id"]:o for o in doc["observations"]}
        evidence={item["id"] for item in doc["evidence"]}
        for eid, refs in study.get("illumination_protocols", {}).items():
            if eid not in exps or not refs or not set(refs)<=evidence:
                raise ValueError("Illumination protocol lacks linked source evidence")
        streams={s["id"]:s["kind"] for c in confs.values() for s in c["streams"]}
        studies[doi]={"record":rec,"doc":doc}
        for m in doc["materials"]:
            if m["role"] not in VARIANT_ROLES|{"necessary_control"}:
                continue
            sig=signature({"components":[{**c,"substance":subs[c["substance_id"]]} for c in m["components"]]})
            parts,tags,motifs=material_features(m,subs,cfg)
            mats[doi,m["id"]]={"doi":doi,"material":m["id"],"name":m["name"].get("value") or m["id"],
                "role":m["role"],"signature":sig,"parts":parts,"families":sorted(tags),"motifs":sorted(motifs),
                "is_final":m["role"] in VARIANT_ROLES,"record_id":rec["record_id"],"evidence_ids":m["evidence_ids"]}
        def make_row(m,e,sid,o,direct):
            rawname=norm(reported(subs[sid]["name"]))
            name=cfg["target_aliases"].get(rawname,rawname) or f"Unresolved [{doi}/{sid}]"
            group=cfg["target_classes"].get(name,"Unclassified")
            conf=confs.get(e.get("configuration_id"),{})
            sun,sbasis=light(o,e,conf,study)
            r,rbasis=route(o,e,study,sun)
            salt,salt_basis,sorigin=salinity(o,e,conf)
            matrix,mbasis=water_matrix(o,e)
            stream=streams.get(o.get("stream_id"))
            endpoint,v=outcome(o,stream,group in ORGANIC) if o.get("id") else ("Protocol only",None)
            return {"doi":doi,"material":m,"experiment":e["id"],"observation":o.get("id"),"target":name,
                    "target_id":sid,"group":group,"family":study["reported_route_family"],"route":r,"route_basis":rbasis,
                    "sun":sun,"sun_basis":sbasis,"salt":salt,"salt_basis":salt_basis,"salt_origin":sorigin,
                    "matrix":matrix,"matrix_basis":mbasis,"endpoint":endpoint,"value":v,
                    "status":o.get("value",{}).get("status"),"time":treatment_minutes(o),"stream":stream,"ownership":direct,
                    "metric_kind":o.get("metric_kind"),"time_window":o.get("time_window"),"coordinates":o.get("coordinates",[]),
                    "raw_value":scalar(o) if o.get("id") else None,"raw_unit":reported(o.get("unit")),
                    "metric_label":reported(o.get("metric")),
                    "is_final":mats[doi,m]["is_final"],"evidence_ids":o.get("evidence_ids") or e["evidence_ids"]}
        by_obs={}
        exp_rows=defaultdict(list)
        for o in obs.values():
            e=exps[o["experiment_id"]]; m=o.get("material_id")
            if (doi,m) not in mats or m not in e["material_ids"] or e["purpose"] not in {"catalysis","coupled"}:
                continue
            ids={t["substance_id"] for t in e["targets"]}
            sid=o.get("substance_id")
            if sid in ids:
                ownership="target observation"
            elif len(ids)==1 and (not sid or o["metric_kind"]=="evaporation_rate"):
                sid=next(iter(ids));ownership="single-target experimental arm"
            else:
                continue
            row=make_row(m,e,sid,o,ownership)
            rows.append(row);by_obs[o["id"]]=row;exp_rows[e["id"]].append(row)
        for e in exps.values():
            if e["purpose"] not in {"catalysis","coupled"}:
                continue
            ids={t["substance_id"] for t in e["targets"]}
            if len(e["material_ids"])==1 and len(ids)==1 and not exp_rows[e["id"]]:
                m=e["material_ids"][0]
                if (doi,m) in mats:
                    rows.append(make_row(m,e,next(iter(ids)),{},"single-material, single-target protocol"))
            for p in e["pairs"]:
                a,b=[obs[x] for x in (p["left_observation_id"],p["right_observation_id"])]
                if b["metric_kind"]=="evaporation_rate":
                    a,b=b,a
                if a["metric_kind"]!="evaporation_rate" or b["id"] not in by_obs:
                    continue
                br=by_obs[b["id"]]
                if br["group"] not in ORGANIC:
                    continue
                ar=by_obs.get(a["id"])
                val=scalar(a)
                own=(a.get("material_id")==b.get("material_id") and a["experiment_id"]==b["experiment_id"]==e["id"])
                valid=(own and p["level"] in {"P1","P2"} and not p["differing_conditions"] and rates_unit(reported(a["unit"]))
                       and number(val) and val>=0 and "signed" not in norm(reported(a["metric"]))
                       and br["endpoint"]=="Liquid parent removal" and number(br["value"]))
                pairs.append({**br,"flux_id":a["id"],"flux":val,"flux_status":a["value"]["status"],"pair_level":p["level"],
                              "pair_note":p,"qualified":bool(valid),"direct_owners":own,"flux_arm":ar["experiment"] if ar else None})
    if set(studies)!=set(cfg["papers"]):
        raise ValueError("Source and reviewed study set differ")
    keys=sorted({m["signature"] for m in mats.values() if m["is_final"] and m["signature"]})
    xy,projection=embed(keys)
    prof={sig:{"id":f"C{i:02}","x":float(x),"y":float(y)} for i,(sig,(x,y)) in enumerate(zip(keys,xy),1)}
    for m in mats.values():
        m["profile"]=prof.get(m["signature"],{}).get("id")
    for r in rows+pairs:
        r["profile"]=mats[r["doi"],r["material"]]["profile"]
    return mats,studies,rows,pairs,prof,projection


ALL = "All formulations"
STAGES = ("light","nacl","model","loose_nacl","dual_nacl","dual_model")


def all_tags(m):
    return {ALL,*m["families"],*m["motifs"]}


def independent_screen(mats, rows, pairs, cfg):
    """Compare two actual material arms across DOIs before collapsing to one vote per DOI pair."""
    good={(p["doi"],p["observation"]):p for p in pairs if p["qualified"]}
    strata=defaultdict(lambda:{"papers":set(),"profiles":set(),**{s:set() for s in STAGES}})
    designs=defaultdict(dict)
    for r in rows:
        if not r["is_final"] or r["route"] in {UNASSIGNED,CTRL} or r["group"] not in ORGANIC:
            continue
        m=mats[r["doi"],r["material"]]
        if not m["profile"]:
            continue
        base=(r["target"],r["route"])
        for tag in all_tags(m):
            s=strata[(*base,tag)]
            s["papers"].add(r["doi"])
            s["profiles"].add(m["profile"])
        if r["sun"] is None or r["sun"]<=0:
            continue
        k=(r["doi"],r["material"],r["sun"],r["salt"],r["salt_basis"],r["matrix"])
        arm=designs[base].setdefault(k,{"row":r,"tags":all_tags(m),"pairs":[]})
        if (r["doi"],r["observation"]) in good:
            arm["pairs"].append(good[r["doi"],r["observation"]])
    match=cfg["matching"]
    witnesses={}
    for base,entries in designs.items():
        for a,b in combinations(entries.values(),2):
            p,q=a["row"],b["row"]
            if p["doi"]==q["doi"] or p["profile"]==q["profile"]:
                continue
            ds=abs(p["sun"]-q["sun"])
            if ds>match["sensitivity_sun"]+1e-8:
                continue
            salt_known=p["salt"] is not None and q["salt"] is not None and p["salt_basis"]==q["salt_basis"]=="NaCl"
            salt_diff=abs(p["salt"]-q["salt"]) if salt_known else None
            levels=[]
            if ds<=match["sun_tolerance"]+1e-8:
                levels.append("light")
                if salt_known and salt_diff<=match["nacl_wt_percent_points"]+1e-8:
                    levels.append("nacl")
                if p["matrix"]==q["matrix"]=="Defined model water":
                    levels.append("model")
            if salt_known and salt_diff<=match["sensitivity_nacl_wt_percent_points"]+1e-8:
                levels.append("loose_nacl")
            pair_witness=next(((x,y) for x in a["pairs"] for y in b["pairs"]
                               if x["time"] is not None and x["time"]==y["time"]),None)
            if pair_witness:
                levels += ["dual_"+lv for lv in ("nacl","model") if lv in levels]
            paper_pair=tuple(sorted((p["doi"],q["doi"])))
            for tag in a["tags"]&b["tags"]:
                for level in levels:
                    strata[(*base,tag)][level].add(paper_pair)
                    key=(*base,tag,level,*paper_pair)
                    pp,qq=pair_witness if level.startswith("dual_") else (p,q)
                    if key not in witnesses:
                        witnesses[key]=[pp,qq]
    return strata,witnesses


def material_comparisons(studies,mats,pairs,rows):
    """Find source-defined comparative series before examining effect direction."""
    qualified=defaultdict(list)
    for p in pairs:
        if p["qualified"]:
            qualified[p["doi"]].append(p)
    by_doi=defaultdict(list)
    for r in rows:
        by_doi[r["doi"]].append(r)
    contrasts,split=[],[]
    for doi,study in studies.items():
        doc=study["doc"]
        for c in doc["comparisons"]:
            refs=set(c["observation_ids"])
            inputs=[p for p in qualified[doi] if {p["flux_id"],p["observation"]}<=refs
                    and p["material"] in c["material_ids"]]
            for a,b in combinations(inputs,2):
                if a["material"]==b["material"] or a["target"]!=b["target"] or a["route"]!=b["route"]:
                    continue
                if (a["route"] in {CTRL,UNASSIGNED} or a["sun"] is None or a["sun"]!=b["sun"] or
                        a["time"] is None or a["time"]!=b["time"] or a["matrix"]!=b["matrix"]):
                    continue
                if (a["salt"],a["salt_basis"])!=(b["salt"],b["salt_basis"]):
                    continue
                x,y=mats[doi,a["material"]],mats[doi,b["material"]]
                # Orient control-to-final, or low-to-high on a documented preparation dose.
                orientation=None
                if x["is_final"]!=y["is_final"]:
                    if x["is_final"]:
                        a,b,x,y=b,a,y,x
                    orientation="source comparator to final"
                else:
                    ao=next(o for o in doc["observations"] if o["id"]==a["observation"])
                    bo=next(o for o in doc["observations"] if o["id"]==b["observation"])
                    def doses(o):
                        return {(norm(v["name"]),reported(v.get("unit"))):reported(v["fact"])
                                for v in o.get("coordinates",[]) if re.search("nominal|preparation",norm(v["name"]))
                                and number(reported(v["fact"])) and reported(v.get("unit"))}
                    ad,bd=doses(ao),doses(bo)
                    terms=[k for k in ad.keys()&bd.keys() if ad[k]!=bd[k]]
                    if len(terms)==1:
                        k=terms[0]
                        if ad[k]>bd[k]:
                            a,b,x,y=b,a,y,x
                        orientation="increasing "+k[0]+" ("+k[1]+")"
                if not orientation:
                    continue
                contrasts.append({"doi":doi,"comparison":c["id"],"operation":reported(c["operation"]),
                    "orientation":orientation,"from_material":a["material"],"to_material":b["material"],
                    "from_flux_id":a["flux_id"],"to_flux_id":b["flux_id"],"from_parent_id":a["observation"],
                    "to_parent_id":b["observation"],"target":a["target"],"route":a["route"],"sun":a["sun"],
                    "matrix":a["matrix"],"salt":a["salt"],"time":a["time"],"from_flux":a["flux"],"to_flux":b["flux"],
                    "from_removal":a["value"],"to_removal":b["value"],"delta_flux":round(b["flux"]-a["flux"],5),
                    "delta_removal_pp":round(b["value"]-a["value"],5),
                    "any_plot_estimate":"estimated" in (a["flux_status"],b["flux_status"],a["status"],b["status"]),
                    "newly_documented_tags":sorted(all_tags(y)-all_tags(x)),
                    "shared_tags":sorted((all_tags(y)&all_tags(x))-{ALL}),"confounders":c.get("confounders",[])})
            # Index separate tests as a distinct evidence tier, without joining their numbers.
            liquid=defaultdict(set); flux=defaultdict(set)
            for r in by_doi[doi]:
                if r["observation"] in refs and r["endpoint"] in {"Liquid parent removal","Liquid parent ratio"} and number(r["value"]):
                    liquid[r["target"]].add(r["material"])
            for o in doc["observations"]:
                if (o["id"] in refs and o.get("material_id") in c["material_ids"] and o["metric_kind"]=="evaporation_rate"
                        and rates_unit(reported(o["unit"])) and number(scalar(o)) and scalar(o)>=0):
                    flux[o["material_id"]].add(o["id"])
            for target,material_ids in liquid.items():
                covered=sorted(material_ids&flux.keys()&set(c["material_ids"]))
                if len(covered)>=2:
                    split.append({"doi":doi,"comparison":c["id"],"operation":reported(c["operation"]),"target":target,
                                  "materials_with_both_test_types":covered,"source_observations":sorted(refs)})
    return contrasts,split


def formulation_series(studies,pairs,mats):
    """Recover nominal recipe sweeps, including paper-labelled removals with unknown stream."""
    catalogue=[]
    by_doi=defaultdict(list)
    for p in pairs:
        if p["direct_owners"] and p["pair_level"] in {"P1","P2"} and not p["pair_note"]["differing_conditions"]:
            by_doi[p["doi"]].append(p)
    for doi,study in studies.items():
        doc=study["doc"]
        experiments={e["id"]:e for e in doc["experiments"]}
        observations={o["id"]:o for o in doc["observations"]}
        for cmp in doc["comparisons"]:
            refs=set(cmp["observation_ids"]); series=defaultdict(list)
            for p in by_doi[doi]:
                if (p["material"] not in cmp["material_ids"] or not {p["flux_id"],p["observation"]}<=refs
                        or p["route"] in {UNASSIGNED,CTRL} or p["endpoint"] not in {"Liquid parent removal","Stream unresolved"}):
                    continue
                parent=observations[p["observation"]]; flux=observations[p["flux_id"]]
                if (parent["metric_kind"] not in PARENT|{"phenol_removal","removal_efficiency"} or
                        reported(parent["unit"])!="%" or not number(scalar(parent)) or not number(p["flux"]) or p["flux"]<0
                        or not rates_unit(reported(flux["unit"])) or "signed" in norm(reported(flux["metric"]))):
                    continue
                e=experiments[p["experiment"]]
                terms=[v for v in [*parent.get("coordinates",[]),*e["conditions"]]
                       if "nominal" in norm(v["name"]) and re.search("prepar|charge|load|input",norm(v["name"]))
                       and number(reported(v["fact"])) and norm(reported(v["unit"])) in {"g","mg","µg","μg"}]
                doses={(v["name"],reported(v["fact"]),reported(v["unit"])) for v in terms}
                if len(doses)!=1:
                    continue
                term,dose,unit=doses.pop()
                m=mats[doi,p["material"]]
                series[p["target"],p["route"],term,unit].append(dict(
                    doi=doi,comparison=cmp["id"],material=p["material"],name=m["name"],profile=m["profile"],
                    experiment=p["experiment"],flux_id=p["flux_id"],parent_id=p["observation"],dose=dose,dose_unit=unit,
                    parameter=term,flux=p["flux"],flux_status=p["flux_status"],removal=scalar(parent),
                    removal_status=p["status"],stream=p["stream"],endpoint=p["endpoint"],minutes=p["time"],sun=p["sun"],
                    salt=p["salt"],salt_basis=p["salt_basis"],matrix=p["matrix"],strict_pair=p["qualified"],
                    evidence_ids=sorted(set(parent["evidence_ids"]+flux["evidence_ids"])),motifs=m["motifs"],is_final=m["is_final"]))
            for (target,route,term,unit),items in series.items():
                positive=[p for p in items if p["dose"]>0 and p["is_final"]]
                if len({p["dose"] for p in items})<3 or len({p["material"] for p in positive})<2:
                    continue
                if len({p["dose"] for p in items})!=len(items):
                    # Parallel conditions at one recipe level need their own comparison design.
                    continue
                for p in sorted(items,key=lambda v:v["dose"]):
                    catalogue.append(dict(target=target,route=route,**p))
    return catalogue
