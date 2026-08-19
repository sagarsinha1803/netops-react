"""REST API for the network troubleshooting agent.

The agent itself -- the LangGraph graph, the read-only guards, the MCP servers,
the clipboard relay -- is untouched by this layer. This module only exposes it:

    POST /api/runs                        start a troubleshooting run
    GET  /api/runs                        list runs
    GET  /api/runs/{id}                   full state: progress, commands, verdict
    GET  /api/runs/{id}/events            SSE: pushed on every change
    POST /api/runs/{id}/approvals/{aid}   approve or reject a device command
    POST /api/runs/{id}/deep              run the deeper diagnostics
    POST /api/ask                         a question, answered from run context
    GET  /api/devices/{name}              CMDB lookup
    GET  /api/health                      backend mode and readiness

Interactive docs: /docs (Swagger) and /redoc. The React app in frontend/ is a
client of exactly these endpoints and nothing else.

A run executes in the background and is addressed by id, so the browser can be
closed and reopened without interrupting the agent, and any other system can
drive it over plain HTTP.

    uvicorn api.main:app --port 8000
"""
import asyncio
import json
import os
import sys

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C                  # noqa: E402
from agent.llm import ip_mask                     # noqa: E402
from agent.utils import tool_text                 # noqa: E402

from api import runs as R                         # noqa: E402
from api.models import (AcceptedResponse, ApprovalRequest,  # noqa: E402
                        AskRequest, DeviceResponse, HealthResponse,
                        RunSummary, RunView, TroubleshootRequest)

app = FastAPI(
    title="NetOps troubleshooting agent",
    version="1.0.0",
    description=(
        "Decides whether a destination is reachable from a source, shows the "
        "path, and says where it breaks.\n\n"
        "**How a run works.** `POST /api/runs` returns immediately with a "
        "`run_id`; the agent keeps working in the background. Poll "
        "`GET /api/runs/{id}` or subscribe to `GET /api/runs/{id}/events` "
        "(server-sent events) to follow it.\n\n"
        "**Approvals.** Every command that touches a device needs a human "
        "decision. When a run's `status` is `waiting_approval`, its "
        "`pending_approval` field holds the exact command; answer it with "
        "`POST /api/runs/{id}/approvals/{aid}`. Nothing runs on a device until "
        "you do.\n\n"
        "**One run at a time.** The agent is a single conversation with a "
        "single model — and in clipboard mode, a single clipboard. Starting a "
        "run while another is active returns 409.\n\n"
        "**No CMDB record?** The agent falls back to probing from the machine "
        "it runs on, skips the SSH and firewall steps, and says so in the "
        "report."
    ),
)


def _run_view(run: R.Run) -> dict:
    """A run as the API describes it. One place, so every endpoint agrees."""
    return {
        "id": run.id,
        "status": run.status,
        "kind": run.kind,
        "request": run.request,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "workflow": run.wf.snapshot(),
        "report": run.report,
        "deep_report": run.deep_report,
        "answer": run.answer,
        "pending_approval": run.pending,
        "offer_deep": run.offer_deep,
        "error": run.error,
        "unavailable_servers": run.unavailable,
    }


def _require(run_id: str) -> R.Run:
    run = R.STORE.get(run_id)
    if not run:
        raise HTTPException(404, f"no run with id '{run_id}'")
    return run


def _refuse_if_busy():
    busy = R.STORE.busy()
    if busy:
        raise HTTPException(
            409,
            f"run '{busy.id}' is still active ({busy.status}). The agent runs "
            "one conversation at a time.")


# ------------------------------------------------------------------ health --
@app.get("/api/health", response_model=HealthResponse, tags=["status"],
         summary="Backend mode and readiness")
async def health():
    """Which model backend is in use, whether the mock MCP servers are wired
    up, and whether a run is currently holding the agent."""
    busy = R.STORE.busy()
    return {"ok": True, "llm_mode": C.LLM_MODE, "mocks": C.USE_MOCKS,
            "masking": ip_mask.enabled(), "active_run": busy.id if busy else None}


# -------------------------------------------------------------------- runs --
@app.post("/api/runs", response_model=RunView, status_code=202, tags=["runs"],
          summary="Start a troubleshooting run",
          responses={409: {"description": "Another run is still active"}})
async def start_run(body: TroubleshootRequest):
    """Start a source-to-destination reachability run.

    Returns straight away, before the agent has done anything: follow the run
    with `GET /api/runs/{id}` or its event stream. Expect the first
    `waiting_approval` within a few seconds — the ping needs your sign-off.
    """
    _refuse_if_busy()
    run = R.start("troubleshoot", body.to_prompt())
    return _run_view(run)


@app.get("/api/runs", response_model=list[RunSummary], tags=["runs"],
         summary="List runs, newest first")
async def list_runs(limit: int = Query(20, ge=1, le=50)):
    """Recent runs without their detail. In-memory: restarting the server
    clears them."""
    ordered = sorted(R.STORE.runs.values(), key=lambda r: r.created_at,
                     reverse=True)
    return [{"id": r.id, "status": r.status, "kind": r.kind,
             "request": r.request, "created_at": r.created_at,
             "updated_at": r.updated_at} for r in ordered[:limit]]


@app.get("/api/runs/{run_id}", response_model=RunView, tags=["runs"],
         summary="One run in full")
