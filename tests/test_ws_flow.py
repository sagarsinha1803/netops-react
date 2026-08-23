"""The whole flow, end to end, with nothing real behind it.

Starts the scripted LLM, opens the app's own /ws socket, sends the same
message the browser sends, approves every command, and reads the panel that
comes back. Nothing else exercises the seams BETWEEN the pieces -- the unit
tests each mock the thing next to them, which is how a real defect (MCP
returning a list as objects run together, so only the last alert row landed)
passed all of them and still showed 1 alert where 4 came back.

    .venv\Scripts\python.exe tests	est_ws_flow.py

No credentials, no devices, no Copilot: mock MCP servers and a fake model.
Takes about a minute -- the mock servers are real subprocesses.
"""
import json
import os
import subprocess
import sys
import time

ROOT = r"S:\Claude Automation\TstVS\netops-react"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:11499/v1"
os.environ["LLM_MODEL"] = "gpt-4o"
os.environ["LLM_API_KEY"] = "EMPTY"

llm = subprocess.Popen([sys.executable, "tests/fake_llm.py", "11499"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


try:
    from starlette.testclient import TestClient
    from api.main import app

    wf, tool_names, final = {}, [], {}
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "chat",
                          "text": "Troubleshoot 10.10.1.20 to 172.20.5.10 "
                                  "on tcp:443"})
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
finally:
    llm.terminate()

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
