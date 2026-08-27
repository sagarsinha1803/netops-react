"""A run that stops must say why, and never in raw JSON.

A real deeper-checks run in clipboard mode ended like this: seven checks in the
list, an orange INCONCLUSIVE banner, and where the report belongs, this --

    {"thought":"...the next deeper check is to list the routing contexts...",
     "tool":"execute_query_on_server","args":{...}}

Three separate faults, all of them here:

  1. The budget ran out, and `wrapup` asked the model to conclude through the
     UNBOUND llm. The clipboard relay only reads a reply as JSON when tool
     schemas are bound to it, so whatever came back -- one more tool call, or
     a perfectly good final report -- was passed through verbatim.
  2. Steps that asked Tufin or the CMDB during a deeper-checks turn were
     silently dropped from the list. The operator could see seven rows, was
     spending a budget of ten, and had no way to reconcile the two.
  3. Nothing anywhere said the budget had run out. "Stopped in between" and
     "looked and found nothing" looked identical.

    .venv/Scripts/python.exe tests/test_stopped_run.py
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["REQUIRE_APPROVAL"] = "0"       # the gate is tested elsewhere

from agent import constants as C                                # noqa: E402
from agent import graph as G                                    # noqa: E402
from agent.llm.clipboard_llm import ClipboardLLM                # noqa: E402
from api.workflow import as_report, path_from_checks            # noqa: E402
from scripted_model import ScriptedModel, ssh                   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"


# ---- 1. the relay, with nothing bound -------------------------------------
# This is the exact shape that reached the panel: the model answered the
# wrap-up question with one more tool call.
relay = ClipboardLLM(mode="delta")          # no bind_tools: nothing is bound

ONE_MORE = json.dumps({
    "thought": "The source-interface probe failed, and route, forwarding and "
               "address resolution all look healthy, so the fault is past the "
               "first hop. Next I would list the routing contexts.",
    "tool": "execute_query_on_server",
    "args": {"device_ip": "203.0.113.9", "commands": ["show vrf all"],
             "region": "REGION-A"}})

msg = relay._to_message(ONE_MORE)
body = str(msg.content)
check("an unbound reply is read, not dumped as braces",
      not body.lstrip().startswith("{"), body[:70])
check("and what survives is the reasoning, which is the useful half",
      "past the first hop" in body, body[:70])
check("nothing is CALLED, because nothing was bound",
      not getattr(msg, "tool_calls", None), str(getattr(msg, "tool_calls", None)))

REPORT = json.dumps({
    "thought": "Everything points at the same interface.",
    "final": {"result": "NOT REACHABLE", "cause": "the link is down",
              "source": SRC, "destination": DST}})
parsed = as_report(str(relay._to_message(REPORT).content))
check("and a real final answer reaches the Report tab, not the raw envelope",
      bool(parsed) and parsed.get("result") == "NOT REACHABLE", str(parsed)[:80])

check("plain prose is still just the answer",
      str(relay._to_message("The link is down.").content) == "The link is down.")


# ---- 2. a policy row is shown, and is not read as a route ------------------
# Tufin answers with a JSON blob full of addresses. It belongs in the list --
# it spent a step -- but reading "via <address>" out of it would invent a hop
# no device ever reported.
TUFIN_ROW = {
    "cmd": "get_firewall_path(a -> b, tcp:22)",
    "kind": "system",
    "output": json.dumps({"result": "ALLOWED",
                          "path": ["FW-EDGE via 198.51.100.9, Ethernet1/1"]}),
}
ROUTE_ROW = {
    "cmd": f"show route {DST}",
    "kind": "device",
    "output": (f"Routing entry for {DST}/32\n"
               "  Known via bgp, distance 20\n"
               "  * via 198.51.100.77, TenGigE0/0/0/3\n"),
}

only_policy = path_from_checks([TUFIN_ROW], "EDGE-A", "EDGE-B", DST)
check("a policy reply on its own draws no path at all",
      only_policy is None, str(only_policy)[:90])

both = path_from_checks([TUFIN_ROW, ROUTE_ROW], "EDGE-A", "EDGE-B", DST)
labels = [n["label"] for n in (both or {}).get("nodes", [])]
check("with a real route beside it, the route is what is drawn",
      "198.51.100.77" in labels and "198.51.100.9" not in labels, str(labels))


# ---- 3. the budget, end to end --------------------------------------------
G.llm = ScriptedModel(
    script=[("get_device_details", {"device_name": SRC})] * 20,
    thoughts=["Looking it up again."] * 20,
    final="never reached")


async def spent_budget():
    C.MAX_TOOL_LOOPS = 2
    app = await G.build_agent()
    return await app.ainvoke(
        {"messages": [("user", f"Troubleshoot {SRC} to {DST} on tcp:443")],
         "loops": 0},
        {"configurable": {"thread_id": "stopped-1"}})


state = asyncio.run(spent_budget())
answer = str(state.get("answer") or "")
print()
print("  wrap-up answer:", answer[:150])

check("a spent budget still ends with words",
      bool(answer.strip()), answer[:60])
check("it does not pass off an unfinished run as a verdict",
      "INCONCLUSIVE" in answer.upper(), answer[:80])
check("and it names the check it would have run next",
      "next check would have been" in answer.lower()
      and "get_device_details" in answer, answer[-120:])
check("the last message carries no dangling tool call",
      not getattr(state["messages"][-1], "tool_calls", None))


# ---- 4. over the socket: every step is visible, and the stop is announced --
from starlette.testclient import TestClient                     # noqa: E402

import api.main as api                                          # noqa: E402

C.MAX_TOOL_LOOPS = 16
G.llm = ScriptedModel(
    script=[("get_device_details", {"device_name": SRC}),
            ("get_device_details", {"device_name": DST}),
            ssh(SRC, f"ping {DST} repeat 3"),
            ("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:22"})],
    thoughts=["", "", "", ""],
    final={"result": "INCONCLUSIVE", "cause": "the ping failed"})

DEEP = ScriptedModel(
    # a deeper-checks turn that re-reads policy first: one step of the budget,
    # and until now not a row anywhere
    script=[("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:22"}),
            ssh(SRC, f"show route {DST}")],
    thoughts=["Re-reading the policy verdict before I dig.",
              "Now the source's own forwarding table."],
    final={"result": "INCONCLUSIVE", "cause": "unfinished"})

wf, notices = {}, []
with TestClient(api.app) as client:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat",
                      "text": f"Troubleshoot {SRC} to {DST} on tcp:22"})
        for _ in range(300):
            m = ws.receive_json()
            if m.get("type") == "approval_request":
                ws.send_json({"type": "approval", "id": m.get("id"),
                              "approved": True})
            elif m.get("type") == "workflow":
                wf = m.get("wf") or {}
            elif m.get("type") == "final":
                break

        G.llm = DEEP                       # build_agent reads it again per turn
        C.DEEP_MAX_LOOPS = 2               # spend it on those two steps
        ws.send_json({"type": "deep_check"})
        for _ in range(300):
            m = ws.receive_json()
            if m.get("type") == "approval_request":
                ws.send_json({"type": "approval", "id": m.get("id"),
                              "approved": True})
            elif m.get("type") == "workflow":
                wf = m.get("wf") or {}
            elif m.get("type") == "status" and m.get("state") == "degraded":
                notices.append(str(m.get("detail") or ""))
            elif m.get("type") == "final":
                break

checks = wf.get("checks") or []
print()
for row in checks:
    print(f"  {str(row.get('cmd'))[:46]:48s} kind={row.get('kind')}")

check("the step that asked Tufin is in the list",
      any("firewall" in str(r.get("cmd")).lower() for r in checks),
      str([r.get("cmd") for r in checks])[:90])
check("marked as a system step, so the path parser leaves it alone",
      all(r.get("kind") == "system" for r in checks
          if "firewall" in str(r.get("cmd")).lower()),
      str([(r.get("cmd"), r.get("kind")) for r in checks])[:90])
check("the device command is in the list too, as a device step",
      any(r.get("kind") == "device" and "show route" in str(r.get("cmd"))
          for r in checks), str([r.get("cmd") for r in checks])[:90])
check("and the run says out loud that the budget ran out",
      any("budget" in n.lower() for n in notices), str(notices)[:120])

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
