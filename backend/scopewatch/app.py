"""FastAPI application for Scopewatch synthetic baseline."""

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from scopewatch import __version__
from scopewatch.config import DB_PATH, DEMO_REVIEWER_ID, WORKSPACE_ROOT
from scopewatch.db import init_db
from scopewatch.demo_guards import (
    DemoGuardConfig,
    DemoGuardPolicy,
    DemoGuardRefusal,
    client_ip_from,
    is_mutating_api_call,
)
from scopewatch.errors import (
    ScopewatchAPIError,
    generic_error_handler,
    scopewatch_api_error_handler,
    validation_error_handler,
)
from scopewatch.events import broadcaster
from scopewatch.models import ApprovalStatus
from scopewatch.provenance import CAPTURE_TOKEN_HEADER
from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import (
    ActionResponse,
    ApprovalRequest,
    ApprovalResolutionResponse,
    CreateRunRequest,
    EvidenceEvent,
    HealthResponse,
    PolicyDecision,
    ResolveApprovalRequest,
    RunResponse,
    SubmitActionRequest,
    UpdateRunRequest,
)
from scopewatch.service import ScopewatchService

logger = logging.getLogger("scopewatch.app")

frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"


async def demo_guard_refusal_handler(request: Request, exc: DemoGuardRefusal) -> JSONResponse:
    """Render demo-guard/token-budget refusals as flat sanitized JSON.

    Shape: {"error": "<code>", "message": "<msg>"} with Retry-After where apt.
    Never echoes tokens, keys, prompts, traces, or provider bodies.
    """
    headers = {}
    if exc.retry_after_s > 0:
        headers["Retry-After"] = str(exc.retry_after_s)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
        headers=headers,
    )


def request_client_ip(request: Request) -> str:
    """Best-effort client IP for per-IP rate limiting (X-Forwarded-For aware)."""
    lowered = {key.lower(): value for key, value in request.headers.items()}
    peer = request.client.host if request.client else "unknown"
    return client_ip_from(lowered, peer)


