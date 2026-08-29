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
from agent.constants import (ALERT_TOOL_NAMES,    # noqa: E402
                             DEVICE_TOOL_NAMES, POLICY_TOOL_NAMES)
from agent.llm import ip_mask                     # noqa: E402
from agent.prompts import DEEP_CHECK_PROMPT       # noqa: E402
from agent.salvage import looks_like_a_call      # noqa: E402
from agent.utils import describe_call, display_command           # noqa: E402

from api.workflow import (Workflow, as_report, check_ok,      # noqa: E402
                          cmdb_record, device_label, failed_line,
                          parse_alerts, parse_request,
                          echo_parts, path_from_checks, path_from_policy,
                          policy_verdict,
                          usable_output)

CLIP = C.LLM_MODE == "clipboard"

GREETING = (
    "**Network Operations troubleshooting agent**\n\n"
    "I check whether a destination is reachable from a source, show the path, "
    "and point out where it breaks. A source the CMDB does not know cannot be "
    "logged into, so those runs report the firewall verdict alone.\n\n"
    "Try: `troubleshoot 10.10.1.20 to 172.20.5.10` · "
    "`fetch device details for edge-a1`"
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
                    # Most models say nothing alongside a tool call: everything
                    # they had to say is in the arguments. Describe the step
                    # ourselves rather than leave the feed blank -- flagged, so
                    # the panel never presents our words as the model's.
                    said = bool(thought)
                    why = thought or describe_call(tc["name"], args)
                    if tc["name"] not in DEVICE_TOOL_NAMES:
                        if not show_commands:
                            cmd = display_command(tc["name"], args)
                            wf.from_tool_call(tc["name"], args, cmd)
                            policy = tc["name"] in POLICY_TOOL_NAMES
                            alerts = tc["name"] in ALERT_TOOL_NAMES
                            label = (
                                f"get_firewall_path({args.get('src', '')} → "
                                f"{args.get('dst', '')}, {args.get('service') or 'any'})"
                                if policy else
                                f"alerts({args.get('device_name') or ''})"
                                if alerts else
                                f"{tc['name']}({args.get('device_name') or ''})")
                            basic_idx[tc.get("id")] = wf.add_basic(
                                label, thought=why, said=said,
                                kind="policy" if policy else
                                     "alerts" if alerts else "cmdb")
                        else:
                            # A deeper-checks turn spends the same budget on
                            # these and showed none of them: the operator
                            # counts seven rows, is told the budget of ten ran
                            # out, and cannot reconcile the two. Show them --
                            # marked, so the path parser still ignores them.
                            check_idx[tc.get("id")] = wf.add_check(
                                display_command(tc["name"], args),
                                ("Tufin SecureTrack"
                                 if tc["name"] in POLICY_TOOL_NAMES
                                 else "Archangel"
                                 if tc["name"] in ALERT_TOOL_NAMES else "CMDB"),
                                args.get("region"), why, said=said,
                                kind="system")
                        continue
                    cmd = display_command(tc["name"], args)
                    where = (args.get("device_ip") or args.get("source")
                             or ("agent host" if tc["name"].startswith("local_")
                                 else "device"))
                    kind = wf.classify(cmd, show_commands)
                    if kind == "deep":
                        check_idx[tc.get("id")] = wf.add_check(
                            cmd, where, args.get("region"), why,
                            said=said)
                    else:
                        wf.from_tool_call(tc["name"], args, cmd)
                        basic_idx[tc.get("id")] = wf.add_basic(
                            cmd, where, args.get("region"), why, kind,
                            said=said)
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
                    parts = echo_parts(echo)
                    useful = [ln for ln in lines
                              if not any(part in ln for part in parts)]
                    detail = failed_line(body) or (useful or lines or [""])[0]
                    # a command is "ok" only if it both ANSWERED and succeeded:
                    # a refusal, a permission error or silence is not a result
                    answered = usable_output(body, echo)
                    ok = check_ok(body) and answered
                    if not detail and not answered:
                        detail = "no output"
                    # A rejected command has not answered the stage, so the
                    # next syntax the model tries is still that stage. Left
                    # claimed, the retry lands under DEEPER CHECKS -- a
                    # different question, and one nobody asked for yet.
                    if tid in basic_idx and not answered:
                        stage = wf.basics[basic_idx[tid]].get("kind")
                        if stage in ("ping", "trace"):
                            wf.release_basic(stage)
                    # A command the graph refused to run a second time is
                    # neither a success nor a failure. A green tick over one
                    # reads as the device having answered it again, which is
                    # the very thing that did not happen.
                    if body.startswith(("ALREADY RUN", "NOT RUN AGAIN")):
                        repeated = True
                        detail = "not run again - answered earlier in this run"
                    else:
                        repeated = False
                    if tid in check_idx:
                        wf.finish_check(check_idx[tid], ok, detail[:70],
                                        output=body,
                                        status="skipped" if repeated else None)
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
                        # SecureTrack modelled a device chain of its own. It is
                        # a different answer from the traceroute -- what SHOULD
                        # happen, against what did -- and worth drawing beside
                        # it rather than instead of it.
                        wf.set_path("tufin", path_from_policy(
                            body, wf.path_source(), wf.path_dest()))
                    elif wf.basics[basic_idx[tid]].get("kind") == "alerts":
                        rows, message = parse_alerts(body)
                        # a device with no open alerts is a good answer, not a
                        # failure -- only a query that could not run is
                        failed = bool(message) and "no open alerts" not in                             message.lower()
                        for row in rows:
                            if row not in wf.alerts:
                                wf.alerts.append(row)
                        wf.finish_basic(
                            basic_idx[tid], not failed,
                            f"{len(rows)} open alert(s)" if rows
                            else (message[:70] or "no open alerts"),
                            device="Archangel", output=body)
                        tickets = {r.get("ticket_id") for r in wf.alerts
                                   if r.get("ticket_id")}
                        wf.set("alerts",
                               "failed" if failed else "done",
                               f"{len(wf.alerts)} alert(s), {len(tickets)} ticket(s)"
                               if wf.alerts else
                               (message[:60] or "no open alerts"))
                    elif wf.basics[basic_idx[tid]].get("kind") == "cmdb":
                        found, label = cmdb_record(body)
                        wf.finish_basic(basic_idx[tid], found,
                                        "" if found else "not found in CMDB",
                                        device=label or None, output=body)
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
    # a NEW request must clear the first-one-wins capture of the previous one,
    # AND the CMDB records it gathered -- the graph thread is shared across
    # turns, so without this the device dict accumulates and the CMDB stage
    # counts a previous run's lookups (a found/not-found tally that mixes runs).
    # A deeper-checks turn keeps them: it reasons from the same run's records.
    if not show_commands:
        first_input.update({"ping_ok": None, "hops": [], "path": "",
                            "devices": {}})

    try:
        state = await drive(sess, app_graph, config, first_input, show_commands)
    except Exception as e:
        for key in ("cmdb", "ping", "trace", "policy", "checks"):
            if wf.state[key]["status"] == "running":
                wf.set(key, "failed", str(e)[:60])
        await sess.push_wf()
        await sess.send({"type": "error", "message": str(e)})
        return

    # A budget that ran out is the difference between "it looked and found
    # nothing" and "it was stopped before it finished". Saying so is the whole
    # of it: the answer below is what it had, not what it concluded.
    cap = int(first_input.get("max_loops") or 0)
    if cap and (state.get("loops") or 0) >= cap:
        await sess.send({"type": "status", "state": "degraded",
                         "detail": f"the tool budget for this turn ran out "
                                   f"after {cap} steps - what follows is what "
                                   f"the agent had, not a verdict"})

    if workflow_turn and not show_commands:
        # The traceroute is not optional and the model walked past it. Ask
        # once, then carry on with whichever answer is worth more.
        nudged = await nudge_traceroute(sess, wf, app_graph, config)
        if nudged is not None:
            if as_report(str(nudged.get("answer") or "")):
                state = nudged
            else:
                # the second answer is not a report, so keep the first -- but
                # the hops it just gathered are real and belong to the path
                keep = dict(nudged)
                keep["answer"] = state.get("answer")
                state = keep
        await fill_alerts(sess, wf)
        # Archangel is keyed by device NAME. With no CMDB record there is no
        # name -- so a lookup by address answers "no open alerts found for
        # <address>", which is true and useless, and a green tick over it says
        # the alerts were checked when they could not be.
        if wf.cmdb_miss and not wf.alerts:
            wf.set("alerts", "skipped",
                   "no CMDB record, so no device name to look alerts up by")
        down = ", ".join(net_agent.LAST_FAILED_SERVERS) or ""
        why = (f"skipped - {down} MCP unavailable" if down
               else "not run - the agent concluded before this step")
        stages = (("cmdb",) if wf.scope == "lookup"
                  else ("cmdb", "ping", "trace", "policy", "alerts"))
        for key in stages:
            if wf.state[key]["status"] not in ("pending", "running"):
                continue
            # A stage that was ATTEMPTED but never produced a usable result is
            # a failure, not something skipped: the model tried its ladder of
            # syntaxes and none of them answered. Grey would read as "we did not
            # get to this", which hides the very thing an operator needs to see.
            tried = [b for b in wf.basics if b.get("kind") == key]
            if tried:
                bad = sum(1 for b in tried if b.get("status") == "failed")
                wf.set(key, "failed",
                       f"{len(tried)} attempt(s), none returned a usable result"
                       if bad == len(tried) else
                       f"{len(tried)} attempt(s), no result to report")
            else:
                wf.set(key, "skipped", wf.state[key]["detail"] or why)
        # A run that ends without a verdict has not concluded, whatever it
        # printed. Ticking Conclusion over "a reachability verdict cannot be
        # determined" tells the operator the opposite of what happened.
        verdict = str((as_report(state.get("answer") or "") or {}).get("result")
                      or "").strip()
        if wf.scope == "lookup":
            wf.set("done", "done", "lookup complete")
        elif verdict:
            wf.set("done", "done", "report ready")
        else:
            wf.set("done", "failed", "no verdict reached")

    answer = state.get("answer") or (
        state["messages"][-1].content if state.get("messages") else "")

    parsed = as_report(answer)
    # A run that ended on an unreadable tool call did not conclude -- it was
    # cut off, and the panel would otherwise show the braces with no word of
    # explanation. The graph already asked once for it again; saying so is
    # what is left.
    if not parsed and looks_like_a_call(answer, net_agent.TOOLS_BY_NAME):
        await sess.send({"type": "status", "state": "degraded",
                         "detail": "the model's last reply could not be read "
                                   "as a tool call, so the run stopped there "
                                   "- that step never ran"})
    if parsed and workflow_turn:
        # a question's answer belongs in the chat, not in the run's report
        if show_commands:
            wf.deep_report = parsed
            # the deeper checks answer the question a trace full of stars
            # leaves open: where does the source think this goes next
            wf.set_path("deep", path_from_checks(
                wf.checks, wf.path_source(), wf.path_dest(), wf.dest_addr()))
        else:
            wf.report = parsed
    await sess.push_wf()

    ping_ok = state.get("ping_ok")
    unresolved = ping_ok is False or not (state.get("hops") or [])
    # Deeper checks are show commands run ON the source device. Without a CMDB
    # record there is no management address or region to reach it with, so
    # there is nothing to dig into -- offering the button would promise work
    # the agent cannot do. Same for a run whose probes came from this host.
    offer = (workflow_turn and wf.scope == "path" and unresolved
             and not show_commands
             and not wf.local
             and not wf.cmdb_miss)

    # A deeper-checks turn that ended INCONCLUSIVE has not finished the job --
    # the ladder is long and a model may stop partway down it. Offer to carry
    # on from where it got to, rather than leaving the operator with a report
    # that says "unresolved" and no way forward but starting again.
    if show_commands and wf.scope == "path" and not wf.cmdb_miss:
        verdict = str((wf.deep_report or {}).get("result") or "").upper()
        offer = verdict != "REACHABLE"

    await sess.status("idle")
    await sess.send({"type": "final", "answer": str(answer),
                     "report": parsed, "offer_deep": offer,
                     "is_deep": show_commands})


TRACE_NUDGE = (
    "You have not run the traceroute step, and it is not optional: the path "
    "is half of what was asked for, a firewall verdict does not replace it, "
    "and a failed ping is the reason to trace, not a reason to skip it. Run "
    "the bounded read-only traceroute on the SOURCE device now, in the form "
    "that platform takes, and nothing else. Then give the final report again "
    "in the required format.")


async def nudge_traceroute(sess, wf, app_graph, config):
    """Ask once for the traceroute the model skipped.

    The workflow is fixed and the prompt says so twice, but a model that has a
    firewall verdict in hand reads the story as finished and goes straight to
    the conclusion. Alerts already had a backstop; the traceroute did not, so
    the stage sat grey next to a green Conclusion, which reads as "there was
    nothing to trace" rather than "nobody looked".

    It goes back through the model rather than picking a command here: the
    syntax belongs to the platform, and choosing it in the server would put a
    guess on a production device behind the same approval card. One turn, a
    small budget, and the operator approves it like any other command.
    """
    if wf.scope != "path" or wf.local or wf.cmdb_miss:
        return None
    if wf.state["trace"]["status"] not in ("pending", ""):
        return None                        # attempted already: tried, or refused
    if wf.state["ping"]["status"] in ("pending", ""):
        return None                        # it never reached the device at all

    await sess.send({"type": "status", "state": "degraded",
                     "detail": "the traceroute step was skipped - asking the "
                               "agent for it before concluding"})
    wf.set("trace", "running", "not run by the agent - asked for by the workflow")
    await sess.push_wf()
    try:
        return await drive(sess, app_graph, config,
                           {"messages": [("user", TRACE_NUDGE)], "loops": 0,
                            "max_loops": 4}, False)
    except Exception as e:                 # noqa: BLE001 -- never lose the run
        wf.set("trace", "failed", str(e)[:60])
        await sess.push_wf()
        return None


async def fill_alerts(sess, wf):
    """Run the alert lookup the model skipped.

    The workflow is fixed -- CMDB, ping, traceroute, policy, alerts -- but the
    model decides the order it works in, and a firewall verdict of BLOCKED
    reads like the end of the story, so a weaker model concludes there and
    never asks about alerts. The operator then sees a grey Alerts stage and an
    empty tab with no way to tell "no alerts" from "never asked".

    So the server runs it: one read-only query per device the CMDB actually
    found, only when the model did not run it, keyed by the NAME the CMDB
    returned. Nothing here touches a device.
    """
    if wf.scope != "path" or wf.alerts:
        return
    if wf.state["alerts"]["status"] not in ("pending", ""):
        return
    tool = net_agent.TOOLS_BY_NAME.get(
        "get_alert_and_ticket_details_from_archangel")
    if tool is None:                       # archangel unavailable: leave it grey
        return

    # the CMDB rows carry the name the record came back under
    names = []
    for row in wf.basics:
        if row.get("kind") != "cmdb" or row.get("status") != "done":
            continue
        name = str(row.get("device") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return                             # nothing in the CMDB to key on

    wf.set("alerts", "running", f"{len(names)} device(s), run by the workflow")
    await sess.push_wf()

    failed_any = False
    for name in names:
        idx = wf.add_basic(
            f"alerts({name})", kind="alerts", said=False,
            thought=("Run by the workflow, not by the model: it concluded "
                     "without asking, and the CMDB found this device."))
        await sess.push_wf()
        try:
            body = str(await tool.ainvoke({"device_name": name}))
        except Exception as ex:            # noqa: BLE001
            body = f"Error querying Archangel: {ex}"
        await sess.send({"type": "tool_result",
                         "name": "get_alert_and_ticket_details_from_archangel",
                         "body": body[:3000]})
        rows, message = parse_alerts(body)
        failed = bool(message) and "no open alerts" not in message.lower()
        failed_any = failed_any or failed
        for row in rows:
            if row not in wf.alerts:
                wf.alerts.append(row)
        wf.finish_basic(idx, not failed,
                        f"{len(rows)} open alert(s)" if rows
                        else (message[:70] or "no open alerts"),
                        device="Archangel", output=body)
        await sess.push_wf()

    tickets = {r.get("ticket_id") for r in wf.alerts if r.get("ticket_id")}
    wf.set("alerts", "failed" if failed_any else "done",
           f"{len(wf.alerts)} alert(s), {len(tickets)} ticket(s)"
           if wf.alerts else "no open alerts")
    await sess.push_wf()


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


def _warn_if_ui_is_stale():
    """Say so when the built UI is older than the code that produced it.

    frontend/dist is a build output and is not in git, so pulling brings new UI
    source and leaves the old bundle in place. The backend then sends stages
    and tabs that the page has no idea how to draw, and the feature looks
    broken rather than unbuilt. run.ps1 rebuilds automatically; this is for
    everyone who starts uvicorn directly.
    """
    index = os.path.join(_DIST, "index.html")
    src = os.path.join(os.path.dirname(_DIST), "src")
    if not os.path.exists(index) or not os.path.isdir(src):
        return
    built = os.path.getmtime(index)
    newest = max((os.path.getmtime(os.path.join(root, f))
                  for root, _dirs, files in os.walk(src) for f in files),
                 default=0)
    if newest > built:
        print("[UI] frontend/dist is OLDER than frontend/src: the page you get "
              "may be missing tabs and stages the backend now sends. "
              "Rebuild it:  cd frontend; npm run build", file=sys.stderr)


class _Frontend(StaticFiles):
    """Static files, with index.html never cached.

    Vite fingerprints the bundles (index-<hash>.js), so those are safe to cache
    forever -- but index.html is the thing that NAMES them. Letting the browser
    cache it means a rebuilt frontend keeps loading the previous bundle, and
    the app silently runs old code against a new server.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("", ".", "/") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


if os.path.isdir(_DIST):
    _warn_if_ui_is_stale()
    app.mount("/", _Frontend(directory=_DIST, html=True), name="frontend")
else:
    @app.get("/")
    async def no_frontend():
        return JSONResponse(
            {"error": "frontend not built",
             "fix": "cd frontend && npm install && npm run build"},
            status_code=503)
