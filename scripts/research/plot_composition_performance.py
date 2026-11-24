"""Render three publication figures from a frozen WaterTreatment composition cohort."""
from __future__ import annotations

import argparse
from collections import defaultdict
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
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.transforms import Bbox
import numpy as np

from composition_performance_data import digest, load, paired_series, reagent_reference


INK, MUTED, LINE, GRID = "#172F39", "#475F68", "#9BABB0", "#E1E8E9"
TEAL, PLUM = "#146C70", "#88405F"
FLUX = LinearSegmentedColormap.from_list("evaporation", ["#B6DFDB", "#66B6B5", "#327F91", "#305979", "#252F5B"])
REMOVAL = LinearSegmentedColormap.from_list("removal", ["#F8D9AE", "#EDB27D", "#D57D69", "#A24B68", "#642D59"])
RATE = "Evaporation rate (kg m⁻² h⁻¹)"
METRIC = "Parent removal (%)"
PANELS = ["01-composition-evaporation", "02-composition-pollutants", "03-formulation-response"]
SETTINGS = {"font.family": "DejaVu Sans", "font.size": 7.4, "axes.labelsize": 7.5, "axes.labelcolor": INK,
            "text.color": INK, "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 6.7,
            "ytick.labelsize": 6.7, "pdf.fonttype": 42, "svg.fonttype": "none",
            "axes.linewidth": .65, "savefig.facecolor": "white", "svg.hashsalt": "composition-figures"}


def page(title, subtitle):
    fig = plt.figure(figsize=(7.55, 6.20), facecolor="white")
    fig.text(.058, .944, title, fontsize=17.2, weight="semibold", va="top")
    fig.text(.060, .891, subtitle, fontsize=8.05, color=MUTED, va="top")
    return fig


def panel(fig, x, y, letter, title, detail=""):
    fig.text(x, y, letter, weight="bold", fontsize=13, va="center")
    fig.text(x + .033, y, title, weight="semibold", fontsize=9.6, va="center")
    if detail:
        fig.text(x + .034, y - .031, detail, fontsize=7, color=MUTED, va="center")


def footer(fig, text):
    fig.add_artist(plt.Line2D([.061, .946], [.083, .083], color=GRID, lw=.65, transform=fig.transFigure))
    fig.text(.061, .066, text, fontsize=7.1, color=MUTED, linespacing=1.35, va="top")


def tidy(ax, grid="y"):
    for key in ("top", "right"):
        ax.spines[key].set_visible(False)
    for key in ("bottom", "left"):
        ax.spines[key].set_color(LINE)
    ax.tick_params(length=2.6, width=.55, pad=2.6)
    ax.set_axisbelow(True)
    ax.grid(axis=grid, color=GRID, lw=.5)


def count_text(rows):
    counts = (len({r["profile"] for r in rows}), len(rows), len({r["doi"] for r in rows}))
    return "  ·  ".join(f"{n} {sing if n == 1 else plural}" for n, (sing, plural) in
                        zip(counts, [("profile", "profiles"), ("observation", "observations"), ("study", "studies")]))


def split_point(ax, x, y, a, b, size, edge, **kwargs):
    ax.plot(x, y, marker="o", ls="none", ms=size, fillstyle="left", markerfacecolor=a,
            markerfacecoloralt=b, markeredgecolor=edge, markeredgewidth=.62, **kwargs)


