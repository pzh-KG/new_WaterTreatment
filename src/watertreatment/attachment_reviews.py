"""Read content-bound Codex attachment judgments without treating them as human review.

The manifest is provenance for a limited role/identity check.  Passing these
checks establishes neither the truth of quoted evidence nor scientific accuracy.
This module never modifies a source, catalog, review, or configuration.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .collection_policy import _absolute, _no_links, is_inventory_noise
from .storage import hash_file


MAX_REVIEW_BYTES = 8 * 1024 * 1024


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ReviewPointer(ReviewModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_id: str = Field(min_length=1, max_length=200)

    @field_validator("path", "review_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("review pointer must not contain blank values")
        return value


class ReviewInput(ReviewModel):
    file_id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("file_id", "path")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("review input must not contain blank values")
        return value


class ReviewEvidence(ReviewModel):
    source_file_id: str = Field(min_length=1, max_length=200)
    location: dict
    quote: str = Field(min_length=1, max_length=64000)
    method: str = Field(min_length=1, max_length=1000)

    @field_validator("source_file_id", "quote", "method")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("review evidence must not contain blank values")
        return value


class ReviewDecision(ReviewModel):
    file_id: str = Field(min_length=1, max_length=200)
    role: Literal["main_article", "supporting_information", "other"]
    main_file_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=16000)
    evidence: list[ReviewEvidence] = Field(min_length=1, max_length=200)

    @field_validator("file_id", "main_file_id", "title", "reason")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("review decision must not contain blank values")
        return value


class EvidenceArtifact(ReviewModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AttachmentReview(ReviewModel):
    version: Literal[1]
    review_id: str = Field(min_length=1, max_length=200)
    paper_id: str = Field(min_length=1, max_length=200)
    reviewed_at: str = Field(min_length=1, max_length=80)
    origin: Literal["codex_attachment_review"]
    reviewer: Literal["Codex desktop assistant"]
    human_validated: Literal[False]
    scientific_validated: Literal[False]
    input_inventory: list[ReviewInput] = Field(min_length=1, max_length=200)
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=200)
    limitations: list[str] = Field(max_length=200)
    evidence_artifacts: list[EvidenceArtifact] = Field(max_length=1000)
    effective_model: dict | None = None

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("review version requires an integer")
        return value

    @field_validator("human_validated", "scientific_validated", mode="before")
    @classmethod
    def explicitly_not_validated(cls, value):
        if value is not False:
            raise ValueError("attachment review cannot assert human or scientific validation")
        return value

    @field_validator("review_id", "paper_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("review identity must not be blank")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def iso_date(cls, value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("review date must be ISO formatted") from error
        return value

    @field_validator("limitations")
    @classmethod
    def meaningful_limitations(cls, values):
        if any(not value.strip() or len(value) > 16000 for value in values):
            raise ValueError("review limitations must be bounded nonblank strings")
        return values

    @model_validator(mode="after")
    def complete_decisions(self):
        identifiers = {item.file_id for item in self.input_inventory}
        paths = {str(_absolute(item.path)).casefold() for item in self.input_inventory}
        if len(identifiers) != len(self.input_inventory) or len(paths) != len(self.input_inventory):
            raise ValueError("review input inventory contains duplicates")
        decisions = {item.file_id: item for item in self.decisions}
        if len(decisions) != len(self.decisions) or set(decisions) != identifiers:
            raise ValueError("review requires one decision for every input file")
        mains = [item for item in self.decisions if item.role == "main_article"]
        if len(mains) != 1:
            raise ValueError("review must identify exactly one main article")
        for item in self.decisions:
            if item.role == "supporting_information":
                if item.main_file_id != mains[0].file_id:
                    raise ValueError("supplement must refer to the identified main article")
            elif item.main_file_id is not None:
                raise ValueError("only supplementary decisions may link a main article")
            if any(e.source_file_id not in identifiers for e in item.evidence):
                raise ValueError("review evidence refers to a file outside its input inventory")
        return self


def _source_folder(settings, review: AttachmentReview) -> tuple[Path, Path]:
    """Validate the declared scope without opening source contents."""
    paths = [_absolute(item.path) for item in review.input_inventory]
    folders = {p.parent for p in paths}
    if len(folders) != 1:
        raise ValueError("attachment review inputs must belong to one source folder")
    folder = folders.pop()
    if folder.parent not in settings.source_roots:
        raise ValueError("attachment review input folder is outside configured source roots")
    return folder, folder.parent


def _read_review(settings, value: str | Path) -> tuple[dict, AttachmentReview, str, Path]:
    path = _no_links(_absolute(value), settings.data_root)
    if not path.is_file() or path.suffix.casefold() != ".json" or path.stat().st_size > MAX_REVIEW_BYTES:
        raise ValueError("attachment review must be a JSON file of at most 8 MiB")
    with path.open("rb") as stream:
        payload = stream.read(MAX_REVIEW_BYTES + 1)
    if len(payload) > MAX_REVIEW_BYTES:
        raise ValueError("attachment review exceeds 8 MiB")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
        review = AttachmentReview.model_validate(raw)
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid attachment review manifest") from error
    _source_folder(settings, review)
    for artifact in review.evidence_artifacts:
        artifact_path = _no_links(_absolute(artifact.path), settings.data_root)
        if not artifact_path.is_file() or hash_file(artifact_path) != artifact.sha256:
            raise ValueError("attachment review evidence artifact is unavailable or its hash changed")
    if hash_file(path) != digest:
        raise ValueError("attachment review manifest changed during loading")
    return raw, review, digest, path


def make_review_pointer(settings, path: str | Path) -> dict:
    """Validate a saved review and return its explicit immutable pointer.

    Historical source versions may already have expired; applicability is
    checked by ``load_attachment_review`` against the current caller and disk.
    """
    _, review, digest, resolved = _read_review(settings, path)
    return {"path": str(resolved), "sha256": digest, "review_id": review.review_id}


def load_attachment_review(settings, pointer: dict | None, paper_id: str,
                           files: list[dict]) -> dict | None:
    """Return original review fields only while the complete source set matches.

    Broken provenance raises an explicit error.  Changed/unavailable source
    inventories return ``None`` so the caller can use fresh identification;
    cached role assignments are never authority to bypass a source change.
    """
    if pointer is None:
        return None
    try:
        checked = ReviewPointer.model_validate(pointer)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid attachment review pointer") from error
    raw, review, digest, path = _read_review(settings, checked.path)
    if digest != checked.sha256 or review.review_id != checked.review_id:
        raise ValueError("attachment review pointer identity or manifest hash does not match")
    if review.paper_id != paper_id:
        raise ValueError("attachment review paper identity does not match")
    expected = {item.file_id: (item.path, item.sha256) for item in review.input_inventory}
    try:
        if not isinstance(files, list) or len(files) != len(expected):
            return None
        supplied = {}
        for item in files:
            identifier = item.get("id", item.get("file_id"))
            if identifier in supplied:
                return None
            supplied[identifier] = (item["path"], item["sha256"])
            if "name" in item and item["name"] != Path(item["path"]).name:
                return None
        if supplied != expected:
            return None
        folder, source_root = _source_folder(settings, review)
        actual_folder = _no_links(folder, source_root)
        if not actual_folder.is_dir():
            return None
        paths = [p for p in actual_folder.iterdir() if not is_inventory_noise(p)]
        by_name = {Path(item.path).name: item.sha256 for item in review.input_inventory}
        if len(by_name) != len(expected) or {p.name for p in paths} != set(by_name):
            return None
        for source in paths:
            actual = _no_links(source, source_root)
            if not actual.is_file() or hash_file(actual) != by_name[source.name]:
                return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    # Do not silently accept a review that changed while hashing its sources.
    if hash_file(path) != digest:
        raise ValueError("attachment review manifest changed during applicability checking")
    return raw
