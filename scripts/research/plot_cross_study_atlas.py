"""Publication figures for the cross-study composition and experiment atlas."""
from __future__ import annotations

from collections import defaultdict
import re
from textwrap import fill

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
import numpy as np

from cross_study_atlas import ALL, DUAL_WATER, FE_ABSORBER, MOTIFS, SULFIDE_HOST, number, reported
from plot_composition_performance import SETTINGS
from plot_material_space import glyph


INK, MUTED, LINE, PAPER = "#16333C", "#48636B", "#CAD6D7", "#F4F7F6"
TEAL, OCHRE, PLUM, FAINT = "#116C71", "#B86126", "#813E64", "#CED9D9"
GROUPS = {"Phenolics":"#CA773D","Dyes":"#8D5592","Pharmaceuticals":"#247D83",
          "Other organics":"#5C73B2","Ions / elements":"#749548","Bulk / mixtures":"#796858","Microbes":"#BC4B71"}
ROUTES = {"Photocatalysis":"#347DA3","External peroxide":"#BB6328","PMS":"#12817C",
          "PDS":"#9A537B","In-situ peroxide":"#C99523","Electro-Fenton":"#6556A2",
          "Electro-oxidation":"#667E37","Thermocatalytic oxidation":"#9F4F44"}
ROUTE_TEXT = {"External peroxide":"Added H₂O₂","In-situ peroxide":"In situ H₂O₂",
              "Thermocatalytic oxidation":"Thermocatalysis"}
ROLE_TEXT = {FE_ABSORBER:"Fe catalyst + separate absorber",
             DUAL_WATER:"Absorbing catalyst + water supply",SULFIDE_HOST:"Metal sulfide + polymer/fibre"}
ROLE_DETAILS = {FE_ABSORBER:"Fe-containing catalyst with a separate carbon or polymer absorber",
                DUAL_WATER:"One absorbing/catalytic phase with a separate water-supply component",
                SULFIDE_HOST:"A metal sulfide with a polymer or fibre framework"}


def paper(title, subtitle):
    fig=plt.figure(figsize=(9.2,7.32),facecolor="white")
    fig.text(.058,.959,title,fontsize=20.3,fontweight="semibold",va="top",color=INK)
    fig.text(.060,.898,subtitle,fontsize=9,va="top",color=MUTED)
    return fig


def panel(fig,x,y,letter,title,detail=None):
    fig.text(x,y,letter,fontsize=14.4,fontweight="bold",va="center",color=TEAL)
    fig.text(x+.034,y,title,fontsize=11.7,fontweight="semibold",va="center")
    if detail:
        fig.text(x+.035,y-.031,detail,fontsize=8,color=MUTED,va="center")


def foot(fig,lines):
    fig.add_artist(Line2D([.061,.951],[.078,.078],transform=fig.transFigure,color=LINE,lw=.7))
    fig.text(.062,.064,lines,va="top",color=MUTED,fontsize=7.45,linespacing=1.43)


def base_map(fig,rect,data,show_neighbors=False):
    ax=fig.add_axes(rect)
    ps=data["profiles"]
    xs=[p["x"] for p in ps]; ys=[p["y"] for p in ps]
    ax.set_xlim(min(xs)-.6,max(xs)+.65)
    ax.set_ylim(min(ys)-.5,max(ys)+.5)
    ax.set_aspect("equal",adjustable="box")
    ax.set_facecolor("#F7F9F8")
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_xlabel("UMAP 1",labelpad=2,color=MUTED,fontsize=7.4)
    ax.set_ylabel("UMAP 2",labelpad=2,color=MUTED,fontsize=7.4)
    if show_neighbors:
        points={p["id"]:p for p in ps}
        for link in data["projection"]["neighbor_links"]:
            a,b=points[link["left"]],points[link["right"]]
            ax.plot([a["x"],b["x"]],[a["y"],b["y"]],color="#B9CCCA",lw=.65,zorder=1)
    return ax


def categories(ax,values,palette,data):
    for p in data["profiles"]:
        colors=[c for name,c in palette.items() if name in values[p["id"]]]
        glyph(ax,(p["x"],p["y"]),colors or [FAINT],8.4,border="white")


def legend(fig,items,x,y,columns,dx,dy=.025):
    for i,(name,color) in enumerate(items):
        row,col=divmod(i,columns)
        xx,yy=x+col*dx,y-row*dy
        fig.add_artist(Line2D([xx],[yy],marker="o",markersize=5.7,mec="white",mew=.2,color=color,
                              linestyle="",transform=fig.transFigure))
        fig.text(xx+.010,yy,name,ha="left",va="center",fontsize=7.3,color=MUTED)


