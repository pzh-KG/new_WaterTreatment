"""Run-local recovery of Word previews from the unchanged original for a new audit."""
import json
from pathlib import Path

from .coverage_visual_inputs import recover_word_occurrences
from .sources import SourceDocument
from .storage import hash_file, write_new_json

POLICY = "original-word-occurrences-for-review-v1"


def review_visuals(run, bundle, *, create, proof=None):
    """Recompute the same registered occurrence bytes at admission, including unresolved items."""
    run = Path(run).resolve(strict=True)
    missing = {(r["source_id"], i["block_id"]) for r in bundle.get("renderings", [])
               for i in r.get("images", []) if not i.get("path")}
    if not missing:
        if proof is not None:
            raise ValueError("Word recovery proof has no source occurrences")
        return bundle, [], None
    docs = [SourceDocument.model_validate(d) for d in bundle["documents"]]
    updated = recover_word_occurrences(bundle, docs, missing, run, create=create, allow_unread=True)
    images, entries = [], []
    for rendering in updated["renderings"]:
        for item in rendering.get("images", []):
            if (rendering["source_id"], item["block_id"]) not in missing:
                continue
            entries.append(item)
            if item.get("path"):
                images.append(Path(item["path"]))
    manifest = {"policy": POLICY, "source_bundle_sha256": hash_file(run / "sources.json"),
                "occurrences": entries}
    path = run / "review_visual_inputs.json"
    if create:
        write_new_json(path, manifest)
    elif path.resolve(strict=True) != path or json.loads(path.read_bytes()) != manifest:
        raise ValueError("Registered Word recovery differs from the immutable document")
    binding = {"policy": POLICY, "manifest_sha256": hash_file(path)}
    if not create and binding != proof:
        raise ValueError("Word recovery proof differs from the frozen plan")
    return updated, [path, *images], binding
