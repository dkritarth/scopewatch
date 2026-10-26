"""FastAPI application for Scopewatch synthetic baseline."""

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from scopewatch import __version__
from scopewatch.config import (
    ALLOW_DEMO_RESET,
    API_KEY,
    CORS_ALLOW_CREDENTIALS,
    CORS_ALLOW_ORIGINS,
    DB_PATH,
    DEFAULT_REVIEWER_IDENTITY,
    DEMO_REVIEWER_ID,
    ENABLE_HSTS,
    REVIEWER_TOKEN,
    WORKSPACE_ROOT,
    get_allow_demo_reset,
    get_api_key,
    get_cors_allow_credentials,
    get_cors_allow_origins,
    get_enable_hsts,
    get_reviewer_identity,
    get_reviewer_token,
    is_demo_mode,
    reviewer_token_expected,
    validate_auth_configuration,
)
from scopewatch.db import db_transaction, init_db
from scopewatch.demo_guards import (
    DemoGuardConfig,
    Decision,
    DemoGuardPolicy,
    DemoGuardRefusal,
    client_ip_from,
    is_mutating_api_call,
    token_fingerprint,
    token_matches,
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


def extract_bearer_token(request: Request) -> Optional[str]:
    """Extract token from Authorization: Bearer <token> if present."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def extract_reviewer_token(request: Request) -> Optional[str]:
    """Extract reviewer credential from request headers."""
    return (
        request.headers.get("x-reviewer-token")
        or request.headers.get("x-reviewer-key")
        or extract_bearer_token(request)
    )


def _active_cors_origins() -> list[str]:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "CORS_ALLOW_ORIGINS", None)
    if val:
        return val if isinstance(val, list) else [v.strip() for v in str(val).split(",") if v.strip()]
    return get_cors_allow_origins()


def _active_cors_credentials() -> bool:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "CORS_ALLOW_CREDENTIALS", None)
    if val is not None and val is not False:
        return bool(val)
    return get_cors_allow_credentials()


def _active_enable_hsts() -> bool:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "ENABLE_HSTS", None)
    if val:
        return bool(val)
    return get_enable_hsts()


def _active_allow_demo_reset() -> bool:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "ALLOW_DEMO_RESET", None)
    if val:
        return bool(val)
    return get_allow_demo_reset()


def _active_api_key() -> str:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "API_KEY", None)
    if val:
        return str(val)
    return get_api_key()


def _active_reviewer_token() -> str:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "REVIEWER_TOKEN", None)
    if val:
        return str(val)
    return get_reviewer_token()


def _active_reviewer_identity() -> str:
    import scopewatch.app as app_mod
    val = getattr(app_mod, "DEFAULT_REVIEWER_IDENTITY", None)
    if val:
        return str(val)
    return get_reviewer_identity()


def create_app(
    db_path: Path | str = DB_PATH,
    workspace_root: Path | str = WORKSPACE_ROOT,
    auditor: Optional[ReasoningAuditor] = None,
    demo_config: Optional[DemoGuardConfig] = None,
    capture_token: Optional[str] = None,
    run_workspaces_root: Optional[Path | str] = None,
) -> FastAPI:
    # Fail closed at startup if agent and reviewer credentials match
    active_agent = _active_api_key()
    active_rev = _active_reviewer_token()
    demo_tok = os.environ.get("DEMO_TOKEN", "").strip()
    if active_rev:
        for a in (demo_tok, active_agent):
            if a and hmac.compare_digest(a, active_rev):
                raise ValueError(
                    "Invalid authentication configuration: Agent credential and "
                    "reviewer credential must be distinct."
                )
    validate_auth_configuration()

    actual_db_path = Path(db_path)
    actual_workspace_root = Path(workspace_root)
    init_db(actual_db_path)
    actual_workspace_root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup: ensure the database and workspace root this app was built
        # with exist. Use the paths given to create_app, never the module
        # defaults, so a scratch app does not touch repo state (#133).
        c_agent = _active_api_key()
        c_rev = _active_reviewer_token()
        c_demo = os.environ.get("DEMO_TOKEN", "").strip()
        if c_rev:
            for a in (c_demo, c_agent):
                if a and hmac.compare_digest(a, c_rev):
                    raise ValueError(
                        "Invalid authentication configuration: Agent credential and "
                        "reviewer credential must be distinct."
                    )
        validate_auth_configuration()
        init_db(actual_db_path)
        actual_workspace_root.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(
        title="Scopewatch Synthetic Gateway",
        description="Controlled baseline demo API for synthetic agent action review.",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS configuration (issue #185):
    # Only enable CORS middleware if explicit origins are configured.
    # Default is empty list: same-origin only, never wildcard with credentials.
    cors_origins = _active_cors_origins()
    cors_creds = _active_cors_credentials()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=cors_creds,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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

    # Security headers middleware (issue #185)
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # HSTS - only when explicitly enabled or under HTTPS, never on local plain HTTP
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        if _active_enable_hsts() or (is_https and not is_demo_mode()):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # CSP - restrict sources without unsafe-inline in script-src
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        # Other security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

    # Cross-origin and Sec-Fetch-Site guard middleware (issue #185)
    @app.middleware("http")
    async def cross_origin_guard_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        if is_mutating_api_call(request.method, request.url.path):
            sec_fetch_site = request.headers.get("sec-fetch-site", "").lower()
            if sec_fetch_site == "cross-site":
                return JSONResponse(
                    status_code=403,
                    content={"error": "cross_site_request_blocked", "message": "Cross-site requests are forbidden."},
                )

            origin = request.headers.get("origin")
            if origin:
                host = request.headers.get("host", "").lower()
                parsed_origin = urlparse(origin)
                origin_netloc = parsed_origin.netloc.lower()
                origin_base = f"{parsed_origin.scheme}://{parsed_origin.netloc}".rstrip("/")
                request_base = str(request.base_url).rstrip("/")

                is_same_origin = (
                    origin_netloc == host
                    or origin_base == request_base
                )
                active_cors_origins = _active_cors_origins()
                is_allowed_cors = origin in active_cors_origins or origin_base in active_cors_origins

                if not (is_same_origin or is_allowed_cors):
                    return JSONResponse(
                        status_code=403,
                        content={"error": "cross_origin_forbidden", "message": "Cross-origin state-changing requests are forbidden."},
                    )

            # Check Content-Type for approval resolution (issue #185 AC)
            if "/approvals/" in request.url.path and (
                request.url.path.endswith("/approve") or request.url.path.endswith("/deny")
            ):
                raw_ct = request.headers.get("content-type")
                if not raw_ct:
                    return JSONResponse(
                        status_code=415,
                        content={"error": "unsupported_media_type", "message": "Content-Type must be application/json"},
                    )
                media_type = raw_ct.split(";")[0].strip().lower()
                if media_type != "application/json":
                    return JSONResponse(
                        status_code=415,
                        content={"error": "unsupported_media_type", "message": "Content-Type must be application/json"},
                    )

        return await call_next(request)

    # API Key authentication for production (issue #185): when API_KEY is configured
    # and not in demo mode, require X-API-Key header on mutating /api/* endpoints.
    # When API_KEY is unset, dev/local mode allows mutating calls without a key.
    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Require API key on mutating /api/* when configured outside demo mode."""
        current_api_key = _active_api_key()
        if current_api_key and not guards.config.enabled and is_mutating_api_call(request.method, request.url.path):
            provided_key = request.headers.get("x-api-key")
            provided_rev = extract_reviewer_token(request)
            exp_rev = reviewer_token_expected()

            # Reject any reviewer credential attempt that matches an agent credential regardless of header
            if provided_rev and token_matches(provided_rev, current_api_key):
                if "/approvals/" in request.url.path or "reset" in request.url.path:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "reviewer_credential_required",
                            "message": "Reviewer credential required to resolve approvals; agent credentials are not permitted.",
                        },
                    )

            authed_agent = bool(provided_key and token_matches(provided_key, current_api_key))
            authed_reviewer = bool(exp_rev and provided_rev and token_matches(provided_rev, exp_rev))

            if not (authed_agent or authed_reviewer):
                return JSONResponse(
                    status_code=401,
                    content={"error": "api_key_required", "message": "API key required. Send it as the X-API-Key header."},
                )
        return await call_next(request)

    @app.middleware("http")
    async def demo_token_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Require the shared demo token or reviewer token on mutating /api/* (demo mode only).

        Health, dashboard reads, and static assets stay public. Denials are
        flat {"error", "message"} JSON; tokens are never echoed or logged.
        """
        if guards.config.enabled and is_mutating_api_call(request.method, request.url.path):
            provided_demo = request.headers.get("x-demo-token")
            provided_rev = extract_reviewer_token(request)
            exp_rev = reviewer_token_expected()
            demo_tok = guards.config.demo_token if guards and guards.config else ""

            if provided_rev and demo_tok and token_matches(provided_rev, demo_tok):
                if "/approvals/" in request.url.path or "reset" in request.url.path:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "reviewer_credential_required",
                            "message": "Reviewer credential required to resolve approvals; agent credentials are not permitted.",
                        },
                    )

            decision = guards.check_token(provided_demo)
            if not decision.allowed and exp_rev and provided_rev and token_matches(provided_rev, exp_rev):
                decision = Decision(True, 200, "ok", "authorised_reviewer")

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

    def _verify_reviewer_auth(
        request: Request,
        req: Optional[ResolveApprovalRequest] = None,
    ) -> tuple[bool, str, Optional[JSONResponse]]:
        """Verify reviewer credential and resolve caller identity (issue #185 AC)."""
        exp_rev = reviewer_token_expected()
        provided_rev = extract_reviewer_token(request)
        provided_demo = request.headers.get("x-demo-token")
        provided_api = request.headers.get("x-api-key")

        current_api_key = _active_api_key()
        demo_tok = guards.config.demo_token if guards and guards.config else os.environ.get("DEMO_TOKEN", "")

        # Request defense: Reject any reviewer credential attempt that matches an agent credential,
        # regardless of header (X-Reviewer-Token, X-Reviewer-Key, or Authorization: Bearer).
        if provided_rev:
            if (current_api_key and token_matches(provided_rev, current_api_key)) or (
                demo_tok and token_matches(provided_rev, demo_tok)
            ):
                return False, "", JSONResponse(
                    status_code=403,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential required to resolve approvals; agent credentials are not permitted.",
                    },
                )

        auth_configured = bool(current_api_key or demo_tok or guards.config.enabled or exp_rev)

        if auth_configured:
            if not exp_rev:
                return False, "", JSONResponse(
                    status_code=403,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential is required to resolve approvals, but SCOPEWATCH_REVIEWER_TOKEN is not configured on the server.",
                    },
                )

            if provided_rev and token_matches(provided_rev, exp_rev):
                pass
            elif provided_demo or provided_api:
                # Agent credential provided: reject, agent cannot approve own holds
                return False, "", JSONResponse(
                    status_code=403,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential required to resolve approvals; agent credentials are not permitted.",
                    },
                )
            elif not provided_rev:
                return False, "", JSONResponse(
                    status_code=401,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential required to resolve approvals.",
                    },
                )
            else:
                return False, "", JSONResponse(
                    status_code=403,
                    content={
                        "error": "invalid_reviewer_credential",
                        "message": "Invalid reviewer credential.",
                    },
                )

            # In authenticated mode: ALWAYS bind resolved_by to explicit server-configured
            # reviewer identity or credential fingerprint. Request claims in body or headers
            # never become authenticated identity.
            current_reviewer_identity = _active_reviewer_identity()
            if current_reviewer_identity:
                reviewer_id = current_reviewer_identity
            else:
                reviewer_id = f"reviewer-{token_fingerprint(provided_rev)}"

            return True, reviewer_id, None

        # Local zero-key mode (no credentials configured on server):
        # Local zero-key documented fallback allowed.
        current_reviewer_identity = _active_reviewer_identity()
        candidate_id = ""
        if req and req.reviewer_id:
            candidate_id = re.sub(r"[^\w\s\-\.\(\)@]", "", req.reviewer_id)[:64].strip()
        if not candidate_id and request.headers.get("x-reviewer-id"):
            candidate_id = re.sub(r"[^\w\s\-\.\(\)@]", "", request.headers.get("x-reviewer-id", ""))[:64].strip()

        if current_reviewer_identity:
            reviewer_id = current_reviewer_identity
        elif candidate_id and candidate_id.lower() not in ("agent", "agent-authored", "model", "assistant"):
            reviewer_id = candidate_id
        else:
            reviewer_id = DEMO_REVIEWER_ID

        return True, reviewer_id, None

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
        exp_rev = reviewer_token_expected()
        return HealthResponse(
            status="ok",
            database=db_status,
            version=__version__,
            demo_mode=guards.config.enabled,
            reviewer_auth=bool(exp_rev),
        )

    # ---------------- Demo Reset ----------------

    @app.post("/api/v1/demo/reset")
    @app.post("/demo/reset")
    def reset_demo(
        request: Request,
        svc: ScopewatchService = Depends(get_service),
    ) -> Any:
        if not _active_allow_demo_reset():
            return JSONResponse(
                status_code=403,
                content={
                    "error": "reset_disabled",
                    "message": "Demo reset is disabled. Set SCOPEWATCH_ALLOW_RESET=true to enable.",
                },
            )

        exp_rev = reviewer_token_expected()
        current_api_key = _active_api_key()
        demo_tok = guards.config.demo_token if guards and guards.config else os.environ.get("DEMO_TOKEN", "")
        auth_configured = bool(exp_rev or current_api_key or demo_tok or guards.config.enabled)

        if auth_configured:
            if not exp_rev:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential required for demo reset, but SCOPEWATCH_REVIEWER_TOKEN is not configured on the server.",
                    },
                )
            provided_rev = extract_reviewer_token(request)
            if provided_rev and (
                (current_api_key and token_matches(provided_rev, current_api_key))
                or (demo_tok and token_matches(provided_rev, demo_tok))
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential required for demo reset; agent credentials are not permitted.",
                    },
                )
            if not (provided_rev and token_matches(provided_rev, exp_rev)):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "reviewer_credential_required",
                        "message": "Reviewer credential required for demo reset.",
                    },
                )

        conn = svc._get_conn()
        try:
            with db_transaction(conn):
                if svc.has_in_flight_executions(conn):
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": "execution_in_flight",
                            "message": "Cannot reset demo while action execution is in flight.",
                        },
                    )
                conn.execute("DELETE FROM evidence_events;")
                conn.execute("DELETE FROM execution_receipts;")
                conn.execute("DELETE FROM approval_requests;")
                conn.execute("DELETE FROM reasoning_audits;")
                conn.execute("DELETE FROM policy_decisions;")
                conn.execute("DELETE FROM action_requests;")
                conn.execute("DELETE FROM runs;")
        finally:
            conn.close()
        return {"status": "ok", "message": "Demo state reset successfully."}

    # ---------------- Runs ----------------

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
        """Side-effect-free deterministic policy dry-run (issue #122)."""
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
        request: Request,
        req: Optional[ResolveApprovalRequest] = None,
        svc: ScopewatchService = Depends(get_service),
    ) -> Any:
        ok, reviewer_id, err_resp = _verify_reviewer_auth(request, req)
        if not ok and err_resp is not None:
            return err_resp
        reason = req.resolution_reason if req else None
        return await svc.resolve_approval(
            approval_id=approval_id,
            approve=True,
            resolved_by=reviewer_id,
            reason=reason,
        )

    @app.post("/api/v1/approvals/{approval_id}/deny", response_model=ApprovalResolutionResponse)
    async def deny_action(
        approval_id: str,
        request: Request,
        req: Optional[ResolveApprovalRequest] = None,
        svc: ScopewatchService = Depends(get_service),
    ) -> Any:
        ok, reviewer_id, err_resp = _verify_reviewer_auth(request, req)
        if not ok and err_resp is not None:
            return err_resp
        reason = req.resolution_reason if req else None
        return await svc.resolve_approval(
            approval_id=approval_id,
            approve=False,
            resolved_by=reviewer_id,
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
    """Build the default ``app`` on first access, not on import (#133)."""
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
