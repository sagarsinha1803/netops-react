"""Runs: the agent executing in the background, addressable by id.

A run is not tied to a browser tab. POST /api/runs starts one and returns
immediately; the graph keeps going in an asyncio task, parks when it needs an
approval, and every change is written into the run's own state. Clients read
that state with GET /api/runs/{id}, or subscribe to the SSE stream to be told
when it changed. Closing the browser does not stop the agent.

ONE RUN AT A TIME. The agent is a single conversation with a single model, and
in clipboard mode there is exactly one clipboard: a second concurrent run would
consume the reply meant for the first. Starting one while another is active is
refused with 409.
"""
import asyncio
import re
import time
import uuid
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent import constants as C
from agent import graph as net_agent
from agent.constants import DEVICE_TOOL_NAMES, POLICY_TOOL_NAMES
from agent.llm import ip_mask
from agent.prompts import DEEP_CHECK_PROMPT
from agent.utils import display_command

from api.workflow import (Workflow, as_report, check_ok, device_label,
                          failed_line, parse_request, policy_verdict)

CLIP = C.LLM_MODE == "clipboard"
APPROVAL_TIMEOUT = 900          # seconds a run waits for a human decision


class Run:
    """One agent turn's worth of state, readable at any moment."""

    def __init__(self, kind: str, request: str, wf: Workflow):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                    # troubleshoot | lookup | question | deep
        self.request = request
        self.status = "running"             # see models.RunSummary
        self.wf = wf
        self.report = None
        self.deep_report = None
        self.answer = ""
        self.error: Optional[str] = None
        self.offer_deep = False
        self.unavailable: list[str] = []
        self.pending: Optional[dict] = None      # {id, tool, command, ...}
        self._approval: Optional[asyncio.Future] = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.version = 0                    # bumped on every change, for SSE

    # ---- change notification -------------------------------------------
    def touch(self):
        self.updated_at = time.time()
        self.version += 1
        STORE.notify(self.id)

    def set_status(self, status: str):
        self.status = status
        self.touch()

    # ---- approvals ------------------------------------------------------
    async def wait_for_approval(self, payload: dict) -> bool:
        """Park the run until a client answers, or the timeout expires."""
        loop = asyncio.get_running_loop()
        self._approval = loop.create_future()
        self.pending = {
            "id": uuid.uuid4().hex[:12],
            "tool": str(payload.get("tool") or ""),
            "command": str(payload.get("command") or ""),
            "device_ip": payload.get("device_ip"),
            "region": payload.get("region"),
        }
        self.set_status("waiting_approval")
        try:
            return bool(await asyncio.wait_for(self._approval, APPROVAL_TIMEOUT))
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending = None
            self._approval = None

    def answer_approval(self, approval_id: str, approved: bool) -> bool:
        """True if this decision was accepted (the run was waiting on it)."""
        if not self.pending or self.pending["id"] != approval_id:
            return False
        fut = self._approval
        if not fut or fut.done():
            return False
        fut.set_result(approved)
        return True


class RunStore:
    """Every run this process has executed, newest last. In memory on purpose:
    a run is a live conversation with a model, not a record to keep."""

    MAX_RUNS = 50

    def __init__(self):
        self.runs: dict[str, Run] = {}
        self.active_id: Optional[str] = None
        # one graph thread for the whole process, so a question can be answered
        # from the previous run's tool results
        self.checkpointer = MemorySaver()
        self.thread_id = uuid.uuid4().hex
        # the panel state persists between turns: deeper checks extend the run
        # that produced them rather than starting a new panel
        self.wf = Workflow()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    # ---- lookup ---------------------------------------------------------
    def get(self, run_id: str) -> Optional[Run]:
        return self.runs.get(run_id)

    def latest(self) -> Optional[Run]:
        return max(self.runs.values(), key=lambda r: r.created_at, default=None)

    def add(self, run: Run) -> Run:
        self.runs[run.id] = run
        if len(self.runs) > self.MAX_RUNS:
            for old in sorted(self.runs.values(),
                              key=lambda r: r.created_at)[: -self.MAX_RUNS]:
                self.runs.pop(old.id, None)
                self._subscribers.pop(old.id, None)
        return run

    def busy(self) -> Optional[Run]:
        """The run currently holding the agent, if any."""
        run = self.runs.get(self.active_id or "")
        return run if run and run.status not in ("done", "error") else None

    # ---- SSE fan-out ----------------------------------------------------
    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue):
        subs = self._subscribers.get(run_id) or []
        if q in subs:
            subs.remove(q)

    def notify(self, run_id: str):
        for q in list(self._subscribers.get(run_id) or []):
            q.put_nowait(1)


