"""Export the full-cohort comparison with traceable experiments, witnesses and offline figures."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

from cross_study_atlas import (
    ALL, MOTIFS, STAGES, all_tags, formulation_series, independent_screen,
    material_comparisons, read_cohort, reported,
)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def table(path, data, fields=None):
    if fields is None:
        fields = list(dict.fromkeys(k for row in data for k in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in data:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v,(dict,list,tuple)) else v
                             for k,v in row.items()})


def select_cases(strata, target, routes):
    """Use the most frequent documented role architecture in each requested target/route stratum."""
    cases = []
    for route in routes:
        keys = [k for k in strata if k[0:2] == (target, route) and k[2] in MOTIFS]
        if not keys:
            raise ValueError(f"No recorded role architecture in {target}, {route}")
        key = min(keys, key=lambda k: (-len(strata[k]["papers"]), -len(strata[k]["light"]), k[2]))
        cases.append(key)
    return cases


def export_tables(out, annotations, source):
    cfg = json.loads(annotations.read_bytes())
    mats, studies, rows, pairs, profiles, projection = read_cohort(source, cfg)
    strata, witnesses = independent_screen(mats, rows, pairs, cfg)
    contrasts, separate_tests = material_comparisons(studies, mats, pairs, rows)
    recipe_series=formulation_series(studies,pairs,mats)
    finals = [m for m in mats.values() if m["is_final"]]
    if any(not m["profile"] for m in finals) or {m["doi"] for m in finals} != set(studies):
        raise ValueError("Some cohort papers or final variants lack composition coordinates")
    codes = {doi: f"S{i:02}" for i,doi in enumerate(studies,1)}
    papers = []
    for doi, study in studies.items():
        rec = study["record"]
        papers.append(dict(code=codes[doi],doi=doi,title=reported(rec["title"]),paper_id=rec["paper_id"],
                           record_id=rec["record_id"],snapshot_id=rec["snapshot_id"],record_path=rec["record_path"],
                           record_sha256=rec["record_sha256"],cohort_role=rec["dataset_role"],sources=rec["sources"],
                           reported_route_family=cfg["papers"][doi]["reported_route_family"],
                           route_reference=cfg["papers"][doi]["route_source"]))
    table(out/"studies.csv", papers)
    table(out/"material-variants.csv", list(mats.values()))
    table(out/"target-observations.csv", rows)
    table(out/"source-pairs.csv", pairs)
    table(out/"within-study-contrasts.csv", contrasts)
    table(out/"separate-test-coverage.csv", separate_tests)
    table(out/"formulation-dose-series.csv",recipe_series)
    evidence_rows=[]
    protocol_rows=[]
    for doi,study in studies.items():
        doc=study["doc"]
        for e in doc["evidence"]:
            evidence_rows.append(dict(doi=doi,**e))
        for e in doc["experiments"]:
            if e["purpose"] in {"coupled","catalysis","evaporation"}:
                protocol_rows.append(dict(doi=doi,**{k:e.get(k) for k in (
                    "id","purpose","configuration_id","material_ids","targets","conditions","energy_inputs","reaction_inputs","evidence_ids")}))
    table(out/"source-evidence.csv",evidence_rows)
    table(out/"experimental-protocols.csv",protocol_rows)

    by_tag = defaultdict(set)
    by_profile = []
    for m in finals:
        for tag in all_tags(m)-{ALL}:
            by_tag[tag].add(m["doi"])
    for sig,p in profiles.items():
        members = [m for m in finals if m["profile"]==p["id"]]
        by_profile.append(dict(**p, ingredients=sig, papers=sorted({m["doi"] for m in members}),
                               variants=[[m["doi"],m["material"]] for m in members]))
    table(out/"umap-coordinates.csv", by_profile)
    table(out/"cohort-component-combinations.csv", [
        dict(combination=t,kind="functional architecture" if t in MOTIFS else "component-family pair",
             paper_count=len(ds),papers=sorted(ds)) for t,ds in sorted(by_tag.items(),key=lambda x:(-len(x[1]),x[0]))])
    table(out/"stratified-recurrence.csv", [
        dict(target=t,operating_route=r,combination=tag,all_papers_in_stratum=len(strata[t,r,ALL]["papers"]),
             papers_with_combination=len(s["papers"]),distinct_profiles=len(s["profiles"]),
             **{k+"_doi_pairs":len(s[k]) for k in STAGES},papers=sorted(s["papers"]),profiles=sorted(s["profiles"]))
        for (t,r,tag),s in sorted(strata.items())])
    witness_rows=[]
    for (target,route,tag,stage,d1,d2), records in sorted(witnesses.items()):
        a,b=sorted(records,key=lambda r:r["doi"])
        if (a["doi"],b["doi"])!=(d1,d2) or a["profile"]==b["profile"]:
            raise ValueError("Mismatched cross-paper witness")
        w=dict(target=target,route=route,combination=tag,stage=stage)
        for side,r in (("a",a),("b",b)):
            for k in ("doi","material","profile","experiment","observation","sun","sun_basis","salt","salt_basis",
                      "matrix","endpoint","time","evidence_ids","flux_id","pair_level"):
                w[k+"_"+side]=r.get(k)
        witness_rows.append(w)
    table(out/"cross-paper-witnesses.csv",witness_rows)
    return dict(cfg=cfg,mats=mats,studies=studies,rows=rows,pairs=pairs,profiles=by_profile,projection=projection,
                strata=strata,witnesses=witnesses,contrasts=contrasts,by_tag=by_tag,papers=papers,codes=codes,
                recipe_series=recipe_series)


def write_explorer(out, data):
    finals=[m for m in data["mats"].values() if m["is_final"]]
    mids={(m["doi"],m["material"]):i for i,m in enumerate(finals)}
    pfields=("profile","name","material","doi","motifs","families","parts")
    payload={"profiles":[{k:p[k] for k in ("id","x","y")} for p in data["profiles"]],
             "links":[[p["left"],p["right"]] for p in data["projection"]["neighbor_links"]],
             "materials":[{k:m[k] for k in pfields} for m in finals],
             "studies":{p["doi"]:[p["code"],p["title"]] for p in data["papers"]},
             "motifs":MOTIFS,"match":data["cfg"]["matching"]}
    pair_by_obs=defaultdict(list)
    for p in data["pairs"]:
        if p["qualified"] and p["is_final"]:
            pair_by_obs[p["doi"],p["material"],p["observation"]].append([p["flux"],p["flux_id"],p["pair_level"],p["flux_status"]])
    fields=("target","group","route","sun","salt","salt_basis","matrix","endpoint","value","time","experiment",
            "observation","status","stream","evidence_ids","raw_value","raw_unit","metric_label")
    payload["fields"]=["mid",*fields,"pairs"]
    payload["rows"]=[[mids[r["doi"],r["material"]],*[r[k] for k in fields],
                      pair_by_obs[r["doi"],r["material"],r["observation"]]] for r in data["rows"] if r["is_final"]]
    template=Path(__file__).with_name("cross_study_explorer.html").read_text(encoding="utf-8")
    content=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(",",":")).replace("<",r"\u003c")
    (out/"material-explorer.html").write_text(template.replace("__ATLAS_DATA__",content),encoding="utf-8")


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,required=True)
    ap.add_argument("--annotations",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--target",required=True)
    ap.add_argument("--routes",nargs="+",required=True)
    ap.add_argument("--detail-route",required=True)
    ap.add_argument("--sun",type=float,default=1.0)
    ap.add_argument("--minutes",type=float,default=60.0)
    args=ap.parse_args()
    out=args.out
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"Use a new output directory to preserve previous research exports: {out}")
    out.mkdir(parents=True,exist_ok=True)
    data=export_tables(out,args.annotations,args.source)
    cases=select_cases(data["strata"],args.target,args.routes)
    from plot_cross_study_atlas import render
    captions=render(out,data,cases,args.detail_route,args.sun,args.minutes)
    write_explorer(out,data)
    (out/"figure-captions.txt").write_text("\n\n".join(captions)+"\n",encoding="utf-8")
    shutil.copy2(args.annotations,out/"cohort-annotations.json")
    source=Path(__file__).parent
    code_paths=[source/f for f in ("cross_study_atlas.py","export_cross_study_atlas.py","plot_cross_study_atlas.py",
                "cross_study_explorer.html","composition_performance_data.py","design_rule_data.py",
                "plot_material_atlas.py","plot_material_space.py","plot_composition_performance.py","build_material_mapping.py")]
    data_counts=dict(studies=len(data["papers"]),final_variants=sum(m["is_final"] for m in data["mats"].values()),
                     composition_profiles=len(data["profiles"]),final_target_records=sum(r["is_final"] for r in data["rows"]),
                     source_pairs=len(data["pairs"]),qualified_pairs=sum(p["qualified"] for p in data["pairs"]))
    manifest=dict(snapshot=str(args.source.resolve()),materials_sha256=data["cfg"]["materials_sha256"],
                  annotations_sha256=file_hash(args.annotations),counts=data_counts,projection=data["projection"],
                  matching=data["cfg"]["matching"],cases=cases,detail=[args.target,args.detail_route,args.sun,args.minutes],
                  endpoints="Evaporation flux in kg m-2 h-1 and percent removal of the parent organic in feed/reaction liquid. "
                            "Condensate rejection, TOC and aqueous parent concentration ratios retained under their original endpoints.",
                  design="Exploratory within this cohort. Count each DOI once per stratum. Exclude equal retained-ingredient profiles "
                         "from cross-paper matches. Match exact target and operating route in owned experimental arms. "
                         "Reported NaCl and defined model water are separate evidence tiers. No salt or joint outcome imputation. "
                         "An absolute response or a high UMAP density does not identify improvement relative to a comparator.",
                  software={p:version(p) for p in ("numpy","matplotlib","scikit-learn","umap-learn")},
                  code_sha256={p.name:file_hash(p) for p in code_paths},
                  exports={p.name:file_hash(p) for p in sorted(out.iterdir()) if p.is_file()})
    save_json(out/"provenance.json",manifest)
    with ZipFile(out/"figures-and-source-data.zip","w",compression=ZIP_DEFLATED) as archive:
        for path in sorted(out.iterdir()):
            if path.is_file() and path.suffix!=".zip":
                archive.write(path,path.name)
        for path in code_paths:
            archive.write(path,"scripts/research/"+path.name)
    print(json.dumps(data_counts),flush=True)


if __name__=="__main__":
    main()
