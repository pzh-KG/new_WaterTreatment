"""Render categorical research maps from a frozen material export.

Counts are label associations, never absence claims or physical distances.
The source export remains authoritative; no scientific fields are revised.
"""
from __future__ import annotations

import argparse
from collections import Counter
from html import escape
import hashlib
import json
from pathlib import Path
import re


UNKNOWN = "无可用标签"


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")


def labels(material, axis, allowed, taxonomy):
    entries = []
    if axis == "role":
        for component in material.get("components", []):
            if component.get("retention") == "retained":
                entries.extend(component.get("roles", []))
    else:
        entries = [item["label"] for item in material.get("classification", []) if item.get("axis") == axis]
    result = {}
    for fact in entries:
        if fact.get("status") not in allowed or fact.get("value") is None:
            continue
        values = fact["value"] if isinstance(fact["value"], list) else [fact["value"]]
        for value in values:
            raw = str(value)
            # Free-text roles have not been reconciled to a common scientific
            # vocabulary. Preserve them in the export, not as dozens of
            # misleadingly separate comparable map coordinates.
            if axis == "role" and (axis, raw) not in taxonomy:
                continue
            display = taxonomy.get((axis, raw), raw)
            if display == UNKNOWN:
                raise ValueError("A source label collides with the unknown-label bucket")
            if result.get(display) != "reported":
                result[display] = fact["status"]
    return result