def create_app(
    db_path: Path | str = DB_PATH,
    workspace_root: Path | str = WORKSPACE_ROOT,
    auditor: Optional[ReasoningAuditor] = None,
    demo_config: Optional[DemoGuardConfig] = None,
capture_token: Optional[str] = None,
    run_workspaces_root: Optional[Path | str] = None,
) -> FastAPI:
    actual_db_path = Path(db_path)
    actual_workspace_root = Path(workspace_root)
    init_db(actual_db_path)
    actual_workspace_root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup: ensure the database and workspace root this app was built
        # with exist. Use the paths given to create_app, never the module
        # defaults, so a scratch app does not touch repo state (#133).
        init_db(actual_db_path)
        actual_workspace_root.mkdir(parents=True, exist_ok=True)
        yield

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
    app.add_exception_handler(DemoGuardRefusal, demo_guard_refusal_handler)  # type: ignore
    app.add_exception_handler(Exception, generic_error_handler)

    # Gateway-native demo guards (#76): one shared policy for the token
    # middleware and the service so rate/budget state never diverges.
    guards = DemoGuardPolicy(demo_config if demo_config is not None else DemoGuardConfig.from_env())
    app.state.guards = guards

    @app.middleware("http")
    async def demo_token_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Require the shared demo token on mutating /api/* (demo mode only).

        Health, dashboard reads, and static assets stay public. Denials are
        flat {"error", "message"} JSON; the token is never echoed or logged.
        """
        if guards.config.enabled and is_mutating_api_call(request.method, request.url.path):
            decision = guards.check_token(request.headers.get("x-demo-token"))
            if not decision.allowed:
                return JSONResponse(
                    status_code=decision.status,
                    content={"error": decision.code, "message": decision.message},
                )
        return await call_next(request)

    service = ScopewatchService(
        db_path=db_path,
        workspace_root=workspace_root,
        auditor=auditor,
        guards=guards,
capture_token=capture_token,
        run_workspaces_root=run_workspaces_root,
    )
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
        return HealthResponse(
            status="ok",
            database=db_status,
            version=__version__,
            demo_mode=guards.config.enabled,
        )

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

    # Run endpoints return RunResponse, not the storage model: the storage
    # model carries the host-side per-run workspace path (#117), which is not
    # something a public, token-free read should publish.

    @app.get("/api/v1/runs", response_model=list[RunResponse])
    def list_runs(svc: ScopewatchService = Depends(get_service)) -> list[RunResponse]:
        return [RunResponse.from_run(run) for run in svc.list_runs()]

    @app.post(
        "/api/v1/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED
    )
    def create_run(
        req: CreateRunRequest,
        request: Request,
        svc: ScopewatchService = Depends(get_service),
    ) -> RunResponse:
        run, _ = svc.create_run(
            name=req.name,
            task_scope=req.task_scope,
            client_ip=request_client_ip(request),
            prompt_version=req.prompt_version,
        )
        return RunResponse.from_run(run)

    @app.get("/api/v1/runs/{run_id}", response_model=RunResponse)
    def get_run(run_id: str, svc: ScopewatchService = Depends(get_service)) -> RunResponse:
        return RunResponse.from_run(svc.get_run(run_id))

    @app.patch("/api/v1/runs/{run_id}", response_model=RunResponse)
    def update_run(
        run_id: str,
        req: UpdateRunRequest,
        svc: ScopewatchService = Depends(get_service),
    ) -> RunResponse:
        if req.prompt_version:
            return RunResponse.from_run(
                svc.set_run_prompt_version(run_id, req.prompt_version)
            )
        return RunResponse.from_run(svc.get_run(run_id))

    @app.post("/api/v1/runs/{run_id}/complete", response_model=RunResponse)
    def complete_run(run_id: str, svc: ScopewatchService = Depends(get_service)) -> RunResponse:
        run, _ = svc.complete_run(run_id)
        return RunResponse.from_run(run)

    @app.post("/api/v1/runs/{run_id}/fail", response_model=RunResponse)
    def fail_run(
        run_id: str,
        reason: Optional[str] = Query(None),
        svc: ScopewatchService = Depends(get_service),
    ) -> RunResponse:
        run, _ = svc.fail_run(run_id, reason=reason or "Agent execution failed.")
        return RunResponse.from_run(run)

    # ---------------- Actions ----------------

    @app.post(
        "/api/v1/runs/{run_id}/actions",
        response_model=ActionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def submit_action(
        run_id: str,
        req: SubmitActionRequest,
        capture_token: Optional[str] = Header(
            None,
            alias=CAPTURE_TOKEN_HEADER,
            description=(
                "Capture credential held only by in-process provider-capture "
                "integrations (#116). Without it, submitted reasoning provenance "
                "is stored as an unverified caller assertion."
            ),
        ),
        svc: ScopewatchService = Depends(get_service),
    ) -> ActionResponse:
        return await svc.submit_action(run_id, req, capture_credential=capture_token)

    @app.post(
        "/api/v1/runs/{run_id}/actions/preview",
        response_model=PolicyDecision,
        status_code=status.HTTP_200_OK,
    )
    def preview_action(
        run_id: str,
        req: SubmitActionRequest,
        svc: ScopewatchService = Depends(get_service),
    ) -> PolicyDecision:
        """Side-effect-free deterministic policy dry-run (issue #122).

        Evaluates the exact gateway policy for the would-be action without
        persisting anything, creating approvals, or executing. Used by
        permission preflight so it agrees with actual submission by
        construction.
        """
        return svc.preview_action(run_id, req)

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
        after_sequence: Optional[int] = Query(None),
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

            cursor = None
            if last_event_id and last_event_id.isdigit():
                cursor = int(last_event_id)
            elif after_sequence is not None:
                cursor = after_sequence

            # Subscribe to future events BEFORE fetching replay events to eliminate race gap (#112)
            queue = await broadcaster.subscribe(run_id)
            seen_sequences: set[int] = set()
            try:
                if cursor is not None:
                    stored_events = svc.get_events(run_id, after_sequence=cursor)
                    for ev in stored_events:
                        seen_sequences.add(ev.sequence)
                        yield ServerSentEvent(
                            id=str(ev.sequence),
                            event=ev.event_type.value,
                            data=ev.model_dump_json(),
                        )
                        count += 1
                        if limit is not None and count >= limit:
                            return

                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        if event.sequence in seen_sequences:
                            continue
                        seen_sequences.add(event.sequence)
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


_default_app: Optional[FastAPI] = None


def __getattr__(name: str) -> Any:
    """Build the default ``app`` on first access, not on import (#133).

    ``uvicorn scopewatch.app:app`` and ``from scopewatch.app import app`` still
    work, but importing the module (tests, scripts that only need
    ``create_app``) no longer creates a database at the default path.
    """
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
