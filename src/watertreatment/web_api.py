"""Loopback-only HTTP surface for the research workbench."""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .web_service import Workbench
from .budget_policy import BudgetPolicy


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Scan(Body):
    folders: list[str] | None = Field(default=None, max_length=500)

class Role(Body):
    file_id: str
    role: Literal["main_article", "supporting_information", "main_article_with_supporting_information", "other", "unknown"]
    reviewer: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=4000)

class Extract(Body):
    paper_ids: list[str] = Field(min_length=1, max_length=500)
    auto_publish: bool = False
    cohort_role: Literal["development", "unassigned", "heldout"] = "unassigned"
    budget_policy: BudgetPolicy | None = None

class Queue(Body):
    paused: bool

class Recovery(Body):
    action: Literal["continue_candidate", "correct_reading", "complete_audit", "resume_initial", "resume_extraction", "repair_identity", "resume_audit", "reextract", "retry_recheck", "extend_budget"]
    base_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

class BudgetExtensionPreview(Body):
    budget_policy: BudgetPolicy

class BudgetExtension(Body):
    base_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    budget_policy: BudgetPolicy | None = None

class Review(Body):
    reviewer: str = Field(min_length=1, max_length=120)
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    entries: list[dict] = Field(max_length=300)

class Publish(Body):
    record_ids: list[str] = Field(min_length=1, max_length=500)

class Recheck(Body):
    record_id: str
    question: str = Field(min_length=1, max_length=8000)
    observation_id: str | None = None

class Digitization(Body):
    record_id: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1, max_length=12)
    question: str = Field(default="", max_length=8000)

class Sample(Body):
    count: int = Field(ge=1, le=100)
    random_count: int = Field(ge=0, le=100)

class Ask(Body):
    question: str = Field(min_length=1, max_length=8000)
    record_ids: list[str] = Field(default_factory=list, max_length=500)
    scope: Literal["selected", "published"] = "selected"


