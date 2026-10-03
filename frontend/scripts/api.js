"use strict";

/**
 * Client library for Scopewatch synthetic baseline gateway API.
 */

const API_BASE = "";

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Mutating gateway API calls are the ones the demo guards require a token for.
 * Mirrors backend/scopewatch/demo_guards.py `is_mutating_api_call`: only
 * /api/* paths, only mutating methods. Reads (dashboard loads, health, event
 * streams) and non-API paths stay public and must never carry a credential.
 */
export function isMutatingRequest(method, url) {
  if (typeof url !== "string" || !url.startsWith("/api/")) return false;
  return MUTATING_METHODS.has(String(method || "GET").toUpperCase());
}

/**
 * Reviewer credential for the hosted demo (#115).
 *
 * Deliberately memory-only. It is never written to localStorage or
 * sessionStorage (a shared judging machine keeps nothing after a reload), never
 * placed in a URL or query string, never included in an error message, and
 * never logged. The only place it leaves this module is the X-Demo-Token header
 * of a mutating /api/ request.
 */
let reviewerToken = "";

/** Current reviewer token, or "" when none is held for this page load. */
export function getReviewerToken() {
  return reviewerToken;
}

/** Hold a reviewer token for this page load. Returns the stored value. */
export function setReviewerToken(token) {
  reviewerToken = typeof token === "string" ? token.trim() : "";
  return reviewerToken;
}

export function clearReviewerToken() {
  reviewerToken = "";
}

export async function fetchJson(url, options = {}) {
  const method = options.method || "GET";
  const headers = {
    Accept: "application/json",
    ...options.headers,
  };
  // Attach the credential ONLY to mutating API calls, and never override a
  // header the caller set explicitly.
  const callerSetToken = Object.keys(headers).some(
    (name) => name.toLowerCase() === "x-demo-token",
  );
  if (!callerSetToken && isMutatingRequest(method, url) && reviewerToken) {
    headers["X-Demo-Token"] = reviewerToken;
  }
  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }

  const response = await fetch(`${API_BASE}${url}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    let errorData = null;
    try {
      errorData = await response.json();
    } catch {
      // Non-JSON error
    }
    const { code, message, details } = parseApiErrorBody(
      errorData,
      response.status,
      response.statusText,
    );
    const error = new Error(message);
    error.status = response.status;
    error.code = code;
    error.details = details;
    throw error;
  }

  return response.json();
}

/**
 * Normalise the two error envelopes this deployment can produce into one
 * (code, message, details) triple.
 *
 * The gateway's own API errors are nested
 * `{"error": {"code", "message", "details"}}`, while the demo-guard and gate
 * refusals are flat `{"error": "<code>", "message": "..."}` (see
 * backend/scopewatch/app.py demo_token_middleware and deploy/gate/gate.py
 * denial_body). #107 aligned the gate with the gateway; the client still reads
 * both so an older deployed gate does not degrade the message to "HTTP 401".
 * Pure and exported so unit tests pin the shapes instead of a live stack.
 */
export function parseApiErrorBody(errorData, status, statusText) {
  const nested = errorData?.error;
  const isNested = nested !== null && typeof nested === "object";
  const code = isNested
    ? nested.code
    : typeof nested === "string"
      ? nested
      : errorData?.error_code || errorData?.code || "HTTP_ERROR";
  const message =
    (isNested ? nested.message : null) ||
    (typeof errorData?.message === "string" ? errorData.message : null) ||
    (typeof errorData?.detail === "string" ? errorData.detail : null) ||
    `HTTP error ${status} (${statusText})`;
  const details = (isNested ? nested.details : null) || errorData?.details || null;
  return { code: code || "HTTP_ERROR", message, details };
}

export async function checkHealth() {
  try {
    return await fetchJson("/api/v1/health");
  } catch {
    return null;
  }
}

export async function resetDemo() {
  return fetchJson("/api/v1/demo/reset", { method: "POST" });
}

export async function getRuns() {
  return fetchJson("/api/v1/runs");
}

export async function createRun(name, taskScope) {
  return fetchJson("/api/v1/runs", {
    method: "POST",
    body: { name, task_scope: taskScope },
  });
}

export async function getRun(runId) {
  return fetchJson(`/api/v1/runs/${encodeURIComponent(runId)}`);
}

export async function completeRun(runId) {
  return fetchJson(`/api/v1/runs/${encodeURIComponent(runId)}/complete`, {
    method: "POST",
  });
}

export async function submitAction(runId, actionData) {
  return fetchJson(`/api/v1/runs/${encodeURIComponent(runId)}/actions`, {
    method: "POST",
    body: actionData,
  });
}

export async function getApprovals(status = null, runId = null) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (runId) params.set("run_id", runId);
  const qs = params.toString();
  return fetchJson(`/api/v1/approvals${qs ? `?${qs}` : ""}`);
}

export async function approveAction(approvalId, reason = null) {
  return fetchJson(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}/approve`,
    {
      method: "POST",
      body: reason ? { resolution_reason: reason } : {},
    },
  );
}

