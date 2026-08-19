// The only place the frontend talks to the backend.
//
// Every call here maps to one documented endpoint -- open /docs to see the
// same list with its schemas. Nothing else in the UI knows a URL, so the API
// can be exercised with curl or driven by another system and this app keeps
// working exactly the same way.

async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const err = new Error(
      (data && (data.detail || data.error)) || `${res.status} ${res.statusText}`,
    );
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** GET /api/health — backend mode, and whether a run is holding the agent. */
export const getHealth = () => call("GET", "/api/health");

/** POST /api/runs — start a troubleshooting run. 409 if one is already active. */
export const startRun = ({ source, destination, protocol, port }) =>
  call("POST", "/api/runs", { source, destination, protocol, port });

/** GET /api/runs — recent runs, newest first. */
export const listRuns = (limit = 20) => call("GET", `/api/runs?limit=${limit}`);

/** GET /api/runs/{id} — one run in full. */
export const getRun = (id) => call("GET", `/api/runs/${id}`);

/** POST /api/runs/{id}/approvals/{aid} — let the parked command run, or skip it. */
export const answerApproval = (runId, approvalId, approved) =>
  call("POST", `/api/runs/${runId}/approvals/${approvalId}`, { approved });

/** POST /api/runs/{id}/deep — escalate to the deeper diagnostics. */
export const startDeep = (runId) => call("POST", `/api/runs/${runId}/deep`);

/** POST /api/ask — a question, answered from the conversation's context. */
export const ask = (question, runId) =>
  call("POST", "/api/ask", { question, run_id: runId || null });

/** GET /api/devices/{name} — a direct CMDB lookup. */
export const getDevice = (name, region = "AUTO") =>
  call("GET", `/api/devices/${encodeURIComponent(name)}?region=${region}`);

/**
 * GET /api/runs/{id}/events — subscribe to a run.
 *
 * Every frame is the whole run object, so `onRun` can just replace state
 * rather than merging partial updates. Returns a function that closes the
 * stream. The browser reconnects an EventSource on its own, and the server
 * replays current state on connect, so a dropped connection self-heals.
 */
export function subscribeToRun(runId, onRun, onError) {
  const source = new EventSource(`/api/runs/${runId}/events`);
  source.onmessage = (ev) => {
    try {
      const run = JSON.parse(ev.data);
      onRun(run);
      if (run.status === "done" || run.status === "error") source.close();
    } catch {
      /* a keep-alive comment, or a frame we cannot parse: ignore */
    }
  };
  source.onerror = () => {
    // EventSource retries by itself; only report it once it has given up
    if (source.readyState === EventSource.CLOSED && onError) onError();
  };
  return () => source.close();
}