def create_app(settings, *, start_worker=True, service=None):
    workbench = service or Workbench(settings)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            workbench.start()
        try:
            yield
        finally:
            if start_worker:
                workbench.close()

    app = FastAPI(title="WaterTreatment 文献工作台", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.workbench = workbench
    app.state.csrf_token = token

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.url.hostname
        permitted = {"127.0.0.1", "localhost", "[::1]", "::1"}
        if not start_worker:
            permitted.add("testserver")
        if host not in permitted:
            return JSONResponse({"detail": "只允许本机访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-wt-token", ""), token):
                return JSONResponse({"detail": "页面令牌失效，请刷新后重试"}, status_code=403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "无效的请求长度"}, status_code=400)
            if length < 0:
                return JSONResponse({"detail": "无效的请求长度"}, status_code=400)
            if length > 2_000_000:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            # A local backend update must not leave a browser using an older UI
            # against newer response fields. Revalidation still permits ETags.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request, exception):
        return JSONResponse({"detail": str(exception)[:1800]}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exception):
        return JSONResponse({"detail": "所选记录、附件或任务不存在"}, status_code=404)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, exception):
        return JSONResponse({"detail": "登记的来源或产物文件不可用，请检查数据目录"}, status_code=404)

    @app.get("/api/status")
    def status():
        return {**workbench.status(), "csrf_token": token}

    @app.get("/api/runtime")
    def runtime_status():
        # Cheap source-metadata check; does not refresh papers or run a model.
        return workbench.runtime_status()

    @app.get("/api/papers")
    def papers():
        return {"papers": workbench.papers(), "source_roots": [str(p) for p in settings.source_roots]}

    @app.get("/api/workspace")
    def workspace_snapshot():
        return workbench.workspace_snapshot()

    @app.get("/api/papers/{paper_id}")
    def paper(paper_id: str):
        return workbench.paper(paper_id)

    @app.post("/api/scan")
    def scan(body: Scan):
        if body.folders is not None:
            if not body.folders:
                raise ValueError("请选择至少一个文献文件夹")
            for path in body.folders:
                resolved = settings.assert_source_allowed(Path(path))
                if resolved.parent not in settings.source_roots or not resolved.is_dir():
                    raise ValueError("请选择配置文献目录下的单篇文献文件夹")
        return {"job_id": workbench.jobs.enqueue("scan", body.model_dump())}

    @app.get("/api/scan-folders")
    def scan_folders(root_index: int = 0):
        return workbench.scan_folders(root_index)

    @app.post("/api/papers/{paper_id}/roles")
    def role(paper_id: str, body: Role):
        return workbench.set_role(paper_id, **body.model_dump())

    @app.post("/api/extractions")
    def extract(body: Extract):
        return {"job_ids": workbench.queue_extractions(**body.model_dump())}

    @app.get("/api/extraction-budget")
    def extraction_budget():
        return workbench.extraction_budget()

    @app.get("/api/jobs")
    def jobs():
        snapshot = workbench.workspace_snapshot()
        return {key: snapshot[key] for key in ("jobs", "paused", "activity_revision", "sync_state")}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        return workbench.job_detail(job_id)

    @app.post("/api/queue")
    def queue(body: Queue):
        return {"paused": workbench.set_queue_paused(body.paused)}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        return {"job_id": workbench.retry_job(job_id)}

    @app.get("/api/jobs/{job_id}/recovery")
    def recovery_options(job_id: str):
        return workbench.recovery_options(job_id)

    @app.post("/api/jobs/{job_id}/recover")
    def recover(job_id: str, body: Recovery):
        return {"job_id": workbench.queue_recovery(job_id, **body.model_dump())}

    @app.get("/api/jobs/{job_id}/budget-extension")
    def budget_extension_options(job_id: str):
        return workbench.budget_extension_options(job_id)

    @app.post("/api/jobs/{job_id}/budget-extension/preview")
    def budget_extension_preview(job_id: str, body: BudgetExtensionPreview):
        return workbench.budget_extension_options(job_id, **body.model_dump())

    @app.post("/api/jobs/{job_id}/budget-extension")
    def budget_extension(job_id: str, body: BudgetExtension):
        return {"job_id": workbench.queue_budget_extension(job_id, **body.model_dump())}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        workbench.jobs.cancel(job_id)
        return {"status": "cancelled"}

    @app.get("/api/records")
    def records():
        return {"records": workbench.record_summaries()}

    @app.post("/api/refresh")
    def refresh():
        return workbench.workspace_snapshot(force_sync=True)

    @app.get("/api/records/{record_id}")
    def record(record_id: str):
        detail = workbench.records.get(record_id)
        # A research view is a separate projection. A damaged/missing view must
        # not hide the intact original record, and source/audit verification is
        # performed once, not again for the same page's second tab.
        try:
            detail["research_view"] = workbench.records._core_view_from_detail(detail)
        except (ValueError, KeyError, OSError) as exc:
            detail["research_view"] = {"available": False, "integrity_error": True,
                                       "reason": "研究视图校验未通过：" + str(exc)}
        detail.pop("sources_bundle", None)
        detail.pop("record_path", None)
        detail["summary"] = workbench.activity.records([detail["summary"]])[0]
        return {**detail, "sources": workbench.evidence.sources(record_id)}

    @app.get("/api/records/{record_id}/download")
    def download(record_id: str):
        detail = workbench.records.get(record_id)
        return FileResponse(detail["record_path"], media_type="application/json", filename=record_id+".json")

    @app.get("/api/records/{record_id}/core-view")
    def core_view(record_id: str, view_id: str | None = None):
        return workbench.records.core_view(record_id, view_id)

    @app.get("/api/records/{record_id}/core-view/download")
    def download_core_view(record_id: str, view_id: str | None = None):
        result = workbench.records.core_view(record_id, view_id)
        if not result["available"]:
            raise HTTPException(status_code=404, detail="此版本尚无独立研究视图")
        return JSONResponse(result["view"], headers={
            "Content-Disposition": f'attachment; filename="{record_id}-research-view.json"'})

    @app.get("/api/records/{record_id}/draft")
    def draft(record_id: str, reviewer: str):
        return workbench.records.get_draft(record_id, reviewer)

    @app.post("/api/records/{record_id}/draft")
    def save_draft(record_id: str, body: Review):
        return workbench.records.save_draft(record_id, **body.model_dump())

    @app.post("/api/records/{record_id}/review")
    def review(record_id: str, body: Review):
        return workbench.records.submit_review(record_id, **body.model_dump())

    @app.get("/api/records/{record_id}/evidence/{evidence_id}")
    def evidence(record_id: str, evidence_id: str):
        return workbench.evidence.evidence(record_id, evidence_id)

    @app.get("/api/records/{record_id}/sources/{source_id}")
    def source(record_id: str, source_id: str):
        return workbench.evidence.source_detail(record_id, source_id)

    @app.get("/api/records/{record_id}/sources/{source_id}/file")
    def source_file(record_id: str, source_id: str):
        path = workbench.evidence.file(record_id, source_id)
        return FileResponse(path, filename=path.name, content_disposition_type="inline")

    @app.get("/api/records/{record_id}/sources/{source_id}/page/{page}.png")
    def page(record_id: str, source_id: str, page: int, scale: float = 2):
        return FileResponse(workbench.evidence.page_image(record_id, source_id, page, scale), media_type="image/png")

    @app.get("/api/records/{record_id}/sources/{source_id}/images/{block_id}")
    def word_image(record_id: str, source_id: str, block_id: str):
        return FileResponse(workbench.evidence.word_image(record_id, source_id, block_id), media_type="image/png")

    @app.post("/api/publish")
    def publish(body: Publish):
        for identifier in body.record_ids:
            summary = workbench.records.get(identifier)["summary"]
            if not summary["admissible"] or not summary["current"]:
                raise ValueError("所选版本尚未完成自动审查或已被更新")
        return {"job_ids": [workbench.jobs.enqueue("publish", {"record_id": i}) for i in dict.fromkeys(body.record_ids)]}

    @app.post("/api/recheck")
    def recheck(body: Recheck):
        detail = workbench.records.get(body.record_id)
        if not detail["summary"]["current"] or detail["summary"]["source_change"]:
            raise ValueError("请选择来源未改变的当前记录")
        if body.observation_id and body.observation_id not in {o["id"] for o in detail["record"]["observations"]}:
            raise ValueError("观测不属于此记录")
        return {"job_id": workbench.enqueue_model_job("recheck", body.model_dump())}

    @app.post("/api/digitizations")
    def digitize(body: Digitization):
        return {"job_id": workbench.queue_digitization(**body.model_dump())}

    @app.post("/api/sample")
    def sample(body: Sample):
        return {"sample": workbench.sample(body.count, body.random_count)}

    @app.get("/api/samples")
    def samples():
        return {"samples": workbench.samples()}

    @app.post("/api/ask")
    def ask(body: Ask):
        if body.scope == "selected":
            if not body.record_ids:
                raise ValueError("请选择用于问答的记录")
            for identifier in body.record_ids:
                workbench.records.get(identifier)
        return {"job_id": workbench.enqueue_model_job("ask", body.model_dump())}

    @app.get("/api/answers")
    def answers():
        return {"answers": workbench.answers()}

    static = Path(__file__).parent / "web_static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html", media_type="text/html")

    return app