def map_labels(ax, profiles, groups, count):
    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    px = fig.dpi / 72
    box = ax.get_window_extent()
    used = []
    for p in profiles:
        x, y = ax.transData.transform((p["x"], p["y"]))
        radius = 2.7 * np.sqrt(p["variants"]) * px
        used.append(Bbox.from_extents(x - radius, y - radius, x + radius, y + radius))
    eligible = [p for p in profiles if p["id"] in groups and p["label"]]
    for p in sorted(eligible, key=lambda a: (-len(groups[a["id"]]), a["id"]))[:count]:
        x, y = ax.transData.transform((p["x"], p["y"]))
        radius = 2.7 * np.sqrt(p["variants"])
        choices = []
        for dy in (radius + 4, -radius - 4, radius + 11, -radius - 11, 0):
            for dx, ha in ((5, "left"), (-5, "right"), (13, "left"), (-13, "right")):
                txt = ax.text(x + dx * px, y + dy * px, p["label"], transform=mpl.transforms.IdentityTransform(),
                              fontsize=6.2, ha=ha, va="center")
                bb = txt.get_window_extent(renderer).expanded(1.03, 1.07)
                txt.remove()
                overlaps = sum(bb.overlaps(o) for o in used)
                inside = box.contains(bb.x0, bb.y0) and box.contains(bb.x1, bb.y1)
                choices.append((int(not inside) * 100 + overlaps * 10 + .022 * abs(dy) + .006 * abs(dx),
                                dx, dy, ha, bb))
        _, dx, dy, ha, bb = min(choices, key=lambda c: c[0])
        used.append(bb)
        ax.annotate(p["label"], (p["x"], p["y"]), xytext=(dx, dy), textcoords="offset points", ha=ha, va="center",
                    fontsize=6.2, color=INK, zorder=7, bbox=dict(fc="white", ec="none", alpha=.88, pad=.55),
                    arrowprops=dict(arrowstyle="-", lw=.42, color=LINE, shrinkA=.6, shrinkB=radius + .8))


def composition_map(fig, bounds, cohort, rows, cmap, norm, label_count):
    ax = fig.add_axes(bounds)
    profiles = cohort.profiles
    groups = defaultdict(list)
    for row in rows:
        groups[row["profile"]].append(row["value"])
    xy = np.array([(p["x"], p["y"]) for p in profiles])
    margin = np.maximum(np.ptp(xy, axis=0) * [.095, .14], .4)
    ax.set_xlim(xy[:, 0].min() - margin[0], xy[:, 0].max() + margin[0])
    ax.set_ylim(xy[:, 1].min() - margin[1], xy[:, 1].max() + margin[1])
    ax.set_aspect("equal", adjustable="box")
    ax.set_facecolor("#FAFBFB")
    for i in range(1, 4):
        ax.axvline(np.quantile(ax.get_xlim(), i / 4), color=GRID, lw=.45, ls=(0, (2, 4)), zorder=0)
        ax.axhline(np.quantile(ax.get_ylim(), i / 4), color=GRID, lw=.45, ls=(0, (2, 4)), zorder=0)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color("#D6E0E1")
        spine.set_linewidth(.52)
    ax.set_xlabel("UMAP 1", labelpad=2.8, fontsize=6.3, color=MUTED)
    ax.set_ylabel("UMAP 2", labelpad=3.2, fontsize=6.3, color=MUTED)
    by_id = {p["id"]: p for p in profiles}
    for link in cohort.projection["neighbor_links"]:
        p, q = by_id[link["left"]], by_id[link["right"]]
        ax.plot([p["x"],q["x"]], [p["y"],q["y"]], lw=.76, c="#B4C6C8", zorder=1)
    for p in sorted(profiles, key=lambda x: (x["id"] in groups, -x["variants"])):
        group = groups.get(p["id"], [])
        diameter = 5.4 * np.sqrt(p["variants"])
        if group:
            split_point(ax, p["x"], p["y"], cmap(norm(min(group))), cmap(norm(max(group))),
                        diameter, "#445960", zorder=4)
        else:
            ax.plot(p["x"], p["y"], marker="o", ls="none", ms=diameter, mfc="#EBEFF0", mec="#BCC9CC",
                    mew=.55, zorder=2)
    map_labels(ax, profiles, groups, label_count)