async def get_run(run_id: str = Path(..., description="Run id from POST /api/runs")):
    """Everything about a run: stage progress, every command with its output,
    the parsed path, the verdict, and whatever it is waiting on."""
    return _run_view(_require(run_id))


@app.get("/api/runs/{run_id}/events", tags=["runs"],
         summary="Live updates (server-sent events)",
         response_class=StreamingResponse,
         responses={200: {"content": {"text/event-stream": {}},
                          "description": "A `data:` frame carrying the same "
                                         "object as GET /api/runs/{id}, sent "
                                         "on every change"}})
async def run_events(run_id: str):
    """Subscribe to a run.

    Each frame is the complete run object, so a client never has to merge
    partial updates -- it renders whatever arrived last. The stream ends when
    the run reaches `done` or `error`. Swagger cannot exercise a stream; use
    `curl -N` or an `EventSource` in the browser.
    """
    run = _require(run_id)

    async def stream():
        queue = R.STORE.subscribe(run_id)
        try:
            # the current state first, so a late subscriber is never behind
            yield f"data: {json.dumps(_run_view(run), default=str)}\n\n"
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"        # keep proxies from closing it
                    if run.status in ("done", "error"):
                        break
                    continue
                yield f"data: {json.dumps(_run_view(run), default=str)}\n\n"
                if run.status in ("done", "error"):
                    break
        finally:
            R.STORE.unsubscribe(run_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/approvals/{approval_id}",
          response_model=AcceptedResponse, tags=["runs"],
          summary="Approve or reject the pending device command",
          responses={409: {"description": "The run is not waiting on this "
                                          "approval any more"}})
async def answer_approval(body: ApprovalRequest, run_id: str,
                          approval_id: str = Path(
                              ..., description="pending_approval.id from the run")):
    """Let the parked command run, or skip it.

    A rejected command is reported to the model as declined; the agent carries
    on with what it has and says so in the report. Approvals expire after 15
    minutes, which counts as a rejection.
    """
    run = _require(run_id)
    if not run.answer_approval(approval_id, body.approved):
        raise HTTPException(
            409, "that approval is no longer pending (already answered, "
                 "expired, or the id does not match)")
    return {"run_id": run.id, "status": run.status}


@app.post("/api/runs/{run_id}/deep", response_model=RunView, status_code=202,
          tags=["runs"], summary="Run the deeper diagnostics",
          responses={409: {"description": "Another run is still active"}})
async def run_deep(run_id: str):
    """Escalate: route presence, VRF, forwarding entry, next hop, interface
    state and ACLs, one check at a time, each still needing approval.

    Only worth calling when the run's `offer_deep` is true. The result appends
    to this conversation as a second verdict rather than replacing the first.
    """
    _require(run_id)
    _refuse_if_busy()
    run = R.start("deep", R.DEEP_PROMPT, deep=True)
    return _run_view(run)


# --------------------------------------------------------------- questions --
@app.post("/api/ask", response_model=RunView, status_code=202, tags=["ask"],
          summary="Ask a question",
          responses={409: {"description": "Another run is still active"}})
async def ask(body: AskRequest):
    """Ask anything: what a result means, a device lookup, a general networking
    question.

    Questions share the agent's conversation, so a question asked after a run
    is answered from that run's evidence -- the CMDB records, the probe output,
    the firewall verdict. The answer arrives in the run's `answer` field.
    """
    _refuse_if_busy()
    kind = R.classify_request(body.question)
    run = R.start(kind, body.question)
    return _run_view(run)


# ----------------------------------------------------------------- devices --
@app.get("/api/devices/{name}", response_model=DeviceResponse, tags=["devices"],
         summary="Look a device up in the CMDB")
async def device(name: str = Path(..., description="Device name or IP address"),
                 region: str = Query("AUTO", description="Region, or AUTO")):
    """A direct CMDB lookup, no agent involved -- vendor, model, OS and
    management address.

    Credentials are stripped at the MCP boundary and never reach this response.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    try:
        client = MultiServerMCPClient({"unicorn": C.MCP_SERVERS["unicorn"]})
        tools = {t.name: t for t in await client.get_tools()}
        raw = tool_text(await tools["get_device_details"].ainvoke(
            {"device_name": name, "region": region}))
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(502, f"CMDB lookup failed: {e}")

    body = raw.strip()
    if body.startswith("{"):
        try:
            return {"query": name, "found": True, "record": json.loads(body),
                    "raw": raw}
        except Exception:
            pass
    return {"query": name, "found": False, "record": None, "raw": raw}


# ------------------------------------------------------------- static files --
# Mounted last so /api and /docs win. html=True serves index.html at "/".
_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "frontend", "dist")


class _Frontend(StaticFiles):
    """Static files, with index.html never cached.

    Vite fingerprints the bundles (index-<hash>.js), so those are safe to cache
    forever -- but index.html is the thing that NAMES them. Letting the browser
    cache it means a rebuilt frontend keeps loading the previous bundle, and
    the app silently runs old code against a new API.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("", ".", "/") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


if os.path.isdir(_DIST):
    app.mount("/", _Frontend(directory=_DIST, html=True), name="frontend")
else:
    @app.get("/", include_in_schema=False)
    async def no_frontend():
        return JSONResponse(
            {"error": "frontend not built",
             "fix": "cd frontend && npm install && npm run build",
             "api_docs": "/docs"},
            status_code=503)