def build_configs(materials, taxonomy):
    configs, coverage = {}, {}
    chem_order = sorted({label for material in materials for label in labels(material, "chemistry", {"reported", "inferred"}, taxonomy)}) + [UNKNOWN]
    for axis in ("form", "role"):
        x_order = sorted({label for material in materials for label in labels(material, axis, {"reported", "inferred"}, taxonomy)}) + [UNKNOWN]
        for policy in ("inclusive", "reported"):
            allowed = {"reported", "inferred"} if policy == "inclusive" else {"reported"}
            memberships = {(chem, form): [] for chem in chem_order for form in x_order}
            both = 0
            seen = set()
            for material in materials:
                chemistry = labels(material, "chemistry", allowed, taxonomy)
                other = labels(material, axis, allowed, taxonomy)
                both += bool(chemistry and other)
                if not chemistry:
                    chemistry = {UNKNOWN: "unresolved"}
                if not other:
                    other = {UNKNOWN: "unresolved"}
                name_fact = material.get("name", {})
                name = name_fact.get("value") or name_fact.get("raw") or material["material_id"]
                for chem, chem_status in chemistry.items():
                    for form, form_status in other.items():
                        pair_status = ("unresolved" if "unresolved" in {chem_status, form_status} else
                                       "reported" if chem_status == form_status == "reported" else "inferred")
                        memberships[chem, form].append((material["uid"], pair_status,
                            f"{name} [{material['material_id']} / {material['paper_id']} / 名称状态:{name_fact.get('status', 'unread')} ]"))
                        seen.add(material["uid"])
            if len(seen) != len(materials):
                raise ValueError("Every material must appear, including unresolved labels")
            rows = []
            for chem in chem_order:
                for other in x_order:
                    members = memberships[chem, other]
                    if len({member[0] for member in members}) != len(members):
                        raise ValueError("Repeated material within a category pair")
                    counts = Counter(member[1] for member in members)
                    rows.append([other, chem, len(members), counts["reported"], counts["inferred"], counts["unresolved"],
                                 "\n".join(member[2] for member in members) or "当前候选无该标签组合"])
            key = f"{axis}-{policy}"
            coverage[key] = {"both": both}
            configs[key] = {"id": "wt-material-grid", "title": "材料特征分布", "showHeader": False,
                "height": 560, "summary": "按当前候选标签统计材料变体；包含无可用标签，类别可重叠。",
                "columns": ["form", "chemistry", "materials", "reported_pairs", "inferred_pairs", "unresolved_pairs", "members"],
                "rows": rows,
                "spec": {"type": "heatmap", "x": "form", "y": "chemistry", "value": "materials",
                    "xLabel": "宏观形态" if axis == "form" else "最终保留组分的功能角色", "yLabel": "化学类别",
                    "xOrder": x_order, "yOrder": chem_order, "showValues": True,
                    "colorDomain": [0, max(1, max(row[2] for row in rows))], "missingValues": "gap",
                    "tooltipFields": [{"field": "materials", "label": "材料变体数"},
                        {"field": "reported_pairs", "label": "两轴均报告"},
                        {"field": "inferred_pairs", "label": "至少一轴为推断"},
                        {"field": "unresolved_pairs", "label": "至少一轴无可用标签"},
                        {"field": "members", "label": "材料名称 / 材料ID / 文献ID"}]}}
    # Use one intentional scale across all views, so switching the evidence
    # policy or feature axis cannot silently change what a color means.
    maximum = max(row[2] for config in configs.values() for row in config["rows"])
    for config in configs.values():
        config["spec"]["colorDomain"] = [0, max(1, maximum)]
    return configs, coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", required=True, type=Path)
    parser.add_argument("--taxonomy", required=True, type=Path)
    parser.add_argument("--name", default="material-map")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.name):
        raise ValueError("name must be a lowercase ASCII filename stem")
    export = args.export_dir.resolve(strict=True)
    manifest_path = export / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    payload_path = export / "materials.json"
    payload_bytes = payload_path.read_bytes()
    payload = json.loads(payload_bytes)
    # Bind the renderer to the unchanged export's registered output hash.
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()
    if payload_sha != manifest["outputs"]["materials.json"]["sha256"]:
        raise ValueError("materials.json does not match its frozen export manifest")
    taxonomy_bytes = args.taxonomy.read_bytes()
    taxonomy = json.loads(taxonomy_bytes)
    display = {(axis["id"], term["id"]): term["label"] for axis in taxonomy["axes"] for term in axis["terms"]}
    materials = [item for item in payload["materials"] if item["role"] in {"primary_final", "optimized_variant"}]
    if not materials or len({item["uid"] for item in materials}) != len(materials):
        raise ValueError("Map requires nonempty, uniquely identified material variants")
    configs, coverage = build_configs(materials, display)
    roles = Counter(item["role"] for item in payload["materials"])
    metadata = {"papers": len(payload["records"]), "total": len(materials),
        "primary": roles["primary_final"], "variants": roles["optimized_variant"],
        "components": roles["final_component"], "coverage": coverage}
    template_path = Path(__file__).with_name("material_mapping.fragment.html")
    template = template_path.read_text(encoding="utf-8")
    chart_markup = "\n".join(f'<script type="application/json" data-viz-chart data-map-key="{escape(key)}">{json_text(config)}</script>' for key, config in configs.items())
    fragment = template.replace("__MAPPING_METADATA__", json_text(metadata)).replace("<!-- MAPPING_CHARTS -->", chart_markup)
    if len(fragment.encode("utf-8")) >= 1_000_000:
        raise ValueError("Map exceeds inline visualization size limit")
    output = export / f"{args.name}.html"
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(fragment)
    rendering_manifest = {"version": "material-map-render-v1", "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_materials_sha256": payload_sha, "template_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
        "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "taxonomy_sha256": hashlib.sha256(taxonomy_bytes).hexdigest(), "taxonomy_is": "display_labels_only_no_inference",
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "metadata": metadata,
        "semantics": {"unit": "material_variant_label_association_count", "scientific_validation": "not_established",
            "coordinates_are_physical_distances": False, "missing_feature_values_are_zero": False,
            "zero_cell_means": "No selected source record has this exact eligible label association",
            "performance_used_for_coordinates": False, "variants_merged": False}}
    with (export / f"{args.name}-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(rendering_manifest, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"output": str(output), "bytes": output.stat().st_size, **metadata}, ensure_ascii=False))


if __name__ == "__main__":
    main()
