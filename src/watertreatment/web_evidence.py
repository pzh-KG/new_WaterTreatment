"""Source-bound browser evidence. Never serve an arbitrary supplied path."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import quote

from .rendering import reading_canvas
from .storage import hash_file

_RENDER_LOCK = threading.RLock()


class EvidenceView:
    def __init__(self, settings, records):
        self.settings, self.records = settings, records
        self.cache = settings.data_root / "workbench" / "previews"
        self.cache.mkdir(parents=True, exist_ok=True)

    def bundle(self, record_id):
        detail = self.records.get(record_id)
        path = Path(detail["sources_bundle"]).resolve(strict=True)
        if self.settings.data_root not in path.parents:
            raise ValueError("来源清单不在数据目录")
        return detail, json.loads(path.read_text(encoding="utf-8"))

    def document(self, record_id, source_id):
        detail, bundle = self.bundle(record_id)
        document = next((d for d in bundle["documents"] if d["source"]["source_id"] == source_id), None)
        if document is None:
            raise ValueError("该来源不属于所选记录")
        source = document["source"]
        path = Path(source["path"]).resolve(strict=True)
        originals = (self.settings.data_root / "originals").resolve()
        if originals not in path.parents or hash_file(path) != source["sha256"]:
            raise ValueError("原件快照位置或哈希不一致")
        return document, bundle

    def sources(self, record_id):
        _, bundle = self.bundle(record_id)
        result = []
        for doc in bundle["documents"]:
            src = doc["source"]
            base = f"/api/records/{quote(record_id, safe='')}/sources/{quote(src['source_id'], safe='')}"
            result.append({"id": src["source_id"], "name": Path(src.get("metadata", {}).get("original_path", src["path"])).name,
                "role": src["role"], "format": src["format"], "pages": src.get("page_count"),
                "file_url": base + "/file", "blocks_url": base,
                "preview_url": base + "/page/1.png" if src["format"] == "pdf" else None,
                "page_template": base + "/page/{page}.png" if src["format"] == "pdf" else None})
        return result

    def source_detail(self, record_id, source_id):
        doc, bundle = self.document(record_id, source_id)
        previews = []
        for rendering in bundle.get("renderings", []):
            if rendering["source_id"] == source_id:
                for image in rendering.get("images", []):
                    if image.get("path"):
                        previews.append({"block_id": image["block_id"], "url":
                            f"/api/records/{quote(record_id,safe='')}/sources/{quote(source_id,safe='')}/images/{quote(image['block_id'],safe='')}",
                            "page": None})
        return {"source": next(s for s in self.sources(record_id) if s["id"] == source_id),
                "blocks": doc["blocks"], "previews": previews, "issues": doc.get("issues", []),
                "word_layout": "原生结构定位；不是完整Word页面排版" if doc["source"]["format"] == "docx" else None}

    def evidence(self, record_id, evidence_id):
        detail = self.records.get(record_id)
        evidence = next((e for e in detail["record"]["evidence"] if e["id"] == evidence_id), None)
        if evidence is None:
            raise ValueError("证据不属于该记录")
        doc, _ = self.document(record_id, evidence["source_id"])
        index = next((i for i, b in enumerate(doc["blocks"]) if b["block_id"] == evidence["block_id"]), None)
        if index is None:
            raise ValueError("证据定位不存在")
        block = doc["blocks"][index]
        location = {**block.get("location", {}), **{k: v for k, v in evidence.get("locator", {}).items() if v is not None}}
        source = next(s for s in self.sources(record_id) if s["id"] == evidence["source_id"])
        page = location.get("physical_page")
        preview = source["page_template"].replace("{page}", str(page)) if page and source["page_template"] else None
        if not preview:
            item = next((p for p in self.source_detail(record_id, evidence["source_id"])["previews"] if p["block_id"] == evidence["block_id"]), None)
            preview = item["url"] if item else None
        return {**evidence, "location": location, "context": doc["blocks"][max(0, index-2):index+3],
                "file_url": source["file_url"], "preview_url": preview, "page": page,
                "source_name": source["name"], "role": source["role"], "format": source["format"],
                "quote_warning": "引用为抽取记录内容，原文上下文单独展示；定位不等于语义已验证。"}

    def file(self, record_id, source_id):
        doc, _ = self.document(record_id, source_id)
        return Path(doc["source"]["path"])

    def page_image(self, record_id, source_id, page_number, scale=2.0):
        if not 0.5 <= scale <= 4:
            raise ValueError("缩放范围为0.5—4")
        doc, _ = self.document(record_id, source_id)
        if doc["source"]["format"] != "pdf":
            raise ValueError("Word使用结构块定位，没有物理页码")
        import pypdfium2 as pdfium
        target = self.cache / f"{doc['source']['sha256']}-{page_number}-{scale:g}.png"
        with _RENDER_LOCK:
            if target.exists():
                return target
            with pdfium.PdfDocument(doc["source"]["path"]) as pdf:
                if not 1 <= page_number <= len(pdf):
                    raise ValueError("页码超出来源范围")
                page = pdf[page_number-1]
                try:
                    width, height = page.get_size()
                    if width*height*scale*scale > 40_000_000:
                        raise ValueError("图像超出像素预算")
                    bitmap = page.render(scale=scale)
                    try:
                        with target.open("xb") as stream:
                            bitmap.to_pil().save(stream, format="PNG")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        return target

    def word_image(self, record_id, source_id, block_id):
        _, bundle = self.document(record_id, source_id)
        image = next((i for r in bundle.get("renderings", []) if r["source_id"] == source_id
                      for i in r.get("images", []) if i.get("block_id") == block_id and i.get("path")), None)
        if image is None:
            raise ValueError("该块没有可用插图预览")
        path = Path(image["path"]).resolve(strict=True)
        if self.settings.data_root not in path.parents or hash_file(path) != image["sha256"]:
            raise ValueError("预览来源或哈希不一致")
        from PIL import Image
        target = self.cache / (image["sha256"] + "-white.png")
        with _RENDER_LOCK:
            if not target.exists():
                with Image.open(path) as original:
                    if original.width*original.height > 40_000_000:
                        raise ValueError("图像超出像素预算")
                    canvas, _ = reading_canvas(original)
                    with target.open("xb") as stream:
                        canvas.save(stream, format="PNG")
        return target