STORE = RunStore()


# ============================== the engine ==================================
async def execute(run: Run, prompt: str, deep: bool = False):
    """Run one agent turn to completion, updating `run` as it goes.

    The body is the drive loop from the Chainlit UI and the WebSocket version
    before it: walk the graph's new messages, mirror them into the workflow
    state, and park on every interrupt until a human decides.
    """
    wf = run.wf
    STORE.active_id = run.id
    try:
        params = parse_request(prompt)

        # a device NAME typed in the request would otherwise reach the model in
        # the first paste, before any CMDB lookup has registered it
        if net_agent._mask_names:
            for value in (params.get("source"), params.get("dest")):
                if value and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(value)):
                    ip_mask.session_mask().register(value, "host")

        workflow_turn = run.kind in ("troubleshoot", "lookup", "deep")

        if deep:
            wf.checks = []
        elif workflow_turn:
            wf.reset(params if run.kind == "troubleshoot" else None,
                     scope="path" if run.kind == "troubleshoot" else "lookup")
            wf.set("cmdb", "running",
                   "looking up source and destination" if run.kind == "troubleshoot"
                   else "looking up device")
        run.touch()

        try:
            app_graph = await net_agent.build_agent(checkpointer=STORE.checkpointer)
        except Exception as e:
            run.error = f"Could not start the agent: {e}"
            wf.set("cmdb", "failed", str(e)[:60])
            run.set_status("error")
            return
        run.unavailable = list(net_agent.LAST_FAILED_SERVERS)

        config = {"configurable": {"thread_id": STORE.thread_id}}
        first_input = {"messages": [("user", prompt)], "loops": 0,
                       "max_loops": (C.DEEP_MAX_LOOPS if deep else C.MAX_TOOL_LOOPS)}
        # a NEW request clears the first-one-wins capture and the CMDB records
        # of the previous one; a deeper-checks turn keeps them
        if not deep:
            first_input.update({"ping_ok": None, "hops": [], "path": "",
                                "devices": {}})

        state = await _drive(run, app_graph, config, first_input, deep)

        if workflow_turn and not deep:
            down = ", ".join(run.unavailable)
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
        # `answer` is what a human reads, so hand back the prose rather than the
        # {"thought":.., "final":..} envelope a model may have wrapped it in.
        run.answer = str((parsed or {}).get("text") or answer)

        if parsed and workflow_turn:
            if deep:
                wf.deep_report = parsed
                run.deep_report = parsed
                # a deeper-checks run EXTENDS the run it was launched from, so
                # it carries that run's verdict too -- otherwise a client
                # reading this run alone sees a second report with no first one
                run.report = wf.report
            else:
                wf.report = parsed
                run.report = parsed
        elif parsed:
            # a question's answer belongs to the run, not to the panel
            run.report = parsed

        ping_ok = state.get("ping_ok")
        unresolved = ping_ok is False or not (state.get("hops") or [])
        run.offer_deep = bool(workflow_turn and not deep and wf.scope == "path"
                              and unresolved and not wf.local)
        run.set_status("done")
    except asyncio.CancelledError:
        run.error = "cancelled"
        run.set_status("error")
        raise
    except Exception as e:                       # noqa: BLE001
        run.error = str(e)
        for key in ("cmdb", "ping", "trace", "policy", "checks"):
            if run.wf.state[key]["status"] == "running":
                run.wf.set(key, "failed", str(e)[:60])
        run.set_status("error")
    finally:
        if STORE.active_id == run.id:
            STORE.active_id = None


