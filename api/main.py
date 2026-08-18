"""FastAPI + WebSocket backend for the network troubleshooting agent.

The graph, the tools, the guards and the clipboard relay are untouched -- this
replaces only Chainlit. One WebSocket per browser tab; each connection is its
own conversation (single-session by design: no login, no thread store).

    uvicorn api.main:app --port 8000

Protocol (JSON, one object per frame):
  client -> server
    {"type": "chat", "text": "..."}          a user turn
    {"type": "deep_check"}                   the Run-deeper-checks button
    {"type": "approval", "id": "...", "approved": true|false}
  server -> client
    {"type": "hello", ...}                   greeting + backend facts
    {"type": "status", "state": "...", "detail": "..."}
    {"type": "user_echo", "text": "..."}     confirms a turn has started
    {"type": "thought", "text": "..."}       the model's reasoning
    {"type": "tool_result", "name", "body"}  a tool's (truncated) output
    {"type": "step", ...}                    a deep-check command + its output
    {"type": "approval_request", "id", "payload"}
    {"type": "rejected", "command": "..."}
    {"type": "workflow", "wf": {...}}        full panel snapshot
    {"type": "final", "answer", "report", "offer_deep", "is_deep"}
    {"type": "error", "message": "..."}
"""
import asyncio
import json
import os
import re
import sys
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C                  # noqa: E402
from agent import graph as net_agent              # noqa: E402
from agent.constants import (DEVICE_TOOL_NAMES,   # noqa: E402
                             POLICY_TOOL_NAMES)
from agent.llm import ip_mask                     # noqa: E402
from agent.prompts import DEEP_CHECK_PROMPT       # noqa: E402
from agent.utils import display_command           # noqa: E402

from api.workflow import (Workflow, as_report, check_ok,      # noqa: E402
                          device_label, failed_line,
                          parse_request, policy_verdict)

CLIP = C.LLM_MODE == "clipboard"

GREETING = (
    "**Network Operations troubleshooting agent**\n\n"
    "I check whether a destination is reachable from a source, show the path, "
    "and point out where it breaks. Sources the CMDB does not know are probed "
    "from this machine instead.\n\n"
    "Try: `troubleshoot 10.10.1.20 to 172.20.5.10` · "
    "`fetch device details for dc10-a1`"
)

app = FastAPI(title="netops-agent")


@app.get("/api/health")
async def health():
    return {"ok": True, "llm_mode": C.LLM_MODE, "mocks": C.USE_MOCKS}


