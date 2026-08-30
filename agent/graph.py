"""Agentic network troubleshooter -- LangGraph built by hand (no prebuilts).

No create_react_agent, no ToolNode, no tools_condition. The graph, the state and
the tool executor are all defined here:

    START -> agent -> (route) -> tools -> agent -> ... -> END

The LLM decides the commands: it reads the device details from the CMDB (unicorn
MCP), works out the correct read-only CLI for THAT vendor/OS/model, runs it via
the SSH MCP, asks Tufin whether policy permits the traffic, and reports the
source -> hop -> hop -> destination path.

Two guards, because the model is choosing commands that run on real devices:
  1. read-only allowlist enforced IN CODE (see guards.py; the model is never
     trusted)
  2. human approval (interrupt) before every SSH execution -- collected BEFORE
     anything executes, so a resume never re-runs a command twice

    python -m agent.graph "troubleshoot 10.10.1.20 to 172.20.5.10"
"""
import asyncio
import os
import re
import subprocess
import sys
import uuid
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C            # noqa: E402
from agent import prompts                   # noqa: E402
from agent.guards import check_command      # noqa: E402
from agent import notebook                 # noqa: E402
from agent.salvage import (looks_like_a_call,       # noqa: E402
                           salvage_tool_call)
from agent.state import NetState            # noqa: E402
from agent.utils import (commands_of, display_command,  # noqa: E402
                         tool_text)

from agent import entities                  # noqa: E402
from agent.llm import ip_mask                # noqa: E402

try:
    from agent import vendors    # optional: regex parsers that cross-check the LLM
except Exception:                # without it the graph still runs, just no parsed state
    vendors = None

# MASK_NAMES decides; see constants. Address masking has to be on either way,
# since names and addresses share the same map and the same round trip.
_mask_names = C.mask_names_enabled(C.LLM_MODE) and ip_mask.enabled()

# .env is loaded by agent.constants, which must do it before its own first
# os.environ lookup -- see the note there.


# ---- LLM ---------------------------------------------------------------------
def build_llm():
    """The model backend named by LLM_MODE. Swapping it changes nothing else."""
    if C.LLM_MODE == "clipboard":
        from agent.llm.clipboard_llm import ClipboardLLM
        # CLIP_MODE=agent -> instructions+tools live in a custom M365 Copilot agent
        # CLIP_MODE=delta -> one normal Copilot chat, only new messages pasted
        # CLIP_MODE=full  -> self-contained paste every time
        return ClipboardLLM(mode=C.CLIP_MODE)
    # Any OpenAI-compatible endpoint: vLLM, GitHub Models, Ollama, a gateway.
    # temperature=0 because this agent picks commands that run on production
    # devices -- the same question must not produce a different command.
    from langchain_openai import ChatOpenAI
    model = ChatOpenAI(base_url=C.LLM_BASE_URL, api_key=C.LLM_API_KEY,
                       model=C.LLM_MODEL, temperature=0,
                       timeout=C.LLM_TIMEOUT)
    # The relay masks its own prompt; an API model has to be wrapped, or
    # MASK_IPS would be silently ignored for every backend but the clipboard.
    if ip_mask.enabled():
        from agent.llm.masked_llm import MaskedChatModel
        return MaskedChatModel(inner=model)
    return model


llm = build_llm()
SYSTEM_PROMPT = prompts.system_prompt(C.LLM_MODE)


