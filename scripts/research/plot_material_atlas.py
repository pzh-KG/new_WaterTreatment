"""Build a print figure from a frozen, hash-bound material mapping export."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from math import cos, radians, sin, sqrt
from pathlib import Path

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgb
from matplotlib.patches import Circle, Rectangle, Wedge
from PIL import Image

from build_material_mapping import labels


FAMILIES = {"chemistry": "Chemical class", "form": "Material form",
            "role": "Retained-component function"}
NAMES = {"polymer": "Polymer", "metal": "Metal / alloy", "carbide_nitride": "Carbide / nitride",
         "multicomponent_assembly": "Multicomponent assembly", "photothermal": "Photothermal heating",
         "support": "Structural support", "catalyst": "Catalysis", "water_supply": "Water supply",
         "oxidant_activator": "Oxidant activation", "mxene": "MXene", "mof": "MOF", "cof": "COF"}
COLORS = {"chemistry": "#126C79", "form": "#BF723A", "role": "#73548F"}
INK, MUTED, PALE, LINE = "#193444", "#4F6570", "#E9EEEF", "#CED8DA"
WIDTH, HEIGHT = 180, 172   # mm at final print size
VARIANT_ROLES = {"primary_final", "optimized_variant"}


def name(term):
    return NAMES.get(term, term.replace("_", " ").capitalize())


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verified_bytes(path, checksum):
    raw = path.read_bytes()
    if sha(raw) != checksum:
        raise ValueError(f"Input differs from the registered SHA-256: {path.name}")
    return raw


def load_frozen(source, taxonomy_path):
    manifest = json.loads((source / "manifest.json").read_bytes())
    payload = json.loads(verified_bytes(source / "materials.json", manifest["outputs"]["materials.json"]["sha256"]))
    taxonomy = json.loads(verified_bytes(taxonomy_path, manifest["taxonomy"]["sha256"]))
    vocabulary = {(a["id"], term["id"]): term["id"] for a in taxonomy["axes"] for term in a["terms"]}
    records = {r["record_id"]: r for r in payload["records"]}
    variants = [m for m in payload["materials"] if m["role"] in VARIANT_ROLES]
    if not variants or len({m["uid"] for m in variants}) != len(variants):
        raise ValueError("Expected distinct mapped material variants")
    represented = {m["record_id"] for m in variants}
    articles = sorted((records[key] for key in represented),
                      key=lambda r: (str(r["doi"].get("value") or "").casefold(), r["paper_id"]))
    paper_index = {r["record_id"]: i for i, r in enumerate(articles, 1)}
    article_key, seen_dois, seen_main_hashes = [], set(), set()
    for article in articles:
        doi = str(article["doi"].get("value") or "").strip().casefold()
        main_hashes = {doc["sha256"] for doc in article["sources"] if doc["role"] == "main_article"}
        if (doi and doi in seen_dois) or main_hashes & seen_main_hashes:
            raise ValueError("Article identity needs review before assigning distinct article numbers")
        seen_dois.add(doi)
        seen_main_hashes.update(main_hashes)
        article_key.append({"index": paper_index[article["record_id"]], "doi": doi,
                            "title": article["title"].get("value"), "main_source_hashes": sorted(main_hashes),
                            **{k: article[k] for k in ("record_id", "paper_id", "snapshot_id", "record_sha256", "dataset_role")}})
    plotted = []
    for m in sorted(variants, key=lambda m: (paper_index[m["record_id"]], m["material_id"])):
        record = records[m["record_id"]]
        if m["paper_id"] != record["paper_id"] or m["snapshot_id"] != record["snapshot_id"]:
            raise ValueError("Variant is not bound to the selected article snapshot")
        features = {}
        for axis in FAMILIES:
            facts = labels(m, axis, {"reported", "inferred"}, vocabulary)
            features[axis] = {term: status for term, status in facts.items() if (axis, term) in vocabulary}
        plotted.append({"article": paper_index[m["record_id"]], "material_id": m["material_id"], "uid": m["uid"],
                        "name": m["name"].get("value"), "name_status": m["name"].get("status"),
                        "role": m["role"], "features": features})
    return article_key, plotted


def summarize(variants):
    by_term = {axis: defaultdict(Counter) for axis in FAMILIES}
    by_pair = defaultdict(lambda: defaultdict(Counter))
    paired, both_reported = set(), set()
    for variant in variants:
        features = variant["features"]
        for axis, terms in features.items():
            for term, state in terms.items():
                by_term[axis][term][state] += 1
        for chem, chem_state in features["chemistry"].items():
            for form, form_state in features["form"].items():
                state = "reported" if chem_state == form_state == "reported" else "inferred"
                by_pair[chem, form][variant["article"]][state] += 1
                paired.add(variant["uid"])
                if state == "reported":
                    both_reported.add(variant["uid"])
    tracks = []
    for axis in FAMILIES:
        ranked = sorted(by_term[axis].items(), key=lambda item: (-item[1].total(), item[0]))
        for term, counts in ranked[:5]:
            tracks.append({"family": axis, "term": term, "label": name(term),
                           "reported": counts["reported"], "inferred": counts["inferred"]})
    pairs = []
    for (chem, form), counts in by_pair.items():
        pairs.append({"chemistry": chem, "form": form, "variants": sum(c.total() for c in counts.values()),
                      "articles": len(counts), "article_counts": dict(counts)})
    pairs.sort(key=lambda p: (-p["variants"], p["chemistry"], p["form"]))
    return tracks, pairs, {"both_axes": len(paired), "both_reported": len(both_reported)}


def tint(color, strength):
    return tuple(1 - strength * (1 - v) for v in to_rgb(color))


def text(ax, x, y, string, size=7.6, color=INK, **options):
    ax.text(x, y, string, fontsize=size, color=color, va="center", **options)


def box(ax, x, y, width, height, color):
    ax.add_patch(Rectangle((x, y), width, height, facecolor=color, edgecolor="none"))


def panel(ax, x, y, letter, title):
    text(ax, x, y, letter, 12, weight="bold")
    text(ax, x + 6.4, y, title, 10.3, weight="bold")


def ring(ax, center, radius, width, start, stop, color):
    ax.add_patch(Wedge(center, radius, start, stop, width=width, facecolor=color, edgecolor="none"))


def draw_fingerprints(ax, variants, articles, tracks):
    panel(ax, 3, 4.8, "a", "Material fingerprints")
    text(ax, 9.4, 9.8, "Each radial slice represents one variant.", color=MUTED)
    cx, cy, outer = 46.2, 55.4, 39.4
    groups = defaultdict(list)
    for material in variants:
        groups[material["article"]].append(material)
    start_gap, article_gap = 3.0, 1.35
    step = (360 - start_gap - article_gap * (len(groups) - 1)) / len(variants)
    track_width = (outer - 15.2 - (len(FAMILIES) - 1)*0.8) / len(tracks)
    angle = -90 + start_gap / 2
    for index, group in groups.items():
        begin = angle
        for m in group:
            radius, previous = outer, tracks[0]["family"]
            for track in tracks:
                family = track["family"]
                if family != previous:
                    radius -= 0.8
                state = m["features"][family].get(track["term"])
                color = COLORS[family]
                fill = color if state == "reported" else tint(color, 0.24) if state == "inferred" else PALE
                start, end = angle + 0.13, angle + step - 0.13
                ring(ax, (cx, cy), radius, track_width - 0.16, start, end, fill)
                if state == "inferred":
                    ring(ax, (cx, cy), radius - track_width*0.36, track_width*0.22, start, end, color)
                radius -= track_width
                previous = family
            angle += step
        ring(ax, (cx, cy), outer+1.45, 0.85, begin, angle, INK)
        mid = radians((begin + angle) / 2)
        text(ax, cx + (outer+3.35)*cos(mid), cy + (outer+3.35)*sin(mid),
             f"{index:02d}", 6.6, color=MUTED, ha="center")
        angle += article_gap
    text(ax, cx, cy-3.7, str(len(variants)), 23, ha="center", fontfamily="DejaVu Serif")
    text(ax, cx, cy+2.7, "material variants", 7.9, ha="center", weight="bold")
    text(ax, cx, cy+7.2, f"{len(articles)} articles", 7.3, color=MUTED, ha="center")
    for x, label, state in [(5.4, "Reported", "reported"), (29.7, "Inferred only", "inferred"),
                            (58.0, "No eligible label", "empty")]:
        box(ax, x, 100.0, 3.8, 2.35, {"reported": INK, "inferred": tint(INK, .24), "empty": PALE}[state])
        if state == "inferred":
            box(ax, x, 100.86, 3.8, .62, INK)
        text(ax, x+5.0, 101.15, label, 7.25)


def draw_frequencies(ax, tracks, total):
    panel(ax, 94.0, 4.8, "b", "Feature frequency")
    text(ax, 100.4, 9.8, "Read rings from outside to inside.", color=MUTED)
    text(ax, 165, 15.0, "Rep.", 7.1, ha="right", color=MUTED)
    text(ax, 177, 15.0, "Inf.", 7.1, ha="right", color=MUTED)
    for group, (axis, label) in enumerate(FAMILIES.items()):
        y0 = 15.1 + group * 29.6
        color = COLORS[axis]
        box(ax, 94.4, y0-1.2, .9, 2.4, color)
        text(ax, 96.8, y0, label, 8.2, weight="bold", color=color)
        subset = [t for t in tracks if t["family"] == axis]
        for idx, t in enumerate(subset):
            y = y0 + 5.25 + idx*4.55
            text(ax, 96.8, y, t["label"], 7.35)
            start, width, height = 130.5, 28.5, 1.70
            rep_w, inf_w = width*t["reported"]/total, width*t["inferred"]/total
            box(ax, start, y-height/2, width, height, PALE)
            box(ax, start, y-height/2, rep_w, height, color)
            if inf_w:
                box(ax, start+rep_w, y-height/2, inf_w, height, tint(color, .24))
                box(ax, start+rep_w, y-height*.13, inf_w, height*.26, color)
            text(ax, 165, y, str(t["reported"]), 7.25, ha="right")
            text(ax, 177, y, str(t["inferred"]), 7.25, ha="right")
    text(ax, 96.8, 101.15, f"Counts are out of {total} variants per feature.", 7.25, color=MUTED)


def draw_pairs(ax, articles, pairs):
    ax.plot([3, 177], [106.8, 106.8], color=LINE, lw=.55)
    panel(ax, 3, 112.0, "c", "Distribution of frequent chemistry–form pairs")
    text(ax, 9.4, 117.1, "Each column is one article, in the same order as the outer rim in a.", color=MUTED)
    first_x, last_x = 47, 150
    spacing = (last_x - first_x) / max(1, len(articles)-1)
    xs = {a["index"]: first_x + i*spacing for i, a in enumerate(articles)}
    for index, x in xs.items():
        text(ax, x, 123.7, f"{index:02d}", 6.55, ha="center", color=MUTED)
    text(ax, 3.2, 123.7, "Chemistry × form", 7.15, color=MUTED)
    text(ax, 161.8, 123.7, "Variants", 7.1, ha="center", color=MUTED)
    text(ax, 173.0, 123.7, "Articles", 7.1, ha="center", color=MUTED)
    max_count = max(sum(c.values()) for p in pairs for c in p["article_counts"].values())
    size = min(.68, .44*spacing/sqrt(max_count))
    for row, pair in enumerate(pairs):
        y = 130.0 + row*5.25
        form = "assembly" if pair["form"] == "multicomponent_assembly" else name(pair["form"]).lower()
        text(ax, 3.2, y, f"{name(pair['chemistry'])} × {form}", 7.65)
        ax.plot([first_x-1.5,last_x+1.5], [y,y], color=LINE, lw=.35, zorder=0)
        for index, counts in pair["article_counts"].items():
            total = sum(counts.values())
            ax.add_patch(Circle((xs[index],y), size*sqrt(total), facecolor="#D5E8E9", edgecolor=COLORS["chemistry"], lw=.45))
            if counts.get("reported"):
                ax.add_patch(Circle((xs[index],y), size*sqrt(counts["reported"]),
                                    facecolor=COLORS["chemistry"], edgecolor="none"))
        text(ax, 161.8, y, str(pair["variants"]), 8.1, ha="center", weight="bold")
        text(ax, 173.0, y, str(pair["articles"]), 8.1, ha="center")
    y = 165.1
    text(ax, 3.2, y, "Variants per article", 7.2, color=MUTED)
    for x, n in ((29.4,1),(39.5,4),(50.2,9)):
        ax.add_patch(Circle((x,y),size*sqrt(n),facecolor=INK,edgecolor="none"))
        text(ax, x+2.9, y, str(n), 7.2)
    ax.add_patch(Circle((76.5,y),1.3,facecolor=COLORS["chemistry"],edgecolor="none"))
    text(ax, 79.3, y, "Both labels reported", 7.25)
    ax.add_patch(Circle((116.6,y),1.3,facecolor="#D5E8E9",edgecolor=COLORS["chemistry"],lw=.45))
    text(ax, 119.4, y, "At least one label inferred", 7.25)


def draw(variants, articles, tracks, pairs):
    mpl.rcParams.update({"font.family": "Arial", "pdf.fonttype": 42, "ps.fonttype": 42,
                         "svg.fonttype": "none", "svg.hashsalt": "material-variant-atlas",
                         "savefig.facecolor": "white", "hatch.linewidth": .4})
    fig = plt.figure(figsize=(WIDTH/25.4, HEIGHT/25.4), facecolor="white")
    ax = fig.add_axes([0,0,1,1], xlim=(0,WIDTH), ylim=(HEIGHT,0), aspect="equal")
    ax.set_axis_off()
    draw_fingerprints(ax, variants, articles, tracks)
    draw_frequencies(ax, tracks, len(variants))
    draw_pairs(ax, articles, pairs)
    return fig


def make_caption(articles, variants, comparable, tracks, displayed_pairs):
    roles = Counter(v["role"] for v in variants)
    return (
        "Material features in solar evaporation and catalytic water treatment\n\n"
        f"(a) Each radial slice represents one of {len(variants)} material variants from {len(articles)} articles "
        f"({roles['primary_final']} primary final materials and {roles['optimized_variant']} records classified as optimized variants). "
        "Slices are grouped by article, numbered clockwise in DOI order, and sorted by the local material ID "
        "within each group. The accompanying figure data identify each article by DOI and each slice by material name "
        "and record ID. Each ring marks a chemical class, material form or function of a retained component. "
        f"(b) The {len(tracks)} rings are listed from the outside in. The five most frequently tagged features "
        "in each family were selected by the number of distinct variants carrying that exact dictionary label, "
        "using reported and inferred labels together. Each variant contributes once per feature. "
        "A full band (Rep.) marks a feature coded as reported in the candidate record. A thin stripe (Inf.) "
        "marks a feature supported only by an inferred classification in that record. Grey indicates that "
        "the selected label lacks an eligible assignment. Its source status remains available in the frozen record. "
        f"(c) The {len(displayed_pairs)} most frequent chemistry–form pairs are ranked by the number of "
        "distinct variants carrying both labels, considering every eligible chemical class and form. "
        f"{comparable['both_axes']} of {len(variants)} variants carry labels on both axes, including "
        f"{comparable['both_reported']} with at least one pair coded as reported on both axes. "
        "Total circle area encodes the number of variants for the pair in that article. The dark core gives "
        "the subset with both labels coded as reported, and the pale area gives the additional variants "
        "requiring an inferred label. Empty positions indicate zero eligible co-assignments. "
        "Assembly denotes multicomponent assembly. Counts can overlap across rows. Article counts show how "
        "many publications contributed to each pair. All counts describe this frozen candidate extraction cohort.\n"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    exports = (Path(cfg["data_root"])/"exports").resolve(strict=True)
    source, out = args.source.resolve(strict=True), args.output.resolve()
    if not source.is_relative_to(exports) or not out.is_relative_to(exports) or out == exports:
        raise ValueError("The figure inputs and output must be under the configured exports directory")
    tax = Path(cfg["project_root"]) / cfg["taxonomy_path"]
    articles, variants = load_frozen(source, tax)
    tracks, pairs, comparable = summarize(variants)
    displayed_pairs = pairs[:6]
    fig = draw(variants, articles, tracks, displayed_pairs)
    out.mkdir(parents=True, exist_ok=False)
    stem = out / "material-feature-atlas"
    fig.savefig(stem.with_suffix(".pdf"), metadata={"Title":"Material features in solar evaporation and catalytic water treatment"})
    fig.savefig(stem.with_suffix(".svg"))
    fig.savefig(stem.with_suffix(".png"), dpi=230)
    with BytesIO() as buffer:
        fig.savefig(buffer, format="png", dpi=600, facecolor="white", transparent=False)
        buffer.seek(0)
        with Image.open(buffer) as rendered:
            rendered.convert("RGB").save(stem.with_suffix(".tif"), dpi=(600, 600), compression="tiff_lzw")
    plt.close(fig)
    (out/"caption.txt").write_text(make_caption(articles,variants,comparable,tracks,displayed_pairs), encoding="utf-8")
    figure_data = {"created_utc": datetime.now(timezone.utc).isoformat(),
                   "source": str(source / "materials.json"), "source_sha256": sha((source/"materials.json").read_bytes()),
                   "source_manifest_sha256": sha((source/"manifest.json").read_bytes()),
                   "taxonomy_sha256": sha(tax.read_bytes()), "plot_script": str(Path(__file__).resolve()),
                   "plot_script_sha256": sha(Path(__file__).read_bytes()), "matplotlib": mpl.__version__,
                   "pillow": Image.__version__, "width_mm": WIDTH, "height_mm": HEIGHT,
                   "data_status": "published extraction candidates, scientific accuracy not independently established",
                   "selection": "Exact vocabulary IDs. Five features per family and six pairs by number of distinct "
                                "variants. Ties sorted by term IDs. Reported takes precedence over inferred for "
                                "the same feature and variant. Unpopulated plotting cells retain their original "
                                "status in the source record. No automatic parent expansion or role normalization.",
                   "design_references": ["https://doi.org/10.1101/gr.092759.109","https://doi.org/10.1109/TVCG.2014.2346248"],
                   "articles": articles, "variants_clockwise": variants, "tracks_outer_to_inner": tracks,
                   "pair_coverage": comparable, "displayed_pairs": len(displayed_pairs), "pairs_ranked": pairs}
    (out/"figure-source.json").write_text(json.dumps(figure_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out} | {len(variants)} variants | {len(articles)} articles | {len(tracks)} tracks")


if __name__ == "__main__":
    main()
