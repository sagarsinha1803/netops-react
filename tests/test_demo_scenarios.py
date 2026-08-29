"""The two demo scenarios, end to end, with no model server anywhere.

A demo that breaks in front of an audience is worse than no demo, and the part
that breaks is never the part you rehearsed. This drives both scenarios through
the real WebSocket -- once by device NAME, once by IP, and the broken one
through its deeper checks as well -- and checks the panel says what DEMO.md
promises it will say.

    .venv\\Scripts\\python.exe tests\\test_demo_scenarios.py

The model is a scripted stand-in dropped into the graph (tests/scripted_model.py),
so this needs no bridge, no Copilot and no credentials. The real demo runs on
the VS Code bridge or the clipboard relay; what this proves is that everything
BENEATH the model is sound, which is the part that can silently rot.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tests", "mocks"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"

from agent import graph as G                                    # noqa: E402
import scenarios as S                                           # noqa: E402
from scripted_model import ScriptedModel, alerts_for, ssh       # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def workflow_script(source, dest, service):
    """What a competent model does with this workflow, in order."""
    return [
        ("get_device_details", {"device_name": source}),
        ("get_device_details", {"device_name": dest}),
        ssh(source, f"ping {dest} repeat 3"),
        ssh(source, f"traceroute {dest} maxttl 5 timeout 1 probe 1 numeric"),
        ("get_firewall_path", {"src": S.ip_of(source), "dst": S.ip_of(dest),
                               "service": service}),
        alerts_for(0),
        alerts_for(1),
    ], [
        "Looking up the source in the CMDB.",
        "Now the destination, so I know its platform and region.",
        "Pinging the destination from the source.",
        "Tracing the path to see where it goes.",
        "Asking Tufin whether policy permits this end to end.",
        "Checking Archangel for open alerts on the source.",
        "And on the destination.",
    ]


DEEP_SCRIPT = [
    "show route {dest}",
    "show vrf all",
    "show cef {dest}",
    "show arp | include {hop}",
    "show interface TenGigE0/0/0/3",
    "show ip bgp summary",
    "show logging | include TenGig",
]

DEEP_THOUGHTS = [
    "Is there a route at all? No route would settle it here.",
    "Route exists. Is the destination in a VRF?",
    "Global table. Is the prefix actually programmed for forwarding?",
    "Adjacency is incomplete. Is the next hop resolving in ARP?",
    "ARP never resolved. Checking the interface it would leave by.",
    "The interface is down. Did the peer go with it?",
    "Confirming the order of events in the log.",
]


def run(source, dest, service="tcp:443", deep=False, deep_hop="10.20.30.129",
        final=None, max_loops=None):
    """One full run, returning the panel the browser would have rendered."""
    from starlette.testclient import TestClient

    script, thoughts = workflow_script(source, dest, service)
    model = ScriptedModel(script=script, thoughts=thoughts,
                          final=final or {"result": "see evidence"})
    G.llm = model                       # build_agent reads this at call time

    import api.main as api               # imported AFTER the model is in place
    wf = {}

    def pump(ws):
        nonlocal wf
        for _ in range(400):
            msg = ws.receive_json()
            kind = msg.get("type")
            if kind == "approval_request":
                ws.send_json({"type": "approval", "id": msg.get("id"),
                              "approved": True})
            elif kind == "workflow":
                wf = msg.get("wf") or {}
            elif kind == "final":
                return

    with TestClient(api.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "chat",
                          "text": f"Troubleshoot {source} to {dest} on {service}"})
            pump(ws)
            if deep:
                model.script = [ssh(source, c.format(dest=dest, hop=deep_hop))
                                for c in DEEP_SCRIPT]
                model.thoughts = DEEP_THOUGHTS
                ws.send_json({"type": "deep_check"})
                pump(ws)
    return wf


def show(title, wf):
    steps = {s["key"]: s for s in (wf.get("steps") or [])}
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)))
    for k, s in steps.items():
        print(f"  {k:8s} {str(s.get('status')):8s} {s.get('detail','')}")
    print(f"  path: {(wf.get('path') or {}).get('line', '')[:76]}")
    for a in (wf.get("alerts") or []):
        print(f"  alert: {a.get('device_name')}  {a.get('alert_title')}  "
              f"{a.get('check_name')}  ticket {a.get('ticket_id')}")
    return steps


# ---- the invented world is self-consistent -------------------------------
check("every device resolves by name and by IP",
      all(S.resolve(name) == name and S.resolve(d["ip"]) == name
          for name, d in S.DEVICES.items()))
check("the good destination answers a ping",
      S.DEVICES["DC2-WEB-LB-01"]["ip"] not in S.PING_FAILS)
check("the broken destination does not",
      S.DEVICES["DC3-DB-CLU-02"]["ip"] in S.PING_FAILS)
check("the broken path is PERMITTED by policy, so the demo cannot stop at "
      "the firewall",
      S.DEVICES["DC3-DB-CLU-02"]["ip"] not in S.BLOCKED_DESTS)
alerted = {a["check_name"] for a in S.ALERTS["DC1-APP-SW-07"]}
check("the alert names the same interface the deep checks find down",
      any("TenGigE0/0/0/3" in c for c in alerted), str(alerted))

# ---- scenario 1: healthy, entered by NAME --------------------------------
wf = run("DC1-EDGE-RTR-01", "DC2-WEB-LB-01", "tcp:443",
         final={"source": "DC1-EDGE-RTR-01", "destination": "DC2-WEB-LB-01",
                "ping": "SUCCESS", "result": "REACHABLE",
                "cause": "Nothing is wrong: the path is up and policy permits "
                         "tcp:443.",
                "evidence": ["ping 3/3", "traceroute arrives",
                             "Tufin ALLOWED by PERMIT-WEB-TIER",
                             "one open alert on the destination, PSU 2 -- "
                             "unrelated to this path"],
                "next_step": "None. Raise PSU 2 with the site team separately."})
steps = show("SCENARIO 1  healthy, by name", wf)
check("1: the CMDB found both devices",
      steps.get("cmdb", {}).get("status") == "done", str(steps.get("cmdb")))
check("1: the ping succeeded",
      steps.get("ping", {}).get("status") == "done", str(steps.get("ping")))
check("1: policy permits it",
      steps.get("policy", {}).get("status") == "done", str(steps.get("policy")))
check("1: the path reaches the destination",
      "X" not in (wf.get("path") or {}).get("line", ""),
      (wf.get("path") or {}).get("line", "")[:60])
check("1: the alerts step ran",
      steps.get("alerts", {}).get("status") == "done", str(steps.get("alerts")))
check("1: the only alert is the unrelated PSU, not a network fault",
      [a.get("alert_title") for a in (wf.get("alerts") or [])]
      == ["PowerSupplyRedundancyLost"],
      str([a.get("alert_title") for a in (wf.get("alerts") or [])]))

# ---- scenario 2: broken, entered by IP, then the deeper checks -----------
wf = run("10.20.30.7", "10.60.40.12", "tcp:3306", deep=True,
         final={"source": "DC1-APP-SW-07", "destination": "DC3-DB-CLU-02",
                "ping": "FAILED", "result": "NOT REACHABLE",
                "cause": "The trace dies after the first hop and Tufin permits "
                         "the traffic, so policy does not explain it.",
                "evidence": ["ping 0/3", "traceroute stops at DC1-CORE-SW-01",
                             "Tufin ALLOWED", "3 open alerts on the source"],
                "next_step": "Run the deeper checks on DC1-APP-SW-07."})
steps = show("SCENARIO 2  broken, by IP", wf)
check("2: the CMDB found both devices",
      steps.get("cmdb", {}).get("status") == "done", str(steps.get("cmdb")))
# amber, not red: the probe RAN and came back with no replies. Red on this
# stage means it could not be run at all, which is a different demo.
check("2: the ping ran and found nothing reachable",
      steps.get("ping", {}).get("status") == "warn", str(steps.get("ping")))
check("2: the path stops short of the destination",
      "X" in (wf.get("path") or {}).get("line", ""),
      (wf.get("path") or {}).get("line", "")[:60])
check("2: policy does NOT explain it -- the interesting part",
      steps.get("policy", {}).get("status") == "done", str(steps.get("policy")))
check("2: the alerts step found the open tickets",
      len(wf.get("alerts") or []) == 3, str(len(wf.get("alerts") or [])))
check("2: and one names the interface the deep checks will find down",
      any("TenGigE0/0/0/3" in str(a.get("check_name"))
          for a in (wf.get("alerts") or [])),
      str([a.get("check_name") for a in (wf.get("alerts") or [])]))

# ---- the escalation, which is what the demo is really about --------------
checks = wf.get("checks") or []
print(f"\n  deeper checks run: {len(checks)}")
for c in checks:
    print(f"     {str(c.get('cmd'))[:46]:48s} {str(c.get('detail'))[:46]}")
seen = " ".join(str(c.get("cmd", "")) + " " + str(c.get("output", ""))
                for c in checks).lower()
check("2: the deeper checks ran", len(checks) >= 5, str(len(checks)))
check("2: a route exists, so a missing route is ruled out",
      "routing entry" in seen)
check("2: the forwarding adjacency is incomplete -- the real fault",
      "incomplete" in seen)
check("2: the interface it would leave by is down",
      "line protocol is down" in seen)
check("2: the BGP peer went down with it, at the same time",
      "idle" in seen and "02:14:33" in seen)
check("2: the log confirms the order of events",
      "link-3-updown" in seen)
check("2: every deep command was answered by the SOURCE, not another device",
      "10.40.20.0/24" not in seen,
      "a healthy device's routing table leaked into the broken scenario")
check("2: the deep report reached the panel", bool(wf.get("deepReport")))

# ---- three instruments, three drawings of the same route -----------------
paths = wf.get("paths") or {}
print("\n  paths drawn:")
for kind in ("traceroute", "tufin", "deep"):
    p = paths.get(kind)
    print(f"     {kind:11s} {(p or {}).get('line', '(none)')[:70]}")
    if p and p.get("note"):
        print(f"     {'':11s} {p['note'][:88]}")

check("2: the traceroute path is drawn", bool(paths.get("traceroute")))
check("2: the Tufin path is drawn beside it", bool(paths.get("tufin")),
      str(sorted(paths)))
check("2: and the deeper checks add their own",
      bool(paths.get("deep")), str(sorted(paths)))
check("2: the traceroute stops at the silence, without drawing it as a device",
      "*" not in (paths.get("traceroute") or {}).get("line", ""),
      (paths.get("traceroute") or {}).get("line", ""))
check("2: Tufin says the traffic is delivered -- it disagrees with the trace",
      (paths.get("tufin") or {}).get("reached") is True,
      str((paths.get("tufin") or {}).get("line"))[:60])
check("2: the deep path names the next hop the source is trying",
      "10.20.30.129" in (paths.get("deep") or {}).get("line", ""),
      (paths.get("deep") or {}).get("line", ""))
check("2: and says why nothing leaves",
      "TenGigE0/0/0/3 is down" in (paths.get("deep") or {}).get("note", ""),
      (paths.get("deep") or {}).get("note", "")[:90])

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