def cohort_map(data):
    n=len(data["papers"]); v=sum(m["is_final"] for m in data["mats"].values()); c=len(data["profiles"])
    fig=paper("Composition map of dual-function materials",
              f"{n} papers  ·  {v} final material variants  ·  {c} retained-ingredient profiles  ·  shared UMAP coordinates")
    panel(fig,.060,.830,"a","Measured targets")
    panel(fig,.532,.830,"b","Operating catalytic routes")
    gr,ro,cells=defaultdict(set),defaultdict(set),defaultdict(set)
    for r in data["rows"]:
        if not r["is_final"]:
            continue
        gr[r["profile"]].add(r["group"])
        if r["route"] in ROUTES:
            ro[r["profile"]].add(r["route"])
            cells[r["route"],r["group"]].add(r["doi"])
    categories(base_map(fig,[.071,.563,.394,.236],data,True),gr,GROUPS,data)
    categories(base_map(fig,[.543,.563,.394,.236],data,True),ro,ROUTES,data)
    short_group={"Ions / elements":"Ions / elements","Bulk / mixtures":"Bulk / mixtures"}
    legend(fig,[(short_group.get(k,k),v) for k,v in GROUPS.items()],.083,.515,3,.131)
    legend(fig,[(ROUTE_TEXT.get(k,k),v) for k,v in ROUTES.items()],.554,.515,3,.135)
    panel(fig,.060,.412,"c","Papers by target class and operating route")
    ax=fig.add_axes([.237,.140,.685,.227])
    cols=list(GROUPS); rs=list(ROUTES)
    values=np.array([[len(cells[r,g]) for g in cols] for r in rs])
    limit=max(1,values.max())
    ramp=LinearSegmentedColormap.from_list("paper_count",["#D6E8E2","#81B9A8","#388F86","#0B555C"])
    ax.set_xlim(-.5,len(cols)-.5);ax.set_ylim(len(rs)-.5,-.5)
    for i,row in enumerate(values):
        for j,val in enumerate(row):
            color=ramp(.1+.9*val/limit) if val else PAPER
            ax.add_patch(Rectangle((j-.5,i-.5),1,1,facecolor=color,edgecolor="white",lw=1.2))
            ax.text(j,i,str(val) if val else "·",ha="center",va="center",fontsize=8.0,
                    color="white" if val>=limit*.60 else INK if val else "#9CAFAF",
                    fontweight="bold" if val else "normal")
    ax.set_yticks(range(len(rs)),[ROUTE_TEXT.get(k,k) for k in rs],fontsize=7.6)
    ax.set_xticks(range(len(cols)),["Phenolics","Dyes","Pharma-\nceuticals","Other\norganics","Ions /\nelements","Bulk /\nmixtures","Microbes"],fontsize=7.3)
    ax.tick_params(length=0,pad=4)
    for side in ax.spines.values():
        side.set_visible(False)
    foot(fig,"Equal sectors denote categories. Gray marks unassigned profiles. Links join mutual chemical neighbours sharing a constituent name or formula.\n"
             "Each matrix cell counts distinct papers. Targets can be measured in different streams within a protocol. A dot denotes zero papers.")
    return fig


def short_material(name):
    for separator in (" prepared from"," (",","):
        name=name.split(separator)[0]
    for raw,pretty in (("Co3O4","Co₃O₄"),("MoS2","MoS₂"),("Fe2O3","Fe₂O₃")):
        name=name.replace(raw,pretty)
    return name if len(name)<=29 else name[:27]+"…"


