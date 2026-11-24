"""Plot a mixed-input UMAP and the source-matched contrasts underlying candidate design rules."""
from __future__ import annotations

import argparse
import csv
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import zipfile

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import Normalize
import numpy as np

from composition_performance_data import digest
from design_rule_data import check_contrasts, fit, load_cases
from plot_composition_performance import FLUX, REMOVAL, SETTINGS, continuous_key, map_labels, split_point, tidy


INK, MUTED, GRID, SOFT = "#192F39", "#4D636E", "#DAE3E4", "#EFF3F3"
VIOLET, GREEN = "#835476", "#087B79"
ROUTES = {"PMS":"#277E83","PMS-free control":"#A6CECC","Peroxide-assisted":"#CB7953",
          "Photocatalysis":"#8173A8","Electro-Fenton":"#476494","Brine test":"#839CA8","Route unresolved":"#C0BDB5"}
ROUTE_LABEL = {"PMS":"PMS-assisted", "Peroxide-assisted":"H₂O₂-assisted", "Brine test":"Brine reference"}
TYPES = ["Phenolics","Antibiotics","Dyes","Aromatic amine","Brine"]
FILES = ["01-material-treatment-umap","02-conditional-design-rules"]


def begin(title, subtitle):
    fig = plt.figure(figsize=(8.05,6.32), facecolor="white")
    fig.text(.061,.954,title,fontsize=17,weight="semibold",va="top",color=INK)
    fig.text(.063,.895,subtitle,fontsize=8.1,color=MUTED,va="top")
    return fig


def heading(fig,x,y,letter,title,sub=""):
    fig.text(x,y,letter,fontsize=13.5,weight="bold",va="center")
    fig.text(x+.035,y,title,fontsize=9.8,weight="semibold",va="center")
    if sub:
        fig.text(x+.035,y-.031,sub,fontsize=7.1,color=MUTED,va="center")


def foot(fig, lines):
    fig.add_artist(plt.Line2D([.062,.95],[.073,.073],color=GRID,lw=.7,transform=fig.transFigure))
    fig.text(.062,.059,lines,fontsize=6.85,color=MUTED,linespacing=1.36,va="top")


def atlas_axis(fig, rect, rows):
    ax = fig.add_axes(rect)
    xy = np.array([[r["x"],r["y"]] for r in rows])
    span = np.ptp(xy,axis=0)
    ax.set_xlim(xy[:,0].min()-.10*span[0],xy[:,0].max()+.11*span[0])
    ax.set_ylim(xy[:,1].min()-.13*span[1],xy[:,1].max()+.17*span[1])
    ax.set_aspect("equal",adjustable="box")
    ax.set_facecolor("#F8FAFA")
    for s in ax.spines.values():
        s.set_color(GRID)
        s.set_linewidth(.55)
    for q in (.25,.50,.75):
        ax.axvline(np.quantile(ax.get_xlim(),q),lw=.42,ls=(0,(2,4)),c=GRID,zorder=0)
        ax.axhline(np.quantile(ax.get_ylim(),q),lw=.42,ls=(0,(2,4)),c=GRID,zorder=0)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("UMAP 1",fontsize=6.4,color=MUTED,labelpad=3)
    ax.set_ylabel("UMAP 2",fontsize=6.4,color=MUTED,labelpad=3)
    return ax


def mark_sites(ax, rows, labels, radius_factor):
    pts = [{"x":r["x"],"y":r["y"],"id":r["key"],"label":labels.get(r["key"],""),
            "variants":radius_factor} for r in rows]
    map_labels(ax, pts, {k:[1] for k in labels}, len(labels))


