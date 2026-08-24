"""A model that concludes at Tufin must still get the alerts filled in.

The real failure this reproduces: the model ran CMDB, ping, traceroute and
Tufin, got BLOCKED, and wrote its answer -- never calling the alert tool. The
Alerts stage stayed grey and the tab was empty, with nothing to tell "this
device has no alerts" apart from "nobody asked".

The scripted model here does exactly that: no alert call anywhere.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"

from agent import graph as G                                    # noqa: E402
from scripted_model import ScriptedModel, ssh                   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"

G.llm = ScriptedModel(
    script=[
        ("get_device_details", {"device_name": SRC}),
        ("get_device_details", {"device_name": DST}),
        ssh(SRC, f"ping {DST} repeat 3"),
        ssh(SRC, f"traceroute {DST} maxttl 5 timeout 1 probe 1 numeric"),
        ("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:443"}),
        # and then it stops: BLOCKED reads like the end of the story
    ],
    thoughts=["CMDB, source.", "CMDB, destination.", "Ping.", "Traceroute.",
              "Asking Tufin."],
    final={"source": SRC, "destination": DST, "ping": "FAILED",
           "result": "NOT REACHABLE",
           "cause": "Denied by policy: DENY-ALL.",
           "evidence": ["Tufin BLOCKED"], "next_step": "Change request."})

import api.main as api                                          # noqa: E402
from starlette.testclient import TestClient                     # noqa: E402

wf, tool_names = {}, []
with TestClient(api.app) as client:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat",
                      "text": f"Troubleshoot {SRC} to {DST} on tcp:443"})
        for _ in range(400):
            msg = ws.receive_json()
            kind = msg.get("type")
            if kind == "approval_request":
                ws.send_json({"type": "approval", "id": msg.get("id"),
                              "approved": True})
            elif kind == "workflow":
                wf = msg.get("wf") or {}
            elif kind == "tool_result":
                tool_names.append(msg.get("name"))
            elif kind == "final":
                break

steps = {s["key"]: s for s in (wf.get("steps") or [])}
alerts = wf.get("alerts") or []
print("\nstages:")
for k, s in steps.items():
    print(f"  {k:8s} {str(s.get('status')):8s} {s.get('detail','')}")
print(f"\nalert rows: {len(alerts)}")

called = [n for n in tool_names
          if n == "get_alert_and_ticket_details_from_archangel"]
check("the model itself never asked for alerts (the case under test)",
      "get_firewall_path" in tool_names)
check("the server ran the lookup anyway", len(called) >= 2, str(len(called)))
check("the stage is no longer grey",
      steps.get("alerts", {}).get("status") == "done", str(steps.get("alerts")))
check("the table has rows", len(alerts) >= 2, str(len(alerts)))
check("one row per device the CMDB found",
      len({a.get("device_name") for a in alerts}) >= 2,
      str({a.get("device_name") for a in alerts}))
check("the activity feed shows they ran",
      any(b.get("kind") == "alerts" for b in (wf.get("basics") or [])))
check("the rest of the run is untouched",
      steps.get("cmdb", {}).get("status") == "done"
      and steps.get("policy", {}).get("status") in ("done", "failed")
      and bool(wf.get("report")))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