def focus_map(fig,data,case,rect,color):
    target,route,tag=case
    combo=data["strata"][case]
    pool=data["strata"][target,route,ALL]["profiles"]
    ax=base_map(fig,rect,data)
    match=set()
    for k,arms in data["witnesses"].items():
        if k[:4]==(*case,"light"):
            match.update(r["profile"] for r in arms)
    labels={}
    for p in data["profiles"]:
        pid=p["id"]; x,y=p["x"],p["y"]
        if pid in combo["profiles"]:
            ax.scatter(x,y,s=80,c=color,edgecolors="white",linewidths=.75,zorder=4)
            if pid in match:
                ax.scatter(x,y,s=140,facecolor="none",edgecolors=color,linewidths=1.05,zorder=3)
            ax.annotate(pid,(x,y),xytext=(6,4),textcoords="offset points",fontsize=7.0,color=INK,zorder=6,
                        bbox=dict(boxstyle="round,pad=0.09",fc="white",alpha=.80,ec="none"))
            candidates={data["mats"][r["doi"],r["material"]]["name"] for r in data["rows"]
                        if r["is_final"] and r["profile"]==pid and r["target"]==target and r["route"]==route
                        and tag in data["mats"][r["doi"],r["material"]]["motifs"]}
            labels[pid]=min((short_material(n) for n in candidates),key=lambda v:(len(v),v))
        elif pid in pool:
            ax.scatter(x,y,s=40,facecolors="none",edgecolors=color,linewidths=.9,zorder=2)
        else:
            ax.scatter(x,y,s=24,facecolors=FAINT,edgecolors="white",linewidths=.6,zorder=1)
    return labels


def candidates_map(data,cases):
    target=cases[0][0]
    fig=paper(f"Recurring architectures for {target} treatment",
              "Two different compositions and two distinct papers are required for each cross-paper match.")
    for i,(case,x,col) in enumerate(zip(cases[:2],[.060,.532],[OCHRE,TEAL])):
        tag=case[2]
        panel(fig,x,.828,"ab"[i],f"{target.capitalize()}  /  {ROUTE_TEXT.get(case[1],case[1])}",ROLE_TEXT[tag])
        labs=focus_map(fig,data,case,[x+.012,.527,.394,.238],col)
        for j,(pid,name) in enumerate(sorted(labs.items())):
            row,cc=divmod(j,2)
            fig.text(x+.017+cc*.205,.475-row*.026,f"{pid}   {name}",fontsize=7.5,color=col,fontweight="medium")
    styles=[("o",False,"Target / route"),("o",True,"Recurring architecture"),("ring",True,"Also matched irradiance")]
    for i,(s,solid,label) in enumerate(styles):
        x=(.219,.420,.648)[i]
        fig.add_artist(Line2D([x],[.397],marker="o",mfc=TEAL if solid else "white",mec=TEAL,mew=.8,ms=6.2,
                              ls="",transform=fig.transFigure))
        if s=="ring":
            fig.add_artist(Line2D([x],[.397],marker="o",mfc="none",mec=TEAL,mew=.8,ms=10.3,ls="",transform=fig.transFigure))
        fig.text(x+.014,.397,label,fontsize=7.6,va="center",color=MUTED)
    panel(fig,.060,.332,"c","Evidence retained as matching criteria tighten")
    left=.073; xs=[.479,.583,.684,.790,.900]
    heads=["Papers with\narchitecture¹","Matched\nlight²","+ defined\nmodel water²","+ numeric\nNaCl²","Paired dual\nendpoints³"]
    for x,h in zip(xs,heads):
        fig.text(x,.279,h,ha="center",va="center",fontsize=7.7,fontweight="semibold",linespacing=1.23)
    fig.text(left,.279,"Target, route and architecture",fontsize=8.0,fontweight="semibold",va="center")
    for i,case in enumerate(cases):
        t,r,tag=case; s=data["strata"][case]; y=.222-i*.045
        if i%2==0:
            fig.add_artist(Rectangle((.061,y-.020),.887,.040,transform=fig.transFigure,fc=PAPER,ec="none",zorder=-1))
        base=len(data["strata"][t,r,ALL]["papers"])
        fig.text(left,y,f"{ROUTE_TEXT.get(r,r)}  ·  {ROLE_TEXT[tag]}",va="center",fontsize=7.6)
        counts=[f"{len(s['papers'])} / {base}",len(s["light"]),len(s["model"]),len(s["nacl"]),len(s["dual_nacl"]|s["dual_model"])]
        for x,value in zip(xs,counts):
            fig.text(x,y,str(value),ha="center",va="center",fontsize=9.1,fontweight="semibold" if value else "normal",
                     color=TEAL if value else MUTED)
    match=data["cfg"]["matching"]
    foot(fig,f"¹ Papers with architecture / papers in stratum.  ² DOI pairs with different compositions at Δlight ≤ {match['sun_tolerance']:g} sun. The matrix filters are parallel.\n"
             f"Numeric NaCl requires Δsalt ≤ {match['nacl_wt_percent_points']:g} wt% points.  ³ Either matrix filter plus linked flux and equal-time liquid-parent removal in both papers.")
    return fig