def map_page(rows, cfg):
    eligible = [r for r in rows if r["paired60"]]
    fig = begin("Mapping dual-function material design",
                f"{len(rows)} selected laboratory conditions  ·  {len({r['doi'] for r in rows})} studies  ·  "
                f"{len({(r['doi'],r['material']) for r in rows})} material variants including the matched comparator")
    heading(fig,.06,.819,"a","Material and treatment conditions","Point colour identifies the operating protocol")
    heading(fig,.535,.819,"b","Paired evaporation and removal",
            f"{len(eligible)} matched 60-min conditions across {len({r['doi'] for r in eligible})} studies")
    a = atlas_axis(fig,[.08,.465,.386,.30],rows)
    b = atlas_axis(fig,[.554,.465,.389,.30],rows)
    index = {r["key"]:r for r in rows}
    flux_scale = Normalize(0,float(np.ceil(max(r["flux"] for r in eligible))))
    rem_scale = Normalize(0,100)
    for r in rows:
        a.scatter(r["x"],r["y"],s=37,color=ROUTES[r["route"]],ec="#455D65",lw=.46,zorder=3)
        if not r["paired60"]:
            b.scatter(r["x"],r["y"],s=21,fc="white",ec="#B9C6CA",lw=.65,zorder=2)
    for r in eligible:
        split_point(b,r["x"],r["y"],FLUX(flux_scale(r["flux"])),REMOVAL(rem_scale(r["removal"])),9,
                    "#354D56",zorder=5)
        if "estimated" in (r["flux_status"],r["removal_status"]):
            b.plot(r["x"],r["y"],marker=".",color="white",mec="#30464B",mew=.28,ms=2.8,zorder=6)
    # Only trace source-owned formulation paths on the common embedding.
    traces, material_labels = {}, {}
    for no,(path,color) in enumerate(zip(cfg["material_paths"],[GREEN,VIOLET]),1):
        points = [index[k] for k in path["arms"]]
        for p,q in zip(points,points[1:]):
            b.annotate("",(q["x"],q["y"]),(p["x"],p["y"]),zorder=3,
                       arrowprops=dict(arrowstyle="-|>",color=color,lw=.85,mutation_scale=5,
                                       shrinkA=3.9,shrinkB=4.3))
        traces[points[0]["key"]] = f"Path {no}"
        material_labels[points[0]["key"]] = points[0]["study_label"]
    brines = sorted((r for r in rows if r["route"]=="Brine test" and r["salt_wt"] is not None),key=lambda r:r["salt_wt"])
    if brines:
        material_labels[brines[0]["key"]] = f"NaCl {brines[0]['salt_wt']:g}–{brines[-1]['salt_wt']:g} wt%"
        if len({(r["doi"],r["material"]) for r in brines}) == 1:
            a.plot([r["x"] for r in brines],[r["y"] for r in brines],color=ROUTES["Brine test"],
                   lw=.8,ls=(0,(2,2)),zorder=1)
    mark_sites(a,rows,material_labels,1.25)
    mark_sites(b,rows,traces,2.5)
    heading(fig,.06,.396,"c","Comparison coverage by treatment and pollutant")
    fig.text(.095,.361,"Each cell gives paired 60-min conditions / selected conditions.",fontsize=7.1,color=MUTED)
    ax = fig.add_axes([.235,.126,.372,.188])
    types = [t for t in TYPES if any(r["target_class"]==t for r in rows)]
    routes = [k for k in ROUTES if any(r["route"]==k for r in rows)]
    ax.set_xlim(-.5,len(types)-.5)
    ax.set_ylim(len(routes)-.5,-.5)
    for i,route in enumerate(routes):
        for j,group in enumerate(types):
            cell = [r for r in rows if r["route"]==route and r["target_class"]==group]
            n = sum(r["paired60"] for r in cell)
            ax.add_patch(plt.Rectangle((j-.48,i-.46),.96,.92,lw=0,fc="#CCE3DF" if n else SOFT if cell else "white"))
            if cell:
                ax.text(j,i,f"{n}/{len(cell)}",ha="center",va="center",fontsize=7.25,
                        weight="semibold" if n else "normal",color=INK if n else MUTED)
        ax.scatter(-.655,i,s=16,c=ROUTES[route],edgecolors="#60737C",lw=.32,clip_on=False)
    ax.set_yticks(range(len(routes)),[ROUTE_LABEL.get(k,k) for k in routes],fontsize=6.6)
    ax.set_xticks(range(len(types)),["Aromatic\namine" if k=="Aromatic amine" else k for k in types],
                  fontsize=6.3)
    ax.xaxis.tick_top()
    ax.tick_params(axis="y",length=0,pad=10)
    ax.tick_params(axis="x",length=0,pad=2.5)
    for sp in ax.spines.values():
        sp.set_visible(False)
    continuous_key(fig,.683,.312,.236,FLUX,flux_scale,[0,flux_scale.vmax/2,flux_scale.vmax],
                   "Left half  ·  Flux (kg m⁻² h⁻¹)")
    continuous_key(fig,.683,.227,.236,REMOVAL,rem_scale,[0,50,100],"Right half  ·  Parent removal (%)")
    fig.text(.682,.159,"Hollow ring   No qualifying 60-min pair",fontsize=6.6,color=MUTED)
    fig.text(.682,.130,"Centre dot    At least one plot estimate",fontsize=6.6,color=MUTED)
    foot(fig,"Shared UMAP inputs include composition, pollutant, activation, irradiance, water matrix, salinity and assay conditions.\n"
             "Arrows trace the two within-study formulation series quantified on the next page.")
    return fig