def continuous_key(fig, x, y, width, cmap, norm, ticks, title):
    fig.text(x, y + .034, title, fontsize=7.3, weight="semibold")
    ax = fig.add_axes([x, y, width, .017])
    bar = fig.colorbar(mpl.cm.ScalarMappable(norm=norm, cmap=cmap), cax=ax, orientation="horizontal", ticks=ticks)
    bar.outline.set_visible(False)
    bar.ax.tick_params(length=1.9, width=.5, pad=1.9, labelsize=6.4)


def map_key(fig, x, y, cmap):
    # Draw in axes coordinates so the glyph uses the same marker implementation as the map.
    ax = fig.add_axes([x, y, .265, .066], frameon=False)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_axis_off()
    split_point(ax, .036, .78, cmap(.18), cmap(.91), 8, "#445960")
    ax.text(.094, .78, "Left / right halves = min / max", va="center", fontsize=6.6)
    ax.plot(.036, .30, "o", ms=6, mfc="#EBEFF0", mec="#BCC9CC", mew=.55)
    ax.text(.094, .30, "No qualifying observation", va="center", fontsize=6.6)


def offsets(values, gap, step):
    result, placed = [], []
    for j, val in sorted(enumerate(values), key=lambda pair: pair[1]):
        blocked = {slot for v, slot in placed if abs(v - val) < gap}
        candidates = [0] + [k * sign for k in range(1, len(values) + 1) for sign in (1, -1)]
        slot = next(k for k in candidates if k not in blocked)
        placed.append((val, slot))
        result.append((j, slot * step))
    return [d for _, d in sorted(result)]


def evaporation_figure(cohort, rows):
    known = [r for r in rows if r["irradiance_sun"] is not None]
    one = [r for r in known if r["irradiance_sun"] == 1]
    higher = [r for r in known if r["irradiance_sun"] > 1]
    norm = Normalize(0, np.ceil(max(r["value"] for r in known)))
    fig = page("Composition and solar evaporation",
               f"{len(cohort.materials)} material variants  ·  {len(cohort.profiles)} retained-component profiles  ·  "
               f"{len(cohort.records)} studies")
    for x, letter, title, subset in [(.06, "a", "One sun", one), (.534, "b", "Above one sun", higher)]:
        panel(fig, x, .827, letter, title, count_text(subset))
        composition_map(fig, [x+.018, .482, .394, .287], cohort, subset, FLUX, norm, 3 if letter=="a" else 2)
    panel(fig, .06, .415, "c", "Individual observations by irradiance")
    ax = fig.add_axes([.139, .168, .469, .20])
    tidy(ax)
    levels = sorted({r["irradiance_sun"] for r in known})
    for i, level in enumerate(levels):
        values = [r["value"] for r in known if r["irradiance_sun"] == level]
        jitter = offsets(values, norm.vmax * .056, .085)
        ax.scatter(np.array(jitter) + i, values, s=18, c=values, cmap=FLUX, norm=norm,
                   edgecolors="#476570", linewidths=.38, zorder=3)
    ax.set_xlim(-.5, len(levels) - .5)
    ax.set_ylim(0, norm.vmax * 1.04)
    ax.set_xticks(range(len(levels)), [f"{v:g}" for v in levels])
    ax.set_xlabel("Irradiance (suns)", labelpad=3)
    ax.set_ylabel("Evaporation rate\n(kg m⁻² h⁻¹)", labelpad=4)
    ax.yaxis.set_major_locator(mpl.ticker.MaxNLocator(4, integer=True))
    continuous_key(fig, .679, .321, .240, FLUX, norm, np.linspace(0, norm.vmax, 3), RATE)
    map_key(fig, .681, .190, FLUX)
    fig.text(.679, .158, "Circle area = number of variants", fontsize=6.6, color=MUTED)
    fig.text(.679, .132, "Links join similar compositions", fontsize=6.6, color=MUTED)
    footer(fig, f"{len(known)} of {len(rows)} reported rates have a scalar irradiance in the observation or experiment fields.\n"
                "Water matrix, geometry and dark-rate corrections vary between experiments.")
    return fig