def clean_family(tag):
    return tag.replace("Polysaccharide / plant fibre","Polysaccharide/fibre").replace("Polyvinyl alcohol","PVA")


def bars(ax,items,col,shorten):
    for i,(tag,n) in enumerate(items):
        ax.barh(i,n,color=col,height=.48,zorder=2)
        ax.text(n+.12,i,str(n),va="center",ha="left",fontweight="semibold",fontsize=8.5)
        ax.text(.01,i-.40,shorten(tag),ha="left",va="center",fontsize=7.8)
    ax.set_xlim(0,max([n for _,n in items]+[1])*1.23)
    ax.set_ylim(len(items)-.38,-.72)
    ax.set_xticks([]);ax.set_yticks([])
    for side in ax.spines.values():
        side.set_visible(False)


def quantitative_figure(data,case,sun,minutes):
    t,r,tag=case
    n=len(data["papers"])
    fig=paper("From recurring ingredients to paired performance",
              f"{n} papers contribute to the ingredient counts. The paired outcomes use one target, catalytic route, irradiance and matrix class.")
    panel(fig,.060,.829,"a","Component roles across the cohort","Distinct papers with each documented functional pairing")
    panel(fig,.541,.829,"b","Frequently co-occurring ingredients","Component-family pairs with the largest paper count")
    roles=sorted(((k,len(data["by_tag"][k])) for k in MOTIFS),key=lambda x:-x[1])
    family=[(k,len(v)) for k,v in data["by_tag"].items() if k not in MOTIFS]
    top=max(n for _,n in family)
    frequent=sorted((v for v in family if v[1]==top),key=lambda x:x[0])
    bars(fig.add_axes([.086,.527,.342,.225]),roles,TEAL,lambda k:ROLE_TEXT[k])
    bars(fig.add_axes([.566,.527,.322,.225]),frequent,OCHRE,clean_family)
    panel(fig,.060,.442,"c",f"{t.capitalize()}  /  {ROUTE_TEXT.get(r,r)}  /  {sun:g} sun  /  defined model water",
          f"Measured pairs link evaporation flux to {minutes:g}-min removal of the parent compound in the liquid.")
    def matches(row):
        return (row["is_final"] and row["target"]==t and row["route"]==r and row["sun"]==sun
                and row["matrix"]=="Defined model water" and tag in data["mats"][row["doi"],row["material"]]["motifs"])
    eligible=[row for row in data["rows"] if matches(row)]
    comparison_refs={(c["doi"],c[side+"_flux_id"],c[side+"_parent_id"]) for c in data["contrasts"]
                     for side in ("from","to")}
    pairs=[p for p in data["pairs"] if matches(p) and p["qualified"] and p["time"]==minutes
           and (p["doi"],p["flux_id"],p["observation"]) in comparison_refs]
    observed=sorted({(row["doi"],row["profile"]) for row in eligible})
    fig.text(.078,.349,"Profile and source",fontsize=8.4,fontweight="semibold")
    ypos=.309
    for doi,prof in observed:
        code=data["codes"][doi]
        pp=[p for p in pairs if p["doi"]==doi and p["profile"]==prof]
        base=[row for row in eligible if row["doi"]==doi and row["profile"]==prof]
        lab=min((short_material(data["mats"][q["doi"],q["material"]]["name"]) for q in base),key=lambda v:(len(v),v))
        fig.text(.078,ypos,f"{prof}  {lab}  ({code})",fontsize=9.1,fontweight="semibold",color=TEAL if pp else MUTED)
        if pp:
            variants=len({p["material"] for p in pp})
            detail=f"{len(pp)} linked flux–removal pairs across {variants} variants"
        else:
            vals={q["value"] for q in base if q["endpoint"]=="Liquid parent ratio" and q["time"]==minutes and q["value"] is not None}
            val=f"Liquid C/C₀ ≈ {next(iter(vals)):g}" if len(vals)==1 else "Liquid-parent assay recorded"
            detail=f"{val},\nno linked flux for this condition"
        fig.text(.078,ypos-.030,detail,fontsize=7.9,color=MUTED,va="center",linespacing=1.4)
        ypos-=.103
    ax=fig.add_axes([.571,.172,.332,.179])
    for side in ("top","right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom","left"):
        ax.spines[side].set_color(LINE)
    ax.grid(color="#DCE5E3",ls="-",lw=.55,zorder=0)
    uniq={(p["doi"],p["flux_id"],p["observation"]):p for p in pairs}.values()
    for p in uniq:
        estimate="estimated" in {p["status"],p["flux_status"]}
        ax.scatter(p["flux"],p["value"],s=60,facecolors="white" if estimate else TEAL,edgecolors=TEAL,
                   lw=1.35,marker="o",zorder=3)
        recipe=[v for v in p.get("coordinates",[]) if "nominal" in v["name"].lower() and number(reported(v["fact"]))]
        if len(recipe)==1:
            v=recipe[0]
            ax.annotate(f"{reported(v['fact']):g} {reported(v['unit']) or ''}",(p["flux"],p["value"]),
                        xytext=(7,1),textcoords="offset points",fontsize=7.0,color=MUTED,va="center")
    ax.margins(.20,.22)
    ax.set_xlabel("Evaporation flux (kg m⁻² h⁻¹)",fontsize=8.2)
    ax.set_ylabel("Liquid-parent removal (%)",fontsize=8.2)
    ax.tick_params(length=2.6,width=.6,labelsize=7.4)
    fig.text(.538,.095,"Labels give nominal preparation doses. Open circles include a plot estimate.",fontsize=7.05,color=MUTED)
    foot(fig,"The bars count papers that report each pairing. The scatter shows individual variants in source-defined formulation comparisons.\n"
             "A point requires linked flux and liquid-parent removal. The model-water designation supplies no numeric NaCl concentration.")
    return fig