def path_panel(ax, points, labels, offsets, color):
    tidy(ax,grid="both")
    xs = [p["flux"] for p in points]
    ys = [p["removal"] for p in points]
    sx,sy = max(np.ptp(xs),.1),max(np.ptp(ys),2)
    ax.set_xlim(min(xs)-.25*sx,max(xs)+.35*sx)
    ax.set_ylim(min(ys)-.24*sy,max(ys)+.31*sy)
    ax.xaxis.set_major_locator(mpl.ticker.MaxNLocator(4))
    ax.yaxis.set_major_locator(mpl.ticker.MaxNLocator(4))
    ax.set_xlabel("Evaporation rate (kg m⁻² h⁻¹)",labelpad=3)
    ax.set_ylabel("Parent removal (%)",labelpad=4)
    for p,q in zip(points,points[1:]):
        ax.annotate("",xy=(q["flux"],q["removal"]),xytext=(p["flux"],p["removal"]),
                    arrowprops=dict(arrowstyle="-|>",color=color,lw=1.35,mutation_scale=8,shrinkA=4,shrinkB=5))
    for p,tag,(dx,dy) in zip(points,labels,offsets):
        est = "estimated" in (p["flux_status"],p["removal_status"])
        ax.plot(p["flux"],p["removal"],marker="o",ls="none",ms=6.9,mec=color,mew=1.15,mfc="white" if est else color,zorder=4)
        ax.annotate(tag,(p["flux"],p["removal"]),xytext=(dx,dy),textcoords="offset points",fontsize=7.3,
                    ha="center",va="center",color=color,weight="semibold",
                    bbox=dict(fc="white",ec="none",pad=.35,alpha=.88))


def text_value(row,key):
    if row[key] is None:
        return None
    est = row[("flux_status" if key=="flux" else "removal_status")] == "estimated"
    return ("≈" if est else "") + (f"{row[key]:.2f}" if key=="flux" else f"{row[key]:g}")


def comparison_value(a,b,k):
    v,w = text_value(a,k),text_value(b,k)
    if v and w:
        return v+" → "+w
    if k=="flux" and b["flux_unit_status"] == "inferred":
        return "Unit unresolved"
    return "Endpoint unavailable"


def operating_condition(c,a,b):
    k = c["detail"]
    if k == "Matrix":
        return a["matrix"]+" → "+b["matrix"]
    field = {"Irradiance":"sun","PMS dose":"dose","Salt":"salt_wt"}[k]
    unit = {"Irradiance":"suns","PMS dose":b["dose_unit"],"Salt":"wt% NaCl"}[k]
    prefix = "PMS  " if k == "PMS dose" else ""
    return f"{prefix}{a[field]:g} → {b[field]:g} {unit}"


