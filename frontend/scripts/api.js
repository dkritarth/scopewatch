"use strict";

/**
 * Client library for Scopewatch synthetic baseline gateway API.
 */

const API_BASE = "";

export async function fetchJson(url, options = {}) {
  const headers = {
    Accept: "application/json",
    ...options.headers,
  };
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
    const message =
      errorData?.error?.message ||
      `HTTP error ${response.status} (${response.statusText})`;
    const error = new Error(message);
    error.status = response.status;
    error.code = errorData?.error?.code || "HTTP_ERROR";
    error.details = errorData?.error?.details || null;
    throw error;
  }

  return response.json();
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
 * Connect to live Server-Sent Events stream with automated fallback to polling.
 */
export function connectLiveEvents(runId, { onEvent, onStatusChange, initialSequence = 0 }) {
  let isClosed = false;
  let eventSource = null;
  let pollInterval = null;
  let lastSequence = initialSequence;

  function setStatus(status, detail = "") {
    if (!isClosed && onStatusChange) {
      onStatusChange({ status, detail });
    }
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
      const url = `/api/v1/runs/${encodeURIComponent(runId)}/events/stream`;
      eventSource = new EventSource(url);

      eventSource.onopen = () => {
        stopPolling();
        setStatus("connected", "Live SSE stream connected.");
      };

      eventSource.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data);
          if (ev.sequence && ev.sequence > lastSequence) {
            lastSequence = ev.sequence;
          }
          onEvent(ev);
        } catch {
          // Keepalive or comment
        }
      };

      eventSource.addEventListener("connected", () => {
        stopPolling();
        setStatus("connected", "Live SSE stream connected.");
      });

      // Register all domain event types
      const eventTypes = [
        "RUN_CREATED",
        "RUN_COMPLETED",
        "ACTION_REQUESTED",
        "POLICY_ALLOWED",
        "POLICY_DENIED",
        "POLICY_HELD_FOR_APPROVAL",
        "APPROVAL_REQUESTED",
        "APPROVAL_GRANTED",
        "APPROVAL_DENIED",
        "APPROVAL_EXPIRED",
        "EXECUTION_STARTED",
        "EXECUTION_SUCCEEDED",
        "EXECUTION_FAILED",
      ];

      for (const eventType of eventTypes) {
        eventSource.addEventListener(eventType, (e) => {
          try {
            const ev = JSON.parse(e.data);
            if (ev.sequence && ev.sequence > lastSequence) {
              lastSequence = ev.sequence;
            }
            onEvent(ev);
          } catch {
            // Ignore parse errors
          }
        });
      }

      eventSource.onerror = () => {
        if (eventSource.readyState === EventSource.CLOSED) {
          eventSource.close();
          eventSource = null;
          startPolling();
        } else {
          setStatus("reconnecting", "Reconnecting live stream...");
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
  };
}