def loading_figure(data,case):
    t,route,tag=case
    series=defaultdict(list)
    for row in data["recipe_series"]:
        if row["target"]==t and row["route"]==route:
            series[row["doi"],row["comparison"]].append(row)
    selected=[]
    for (doi,cmp),items in sorted(series.items()):
        pos=sorted((p for p in items if p["dose"]>0),key=lambda p:p["dose"])
        documented_profiles={p["profile"] for p in pos if tag in p["motifs"]}
        if (len(pos)>=2 and len(documented_profiles)==1 and all(p["profile"] in documented_profiles for p in pos)
                and doi not in [s[0] for s in selected]):
            selected.append((doi,cmp,items,pos))
    if len(selected)<2:
        return None
    fig=paper(f"Loading responses in two {t}–{route} materials",
              "Within-paper formulation series. Shading spans each paper’s two lowest nonzero preparation doses.")
    for i,(doi,cmp,items,pos) in enumerate(selected[:2]):
        x=(.061,.540)[i]; color=(TEAL,PLUM)[i]; code=data["codes"][doi]
        items=sorted(items,key=lambda p:p["dose"]); doses=[p["dose"] for p in items]; unit=items[0]["dose_unit"]
        phase=re.sub(r"\b(?:preparation|prep|input|charge|loading|addition)\b.*","",
                     pos[0]["parameter"].replace("_"," ").removeprefix("nominal " ),flags=re.I).strip()
        phase=short_material(phase)
        panel(fig,x,.829,"ab"[i],f"{phase} formulation series  ({code})")
        q=pos[0]; strict=all(p["strict_pair"] for p in pos)
        cond=(f"{q['sun']:g} sun, {q['minutes']:g} min, defined model water" if q["sun"] is not None
              and q["minutes"] is not None and q["matrix"]=="Defined model water" else "Panel-specific light, salt and reaction time unresolved")
        fig.text(x+.034,.795,cond,color=MUTED,fontsize=7.95)
        for j,(metric,status,label) in enumerate([("flux","flux_status","Evaporation flux (kg m⁻² h⁻¹)"),
                        ("removal","removal_status",f"{'Liquid' if strict else 'Reported'} {t} removal (%)")]):
            ax=fig.add_axes([x+.061,.551-j*.265,.326,.187])
            ax.axvspan(pos[0]["dose"],pos[1]["dose"],color=color,alpha=.10,lw=0,zorder=0)
            ax.plot(doses,[p[metric] for p in items],color=color,lw=1.0,zorder=2)
            for p in items:
                ax.scatter(p["dose"],p[metric],s=49,edgecolors=color,linewidths=1.2,zorder=3,
                           facecolors="white" if p[status]=="estimated" else color)
            ax.set_ylabel(label,fontsize=8.4)
            ax.set_xticks(doses,[f"{v:g}" for v in doses]);ax.margins(x=.11,y=.16)
            ax.tick_params(length=2.5,width=.55,labelsize=7.7)
            ax.grid(axis="y",color=LINE,alpha=.85,lw=.5,zorder=1)
            for k in ("right","top"):
                ax.spines[k].set_visible(False)
            for k in ("bottom","left"):
                ax.spines[k].set_color(LINE)
            if j==0:
                ax.tick_params(labelbottom=False)
            else:
                ax.set_xlabel(f"Nominal {phase} addition ({unit})",fontsize=8.2)
        if strict:
            line="The parent removal was measured in the reaction liquid. Source-linked flux and removal are available for each positive dose."
        else:
            line="The panel labels phenol removal without identifying its sampling stream."
        note=data["cfg"]["papers"][doi].get("recipe_source_notes",{}).get(cmp,{}).get("note")
        if note:
            line+=" "+note
        fig.text(x+.011,.182,fill(line,62),fontsize=7.65,color=MUTED,va="top",linespacing=1.42)
    foot(fig,"Open points are source-plot estimates. Lines join observed preparation levels within each paper. Native recipe units are retained.\n"
             "Missing irradiance, numeric NaCl and sampling location in the loading series limit cross-paper comparisons to qualitative directions.")
    return fig