# ------------------------------------------------------------------ session --
class Session:
    """One WebSocket connection: its own conversation, approvals and panel."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.checkpointer = MemorySaver()
        self.thread_id = uuid.uuid4().hex
        self.wf = Workflow()
        self.approvals: dict[str, asyncio.Future] = {}
        self.busy = False

    async def send(self, obj: dict):
        await self.ws.send_text(json.dumps(obj, default=str))

    async def push_wf(self):
        await self.send({"type": "workflow", "wf": self.wf.snapshot()})

    async def status(self, state: str, detail: str = ""):
        await self.send({"type": "status", "state": state, "detail": detail})

    async def ask_approval(self, payload: dict) -> bool:
        """Send the approval card and wait for the button click."""
        aid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.approvals[aid] = fut
        await self.send({"type": "approval_request", "id": aid,
                         "payload": payload})
        try:
            return bool(await asyncio.wait_for(fut, timeout=600))
        except asyncio.TimeoutError:
            return False
        finally:
            self.approvals.pop(aid, None)

    def resolve_approval(self, aid: str, approved: bool):
        fut = self.approvals.get(aid)
        if fut and not fut.done():
            fut.set_result(approved)


# ---------------------------------------------------------------- the drive --
async def drive(sess: Session, app_graph, config, first_input,
                show_commands=False):
    """Run the graph, emitting each step, pausing at every approval.

    A straight port of the Chainlit drive(): the same message-walk, the same
    basic/deep classification, the same interrupt handling -- but each render
    is one WebSocket frame instead of a Chainlit step.
    """
    wf = sess.wf
    try:
        snap = await app_graph.aget_state(config)
        seen = len((getattr(snap, "values", None) or {}).get("messages") or [])
    except Exception:
        seen = 0

    await sess.status("waiting_clipboard" if CLIP else "thinking")
    state = await app_graph.ainvoke(first_input, config)
    issued = {}                    # tool_call id -> (name, args)
    check_idx = {}                 # tool_call id -> row in the Deep list
    basic_idx = {}                 # tool_call id -> row in the Basic list
    step_no = 0

    while True:
        msgs = state.get("messages", [])

        for m in msgs[seen:]:
            if isinstance(m, HumanMessage):
                continue
            if isinstance(m, AIMessage) and m.tool_calls:
                thought = str(m.content or "").strip()
                if thought:
                    await sess.send({"type": "thought", "text": thought})
                for tc in m.tool_calls:
                    args = tc.get("args") or {}
                    issued[tc.get("id")] = (tc["name"], args)
                    if tc["name"] not in DEVICE_TOOL_NAMES:
                        if not show_commands:
                            cmd = display_command(tc["name"], args)
                            wf.from_tool_call(tc["name"], args, cmd)
                            policy = tc["name"] in POLICY_TOOL_NAMES
                            label = (
                                f"get_firewall_path({args.get('src', '')} → "
                                f"{args.get('dst', '')}, {args.get('service') or 'any'})"
                                if policy else
                                f"{tc['name']}({args.get('device_name') or ''})")
                            basic_idx[tc.get("id")] = wf.add_basic(
                                label, thought=thought,
                                kind="policy" if policy else "cmdb")
                        continue
                    cmd = display_command(tc["name"], args)
                    where = (args.get("device_ip") or args.get("source")
                             or ("agent host" if tc["name"].startswith("local_")
                                 else "device"))
                    kind = wf.classify(cmd, show_commands)
                    if kind == "deep":
                        check_idx[tc.get("id")] = wf.add_check(
                            cmd, where, args.get("region"), thought)
                    else:
                        wf.from_tool_call(tc["name"], args, cmd)
                        basic_idx[tc.get("id")] = wf.add_basic(
                            cmd, where, args.get("region"), thought, kind)
                await sess.push_wf()
            elif isinstance(m, ToolMessage):
                body = str(m.content)
                await sess.send({"type": "tool_result", "name": m.name,
                                 "body": body[:3000]})

                tid = getattr(m, "tool_call_id", None)
                name, args = issued.get(tid, (m.name, {}))
                if tid in check_idx or tid in basic_idx:
                    lines = [ln for ln in body.splitlines() if ln.strip()]
                    echo = display_command(name, args)
                    useful = [ln for ln in lines if echo not in ln]
                    detail = failed_line(body) or (useful or lines or [""])[0]
                    ok = check_ok(body)
                    if tid in check_idx:
                        wf.finish_check(check_idx[tid], ok, detail[:70],
                                        output=body)
                    elif wf.basics[basic_idx[tid]].get("kind") == "policy":
                        verdict, acl = policy_verdict(body)
                        wf.finish_basic(
                            basic_idx[tid], verdict == "ALLOWED",
                            f"{verdict}{f' · {acl}' if acl else ''}",
                            device="Tufin SecureTrack", output=body)
                        wf.set("policy",
                               "done" if verdict == "ALLOWED"
                               else "failed" if verdict == "BLOCKED" else "skipped",
                               (f"Traffic {verdict.lower()}"
                                + (f" by {acl}" if acl else "")))
                    elif wf.basics[basic_idx[tid]].get("kind") == "cmdb":
                        found = device_label(body, "")
                        wf.finish_basic(basic_idx[tid], bool(found),
                                        "" if found else "not found in CMDB",
                                        device=found or None, output=body)
                    else:
                        wf.finish_basic(basic_idx[tid], ok, detail[:70],
                                        output=body)
                    await sess.push_wf()
                if show_commands and name in DEVICE_TOOL_NAMES:
                    step_no += 1
                    await sess.send({
                        "type": "step", "no": step_no,
                        "where": (args.get("device_ip") or args.get("source")
                                  or ("agent host" if name.startswith("local_")
                                      else "device")),
                        "region": args.get("region"),
                        "command": display_command(name, args),
                        "output": body[:1200] or "(no output)"})
        seen = len(msgs)

        # a deeper-checks turn probes other addresses; it must not rewrite the
        # Basic timeline or the Path of the run it extends
        if not show_commands:
            wf.from_state(state)
            await sess.push_wf()

        if "__interrupt__" not in state:
            break

        payload = dict(state["__interrupt__"][0].value or {})
        cmd = str(payload.get("command", ""))

        stage = wf.classify(cmd, show_commands)
        timeline = stage in ("ping", "trace")
        if timeline:
            wf.set(stage, "running", f"awaiting approval · {cmd[:50]}")
            await sess.push_wf()

        await sess.status("approval", cmd)
        approved = await sess.ask_approval(payload)
        if not approved:
            if timeline:
                wf.set(stage, "skipped", "rejected by reviewer")
                await sess.push_wf()
            await sess.send({"type": "rejected", "command": cmd})
            await sess.status("waiting_clipboard" if CLIP else "thinking")
            state = await app_graph.ainvoke(Command(resume=False), config)
            continue

        if timeline:
            wf.set(stage, "running", f"running · {cmd[:50]}")
            await sess.push_wf()
        await sess.status("executing", cmd)
        state = await app_graph.ainvoke(Command(resume=True), config)
        await sess.status("waiting_clipboard" if CLIP else "thinking")

    return state


# ----------------------------------------------------------------- one turn --
async def run_turn(sess: Session, text: str, show_commands=False):
    """One agent turn. Shared by typed messages and the deeper-checks button."""
    wf = sess.wf
    params = parse_request(text)

    # a device NAME typed in the request would otherwise reach the model in the
    # first paste, before any CMDB lookup has registered it
    if net_agent._mask_names:
        for value in (params.get("source"), params.get("dest")):
            if value and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(value)):
                ip_mask.session_mask().register(value, "host")

    wants_path = bool(re.search(r"\b(troubleshoot|reachab|ping|trace)\w*\b",
                                text, re.I)) and bool(params)
    wants_lookup = bool(re.search(
        r"\b(device\s*detail|cmdb|look\s*up|lookup|fetch|inventory)\w*\b",
        text, re.I))

    # A plain question is not a workflow turn: it must not reset the panel on
    # the way in, nor mark its stages skipped on the way out. Otherwise asking
    # "why is it blocked?" rewrites the timeline of the run being asked about.
    workflow_turn = show_commands or wants_path or wants_lookup

    if show_commands:
        wf.checks = []
        await sess.push_wf()
    elif wants_path or wants_lookup:
        wf.reset(params if wants_path else None,
                 scope="path" if wants_path else "lookup")
        wf.set("cmdb", "running",
               "looking up source and destination" if wants_path
               else "looking up device")
        await sess.push_wf()

    try:
        app_graph = await net_agent.build_agent(checkpointer=sess.checkpointer)
    except Exception as e:
        wf.set("cmdb", "failed", str(e)[:60])
        await sess.push_wf()
        await sess.send({"type": "error",
                         "message": f"Could not start the agent: {e}"})
        return
    if net_agent.LAST_FAILED_SERVERS:
        await sess.send({"type": "status", "state": "degraded",
                         "detail": "unreachable MCP: "
                         + ", ".join(net_agent.LAST_FAILED_SERVERS)})

    config = {"configurable": {"thread_id": sess.thread_id}}
    first_input = {"messages": [("user", text)], "loops": 0,
                   "max_loops": (C.DEEP_MAX_LOOPS if show_commands
                                 else C.MAX_TOOL_LOOPS)}
    # a NEW request must clear the first-one-wins capture of the previous one
    if not show_commands:
        first_input.update({"ping_ok": None, "hops": [], "path": ""})

    try:
        state = await drive(sess, app_graph, config, first_input, show_commands)
    except Exception as e:
        for key in ("cmdb", "ping", "trace", "policy", "checks"):
            if wf.state[key]["status"] == "running":
                wf.set(key, "failed", str(e)[:60])
        await sess.push_wf()
        await sess.send({"type": "error", "message": str(e)})
        return

    if workflow_turn and not show_commands:
        down = ", ".join(net_agent.LAST_FAILED_SERVERS) or ""
        why = (f"skipped - {down} MCP unavailable" if down
               else "not run - the agent concluded before this step")
        stages = (("cmdb",) if wf.scope == "lookup"
                  else ("cmdb", "ping", "trace", "policy"))
        for key in stages:
            if wf.state[key]["status"] in ("pending", "running"):
                wf.set(key, "skipped", wf.state[key]["detail"] or why)
        wf.set("done", "done",
               "lookup complete" if wf.scope == "lookup" else "report ready")

    answer = state.get("answer") or (
        state["messages"][-1].content if state.get("messages") else "")

    parsed = as_report(answer)
    if parsed and workflow_turn:
        # a question's answer belongs in the chat, not in the run's report
        if show_commands:
            wf.deep_report = parsed
        else:
            wf.report = parsed
    await sess.push_wf()

    ping_ok = state.get("ping_ok")
    unresolved = ping_ok is False or not (state.get("hops") or [])
    offer = (workflow_turn and wf.scope == "path" and unresolved
             and not show_commands
             and not wf.local)          # no device to dig into on a local run

    await sess.status("idle")
    await sess.send({"type": "final", "answer": str(answer),
                     "report": parsed, "offer_deep": offer,
                     "is_deep": show_commands})


# ---------------------------------------------------------------- websocket --
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sess = Session(ws)
    await sess.send({"type": "hello", "greeting": GREETING, "clip": CLIP,
                     "llm_mode": C.LLM_MODE, "mocks": C.USE_MOCKS,
                     "mask": ip_mask.enabled()})
    await sess.push_wf()

    async def start_turn(text, show_commands=False):
        if sess.busy:
            await sess.send({"type": "error",
                             "message": "A run is already in progress."})
            return
        sess.busy = True
        try:
            await run_turn(sess, text, show_commands)
        finally:
            sess.busy = False

    tasks = set()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            kind = msg.get("type")
            if kind == "chat":
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                await sess.send({"type": "user_echo", "text": text})
                t = asyncio.create_task(start_turn(text))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            elif kind == "deep_check":
                await sess.send({"type": "user_echo",
                                 "text": "Run deeper checks"})
                t = asyncio.create_task(start_turn(DEEP_CHECK_PROMPT,
                                                   show_commands=True))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            elif kind == "approval":
                sess.resolve_approval(str(msg.get("id") or ""),
                                      bool(msg.get("approved")))
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()


# ------------------------------------------------------------- static files --
# Mounted last so /ws and /api win. html=True serves index.html at "/".
_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "frontend", "dist")
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
else:
    @app.get("/")
    async def no_frontend():
        return JSONResponse(
            {"error": "frontend not built",
             "fix": "cd frontend && npm install && npm run build"},
            status_code=503)
