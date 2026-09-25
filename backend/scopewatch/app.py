"""FastAPI application for Scopewatch synthetic baseline."""

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from scopewatch import __version__
from scopewatch.config import DB_PATH, DEMO_REVIEWER_ID, WORKSPACE_ROOT
from scopewatch.db import init_db
from scopewatch.errors import (
    ScopewatchAPIError,
    generic_error_handler,
    scopewatch_api_error_handler,
    validation_error_handler,
)
from scopewatch.events import broadcaster
from scopewatch.models import ApprovalStatus
from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import (
    ActionResponse,
    ApprovalRequest,
    ApprovalResolutionResponse,
    CreateRunRequest,
    EvidenceEvent,
    HealthResponse,
    ResolveApprovalRequest,
    Run,
    SubmitActionRequest,
)
from scopewatch.service import ScopewatchService

logger = logging.getLogger("scopewatch.app")

frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure database and demo workspace root exist
    init_db(DB_PATH)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    yield
    # Shutdown


def create_app(
    db_path: Path | str = DB_PATH,
    workspace_root: Path | str = WORKSPACE_ROOT,
    auditor: Optional[ReasoningAuditor] = None,
) -> FastAPI:
    app = FastAPI(
        title="Scopewatch Synthetic Gateway",
        description="Controlled baseline demo API for synthetic agent action review.",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register standardized error handlers
    app.add_exception_handler(ScopewatchAPIError, scopewatch_api_error_handler)  # type: ignore
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore
    app.add_exception_handler(Exception, generic_error_handler)

    service = ScopewatchService(db_path=db_path, workspace_root=workspace_root, auditor=auditor)
    app.state.service = service

    def get_service() -> ScopewatchService:
        return service

    # ---------------- Health ----------------

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health_check(svc: ScopewatchService = Depends(get_service)) -> HealthResponse:
        db_status = "ok"
        try:
            conn = svc._get_conn()
            conn.execute("SELECT 1")
            conn.close()
        except Exception:
            db_status = "error"
        return HealthResponse(status="ok", database=db_status, version=__version__)

    # ---------------- Demo Reset ----------------

    @app.post("/api/v1/demo/reset")
    def reset_demo(svc: ScopewatchService = Depends(get_service)) -> dict[str, str]:
        init_db(svc.db_path)
        conn = svc._get_conn()
        try:
            conn.execute("DELETE FROM evidence_events;")
            conn.execute("DELETE FROM execution_receipts;")
            conn.execute("DELETE FROM approval_requests;")
            conn.execute("DELETE FROM policy_decisions;")
            conn.execute("DELETE FROM action_requests;")
            conn.execute("DELETE FROM runs;")
        finally:
            conn.close()
        return {"status": "ok", "message": "Demo state reset successfully."}

    # ---------------- Runs ----------------

    @app.get("/api/v1/runs", response_model=list[Run])
    def list_runs(svc: ScopewatchService = Depends(get_service)) -> list[Run]:
        return svc.list_runs()

    @app.post("/api/v1/runs", response_model=Run, status_code=status.HTTP_201_CREATED)
    def create_run(req: CreateRunRequest, svc: ScopewatchService = Depends(get_service)) -> Run:
        run, _ = svc.create_run(name=req.name, task_scope=req.task_scope)
        return run

    @app.get("/api/v1/runs/{run_id}", response_model=Run)
    def get_run(run_id: str, svc: ScopewatchService = Depends(get_service)) -> Run:
        return svc.get_run(run_id)

    @app.post("/api/v1/runs/{run_id}/complete", response_model=Run)
    def complete_run(run_id: str, svc: ScopewatchService = Depends(get_service)) -> Run:
        run, _ = svc.complete_run(run_id)
        return run

    @app.post("/api/v1/runs/{run_id}/fail", response_model=Run)
    def fail_run(
        run_id: str,
        reason: Optional[str] = Query(None),
        svc: ScopewatchService = Depends(get_service),
    ) -> Run:
        run, _ = svc.fail_run(run_id, reason=reason or "Agent execution failed.")
        return run

    # ---------------- Actions ----------------

    @app.post(
        "/api/v1/runs/{run_id}/actions",
        response_model=ActionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def submit_action(
        run_id: str,
        req: SubmitActionRequest,
        svc: ScopewatchService = Depends(get_service),
    ) -> ActionResponse:
        return await svc.submit_action(run_id, req)

    @app.get("/api/v1/runs/{run_id}/actions/{action_id}", response_model=ActionResponse)
    def get_action(
        run_id: str,
        action_id: str,
        svc: ScopewatchService = Depends(get_service),
    ) -> ActionResponse:
        return svc.get_action(run_id, action_id)

    # ---------------- Approvals ----------------

    @app.get("/api/v1/approvals", response_model=list[ApprovalRequest])
    def list_approvals(
        status: Optional[ApprovalStatus] = Query(None),
        run_id: Optional[str] = Query(None),
        svc: ScopewatchService = Depends(get_service),
    ) -> list[ApprovalRequest]:
        return svc.list_approvals(status_filter=status, run_id=run_id)

    @app.get("/api/v1/runs/{run_id}/approvals", response_model=list[ApprovalRequest])
    def list_run_approvals(
        run_id: str,
        status: Optional[ApprovalStatus] = Query(None),
        svc: ScopewatchService = Depends(get_service),
    ) -> list[ApprovalRequest]:
        svc.get_run(run_id)
        return svc.list_approvals(status_filter=status, run_id=run_id)

    @app.post("/api/v1/approvals/{approval_id}/approve", response_model=ApprovalResolutionResponse)
    async def approve_action(
        approval_id: str,
        req: Optional[ResolveApprovalRequest] = None,
        svc: ScopewatchService = Depends(get_service),
    ) -> ApprovalResolutionResponse:
        reason = req.resolution_reason if req else None
        return await svc.resolve_approval(
            approval_id=approval_id,
            approve=True,
            resolved_by=DEMO_REVIEWER_ID,
            reason=reason,
        )

    @app.post("/api/v1/approvals/{approval_id}/deny", response_model=ApprovalResolutionResponse)
    async def deny_action(
        approval_id: str,
        req: Optional[ResolveApprovalRequest] = None,
        svc: ScopewatchService = Depends(get_service),
    ) -> ApprovalResolutionResponse:
        reason = req.resolution_reason if req else None
        return await svc.resolve_approval(
            approval_id=approval_id,
            approve=False,
            resolved_by=DEMO_REVIEWER_ID,
            reason=reason,
        )

    # ---------------- Events ----------------

    @app.get("/api/v1/runs/{run_id}/events", response_model=list[EvidenceEvent])
    def get_events(
        run_id: str,
        after_sequence: Optional[int] = Query(None),
        limit: Optional[int] = Query(None),
        svc: ScopewatchService = Depends(get_service),
    ) -> list[EvidenceEvent]:
        svc.get_run(run_id)
        return svc.get_events(run_id, after_sequence=after_sequence, limit=limit)

    @app.get("/api/v1/runs/{run_id}/events/stream")
    async def stream_events(
        run_id: str,
        request: Request,
        last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
        limit: Optional[int] = Query(None),
        svc: ScopewatchService = Depends(get_service),
    ) -> EventSourceResponse:
        svc.get_run(run_id)

        async def event_generator():
            count = 0
            yield ServerSentEvent(event="connected", data="ready")
            count += 1
            if limit is not None and count >= limit:
                return

            if last_event_id and last_event_id.isdigit():
                stored_events = svc.get_events(run_id, after_sequence=int(last_event_id))
                for ev in stored_events:
                    yield ServerSentEvent(
                        id=str(ev.sequence),
                        event=ev.event_type.value,
                        data=ev.model_dump_json(),
                    )
                    count += 1
                    if limit is not None and count >= limit:
                        return

            queue = await broadcaster.subscribe(run_id)
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield ServerSentEvent(
                            id=str(event.sequence),
                            event=event.event_type.value,
                            data=event.model_dump_json(),
                        )
                        count += 1
                        if limit is not None and count >= limit:
                            return
                    except asyncio.TimeoutError:
                        yield ServerSentEvent(comment="keepalive")
            finally:
                await broadcaster.unsubscribe(run_id, queue)

        return EventSourceResponse(
            event_generator(),
            ping=None,
        )

    # ---------------- Frontend Static Serving ----------------

    if frontend_dir.exists():
        styles_dir = frontend_dir / "styles"
        scripts_dir = frontend_dir / "scripts"
        if styles_dir.exists():
            app.mount("/styles", StaticFiles(directory=styles_dir), name="styles")
        if scripts_dir.exists():
            app.mount("/scripts", StaticFiles(directory=scripts_dir), name="scripts")

        @app.get("/", include_in_schema=False)
        def serve_index() -> FileResponse:
            index_path = frontend_dir / "index.html"
            return FileResponse(index_path)

    return app


app = create_app()