# ============================== GRAPH ========================================
def _why_dead(server: str, spec, error) -> str:
    """What actually went wrong, rather than what the wrapper says went wrong.

    A stdio MCP server that dies during startup surfaces as "unhandled errors
    in a TaskGroup (1 sub-exception)" -- the same sentence whether the script
    is missing, an import failed, or the interpreter is the wrong one. Four
    servers failing at once printed that sentence four times and named no
    cause, which is a dead end for anyone who is not holding the source.

    So: unwrap the exception group, and for a local subprocess RUN it and
    report what it printed on the way down. That turns the message into
    "ModuleNotFoundError: No module named 'scenarios'".
    """
    def flatten(exc, depth=0):
        subs = getattr(exc, "exceptions", None)
        if subs and depth < 4:
            out = []
            for sub in subs:
                out.extend(flatten(sub, depth + 1))
            return out
        text = str(exc).strip()
        return [f"{type(exc).__name__}: {text}" if text else type(exc).__name__]

    detail = "; ".join(dict.fromkeys(flatten(error)))

    if not isinstance(spec, dict) or spec.get("transport") != "stdio":
        return detail or "unreachable"

    args = list(spec.get("args") or [])
    script = args[0] if args else ""
    if script and not os.path.exists(script):
        return f"{os.path.basename(script)} is missing from {os.path.dirname(script)}"

    try:
        # it exits as soon as stdin closes; whatever it printed to stderr on
        # the way down is the actual fault
        done = subprocess.run([spec.get("command", sys.executable), *args],
                              input="", capture_output=True, text=True,
                              timeout=20)
        lines = [ln.strip() for ln in (done.stderr or "").splitlines() if ln.strip()]
        # the last line of a traceback is the exception; the rest is noise
        blame = next((ln for ln in reversed(lines)
                      if "Error" in ln or "error" in ln), "")
        if blame:
            return f"{os.path.basename(script)}: {blame}"
    except Exception:                        # noqa: BLE001 -- diagnosis only
        pass
    return detail or "unreachable"


# A connection that was dropped rather than an answer that was refused. An MCP
# server reached over SSE is two HTTP requests -- a long-lived stream and the
# posts that ride alongside it -- and anything between here and it (the server
# recycling a worker, a proxy's idle timeout, a keep-alive expiring) can close
# them. The next attempt opens a new one and usually just works, so treating it
# as "that server is down" throws away a run over a hiccup.
_DROPPED = ("remoteprotocolerror", "server disconnected", "connecterror",
            "connecttimeout", "readerror", "readtimeout", "closedresource",
            "peer closed", "connection reset", "broken pipe",
            "incomplete chunked read")


def _was_dropped(exc) -> bool:
    """True when the transport failed, rather than the server saying no."""
    seen, queue = set(), [exc]
    while queue:
        err = queue.pop()
        if err is None or id(err) in seen:
            continue
        seen.add(id(err))
        blob = f"{type(err).__name__} {err}".lower()
        if any(word in blob for word in _DROPPED):
            return True
        queue.extend(getattr(err, "exceptions", None) or [])
        queue.extend([getattr(err, "__cause__", None),
                      getattr(err, "__context__", None)])
    return False


async def _retrying(what, attempts: int = 3, delay: float = 0.6):
    """Run an awaitable factory, trying again when the transport drops."""
    last = None
    for attempt in range(attempts):
        try:
            return await what()
        except Exception as e:                   # noqa: BLE001 -- re-raised below
            last = e
            if not _was_dropped(e) or attempt == attempts - 1:
                raise
            print(f"[MCP] connection dropped ({type(e).__name__}); "
                  f"retrying {attempt + 1}/{attempts - 1}", file=sys.stderr)
            await asyncio.sleep(delay * (attempt + 1))
    raise last                                   # unreachable, kept explicit


async def _load_tools(client):
    """Return (tools, owner, failed).

    owner maps tool name -> MCP server name. A server that cannot be reached is
    reported in `failed` rather than killing the whole run: the agent still works
    with the tools it does have and says what is missing.
    """
    tools, owner, failed = [], {}, {}
    for server in C.MCP_SERVERS:
        try:
            got = await _retrying(lambda s=server: client.get_tools(server_name=s))
        except TypeError as e:
            # Only an adapter that does not accept server_name should land here.
            # Catching every TypeError swallowed errors raised INSIDE get_tools
            # and silently dropped the ownership map -- which makes
            # touches_device() deny by default, so CMDB lookups start asking for
            # approval and show up in the device command audit.
            if "server_name" not in str(e):
                raise
            print("[MCP] adapter cannot load per server; tool origins unknown, "
                  "every tool will be treated as device-touching",
                  file=sys.stderr)
            got = await client.get_tools()
            return got, {}, {}               # unknown origin -> deny by default
        except Exception as e:               # unreachable / crashed MCP server
            msg = ("dropped the connection three times -- it is up but not "
                   "holding a session open" if _was_dropped(e)
                   else _why_dead(server, C.MCP_SERVERS.get(server), e))
            failed[server] = msg
            print(f"[MCP] server '{server}' unavailable: {msg}", file=sys.stderr)
            continue
        for t in got:
            owner[t.name] = server
        tools.extend(got)
    return tools, owner, failed


