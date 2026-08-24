"""The whole flow, end to end, over the app's own WebSocket.

Sends what the browser sends, approves every command and reads the panel that
comes back. Nothing else exercises the seams BETWEEN the pieces -- the unit
tests each mock the thing next to them, which is how a real defect (MCP
returning a list as objects run together, so only the last alert row landed)
passed all of them and still showed 1 alert where 4 came back.

    .venv\\Scripts\\python.exe tests\\test_ws_flow.py

Mock MCP servers and a scripted stand-in for the model, so it needs no
credentials, no bridge and no Copilot.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"

from agent import graph as G                                    # noqa: E402
from scripted_model import ScriptedModel, alerts_for, ssh       # noqa: E402

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
        alerts_for(0),
        alerts_for(1),
        ssh(SRC, f"show route {DST}"),
    ],
    thoughts=[
        "Looking up the source device in the CMDB.",
        "Now the destination, so I know its platform and region.",
        "Source is Cisco IOS-XE, so: ping <dest> repeat 3.",
        "Ping failed. Running a bounded traceroute to find where it dies.",
        "Trace stops at the edge firewall. Asking Tufin about tcp:443.",
        "Checking Archangel for open alerts on the source.",
        "And on the destination.",
        "Confirming the route exists, to rule out a routing fault.",
    ],
    final={"source": "APP-SRV-DC1-020 / 10.10.1.20 (cisco IOS-XE)",
           "destination": "PAY-API-DC2-010 / 172.20.5.10 (cisco NX-OS)",
           "ping": "FAILED", "result": "NOT REACHABLE",
           "evidence": ["ping 0/3", "traceroute stops at FW-DC1-EDGE-01",
                        "Tufin BLOCKED by DENY-ALL"],
           "cause": "Denied by policy: ACL DENY-ALL drops tcp:443.",
           "next_step": "Raise a Tufin change request."})

import api.main as api                                          # noqa: E402
from starlette.testclient import TestClient                     # noqa: E402

wf, tool_names, final = {}, [], {}
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
            elif kind == "error":
                print("  server error:", str(msg.get("message"))[:200])
            elif kind == "final":
                final = msg
                break

steps = {s["key"]: s for s in (wf.get("steps") or [])}
print("\nstages:")
for k, s in steps.items():
    print(f"  {k:8s} {str(s.get('status')):8s} {s.get('detail','')}")
alerts = wf.get("alerts") or []
print(f"\nalert rows: {len(alerts)}")
for a in alerts[:6]:
    print("  ", {k: a.get(k) for k in ("device_name", "check_name",
                                       "alert_type", "ticket_id")})

check("the alert tool was actually called",
      "get_alert_and_ticket_details_from_archangel" in tool_names,
      str(sorted(set(tool_names))))
check("the alerts stage finished",
      steps.get("alerts", {}).get("status") == "done", str(steps.get("alerts")))
check("alert rows reached the panel for the table", len(alerts) >= 2,
      str(len(alerts)))
check("both devices are represented",
      len({a.get("device_name") for a in alerts}) >= 2,
      str({a.get("device_name") for a in alerts}))
check("each row carries a ticket to open",
      bool(alerts) and all(a.get("ticket_id") for a in alerts))
check("the earlier stages still reached a verdict",
      steps.get("cmdb", {}).get("status") == "done"
      and steps.get("policy", {}).get("status") in ("done", "failed"),
      f"cmdb={steps.get('cmdb',{}).get('status')} "
      f"policy={steps.get('policy',{}).get('status')}")
check("a verdict was still produced", bool(final.get("answer")),
      str(final.get("answer"))[:60])
check("the report tab has something to show", bool(wf.get("report")))
check("the path was parsed for the Path tab",
      bool((wf.get("path") or {}).get("nodes")),
      str((wf.get("path") or {}).get("line"))[:60])
check("the commands that ran are listed for the activity feed",
      len(wf.get("basics") or []) >= 3, str(len(wf.get("basics") or [])))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