def removal_figure(cohort, rows, targets):
    fig = page("Composition and pollutant removal",
               "Parent-compound concentration decrease in feedwater or reaction liquid")
    norm = Normalize(0, 100)
    names = {p["id"]: p["label"] or p["id"] for p in cohort.profiles}
    for x, letter, target in zip((.06, .534), ("a", "b"), targets):
        subset = [r for r in rows if r["target"] == target]
        panel(fig, x, .827, letter, target.capitalize(), count_text(subset))
        composition_map(fig, [x+.018, .482, .394, .287], cohort, subset, REMOVAL, norm, 3)
    panel(fig, .06, .415, "c", "Individual endpoints by material profile")
    ax = fig.add_axes([.244, .164, .382, .219])
    tidy(ax, "x")
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=6.6)
    groups = defaultdict(list)
    for r in rows:
        groups[(targets.index(r["target"]), r["profile"])].append(r)
    ordered = sorted(groups, key=lambda k: (k[0], -len(groups[k]), names[k[1]]))
    for j, key in enumerate(ordered):
        subset = groups[key]
        values = [r["value"] for r in subset]
        ax.axhline(j, color=GRID, lw=.45)
        for r, jitter in zip(subset, offsets(values, 3.3, .16)):
            time = r["endpoint_min"]
            marker = "s" if time is None else "o" if time == 60 else "^"
            ax.scatter(r["value"], j+jitter, marker=marker, s=18, ec=PLUM, lw=.6,
                       fc="white" if time is None else REMOVAL(norm(r["value"])), zorder=3)
    ax.set_xlim(-1, 102)
    ax.set_ylim(len(ordered)-.5, -.6)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_yticks(range(len(ordered)), [names[k[1]] for k in ordered])
    ax.set_xlabel("Parent-compound removal (%)", labelpad=3)
    for t, target in enumerate(targets):
        positions = [j for j, key in enumerate(ordered) if key[0] == t]
        mid = (positions[0]+positions[-1])/2
        y = fig.transFigure.inverted().transform(ax.transData.transform([0, mid]))[1]
        label = "TC·HCl" if target == "tetracycline hydrochloride" else target.capitalize()
        fig.text(.061, y, label, fontsize=6.35, weight="semibold", color=MUTED, va="center")
        if t:
            ax.axhline(positions[0]-.5, color=LINE, lw=.58, ls=(0,(2,2)))
    continuous_key(fig, .683, .345, .235, REMOVAL, norm, [0, 50, 100], METRIC)
    legend = fig.add_axes([.681, .141, .278, .143], frameon=False)
    legend.set_xlim(0, 1)
    legend.set_ylim(0, 1)
    legend.set_axis_off()
    other_times = sorted({r["endpoint_min"] for r in rows if r["endpoint_min"] not in {None, 60}})
    other = ", ".join(f"{v:g}" for v in other_times)+" min"
    for y, m, label in [(.87,"o","60 min"), (.55,"^",other), (.23,"s","Endpoint time unresolved")]:
        legend.plot(.034, y, marker=m, ls="none", ms=4.5, mfc="white" if m=="s" else REMOVAL(.6), mec=PLUM, mew=.6)
        legend.text(.095, y, label, va="center", fontsize=6.6)
    footer(fig, "Map halves show the observed range. Links indicate shared constituents between chemical neighbours.\n"
                "Grey sites lack a qualifying endpoint. Time, oxidant, light and electrical input vary, including control conditions.")
    return fig