def paths_page(rows,cfg):
    idx={r["key"]:r for r in rows}
    fig=begin("Candidate design rules from matched experiments",
              "Arrows follow a reported change in formulation. Each point retains both outcomes of its own experimental arm.")
    for n,(p,c,x) in enumerate(zip(cfg["material_paths"],[GREEN,VIOLET],[.06,.535])):
        points=[idx[k] for k in p["arms"]]
        heading(fig,x,.818,"ab"[n],f"Path {n+1}   {p['name']}",p["caption"])
        ax=fig.add_axes([x+.071,.505,.324,.220])
        tags=p.get("labels") or [f"{r['recipe']:.2f} {r['recipe_unit']}" for r in points]
        offsets=[(0,-11),(0,10)] if len(points)==2 else [(0,-11),(0,-12),(-16,3),(4,9)]
        path_panel(ax,points,tags,offsets,c)
        a,b = [idx[k] for k in p["focus"]]
        dflux, drem = b["flux"]-a["flux"], b["removal"]-a["removal"]
        rem_text = (f"≈{drem:+.0f}" if "estimated" in (a["removal_status"],b["removal_status"]) else f"{drem:+.1f}")
        prefix = f"{a['recipe']:.2f} → {b['recipe']:.2f} g    " if a["recipe"] is not None else ""
        fig.text(x+.067,.426,f"{prefix}Δ flux {dflux:+.2f}   ·   Δ removal {rem_text} pp",
                 fontsize=7.1,color=c,weight="semibold")
    heading(fig,.06,.388,"c","Operating changes with the formulation held fixed")
    fig.text(.095,.359,"Each row is a source-linked comparison within one study.",fontsize=7.1,color=MUTED)
    table=fig.add_axes([.064,.126,.878,.199],frameon=False)
    table.set_xlim(0,1)
    table.set_ylim(0,1)
    table.set_axis_off()
    cols=[.002,.254,.595,.809]
    for x,title in zip(cols,["Material","Operating condition","Flux (kg m⁻² h⁻¹)","Parent removal (%)"]):
        table.text(x,.943,title,va="center",fontsize=7.4,weight="semibold",color=INK)
    for i,c in enumerate(cfg["operating_contrasts"]):
        a,b=[idx[k] for k in c["arms"]]
        y=.760-i*.204
        table.axhline(y+.105,color=GRID,lw=.64)
        values=[a["study_label"],operating_condition(c,a,b),comparison_value(a,b,"flux"),comparison_value(a,b,"removal")]
        for x,v in zip(cols,values):
            table.text(x,y,v,va="center",fontsize=7.5 if x<.5 else 7.4,
                       color=MUTED if v in {"Unit unresolved","Endpoint unavailable"} else INK)
    foot(fig,"Open markers include a plot estimate. pp = percentage points. Co₃O₄ masses are nominal preparation additions.\n"
             "These are within-study condition-group comparisons. Actual retained loading and physical replicate identity are unresolved.")
    return fig


def to_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def table(path,rows):
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(tuple,list,dict)) else v for k,v in row.items()})