def _call_key(name: str, args: dict) -> str:
    """One string for "this exact question, asked of this exact thing".

    Built from what the operator sees rather than from the raw arguments, so
    the same command written with different spacing is one question, and the
    same command aimed at a different device is two.
    """
    where = str((args or {}).get("device_ip") or (args or {}).get("source") or "")
    shown = " ".join(display_command(name, args or {}).lower().split())
    return f"{name.lower()}|{where.lower()}|{shown}"


def _command_keys(name: str, args: dict):
    """[(key, command)] -- one entry per COMMAND, not per call.

    Keying whole calls only caught a model that repeated itself word for word.
    A model that repeats itself while shuffling the list does not:
    ["show route X"] and then ["show route X", "show vrf"] are two different
    calls carrying one identical command, and that command ran twice. Per
    command, the second call runs only the half that is new.
    """
    cmds = [str(c).strip() for c in commands_of(args or {}) if str(c).strip()]
    if not cmds:                                # not a device tool: one question
        return [(_call_key(name, args), display_command(name, args or {}))]
    where = str((args or {}).get("device_ip") or (args or {}).get("source") or "")
    return [(f"{name.lower()}|{where.lower()}|{' '.join(c.lower().split())}", c)
            for c in cmds]


def _already_run(messages) -> dict:
    """key -> what it returned, for every command this thread has run.

    Whole thread, not just this turn: the deeper checks continue the same
    investigation, and re-running the basic ping there is exactly as useless
    as re-running it here.

    A call carrying several commands answers with one blob, so every command
    in it maps to that blob. It is the right answer to give back: the output
    of the command asked for again is somewhere inside it.
    """
    out, pending = {}, {}
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            pending[tc.get("id")] = _command_keys(tc.get("name") or "",
                                                  tc.get("args") or {})
        if isinstance(m, ToolMessage):
            keys = pending.get(getattr(m, "tool_call_id", None)) or []
            body = str(getattr(m, "content", "") or "")
            # a rejection or an error is not an answer: asking again with the
            # same words is pointless, but it is not the model repeating work
            if body and not body.startswith(("REJECTED:", "ALREADY RUN")):
                for key, _cmd in keys:
                    out.setdefault(key, body)
    return out


def _ran_note(messages) -> str:
    """The list of what has run, for a model that has just asked twice.

    Sent only when it repeats itself, which is the moment the words are worth
    it -- and it rides inside a tool result, so it costs no extra message and
    cannot shift the relay's idea of what it has already pasted.
    """
    ran = _ran_so_far(messages)
    if len(ran) < 2:
        return ""
    return ("Everything run in this investigation so far, none of which needs "
            "running again: " + "; ".join(ran) + "\n\n")


def _ran_so_far(messages, limit: int = 20):
    """The commands this investigation has run, in order, for the model.

    A model repeats itself when it cannot see its own progress -- and through
    the bridge there is no recap, only a conversation long enough to lose the
    beginning of. This is one short line per turn, and it is the difference
    between "I do not remember checking the routing table" and not asking
    again.
    """
    seen, pending = [], {}
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            pending[tc.get("id")] = _command_keys(tc.get("name") or "",
                                                  tc.get("args") or {})
        if isinstance(m, ToolMessage):
            body = str(getattr(m, "content", "") or "")
            if body.startswith(("REJECTED:", "ALREADY RUN")):
                continue
            for _key, cmd in pending.get(getattr(m, "tool_call_id", None)) or []:
                if cmd not in seen:
                    seen.append(cmd)
    return seen[-limit:]


notes = notebook.shared()          # what has worked on this estate before

# A command "answered" for the notebook's purposes when the device came back
# with something other than a refusal. Deliberately not the same test the
# panel uses for a green tick: a ping that answers "0 received" is a FAILED
# probe and a perfectly good command, and the notebook is about syntax.
_REFUSED = re.compile(
    r"%\s*(invalid|incomplete|ambiguous|permission|error)"
    r"|syntax error|unknown command|unrecognized command"
    r"|command not found|not supported|invalid input", re.I)