def case_curve(ax, pairs, field, color, xlabel, ylabel):
    tidy(ax)
    xs = [r["dose"] for r in pairs]
    vals = [r[field]["value"] for r in pairs]
    ax.plot(xs, vals, color=color, lw=1.2, zorder=2)
    for x, y, p in zip(xs, vals, pairs):
        estimate = p[field]["status"] == "estimated"
        ax.plot(x, y, "o", ms=6.5, mfc="white" if estimate else color, mec=color, mew=1.05, zorder=3)
        # Prefix distinguishes source precision from approximate figure readings in print.
        value = ("≈" if estimate else "") + (f"{y:.2f}" if field == "evaporation" else f"{y:g}")
        # Position valley labels below their marker to clear descending line segments.
        j = xs.index(x)
        valley = (j < len(xs)-1 and vals[j+1] > y and (j == 0 or vals[j-1] > y))
        offset = -12 if valley and field == "evaporation" else 7.3
        ax.annotate(value, (x, y), xytext=(0, offset), textcoords="offset points", ha="center",
                    fontsize=7.4, weight="semibold", color=color)
    spread = max(vals) - min(vals)
    ax.set_ylim(min(vals)-.34*spread, max(vals)+.5*spread)
    ax.set_xlim(min(xs)-.12*np.ptp(xs), max(xs)+.12*np.ptp(xs))
    ax.set_xticks(xs, [f"{x:.2f}" for x in xs])
    ax.yaxis.set_major_locator(mpl.ticker.MaxNLocator(4))
    ax.set_xlabel(xlabel, labelpad=4)
    ax.set_ylabel(ylabel, labelpad=4)