def save_data(out,rows,papers,cfg,source,selection,diag):
    table(out/"material-treatment-arms.csv",[{k:v for k,v in r.items() if k!="source"} for r in rows])
    idx={r["key"]:r for r in rows}
    contrasts=[]
    for c in cfg["material_paths"]+cfg["operating_contrasts"]:
        steps = list(zip(c["arms"],c["arms"][1:]))
        if "focus" in c and tuple(c["focus"]) not in steps:
            steps.append(tuple(c["focus"]))
        for l,h in steps:
            a,b=idx[l],idx[h]
            comp=papers[a["doi"]]["comparisons"][c["comparison"]]
            contrasts.append({"comparison":c.get("name") or c["label"],"from_arm":l,"to_arm":h,
                "doi":a["doi"],"reference_comparison":comp["id"],"flux_from":a["flux"],"flux_to":b["flux"],
                "removal_from":a["removal"],"removal_to":b["removal"],
                "delta_flux":round(b["flux"]-a["flux"],4) if None not in (a["flux"],b["flux"]) else None,
                "delta_removal_pp":round(b["removal"]-a["removal"],4) if None not in (a["removal"],b["removal"]) else None,
                "from_status":[a["flux_status"],a["removal_status"]],"to_status":[b["flux_status"],b["removal_status"]],
                "comparison_source":comp})
    table(out/"within-study-contrasts.csv",contrasts)
    modules=["plot_design_rule_pilot.py","design_rule_data.py","composition_performance_data.py","plot_material_atlas.py",
             "plot_material_space.py","plot_composition_performance.py","build_material_mapping.py"]
    project=Path(__file__).resolve().parent
    to_json(out/"figure-provenance.json",{"selection_sha256":digest(selection),"inputs":str(source.resolve()),
        "snapshot":cfg["source_snapshot_sha256"],"purposeful_selection":cfg["scope"],"projection":diag,
        "versions":{k:version(k) for k in ("numpy","scikit-learn","umap-learn","matplotlib")},
        "scripts":{k:digest(project/k) for k in modules},
        "papers":{k:{f:v["record"][f] for f in ("record_id","snapshot_id","record_path","record_sha256","sources","dataset_role")}
                  for k,v in papers.items()},"case_evidence":{r["key"]:r["source"] for r in rows}})
    shutil.copyfile(selection,out/"selected-protocols.json")
    methods = (
        "Figure 1. Material–treatment design space. A point represents a selected material variant, pollutant and operating arm. "
        f"The pilot includes {len(rows)} laboratory arms from {len(papers)} studies, selected for interpretable comparisons from a "
        "23-study published snapshot. Figure 1a colours inputs by operating protocol. The brine reference has no organic target "
        "specified. Figure 1b uses the same coordinates and colours only observations linked by a P1 or P2 source pair within "
        "the same variant and experiment, with an observation-specific 60-minute removal endpoint. Both numeric endpoints "
        "must have a reported unit. Removal is parent-compound concentration decline in feedwater or reaction liquid. "
        "Evaporation is an areal gravimetric flux. A white centre dot marks a pair containing a plot estimate. No outcomes "
        "are interpolated. Figure 1c counts arms, including explicit controls. Missing joint results can coexist with separate "
        "evidence of the two functions in the same paper. Experimental protocols describe process conditions. Chemical "
        "mechanisms require their own evidence. PMS denotes peroxymonosulfate. TC·HCl denotes tetracycline hydrochloride.\n\n"
        "Coordinates. We computed mixed-data dissimilarities prior to overlaying performance. Material inputs carry 40% of "
        "the nominal weight, pollutant identity/class and loading 18%, activation protocol and reagent dose 18%, light 9%, "
        "water matrix and recorded NaCl mass fraction 10%, and endpoint time/controlled bulk temperature 5%. Chemical "
        "similarity uses reported retained-component names, formulae and explicit elements. Within a materially comparable "
        "recipe, nominal addition contributes 24% of the material block. For numerical descriptors we divide absolute differences "
        "by the observed range within a comparable unit and scope. Comparisons of reagent mass and solution molarity stay separate. "
        "Unavailable fields are omitted pairwise and the remaining weights renormalised. Model water is a matrix category. "
        "It supplies no quantitative salinity value. Shared feature profiles are projected once. UMAP uses a precomputed "
        f"distance, {diag['n_neighbors']} neighbours, minimum distance {diag['min_dist']} and seed {diag['seed']}. "
        "Neighbour counts of 7, 9, 11, 13, 15 and 18 were compared using only the input distances; nine retained "
        "the closest agreement with the original five-neighbour ordering by trustworthiness in this pilot. "
        f"Neighbourhood trustworthiness at k={diag['k']} is {diag['trustworthiness']:.3f}. Reweighting composition to 30% or "
        "50% is recorded in figure-provenance.json. This checks neighbourhood sensitivity to design choices, independently "
        "of quantitative performance.\n\n"
        "Figure 2. Matched design changes. Arrows join the nominal conditions of source-defined comparisons within each "
        "paper. The MoS₂ experiment changes the precursor route and morphology as well as B incorporation; retained active "
        "mass equivalence is unresolved. Co₃O₄ masses are preparation inputs. Source-figure estimates remain marked. "
        "Changes in irradiance can change temperature. The river-water flux unit is inferred in the source record and is "
        "excluded from the comparison. Brine flux values have no matched parent-removal endpoint. Experimental-arm matching "
        "does not demonstrate identical physical replicates. Candidate directions require independent replication.\n\n"
        "Data. material-treatment-arms.csv links numeric endpoints and input fields to each material and experiment. "
        "within-study-contrasts.csv retains the source-defined comparison, confounders and evidence. figure-provenance.json "
        "includes original statuses, observations, protocol evidence and source file hashes.\n")
    (out/"figure-captions.txt").write_text(methods,encoding="utf-8")
    with zipfile.ZipFile(out/"figures-and-source-data.zip","w",compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.iterdir()):
            if p.suffix in {".svg",".csv",".json",".txt"}:
                z.write(p,p.name)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("source","selection","out"):
        parser.add_argument("--"+name,required=True,type=Path)
    args=parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("Use an empty destination to preserve earlier figures")
    cfg=json.loads(args.selection.read_bytes())
    rows,papers=load_cases(args.source,cfg)
    check_contrasts(rows,papers,cfg)
    diag=fit(rows)
    args.out.mkdir(parents=True,exist_ok=True)
    with mpl.rc_context(SETTINGS), PdfPages(args.out/"dual-function-design-rules.pdf") as pdf:
        for name,fig in zip(FILES,[map_page(rows,cfg),paths_page(rows,cfg)]):
            pdf.savefig(fig)
            fig.savefig(args.out/(name+".png"),dpi=450)
            fig.savefig(args.out/(name+".svg"))
            plt.close(fig)
    save_data(args.out,rows,papers,cfg,args.source,args.selection,diag)
    print(len(rows),"arms;",diag["groups"],"positions;",sum(r["paired60"] for r in rows),"60-min pairs")


if __name__ == "__main__":
    main()
