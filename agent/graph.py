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
import sys
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C            # noqa: E402
from agent import prompts                   # noqa: E402
from agent.guards import check_command      # noqa: E402
from agent.state import NetState            # noqa: E402
from agent.utils import (commands_of, display_command,  # noqa: E402
                         tool_text)

from agent import entities                  # noqa: E402
from agent import tracing                   # noqa: E402
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

# TRACE=file|phoenix records every prompt and reply locally -- see agent/tracing.py.
# Attached to the model itself, so it captures whichever backend is in use and
# every call the graph makes, without threading a config through each node.
# Set as a FIELD on the model, not with_config(): that returns a
# RunnableBinding, and the later bind_tools() call reaches through it to the
# model underneath -- dropping the config, exactly as it drops bound kwargs.
_TRACERS = tracing.callbacks()
if _TRACERS:
    llm.callbacks = _TRACERS
SYSTEM_PROMPT = prompts.system_prompt(C.LLM_MODE)


# ============================== GRAPH ========================================
async def _load_tools(client):
    """Return (tools, owner, failed).

    owner maps tool name -> MCP server name. A server that cannot be reached is
    reported in `failed` rather than killing the whole run: the agent still works
    with the tools it does have and says what is missing.
    """
    tools, owner, failed = [], {}, {}
    for server in C.MCP_SERVERS:
        try:
            got = await client.get_tools(server_name=server)
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
            msg = str(e) or e.__class__.__name__
            failed[server] = msg
            print(f"[MCP] server '{server}' unavailable: {msg}", file=sys.stderr)
            continue
        for t in got:
            owner[t.name] = server
        tools.extend(got)
    return tools, owner, failed


LAST_FAILED_SERVERS: dict = {}     # set by build_agent, read by the UI


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
        out: dict = {"messages": [reply]}
        if not getattr(reply, "tool_calls", None) and reply.content:
            out["answer"] = str(reply.content)
        return out

    # ---- node: tools (validate -> approve -> execute) --------------------
    async def tools_node(state: NetState):
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", []) or [])

        # PHASE 1 -- validate + collect every approval BEFORE anything runs, so a
        # resume (which re-executes this node) can never run a command twice.
        verdict: dict = {}
        for tc in calls:
            name, args = tc["name"], tc.get("args") or {}
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
            name, args, tid = tc["name"], tc.get("args") or {}, tc["id"]
            gate = verdict.get(tid)

            if gate is not True:
                result: Any = gate
            elif name not in by_name:
                result = f"unknown tool '{name}' (have: {sorted(by_name)})"
            else:
                try:
                    result = await by_name[name].ainvoke(args)
                except Exception as e:
                    result = f"error calling {name}: {e}"

            text = tool_text(result)

            # Register the names in this result so the relay can swap them out
            # of the next paste. Done here, where results are still structured,
            # rather than by guessing at names in the rendered prompt.
            if _mask_names:
                try:
                    entities.learn(ip_mask.session_mask(), name, text)
                except Exception as e:      # never break a run over masking
                    print(f"[mask] could not learn from {name}: {e}",
                          file=sys.stderr)

            out_msgs.append(ToolMessage(content=text, name=name, tool_call_id=tid))

            if touches_device(name):
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

    # ---- router: keep looping while the model asks for tools -------------
    def route(state: NetState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            cap = state.get("max_loops") or C.MAX_TOOL_LOOPS
            if (state.get("loops") or 0) >= cap:
                return END          # cap reached -> stop instead of looping forever
            return "tools"
        return END

    g = StateGraph(NetState)
    g.add_node("agent", agent)
    g.add_node("tools", tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
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