def formulation_figure(cohort, case, loading, reagent, ref):
    material, = {r["removal"]["profile"] for r in loading}
    sun, = {r["evaporation"]["irradiance_sun"] for r in loading+reagent}
    end, = {r["removal"]["endpoint_min"] for r in loading+reagent}
    fig = page(f"Formulation response within {case['name']}",
               f"Profile {material}  ·  {case['substrate_concentration']}  ·  {sun:g} sun  ·  {end:g}-min treatment")
    panel(fig, .06, .827, "a", "Evaporation across the loading series", case["reagent_concentration"])
    panel(fig, .535, .827, "b", "Phenol removal across the same series", case["reagent_concentration"])
    a = fig.add_axes([.136, .525, .308, .225])
    b = fig.add_axes([.616, .525, .308, .225])
    case_curve(a, loading, "evaporation", TEAL, case["loading"]["axis"], "Rate (kg m⁻² h⁻¹)")
    case_curve(b, loading, "removal", PLUM, case["loading"]["axis"], "Phenol removal (%)")
    panel(fig, .06, .402, "c", "Oxidant response at fixed material loading",
          f"{ref['loading']:.2f} g nominal Co₃O₄ addition")
    ax = fig.add_axes([.138, .163, .470, .164])
    tidy(ax)
    entries = sorted([(r["dose"], r["removal"]["value"], False) for r in reagent]
                     + [(ref["dose"], ref["removal"]["value"], True)])
    ax.plot([r[0] for r in entries], [r[1] for r in entries], color=PLUM, lw=1.0)
    for x, y, shared in entries:
        ax.plot(x, y, marker="D" if shared else "o", ls="none", ms=5.7, mfc="white" if shared else PLUM,
                mec=PLUM, mew=1.0)
        ax.annotate(f"{y:g}", (x, y), xytext=(0, 5.0), textcoords="offset points", ha="center",
                    fontsize=6.9, color=PLUM)
    ax.set_ylim(0, 117)
    ax.set_xlim(entries[0][0]-.25, entries[-1][0]+.25)
    ax.set_xticks([r[0] for r in entries])
    ax.set_yticks([0, 50, 100])
    ax.set_xlabel(case["reagent"]["axis"], labelpad=3)
    ax.set_ylabel("Phenol removal (%)", labelpad=4)
    side = fig.add_axes([.669, .149, .28, .182], frameon=False)
    side.set_xlim(0,1)
    side.set_ylim(0,1)
    side.set_axis_off()
    side.text(0, 1, "Source of each value", va="top", fontsize=7.8, weight="semibold")
    for y, marker, fill, label in [(.69,"o",PLUM,"Reported by the authors"),
                                   (.41,"o","white","Estimated from the plot (≈)"),
                                   (.13,"D","white","Shared reference from panel b")]:
        side.plot(.035, y, marker=marker, ls="none", ms=5.8, mfc=fill, mec=PLUM, mew=1)
        side.text(.108, y, label, va="center", fontsize=6.65)
    footer(fig, "Loading is the mass added during preparation. Panels a and b share nominal conditions. PMS = peroxymonosulfate.\n"
                f"Source  {case['doi']}  ·  {case['source_panels']}.")
    return fig


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def archive_data(out, cohort, source, selection_path, selection, loading, reagent, ref):
    figures = defaultdict(list)
    for p in loading:
        for k in ("evaporation", "removal"):
            r = p[k]
            figures[(r["record_id"],r["observation_id"])].append("3a" if k=="evaporation" else "3b")
    for p in reagent + [ref]:
        r = p["removal"]
        figures[(r["record_id"],r["observation_id"])].append("3c")
    evidence_rows = []
    for row in cohort.rows:
        raw = row["raw"]
        record = cohort.records[row["record_id"]]
        lookup = record["evidence"]
        eid = row["experiment_id"]
        obs_key = (row["record_id"],row["observation_id"])
        displayed = figures[obs_key].copy()
        if row["status"] == "reported" and row["profile"]:
            if row["outcome"] == "removal":
                displayed += ["2a" if row["target"] == selection["targets"][0] else "2b", "2c"]
            elif row["irradiance_sun"] is not None:
                displayed += ["1c"]
                if row["irradiance_sun"] >= 1:
                    displayed += ["1a" if row["irradiance_sun"] == 1 else "1b"]
        evidence_rows.append({**{k:v for k,v in row.items() if k != "raw"}, "panels": "|".join(displayed),
                              "evidence_ids": "|".join(raw["evidence_ids"]),
                              "locators": json.dumps([lookup[k] for k in raw["evidence_ids"]], ensure_ascii=False),
                              "conditions": json.dumps(record["experiments"][eid], ensure_ascii=False),
                              "observation": json.dumps(raw, ensure_ascii=False)})
    write_csv(out/"observations.csv", list(evidence_rows[0]), evidence_rows)
    write_csv(out/"material-variants.csv", ["profile","uid","doi","record_id","material_id","name","role",
              "reported_retained_amounts"], cohort.materials)
    prof = [{**p, "retained_components": json.dumps(p["signature"], ensure_ascii=False)} for p in cohort.profiles]
    write_csv(out/"composition-profiles.csv", ["id","label","x","y","variants","papers","retained_components"], prof)
    links = [{**link, "shared_descriptors": json.dumps(link["shared_descriptors"], ensure_ascii=False)}
             for link in cohort.projection["neighbor_links"]]
    write_csv(out/"composition-neighbors.csv", ["left","right","distance","shared_descriptors"], links)
    script = Path(__file__).resolve()
    meta = {"source_export": str(source.resolve()), "materials_sha256": cohort.input_sha,
            "export_manifest_sha256": digest(source/"manifest.json"), "selection_sha256": digest(selection_path),
            "projection": cohort.projection, "runtime": {p: version(p) for p in
            ("matplotlib","numpy","scipy","scikit-learn","umap-learn","numba")},
            "code": {f: digest(script.with_name(f)) for f in (script.name, "composition_performance_data.py",
                     "plot_material_space.py", "plot_material_atlas.py","build_material_mapping.py")},
            "papers": [{k: v["meta"][k] for k in ("record_id","record_path","record_sha256","sources","paper_id",
                       "dataset_role","snapshot_id")} for v in cohort.records.values()],
            "paired_evidence": [{"dose": p["dose"],"pair": p["pair"],"joint": p["joint"]}
                                for p in loading+reagent], "shared_reference": ref["association"],
            "scope": "Published extraction snapshot. Data are descriptive and include study-specific controls. "
                     "No held-out performance prediction or independent scientific validation is claimed."}
    dump(out/"figure-provenance.json", meta)
    shutil.copyfile(selection_path, out/"case-selection.json")