export async function denyAction(approvalId, reason = null) {
  return fetchJson(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}/deny`,
    {
      method: "POST",
      body: reason ? { resolution_reason: reason } : {},
    },
  );
}

export async function getEvents(runId, afterSequence = null) {
  const params = new URLSearchParams();
  if (afterSequence !== null) params.set("after_sequence", String(afterSequence));
  const qs = params.toString();
  return fetchJson(
    `/api/v1/runs/${encodeURIComponent(runId)}/events${qs ? `?${qs}` : ""}`,
  );
}

/**
 * Backoff schedule shared by SSE reconnects and failed-fetch retries.
 * Exponential: baseMs * 2^attempt, capped at maxMs. Pure and exported so
 * unit tests pin the schedule instead of wall-clock timers.
 */
export const RECONNECT_BASE_DELAY_MS = 1000;
export const RECONNECT_MAX_DELAY_MS = 30000;

export function computeBackoffDelay(attempt, baseMs = RECONNECT_BASE_DELAY_MS, maxMs = RECONNECT_MAX_DELAY_MS) {
  const n = Math.max(0, Math.floor(Number(attempt) || 0));
  const base = Math.max(0, Number(baseMs) || 0);
  const cap = Math.max(0, Number(maxMs) || 0);
  return Math.min(base * 2 ** n, cap);
}

/** SSE reconnect schedule: 1s, 2s, 4s, ... capped at 30s. */
export function computeReconnectDelay(attempt) {
  return computeBackoffDelay(attempt, RECONNECT_BASE_DELAY_MS, RECONNECT_MAX_DELAY_MS);
}

/**
 * Parse one SSE `data:` payload into a live event object.
 * Malformed frames are logged and skipped (returns null) — never thrown —
 * so one bad frame cannot crash the timeline (#hardening: malformed-frame
 * tolerance). Callers must check for null.
 */
export function parseLiveFrame(data) {
  if (typeof data !== "string" || data.trim() === "") {
    console.warn("[scopewatch] skipping malformed live frame: empty or non-string payload");
    return null;
  }
  let parsed = null;
  try {
    parsed = JSON.parse(data);
  } catch {
    console.warn("[scopewatch] skipping malformed live frame: invalid JSON");
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    console.warn("[scopewatch] skipping malformed live frame: expected a JSON object");
    return null;
  }
  return parsed;
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Run an async `task(attempt)` with exponential-backoff retries.
 * Resolves with the first success; rethrows the last error after
 * `retries` failed retries. `sleep` is injectable so unit tests assert the
 * schedule without waiting. Used for failed-fetch retry on approvals/runs.
 */
export async function withRetry(task, { retries = 2, baseDelayMs = 300, maxDelayMs = 5000, sleep = defaultSleep } = {}) {
  const maxRetries = Math.max(0, Math.floor(Number(retries) || 0));
  let attempt = 0;
  for (;;) {
    try {
      return await task(attempt);
    } catch (err) {
      if (attempt >= maxRetries) throw err;
      await sleep(computeBackoffDelay(attempt, baseDelayMs, maxDelayMs));
      attempt += 1;
    }
  }
}

/**
 * Every EventType the backend gateway can emit over SSE
 * (backend/scopewatch/models.py `EventType`, streamed by `app.py`
 * `GET /api/v1/runs/{run_id}/events/stream`). Kept as an exported constant
 * so unit tests can assert the subscription tracks the backend 1:1 instead
 * of drifting silently (defect 3a: POLICY_HELD, REASONING_AUDIT_COMPLETED,
 * REASONING_AUDIT_FAILED were omitted and never rendered live).
 */
export const LIVE_EVENT_TYPES = [
  "RUN_CREATED",
  "ACTION_REQUESTED",
  "POLICY_ALLOWED",
  "POLICY_DENIED",
  "POLICY_HELD",
  "APPROVAL_REQUESTED",
  "APPROVAL_GRANTED",
  "APPROVAL_DENIED",
  "APPROVAL_EXPIRED",
  "EXECUTION_STARTED",
  "EXECUTION_SUCCEEDED",
  "EXECUTION_FAILED",
  "RUN_COMPLETED",
  "SYSTEM_ERROR",
  "REASONING_AUDIT_COMPLETED",
  "REASONING_AUDIT_FAILED",
];

/**
 * Connect to live Server-Sent Events stream with automated fallback to polling.
 *
 * Robustness contract (frontend hardening):
 * - Every inbound frame goes through `parseLiveFrame`: malformed frames are
 *   logged and skipped, never delivered to `onEvent`, never thrown.
 * - SSE errors report `reconnecting` with the backoff schedule
 *   (`computeReconnectDelay`) instead of failing silently; a permanent close
 *   falls back to polling.
 * - `staleAfterMs` (default 45s) emits a `stale` status when no frame arrives
 *   in time, driving the dashboard's stale banner. Any valid frame resets it.
 */
export const STALE_AFTER_MS = 45000;

export function connectLiveEvents(runId, { onEvent, onStatusChange, initialSequence = 0, staleAfterMs = STALE_AFTER_MS }) {
  let isClosed = false;
  let eventSource = null;
  let pollInterval = null;
  let lastSequence = initialSequence;
  let reconnectAttempt = 0;
  let staleTimer = null;

  function setStatus(status, detail = "") {
    if (!isClosed && onStatusChange) {
      onStatusChange({ status, detail });
    }
  }

  function armStaleTimer() {
    clearStaleTimer();
    if (isClosed) return;
    const after = Math.max(0, Number(staleAfterMs) || 0);
    if (after === 0) return;
    staleTimer = setTimeout(() => {
      staleTimer = null;
      if (!isClosed) {
        setStatus("stale", `No live frames for ${Math.round(after / 1000)}s. Showing last known state.`);
      }
    }, after);
    if (typeof staleTimer.unref === "function") staleTimer.unref();
  }

  function clearStaleTimer() {
    if (staleTimer) {
      clearTimeout(staleTimer);
      staleTimer = null;
    }
  }

  function deliver(rawData) {
    const ev = parseLiveFrame(rawData);
    if (!ev) return;
    reconnectAttempt = 0;
    armStaleTimer();
    if (ev.sequence && ev.sequence > lastSequence) {
      lastSequence = ev.sequence;
    }
    onEvent(ev);
  }

  function startPolling() {
    if (pollInterval || isClosed) return;
    setStatus("polling", "Live stream unavailable. Polling for updates.");

    async function poll() {
      if (isClosed) return;
      try {
        const events = await getEvents(runId, lastSequence);
        for (const ev of events) {
          if (ev.sequence > lastSequence) {
            lastSequence = ev.sequence;
            onEvent(ev);
          }
        }
      } catch (err) {
        setStatus("polling_error", err.message);
      }
    }

    pollInterval = setInterval(poll, 2500);
    poll();
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  }

  if (typeof EventSource !== "undefined") {
    try {
      const url = initialSequence > 0
        ? `/api/v1/runs/${encodeURIComponent(runId)}/events/stream?after_sequence=${encodeURIComponent(initialSequence)}`
        : `/api/v1/runs/${encodeURIComponent(runId)}/events/stream`;
      eventSource = new EventSource(url);

      eventSource.onopen = () => {
        reconnectAttempt = 0;
        stopPolling();
        armStaleTimer();
        setStatus("connected", "Live SSE stream connected.");
      };

      eventSource.onmessage = (e) => {
        deliver(e.data);
      };

      eventSource.addEventListener("connected", () => {
        reconnectAttempt = 0;
        stopPolling();
        armStaleTimer();
        setStatus("connected", "Live SSE stream connected.");
      });

      // Register all domain event types (see LIVE_EVENT_TYPES above).
      const eventTypes = LIVE_EVENT_TYPES;

      for (const eventType of eventTypes) {
        eventSource.addEventListener(eventType, (e) => {
          deliver(e.data);
        });
      }

      eventSource.onerror = () => {
        if (eventSource.readyState === EventSource.CLOSED) {
          eventSource.close();
          eventSource = null;
          startPolling();
        } else {
          const delayMs = computeReconnectDelay(reconnectAttempt);
          reconnectAttempt += 1;
          setStatus("reconnecting", `Reconnecting live stream (attempt ${reconnectAttempt}, retry in ${Math.round(delayMs / 1000)}s)...`);
        }
      };
    } catch {
      startPolling();
    }
  } else {
    startPolling();
  }

  return {
    close() {
      isClosed = true;
      clearStaleTimer();
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      stopPolling();
      setStatus("disconnected", "Stream closed.");
    },
    getLastSequence() {
      return lastSequence;
    },
    getReconnectAttempt() {
      return reconnectAttempt;
    },
  };
}
