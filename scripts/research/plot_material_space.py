"""Map a frozen WaterTreatment cohort using reported material descriptors and shared UMAP coordinates."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from importlib.metadata import version
import json
import math
from pathlib import Path
import unicodedata

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Circle, Wedge
import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import trustworthiness
from sklearn.metrics.pairwise import cosine_similarity
from umap import UMAP

from build_material_mapping import labels
from plot_material_atlas import VARIANT_ROLES, name, sha, verified_bytes


INK, MUTED, LINE, MISSING = "#1F3445", "#4E6572", "#D5DFE4", "#D9E1E5"
CHEM_COLORS = ["#146F73", "#C5703D", "#67609C", "#506D85"]
TEST_COLORS = {"coupled": "#B94E39", "catalysis": "#3F7FA1", "both": "#725187"}
WEIGHTS = {"components": .60, "form_and_roles": .25, "name": .15}
SEED = 42
ORIGINS = {"reported_measurement", "author_calculation"}
PURPOSES = {"evaporation", "catalysis", "coupled"}


def reported(fact):
    if fact and fact.get("status") == "reported":
        return fact.get("value")
    return None


def clean(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def descriptors(mat, vocabulary):
    components, functions = set(), set()
    for part in mat.get("components", []):
        if part["retention"] != "retained":
            continue
        substance = part["substance"]
        component = clean(reported(substance.get("name"))).casefold()
        formula = clean(reported(substance.get("formula")))
        if component:
            components.add("name:" + component)
        if formula:
            # The case of a chemical formula can distinguish different substances.
            components.add("formula:" + formula)
        for role in part.get("roles", []):
            value = reported(role)
            for term in (value if isinstance(value, list) else [value]):
                if ("role", term) in vocabulary:
                    functions.add("role:" + term)
    form = labels(mat, "form", {"reported"}, vocabulary)
    functions.update("form:" + term for term in form if ("form", term) in vocabulary)
    return {"components": sorted(components), "form_and_roles": sorted(functions),
            "name": clean(reported(mat.get("name"))).casefold()}


def rates_unit(unit):
    normalized = clean(unit).replace("−", "-").replace("^", "").replace(" ", "").replace("·", "")
    return normalized in {"kgm-2h-1", "kg/m2/h", "kg/(m2h)"}


def observations(doc, selected, target):
    experiments = {e["id"]: e for e in doc["experiments"]}
    target_ids = {s["id"] for s in doc["substances"] if clean(reported(s["name"])).casefold() == target}
    result = {mid: {"rates": [], "target_observations": []} for mid in selected}
    used_experiments = set()
    for obs in doc["observations"]:
        mid = obs.get("material_id")
        value = reported(obs.get("value"))
        if (mid not in selected or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or obs["origin"] not in ORIGINS):
            continue
        exp = experiments[obs["experiment_id"]]
        if exp["purpose"] not in PURPOSES or mid not in exp["material_ids"]:
            continue
        unit = reported(obs.get("unit"))
        rate = (exp["purpose"] in {"evaporation", "coupled"} and obs["metric_kind"] == "evaporation_rate"
                and rates_unit(unit))
        sid = obs.get("substance_id")
        target_result = (exp["purpose"] in {"catalysis", "coupled"} and sid in target_ids
                         and any(t["substance_id"] == sid for t in exp["targets"]))
        if not (rate or target_result):
            continue
        used_experiments.add(exp["id"])
        item = {k: obs.get(k) for k in ("id", "experiment_id", "material_id", "substance_id", "metric_kind",
                                        "origin", "stream_id", "time_window", "coordinates", "evidence_ids", "limitations")}
        item.update(value=value, unit=unit, metric_label=reported(obs.get("metric")), purpose=exp["purpose"])
        if rate:
            result[mid]["rates"].append(item)
        if target_result:
            result[mid]["target_observations"].append(item)
    return result, {key: experiments[key] for key in sorted(used_experiments)}


def load_records(source, taxonomy_path, target):
    manifest = json.loads((source / "manifest.json").read_bytes())
    payload = json.loads(verified_bytes(source / "materials.json", manifest["outputs"]["materials.json"]["sha256"]))
    tax = json.loads(verified_bytes(taxonomy_path, manifest["taxonomy"]["sha256"]))
    vocabulary = {(a["id"], t["id"]): t["id"] for a in tax["axes"] for t in a["terms"]}
    subsets = defaultdict(list)
    for mat in payload["materials"]:
        if mat["role"] in VARIANT_ROLES:
            subsets[mat["record_id"]].append(mat)
    points, articles = [], {}
    for rec in sorted(payload["records"], key=lambda r: r["record_id"]):
        rid = rec["record_id"]
        doc = json.loads(verified_bytes(Path(rec["record_path"]), rec["record_sha256"]))
        subset = subsets.pop(rid, [])
        observed, experiments = observations(doc, {m["material_id"] for m in subset}, target)
        doi = clean(reported(rec["doi"])).casefold()
        articles[rid] = {k: rec[k] for k in ("paper_id", "snapshot_id", "record_path", "record_sha256",
                                              "dataset_role", "sources")}
        articles[rid].update(doi=doi, experiments=experiments)
        for mat in subset:
            if mat["paper_id"] != rec["paper_id"] or mat["snapshot_id"] != rec["snapshot_id"]:
                raise ValueError("Material and record snapshots do not match")
            chemistry = labels(mat, "chemistry", {"reported", "inferred"}, vocabulary)
            point = {k: mat[k] for k in ("uid", "material_id", "record_id", "paper_id", "snapshot_id", "role")}
            point.update(doi=doi, name=mat["name"].get("value"), name_status=mat["name"]["status"],
                         descriptors=descriptors(mat, vocabulary),
                         chemistry={k: v for k, v in chemistry.items() if ("chemistry", k) in vocabulary},
                         **observed[mat["material_id"]])
            points.append(point)
    if subsets or len({p["uid"] for p in points}) != len(points):
        raise ValueError("Unbound or duplicate variant identities")
    return sorted(points, key=lambda p: p["uid"]), articles, manifest


def descriptor_distances(profiles):
    n = len(profiles)
    numerator, denominator = np.zeros((n, n)), np.zeros((n, n))
    for field, weight in WEIGHTS.items():
        values = [d[field] for d in profiles]
        mask = np.outer([bool(v) for v in values], [bool(v) for v in values])
        if not mask.any():
            continue
        if field == "form_and_roles":
            vec = DictVectorizer().fit_transform([{t: 1 for t in tags} for tags in values])
        else:
            # Within-word character fragments handle formulae, acronyms and variant designations.
            texts = [" ".join(v) if isinstance(v, list) else v for v in values]
            vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), lowercase=False).fit_transform(texts)
        numerator += weight * mask * cosine_similarity(vec)
        denominator += weight * mask
    if np.any(denominator == 0):
        raise ValueError("At least one profile pair has no comparable descriptor blocks")
    distance = np.clip(1 - numerator / denominator, 0, 1)
    np.fill_diagonal(distance, 0)
    return distance


def feature_map(points):
    groups = defaultdict(list)
    for p in points:
        if not any(p["descriptors"].values()):
            p["position"] = None
            continue
        key = json.dumps(p["descriptors"], ensure_ascii=False, sort_keys=True)
        groups[key].append(p)
    groups = [groups[key] for key in sorted(groups)]
    n = len(groups)
    if n < 4:
        raise ValueError("Insufficient distinct documented profiles for a two-dimensional map")
    distance = descriptor_distances([g[0]["descriptors"] for g in groups])
    neighbors = min(20, n - 1)
    projection = UMAP(metric="precomputed", n_components=2, n_neighbors=neighbors,
                      min_dist=.65, random_state=SEED, n_jobs=1, init="spectral")
    xy = projection.fit_transform(distance).astype(float)
    if not np.isfinite(xy).all():
        raise ValueError("Nonfinite UMAP coordinates")
    for index, (g, pair) in enumerate(zip(groups, xy), 1):
        for p in g:
            p.update(profile=index, position=pair.tolist())
    k = min(5, (n - 1) // 2)
    return groups, {"n_neighbors": neighbors, "min_dist": .65, "seed": SEED, "distinct_profiles": n,
                    "trustworthiness_k": k, "trustworthiness": float(trustworthiness(distance, xy, n_neighbors=k, metric="precomputed"))}


def glyph(ax, xy, colors, diameter, border="white", zorder=3):
    box = DrawingArea(diameter, diameter)
    r = diameter / 2
    if len(colors) == 1:
        box.add_artist(Circle((r, r), r, fc=colors[0], ec="none"))
    else:
        for i, color in enumerate(colors):
            box.add_artist(Wedge((r, r), r, 90+i*360/len(colors), 90+(i+1)*360/len(colors),
                                 fc=color, ec="white", lw=.2))
    box.add_artist(Circle((r, r), r, fc="none", ec=border, lw=.45))
    ax.add_artist(AnnotationBbox(box, xy, xycoords="data", frameon=False, pad=0, zorder=zorder))


def chemistry_bins(group, named):
    bins = {}
    for point in group:
        for term, status in point["chemistry"].items():
            key = term if term in named else "other"
            if bins.get(key) != "reported":
                bins[key] = status
    return bins


def tint(color):
    rgb = np.array(mpl.colors.to_rgb(color))
    return .55 + .45 * rgb


def target_test_type(point):
    kinds = {o["purpose"] for o in point["target_observations"]}
    return "both" if len(kinds) == 2 else next(iter(kinds), "none")


def lookup_pair(case, articles, ids):
    """Only display two case numbers together if the frozen experiment explicitly links them."""
    rates = {o["id"]: o for o in case["rates"]}
    target_obs = {o["id"]: o for o in case["target_observations"]}
    rate, target = rates[ids[0]], target_obs[ids[1]]
    if rate["experiment_id"] != target["experiment_id"]:
        raise ValueError("Case observations must belong to the same experiment")
    exp = articles[case["record_id"]]["experiments"][rate["experiment_id"]]
    choices = [p for p in exp["pairs"] if p["level"] in {"P1","P2"}
               and {p["left_observation_id"],p["right_observation_id"]} == set(ids)
               and p["evidence_ids"] and not p["differing_conditions"]]
    if len(choices) != 1:
        raise ValueError("Case observations require one explicit, compatible P1 or P2 pair")
    return {"rate": rate, "target_observation": target, "pair": choices[0], "joint_evidence": exp["joint_evidence"]}


def figure(points, groups, target, case, case_pair):
    plt.rcParams.update({"font.family": "DejaVu Sans", "pdf.fonttype": 42, "svg.fonttype": "none",
                         "axes.unicode_minus": False, "text.color": INK})
    fig = plt.figure(figsize=(9.35, 5.25), facecolor="white")
    def note(x, y, words, size=8, **kw):
        return fig.text(x, y, words, va="center", fontsize=size, **kw)
    def key(x, y, words, color):
        note(x, y, "●", 9, color=color)
        note(x+.016, y, words, 7.6, color=MUTED)
    n = len(points)
    xy = np.array([g[0]["position"] for g in groups])
    counts = Counter(t for p in points for t in p["chemistry"])
    named = sorted(counts, key=lambda t: (-counts[t], t))[:3]
    palette = dict(zip(named+["other"], CHEM_COLORS))
    all_rates = [o["value"] for p in points for o in p["rates"]]
    vmax = max(all_rates, default=1)
    norm = mpl.colors.Normalize(min(0., min(all_rates, default=0.)), max(1., math.ceil(vmax)))
    cmap = mpl.colormaps["viridis"]
    article_count = len({p["record_id"] for p in points})
    map_count = sum(len(g) for g in groups)
    note(.038, .935, "Material space of solar evaporation and catalytic water treatment", 16.1, weight="bold")
    note(.039, .872, f"{map_count} variants   /   {article_count} articles   /   {len(groups)} distinct descriptor profiles on a shared UMAP", 9.4, color=MUTED)
    if n != map_count:
        note(.665, .872, f"{n-map_count} variants lack mapping features", 8, color=MUTED)
    lefts = [.039, .363, .686]
    subtitles = [f"{sum(bool(p['chemistry']) for p in points)} variants with class labels",
                 f"{sum(bool(p['rates']) for p in points)} variants with reported rates",
                 f"{sum(bool(p['target_observations']) for p in points)} variants with target observations"]
    titles = ["Chemical classes", "Evaporation rate", f"{target.capitalize()} experiments"]
    for i, left in enumerate(lefts):
        note(left, .778, "abc"[i], 14, weight="bold")
        note(left+.025, .778, titles[i], 10.9, weight="bold")
        note(left+.001, .734, subtitles[i], 8.0, color=MUTED)
    span = np.ptp(xy, axis=0)
    spread = max(max(span), 1.)
    limits = [(lo-.07*spread, hi+.07*spread) for lo, hi in zip(xy.min(axis=0), xy.max(axis=0))]
    axes = []
    for left in lefts:
        ax = fig.add_axes([left, .295, .281, .408])
        ax.set_xlim(*limits[0])
        ax.set_ylim(*limits[1])
        ax.set_aspect("equal")
        ax.set_axis_off()
        axes.append(ax)
    for g in groups:
        pos = g[0]["position"]
        size = 6.2 + .9 * math.log2(len(g))
        bins = chemistry_bins(g, named)
        cats = [palette[k] if bins[k] == "reported" else tint(palette[k]) for k in palette if k in bins]
        glyph(axes[0], pos, cats or [MISSING], size, zorder=4 if cats else 2)
        rates = sorted({o["value"] for p in g for o in p["rates"]})
        glyph(axes[1], pos, [cmap(norm(v)) for v in rates] or [MISSING], size, zorder=4 if rates else 2)
        states = sorted({target_test_type(p) for p in g})
        glyph(axes[2], pos, [TEST_COLORS.get(s, MISSING) for s in states], size,
              zorder=4 if any(s != "none" for s in states) else 2)
    if case:
        for ax in axes:
            ax.scatter(*case["position"], s=108, facecolors="none", edgecolors=INK, lw=.85, zorder=7)
    for i, k in enumerate(named+["other"]):
        key(lefts[0]+(i%2)*.134, .260-(i//2)*.045, name(k) if k != "other" else "Other classes", palette[k])
    note(lefts[0], .158, "Pale sectors indicate inferred labels.", 7.2, color=MUTED)
    cbax = fig.add_axes([lefts[1]+.002, .249, .215, .017])
    cb = fig.colorbar(mpl.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cbax, orientation="horizontal")
    cb.outline.set_visible(False)
    ticks = mpl.ticker.MaxNLocator(nbins=4, integer=True).tick_values(norm.vmin, norm.vmax)
    cb.set_ticks(ticks[(ticks >= norm.vmin) & (ticks <= norm.vmax)])
    cb.ax.xaxis.set_major_formatter(mpl.ticker.FormatStrFormatter("%g"))
    cb.ax.tick_params(labelsize=7.1, length=2, color=LINE, labelcolor=MUTED, pad=2)
    note(lefts[1], .184, "kg m⁻² h⁻¹  ·  test conditions vary", 7.6, color=MUTED)
    note(lefts[1], .145, "Sectors show distinct reported values.", 7.2, color=MUTED)
    key(lefts[2], .260, "Coupled tests only", TEST_COLORS["coupled"])
    key(lefts[2], .215, "Catalysis tests only", TEST_COLORS["catalysis"])
    key(lefts[2], .170, "Both test types", TEST_COLORS["both"])
    fig.add_artist(mpl.lines.Line2D([.039,.967],[.114,.114], transform=fig.transFigure, color=LINE, lw=.7))
    if case:
        heading = "Ringed case    " + str(case["name"] or case["material_id"])
        if case_pair:
            rate, obs = case_pair["rate"], case_pair["target_observation"]
            heading += f"    /    {rate['value']:.2f} kg m⁻² h⁻¹    /    {obs['value']:g}{obs['unit'] or ''} {obs['metric_label'] or obs['metric_kind']}"
        note(.039, .078, heading, 8.4, weight="bold")
    footer = "Grey indicates no eligible record in that panel."
    if case_pair:
        level = case_pair["pair"]["level"]
        footer += ("   Case values share a nominal condition group (P2)."
                   if level == "P2" else "   Case values are linked by an explicit P1 pair in the source record.")
    note(.039, .033, footer, 7.3, color=MUTED)
    return fig, named


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target", default="phenol")
    parser.add_argument("--case-doi")
    parser.add_argument("--case-material")
    parser.add_argument("--case-observations", nargs=2, metavar=("RATE_ID", "TARGET_ID"))
    args = parser.parse_args()
    source = args.export_dir.resolve(strict=True)
    target = clean(args.target).casefold()
    if bool(args.case_doi) != bool(args.case_material) or (args.case_observations and not args.case_doi):
        parser.error("Use both case arguments together")
    points, articles, manifest = load_records(source, args.taxonomy, target)
    groups, projection = feature_map(points)
    case = None
    if args.case_doi:
        matches = [p for p in points if p["doi"] == args.case_doi.casefold() and p["material_id"] == args.case_material]
        if len(matches) != 1 or matches[0]["position"] is None:
            raise ValueError("The requested case is not a distinct mapped variant")
        case = matches[0]
    case_pair = lookup_pair(case, articles, args.case_observations) if args.case_observations else None
    fig, named = figure(points, groups, target, case, case_pair)
    dest = args.output_dir.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    for ext in ("pdf", "svg", "png"):
        fig.savefig(dest / f"material-space.{ext}", dpi=320, facecolor="white")
    plt.close(fig)
    fields = ["uid","doi","paper_id","material_id","name","name_status","role","profile","umap_1","umap_2",
              "chemistry","reported_rates_kg_m2_h","target_test_type"]
    with (dest/"material-space-points.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for p in points:
            x, y = p["position"] or (None, None)
            writer.writerow({**{k: p.get(k) for k in fields[:8]}, "umap_1": x, "umap_2": y,
                             "chemistry": json.dumps(p["chemistry"], ensure_ascii=False),
                             "reported_rates_kg_m2_h": json.dumps(sorted({o["value"] for o in p["rates"]})),
                             "target_test_type": target_test_type(p)})
    summary = {"articles": len({p["record_id"] for p in points}), "variants": len(points),
               "mapped": sum(len(g) for g in groups), "rates": sum(bool(p["rates"]) for p in points),
               "target": target, "target_coverage": dict(Counter(target_test_type(p) for p in points)),
               "case_uid": case["uid"] if case else None}
    data = {"version": "material-space-v1", "reference_doi": "10.1002/adma.75148", "summary": summary,
            "inputs": {"source_manifest": str(source/"manifest.json"), "source_manifest_sha256": sha((source/"manifest.json").read_bytes()),
                       "materials_sha256": manifest["outputs"]["materials.json"]["sha256"], "taxonomy_sha256": manifest["taxonomy"]["sha256"]},
            "method": {"descriptor_block_weights": WEIGHTS, "pairwise_missing_blocks": "omit and renormalize over jointly documented blocks",
                       "text": "NFKC, within-word 2–4 character TF-IDF; reported components, labels and roles only; no performance fields",
                       "map": "UMAP of weighted cosine dissimilarities between distinct descriptor profiles; identical profiles share coordinates",
                       "projection": projection, "chemistry": {"named_top_three": named, "policy": "reported and inferred; remaining classes pooled; equal sectors are labels, not mass fractions"},
                       "rates": "Distinct reported numeric evaporation_rate values in kg m-2 h-1, in evaporation/coupled experiments; author calculation or measurement; no pooling, maxima selection or normalization across operating conditions",
                       "target": "Exact reported substance name; targeted numeric result owned by the same variant and experiment, in catalysis/coupled tests. Does not itself establish degradation, joint performance or mineralization.",
                       "interpretation": "Exploratory candidate records. Geometry is local, and can reflect study-specific material naming. No recommendation or validation is inferred."},
            "software": {p: version(p) for p in ("numpy","scikit-learn","umap-learn","matplotlib","scipy","numba")},
            "code_sha256": {path.name: sha(path.read_bytes()) for path in
                            (Path(__file__), Path(__file__).with_name("plot_material_atlas.py"),
                             Path(__file__).with_name("build_material_mapping.py"))},
            "case_observation_pair": case_pair,
            "articles": articles, "points": points,
            "file_sha256": {p.name: sha(p.read_bytes()) for p in sorted(dest.iterdir())}}
    (dest/"material-space-source.json").write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(json.dumps({**summary, **projection}, ensure_ascii=False))


if __name__ == "__main__":
    main()