def captions(out, cohort, rates, outcomes, selection, loading, reagent, ref):
    one = [r for r in rates if r["irradiance_sun"] == 1]
    high = [r for r in rates if r["irradiance_sun"] is not None and r["irradiance_sun"] > 1]
    known = sum(r["irradiance_sun"] is not None for r in rates)
    case = selection["case"]
    sweep = [p["removal"] for p in reagent]
    targets = selection["targets"]
    peak = max(loading, key=lambda p:p["evaporation"]["value"])
    amount_count = sum(m["reported_retained_amounts"] for m in cohort.materials)
    target_counts = [count_text([r for r in outcomes if r["target"] == t]).replace("  ·  ", ", ") for t in targets]
    paragraphs = [
        "Figure 1. Composition and solar evaporation",
        f"The composition-only UMAP places {len(cohort.materials)} final material variants from {len(cohort.records)} "
        f"studies at {len(cohort.profiles)} distinct positions. Circle area is proportional to the number of registered "
        "variants sharing an identical documented component profile. Within each site, left and right halves show the "
        "lowest and highest author-reported evaporation rates under the irradiance selection. Small ranges may have "
        f"visually identical halves. Panel a covers one sun ({count_text(one)}). Panel b covers higher irradiances "
        f"({count_text(high)}). Panel c shows one dot per observation across all resolved irradiances, including dark "
        f"controls. Of {len(rates)} reported rates in kg m⁻² h⁻¹, {known} have a usable scalar irradiance at observation "
        "or experiment level. A sun was converted to 1 kW m⁻². Small horizontal offsets separate points at the same "
        "irradiance. Experiments differ in matrix, geometry, additional energy inputs and dark-rate corrections.",
        "Figure 2. Composition and pollutant removal",
        f"The same material coordinates show parent-compound removal for {targets[0]} ({target_counts[0]}) and "
        f"{targets[1]} ({target_counts[1]}). The two panels have the same 0–100% colour scale. Each point in panel c "
        "is an author-reported parent-compound concentration decrease in feedwater or reaction liquid in a catalytic "
        "or coupled experiment, including its controls. Only material-owned observations with an explicit target and "
        "stream are included. Observation-specific treatment time determines the symbol. The source table preserves "
        "other doses, input energies and experimental conditions. These values quantify disappearance of the parent "
        "compound. Establishing mineralisation or intrinsic reaction kinetics requires additional measurements.",
        f"Figure 3. Formulation response within {case['name']}",
        f"The material loading series includes {len(loading)} nominal preparation doses. Panels a and b retain the "
        "evaporation and liquid-phase removal outcomes owned by each variant under the same nominal experimental "
        "conditions. Solid points are author-reported values. Open circles with ≈ are estimates read from the "
        f"paper's plots. At {peak['dose']:.2f} g the reported evaporation rate reaches {peak['evaporation']['value']:.2f} "
        f"kg m⁻² h⁻¹. At {ref['loading']:.2f} g, phenol removal reaches {ref['removal']['value']:g}%. The oxidant "
        f"screen in panel c uses that fixed recipe. Author-reported removal spans {min(r['value'] for r in sweep):g}% "
        f"to {max(r['value'] for r in sweep):g}%. The diamond at {ref['dose']:g} mM repeats the nominal reference "
        "from panel b. Physical replicate identities and error-bar bases for individual plotted estimates are unresolved. "
        f"Points are connected in dose order. Source DOI {case['doi']}, {case['source_panels']} and Supplementary Note S3.",
        "Composition coordinates and evidence",
        "Reported retained-component names and formulae define each profile. Distinct materials remain separately "
        "addressable in material-variants.csv. Pairwise similarity combines Jaccard overlap of exact name/formula tokens "
        "(0.50), Jaccard overlap of explicit elements in parseable formulae (0.30), and cosine similarity of component "
        "names represented by character 2–4-gram TF-IDF (0.20). Weights are renormalised over available pairwise "
        "blocks. The two-dimensional UMAP uses the precomputed distances, eight neighbours, a minimum distance of 0.60 "
        f"and random seed 42. Trustworthiness at five neighbours is {cohort.projection['trustworthiness']:.3f}. "
        "Links join reciprocal top-two neighbours that have at least one exact component-name or chemical-formula token "
        "in common. Link descriptors are available in composition-neighbors.csv. A shared coordinate denotes agreement "
        "of the reported ingredient list at the available level of detail. Missing components, alternative names and "
        "unreported retained fractions limit chemical resolution.",
        f"The snapshot contains {amount_count} reported retained-component amounts in the selected variants. "
        f"{sum(p['papers'] == 1 for p in cohort.profiles)} of {len(cohort.profiles)} ingredient profiles occur "
        "in only one source study. Figure 3 uses a preparation variable explicitly supplied by the source. The maps describe "
        "literature observations and study-specific protocols. Their neighbourhood score measures embedding fidelity. "
        "Out-of-study prediction of either outcome would require a separate validation design.",
        "Traceability",
        "observations.csv contains material, experiment and observation IDs, original measurement fields, procedure "
        "details, source locators and display membership for each accepted numeric observation or plot estimate. "
        "The panels column identifies the marks shown in the figures. figure-provenance.json records source hashes, "
        "software versions, pairing evidence and reference ownership. case-selection.json supplies the case-study "
        "selectors and the display labels used across figures.",
    ]
    (out/"figure-captions.txt").write_text("\n\n".join(paragraphs)+"\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "selection", "out"):
        parser.add_argument("--"+arg, required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("Choose an empty destination to preserve earlier figures")
    selection = json.loads(args.selection.read_bytes())
    if len(selection["targets"]) != 2 or len(set(selection["targets"])) != 2:
        raise ValueError("Choose two distinct pollutants for the paired atlas")
    cohort = load(args.source, selection)
    case = selection["case"]
    loading = paired_series(cohort, case, case["loading"])
    reagent = paired_series(cohort, case, case["reagent"])
    ref = reagent_reference(cohort, case, loading, reagent)
    reported = [r for r in cohort.rows if r["status"] == "reported" and r["profile"]]
    rates = [r for r in reported if r["outcome"] == "evaporation"]
    outcomes = [r for r in reported if r["outcome"] == "removal"]
    args.out.mkdir(parents=True, exist_ok=True)
    with mpl.rc_context(SETTINGS), PdfPages(args.out/"composition-performance.pdf",
            metadata={"Title": "Material composition and water-treatment performance"}) as pdf:
        figs = [evaporation_figure(cohort, rates), removal_figure(cohort, outcomes, selection["targets"]),
                formulation_figure(cohort, case, loading, reagent, ref)]
        for name, fig in zip(PANELS, figs):
            pdf.savefig(fig)
            fig.savefig(args.out/(name+".svg"))
            fig.savefig(args.out/(name+".png"), dpi=450)
            plt.close(fig)
    archive_data(args.out, cohort, args.source, args.selection, selection, loading, reagent, ref)
    captions(args.out, cohort, rates, outcomes, selection, loading, reagent, ref)
    with zipfile.ZipFile(args.out/"source-data.zip", "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for f in sorted([*args.out.glob("*.csv"), *args.out.glob("*.json"), args.out/"figure-captions.txt"]):
            bundle.write(f,f.name)
    with zipfile.ZipFile(args.out/"editable-figures.zip", "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for f in [*(args.out/(name+".svg") for name in PANELS), args.out/"figure-captions.txt"]:
            bundle.write(f, f.name)
    print(f"{len(cohort.materials)} variants, {len(cohort.profiles)} compositions, "
          f"{len(rates)} reported rates, {len(outcomes)} reported pollutant endpoints")


if __name__ == "__main__":
    main()