def render(out,data,cases,detail_route,sun,minutes):
    if not 2<=len(cases)<=3:
        raise ValueError("The printed comparison supports two map panels and up to three table rows")
    chosen=next(c for c in cases if c[1]==detail_route)
    captions=[
        "Figure 1. Composition-only UMAP of all final variants in the frozen cohort. Retained constituent identities, explicit "
        "formula elements and character-based names determine the embedding. Variants with identical ingredient signatures share "
        "coordinates but retain separate identities and observations in the source tables. Equal sectors encode linked target classes "
        "or confirmed operating routes. Links join mutual top-two neighbours in the original descriptor space with a shared "
        "constituent name or formula. Heatmap cells count distinct DOIs and may overlap across cells.",
        "Figure 2. Recurring material architectures for the same exact target. Each route uses its most frequent documented functional "
        "architecture by distinct paper count. Ties first use light-matched pair count and then the architecture label. Rings identify "
        "profiles in a recorded light-match witness. The table first requires distinct DOIs and ingredient profiles. The two additional "
        "matrix columns separately require explicitly defined model water or comparable numeric NaCl. The joint endpoint adds source-linked "
        "flux and liquid-parent removal at equal elapsed times in both papers. No frequency in this figure is a probability of improvement.",
        "Figure 3. Recurrence over all final variants counts each DOI once per ingredient pairing. Component roles use reported assignments. "
        "The quantitative panel uses the fixed target, route, irradiance and defined model-water class in its title. It includes recorded P1/P2 "
        "flux/removal pairs under matching nominal conditions. C/C0 is printed in its original scale. A missing linked flux cannot be "
        "supplied from a loading, burden, evaporation-only or condensate experiment. No regression or surface is fitted."
    ]
    with mpl.rc_context({**SETTINGS,"text.color":INK,"svg.hashsalt":"cross-study-atlas"}), PdfPages(
            out/"cross-study-material-atlas.pdf",metadata={"Title":"Cross-study material design for evaporation and catalytic treatment"}) as pdf:
        figures=[("01-cohort-umap",cohort_map(data)),("02-cross-study-architectures",candidates_map(data,cases)),
                 ("03-component-pairs-and-response",quantitative_figure(data,chosen,sun,minutes))]
        loading=loading_figure(data,chosen)
        if loading is not None:
            figures.append(("04-cross-material-loading",loading))
            captions.append("Figure 4. Source-defined nominal active-phase loading series for two different materials in the same "
                            "target, operating-route and role-architecture stratum. Each trace uses the flux and percentage removal "
                            "jointly labelled by the original source for that formulation. Open symbols are plotted-value estimates. "
                            "The shaded ranges select the two lowest nonzero reported inputs before examining the direction of change. "
                            "Sampling stream, irradiance and electrolyte are only assigned if documented for the corresponding arm. "
                            "The unresolved-stream series remains outside all strict liquid-parent or numeric-salt comparisons. Nominal "
                            "recipe additions are not retained mass fractions or common optimal loadings. Each positive-dose series "
                            "shares one ingredient profile, with explicit role assignments on at least one variant. Individual dose "
                            "records retain their original role assignments and experimental conditions.")
        for name,fig in figures:
            pdf.savefig(fig)
            fig.savefig(out/(name+".svg"))
            fig.savefig(out/(name+".png"),dpi=600)
            plt.close(fig)
    return captions