async def _drive(run: Run, app_graph, config, first_input, deep: bool):
    """Walk the graph, mirroring every step into the run's workflow state."""
    wf = run.wf
    try:
        snap = await app_graph.aget_state(config)
        seen = len((getattr(snap, "values", None) or {}).get("messages") or [])
    except Exception:
        seen = 0

    run.set_status("waiting_clipboard" if CLIP else "running")
    state = await app_graph.ainvoke(first_input, config)
    issued: dict = {}
    check_idx: dict = {}
    basic_idx: dict = {}

    while True:
        msgs = state.get("messages", [])

        for m in msgs[seen:]:
            if isinstance(m, HumanMessage):
                continue
            if isinstance(m, AIMessage) and m.tool_calls:
                thought = str(m.content or "").strip()
                for tc in m.tool_calls:
                    args = tc.get("args") or {}
                    issued[tc.get("id")] = (tc["name"], args)
                    if tc["name"] not in DEVICE_TOOL_NAMES:
                        if not deep:
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
                    kind = wf.classify(cmd, deep)
                    if kind == "deep":
                        check_idx[tc.get("id")] = wf.add_check(
                            cmd, where, args.get("region"), thought)
                    else:
                        wf.from_tool_call(tc["name"], args, cmd)
                        basic_idx[tc.get("id")] = wf.add_basic(
                            cmd, where, args.get("region"), thought, kind)
                run.touch()
            elif isinstance(m, ToolMessage):
                body = str(m.content)
                tid = getattr(m, "tool_call_id", None)
                name, args = issued.get(tid, (m.name, {}))
                if tid in check_idx or tid in basic_idx:
                    lines = [ln for ln in body.splitlines() if ln.strip()]
                    echo = display_command(name, args)
                    useful = [ln for ln in lines if echo not in ln]
                    detail = failed_line(body) or (useful or lines or [""])[0]
                    ok = check_ok(body)
                    if tid in check_idx:
                        wf.finish_check(check_idx[tid], ok, detail[:70], output=body)
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
                        wf.finish_basic(basic_idx[tid], ok, detail[:70], output=body)
                    run.touch()
        seen = len(msgs)

        # a deeper-checks turn probes other addresses; it must not rewrite the
        # Basic timeline or the Path of the run it extends
        if not deep:
            wf.from_state(state)
            run.touch()

        if "__interrupt__" not in state:
            break

        payload = dict(state["__interrupt__"][0].value or {})
        cmd = str(payload.get("command", ""))
        stage = wf.classify(cmd, deep)
        timeline = stage in ("ping", "trace")
        if timeline:
            wf.set(stage, "running", f"awaiting approval · {cmd[:50]}")

        approved = await run.wait_for_approval(payload)
        if not approved:
            if timeline:
                wf.set(stage, "skipped", "rejected by reviewer")
            run.set_status("waiting_clipboard" if CLIP else "running")
            state = await app_graph.ainvoke(Command(resume=False), config)
            continue

        if timeline:
            wf.set(stage, "running", f"running · {cmd[:50]}")
        run.set_status("running")
        state = await app_graph.ainvoke(Command(resume=True), config)
        run.set_status("waiting_clipboard" if CLIP else "running")

    return state


# ---------------------------------------------------------------- starting --
def start(kind: str, prompt: str, deep: bool = False) -> Run:
    """Create a run and launch it in the background. Caller checks busy() first."""
    run = Run(kind=kind, request=prompt, wf=STORE.wf)
    STORE.add(run)
    STORE.active_id = run.id
    task = asyncio.create_task(execute(run, prompt, deep=deep))
    # keep a reference so the task is not garbage collected mid-flight
    run._task = task                                     # type: ignore[attr-defined]
    return run


def classify_request(text: str) -> str:
    """Which of the agent's four request kinds this is -- see the system prompt."""
    params = parse_request(text)
    if re.search(r"\b(troubleshoot|reachab|ping|trace)\w*\b", text, re.I) and params:
        return "troubleshoot"
    if re.search(r"\b(device\s*detail|cmdb|look\s*up|lookup|fetch|inventory)\w*\b",
                 text, re.I):
        return "lookup"
    return "question"


DEEP_PROMPT = DEEP_CHECK_PROMPT