def _answered(text: str, command: str) -> bool:
    body = str(text or "")
    if _REFUSED.search(body):
        return False
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    echo = " ".join(str(command or "").split()).lower()
    lines = [ln for ln in lines if echo not in " ".join(ln.split()).lower()]
    return bool(lines)


LAST_FAILED_SERVERS: dict = {}     # set by build_agent, read by the UI

# name -> tool, for the few steps the API layer runs ITSELF when the model
# skipped one. The workflow is fixed (CMDB, ping, traceroute, policy, alerts):
# a model that concludes early should not silently drop a step the operator
# asked for.
TOOLS_BY_NAME: dict = {}


async def build_agent(checkpointer=None):
    client = MultiServerMCPClient(C.MCP_SERVERS)
    tools, owner, failed = await _load_tools(client)
    LAST_FAILED_SERVERS.clear()
    LAST_FAILED_SERVERS.update(failed)
    if not tools:
        raise RuntimeError(
            "no MCP tools available - " +
            "; ".join(f"{s}: {e}" for s, e in failed.items()))
    by_name = {t.name: t for t in tools}
    TOOLS_BY_NAME.clear()
    TOOLS_BY_NAME.update(by_name)
    llm_with_tools = llm.bind_tools(tools)

    unavailable = ("\n\nNOTE: these MCP servers are unreachable right now, so "
                   "their tools cannot be used: "
                   + ", ".join(failed) +
                   ". Work with the tools you do have and say clearly in the "
                   "final answer which checks could not be run."
                   ) if failed else ""

    def touches_device(tool_name: str) -> bool:
        """Deny by default: unknown origin is treated as device-touching."""
        return (owner.get(tool_name, "?") in C.DEVICE_SERVERS
                or tool_name not in owner)

    # ---- node: agent (think + decide) -----------------------------------
    async def agent(state: NetState):
        msgs = state["messages"]
        if not any(getattr(m, "type", "") == "system" for m in msgs):
            msgs = [("system", SYSTEM_PROMPT + unavailable)] + list(msgs)

        reply = await llm_with_tools.ainvoke(msgs)

        # A model that writes its tool call out as prose instead of calling
        # ends the run on step one: no tool calls means "this is the answer",
        # so every stage stays grey and the Conclusion goes green over
        # nothing. Read the call out of the text instead. It still passes
        # through the allowlist and the human gate like any other.
        if not getattr(reply, "tool_calls", None):
            rescued = salvage_tool_call(str(reply.content or ""), by_name)
            if rescued:
                name, args = rescued
                print(f"[LLM] tool call written as text, not called: {name} "
                      f"-- salvaged", file=sys.stderr)
                reply = AIMessage(
                    content=str(reply.content or ""),
                    tool_calls=[{"name": name, "args": args,
                                 "id": "salvaged_" + uuid.uuid4().hex[:12]}])
            elif looks_like_a_call(str(reply.content or ""), by_name):
                # It meant to run something and the text will not parse as
                # anything runnable. Accepting that as the final answer stops
                # the run mid-ladder and prints braces where the report goes,
                # which is the worst of both. Ask once, for the same step,
                # written so it can be read.
                print("[LLM] a reply that meant to run something ran nothing "
                      "-- asking once more for the same step", file=sys.stderr)
                again = await llm_with_tools.ainvoke(
                    list(msgs) + [AIMessage(content=str(reply.content or "")),
                                  ("user",
                                   "You described a step but did not take it: "
                                   "no tool was called, nothing ran, and the "
                                   "investigation is exactly where it was. "
                                   "Saying you will call something is not "
                                   "calling it. Make that SAME call now -- as a "
                                   "real tool call if you can, otherwise as ONE "
                                   "JSON object and nothing else, with no prose "
                                   "around it and every brace closed.")])
                if getattr(again, "tool_calls", None):
                    reply = again
                else:
                    retry = salvage_tool_call(str(again.content or ""), by_name)
                    if retry:
                        name, args = retry
                        reply = AIMessage(
                            content=str(again.content or ""),
                            tool_calls=[{"name": name, "args": args,
                                         "id": "repaired_" + uuid.uuid4().hex[:12]}])

        out: dict = {"messages": [reply]}
        if not getattr(reply, "tool_calls", None) and reply.content:
            out["answer"] = str(reply.content)
        return out

    # ---- node: tools (validate -> approve -> execute) --------------------
    async def tools_node(state: NetState):
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", []) or [])

        # What this investigation has already asked, and what it got back. A
        # model that cannot see progress asks the same question again: a real
        # run spent four of its steps re-running two commands it had already
        # run, and the operator watched the same lines scroll past twice.
        # Running it a second time cannot answer anything the first did not.
        earlier = _already_run(state["messages"][:-1])

        # PHASE 1 -- validate + collect every approval BEFORE anything runs, so a
        # resume (which re-executes this node) can never run a command twice.
        verdict: dict = {}
        trimmed: dict = {}                 # call id -> the args actually run
        for tc in calls:
            name, args = tc["name"], tc.get("args") or {}

            # Which of the commands in THIS call are new. A call that carries
            # three, two of which have already run, runs the one that has not
            # -- shuffling the list is not a new question.
            pairs = _command_keys(name, args)
            done = [(key, cmd) for key, cmd in pairs if key in earlier]
            fresh = [cmd for key, cmd in pairs if key not in earlier]
            if done and not fresh:
                # no device is touched, so no approval is asked for either
                verdict[tc["id"]] = ("REPEAT", earlier[done[0][0]])
                continue
            if done and commands_of(args):
                args = dict(args, commands=fresh)
                trimmed[tc["id"]] = (args, [cmd for _key, cmd in done])
                print(f"[LLM] dropping {len(done)} command(s) already run; "
                      f"running {fresh}", file=sys.stderr)
            if not touches_device(name):
                verdict[tc["id"]] = True
                continue
            bad = next((e for e in (check_command(c) for c in commands_of(args))
                        if e), None)
            if bad:
                verdict[tc["id"]] = f"REJECTED: {bad}"
                continue
            if C.REQUIRE_APPROVAL:
                approved = interrupt({
                    "action": "device_command",
                    "tool": name,
                    "device_ip": args.get("device_ip") or args.get("source")
                    or ("agent host" if name.startswith("local_") else None),
                    "region": args.get("region"),
                    "command": display_command(name, args),
                })
                verdict[tc["id"]] = True if approved else \
                    "REJECTED: the reviewer declined this command."
            else:
                verdict[tc["id"]] = True

        # PHASE 2 -- execute, capture structured state
        out_msgs, audit = [], list(state.get("commands_run") or [])
        devices = dict(state.get("devices") or {})
        upd: dict = {}

        for tc in calls:
            name, tid = tc["name"], tc["id"]
            # what PHASE 1 approved, which is the call minus anything already
            # run -- never the model's original list, or a command the
            # operator was never shown would run
            args = (trimmed[tid][0] if tid in trimmed else tc.get("args") or {})
            gate = verdict.get(tid)

            if isinstance(gate, tuple) and gate[0] == "REPEAT":
                print(f"[LLM] asked again for a command already run: "
                      f"{display_command(name, args)}", file=sys.stderr)
                result: Any = (
                    "ALREADY RUN in this investigation. This is the result "
                    "from the first time, unchanged -- running it again "
                    "cannot answer anything it did not answer then. Take it "
                    "as read and choose a DIFFERENT check, or conclude with "
                    "what you have.\n\n"
                    + _ran_note(state["messages"]) + str(gate[1]))
            elif gate is not True:
                result = gate
            elif name not in by_name:
                result = f"unknown tool '{name}' (have: {sorted(by_name)})"
            else:
                try:
                    # a dropped connection is not an answer: open another one
                    result = await _retrying(
                        lambda n=name, a=args: by_name[n].ainvoke(a))
                except Exception as e:
                    result = (
                        f"the {owner.get(name, 'MCP')} server dropped the "
                        f"connection three times, so {name} did not run. It is "
                        f"reachable but not holding a session open; this says "
                        f"nothing about the device."
                        if _was_dropped(e) else f"error calling {name}: {e}")

            # `text` is what the tool actually said and is what everything
            # PARSES: the CMDB record must still be the JSON object it was, or
            # cmdb_record() reads a perfectly good lookup as "not found".
            # `shown` is that plus anything the model needs told alongside it.
            text = tool_text(result)
            shown = text
            if tid in trimmed and gate is True:
                # say which half of the call was dropped, or the model reads
                # the shorter output as the device having answered less
                shown = ("NOT RUN AGAIN, already run earlier in this "
                         "investigation: " + "; ".join(trimmed[tid][1]) +
                         ". Their output stands. What follows is only the "
                         "part of this call that was new.\n\n"
                         + _ran_note(state["messages"]) + shown)

            # ---- the notebook ------------------------------------------
            # What the box says it is beats what the CMDB says it is, so the
            # platform is re-read from every result that could name it. Then:
            # write down whether this shape answered on this platform, and --
            # the moment the platform becomes known -- hand back what has
            # worked on it before. That rides on the tool result rather than a
            # message of its own, so it costs no extra turn and cannot shift
            # the relay's count of what it has already pasted.
            if gate is True and not isinstance(gate, tuple):
                seen_platform = notebook.platform_of(
                    record=text if not touches_device(name) else "",
                    version_output=text if touches_device(name) else "")
                if seen_platform:
                    known = upd.get("platform") or state.get("platform") or ""
                    # a version reply outranks a CMDB record: it is the box
                    # itself talking, and the record may be years out of date
                    if touches_device(name) or not known:
                        upd["platform"] = seen_platform
                platform = upd.get("platform") or state.get("platform") or ""

                if touches_device(name) and platform:
                    for command in commands_of(args):
                        notes.record(platform, command,
                                     worked=_answered(text, command))

                # Only ever onto DEVICE output. A CMDB or Tufin reply is a JSON
                # object that the panel parses, and a note appended to one
                # turns a perfectly good record into "not found in CMDB".
                hints = notes.hints(platform) if touches_device(name) else ""
                if hints:
                    shown = shown + "\n\n" + hints

            # Register the names in this result so the relay can swap them out
            # of the next paste. Done here, where results are still structured,
            # rather than by guessing at names in the rendered prompt.
            if _mask_names:
                try:
                    entities.learn(ip_mask.session_mask(), name, text)
                except Exception as e:      # never break a run over masking
                    print(f"[mask] could not learn from {name}: {e}",
                          file=sys.stderr)

            out_msgs.append(ToolMessage(content=shown, name=name,
                                        tool_call_id=tid))

            if isinstance(gate, tuple):
                pass                            # a repeat ran nothing to audit
            elif touches_device(name):
                audit.append({"device_ip": args.get("device_ip") or args.get("source")
                              or ("agent host" if name.startswith("local_") else None),
                              "command": display_command(name, args),
                              "approved": gate is True})
            elif "device_details" in name or "get_device" in name:
                # CMDB lookups only. Filing EVERY non-device tool here put
                # Tufin's reply in the device map under "?" -- and since a
                # policy reply is a JSON object, the panel counted it as a
                # device that had been found, so a run where nothing was in
                # the CMDB reported "1 found" and kept its green tick.
                key = args.get("device_name") or args.get("device_ip") or "?"
                devices[key] = text

            # Structured capture from the raw CLI output (independent of the
            # LLM). FIRST ONE WINS: the escalation checks ping the next hop and
            # traceroute a remote PE loopback, and those answer a different
            # question -- letting them through would report a dead destination
            # as reachable and replace the path with the underlay. The basic
            # result is captured once and then left alone; the UI resets these
            # fields when a new request starts.
            if vendors is None or gate is not True:
                continue
            blob = display_command(name, args).lower()
            first_ping = state.get("ping_ok") is None and "ping_ok" not in upd
            first_trace = not (state.get("hops") or upd.get("hops"))
            if first_ping and "ping" in blob:
                upd["ping_ok"] = vendors.ping_ok(text)
            if first_trace and ("trace" in blob or "tracert" in blob):
                hops = vendors.parse_hops(text)
                if hops:
                    upd["hops"] = hops
                    upd["path"] = vendors.path_line(
                        str(args.get("device_ip") or args.get("source") or "source"),
                        hops,
                        str(args.get("dest") or args.get("destination") or "destination"),
                        # the ping may have been captured in this same batch, so
                        # prefer the pending value over the pre-node state
                        reached=bool(upd.get("ping_ok", state.get("ping_ok"))))

        return {"messages": out_msgs, "loops": (state.get("loops") or 0) + 1,
                "commands_run": audit, "devices": devices, **upd}

    # ---- node: wrapup (answer with what we have) -------------------------
    async def wrapup(state: NetState):
        """The budget ran out mid-investigation: get an answer anyway.

        Ending here used to mean ending on an AI message that is nothing but
        tool calls -- no content, so no report, so the panel showed a run that
        simply stopped. An engineer cannot tell that from a crash.

        So ask once more, saying the budget is gone. Anything it asks for
        anyway is stripped here and reported as the check it WOULD have run
        next, which is the one thing an operator can act on.
        """
        msgs = list(state["messages"])
        if not any(getattr(m, "type", "") == "system" for m in msgs):
            msgs = [("system", SYSTEM_PROMPT + unavailable)] + msgs
        msgs = msgs + [("user",
                        "Stop investigating: the tool budget for this run is "
                        "spent. Answer NOW from what you already have, in the "
                        "required final format. Do not ask for another tool "
                        "call. Any step you could not finish is INCONCLUSIVE "
                        "-- say which, say what you would run next, and do not "
                        "present a guess as a result.")]
        # llm_with_tools, NOT the bare llm. The clipboard relay only reads a
        # reply as JSON when tool schemas are bound to it, so answering here
        # through the unbound model dropped whatever came back into the panel
        # verbatim -- a wall of braces where the report belongs, whether the
        # model sent one more tool call or a perfectly good final answer.
        # Binding is not a licence to call: anything asked for is stripped.
        reply = await llm_with_tools.ainvoke(msgs)
        text = str(getattr(reply, "content", "") or "").strip()
        asked = list(getattr(reply, "tool_calls", None) or [])
        if asked:
            nxt = "; ".join(display_command(c["name"], c.get("args") or {})
                            for c in asked)
            text = (text + "\n\n" if text else "") + (
                "INCONCLUSIVE: the tool budget for this run is spent, so this "
                "is not a verdict. The next check would have been: " + nxt)
        if not text:
            text = ("The tool budget for this run was spent before a "
                    "conclusion was reached. Nothing here is a verdict: "
                    "re-run with a narrower question, or run the remaining "
                    "checks by hand.")
        # a plain message, not the model's own: carrying its tool_calls through
        # would leave a dangling call as the last message, and everything
        # downstream reads that as "still working"
        return {"messages": [AIMessage(content=text)], "answer": text}

    # ---- router: keep looping while the model asks for tools -------------
    def route(state: NetState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            cap = state.get("max_loops") or C.MAX_TOOL_LOOPS
            if (state.get("loops") or 0) >= cap:
                return "wrapup"     # cap reached -> answer, do not just stop
            return "tools"
        return END

    g = StateGraph(NetState)
    g.add_node("agent", agent)
    g.add_node("tools", tools_node)
    g.add_node("wrapup", wrapup)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route,
                            {"tools": "tools", "wrapup": "wrapup", END: END})
    g.add_edge("tools", "agent")
    g.add_edge("wrapup", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


# ============================== CLI ==========================================
async def main():
    question = " ".join(sys.argv[1:]) or "troubleshoot 10.10.1.20 to 172.20.5.10"
    app = await build_agent()
    config = {"configurable": {"thread_id": "cli-1"}}

    state = await app.ainvoke({"messages": [("user", question)], "loops": 0}, config)

    while "__interrupt__" in state:
        p = state["__interrupt__"][0].value
        print("\n" + "!" * 60)
        print("DEVICE COMMAND NEEDS HUMAN APPROVAL")
        print(f"  tool      : {p.get('tool')}")
        print(f"  device_ip : {p.get('device_ip')}")
        print(f"  region    : {p.get('region')}")
        print(f"  command   : {p.get('command')}")
        print("!" * 60)
        ans = await asyncio.to_thread(input, "Run it? [y/N]: ")
        state = await app.ainvoke(
            Command(resume=ans.strip().lower() in ("y", "yes")), config)

    print("\n" + "=" * 60)
    print(state.get("answer") or state["messages"][-1].content)
    if state.get("path"):
        print(f"\nparsed path : {state['path']}")
    if state.get("commands_run"):
        print("commands    :", *[f"\n  {c}" for c in state["commands_run"]])


if __name__ == "__main__":
    asyncio.run(main())
