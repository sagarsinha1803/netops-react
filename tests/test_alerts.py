"""Archangel alerts: the reply shapes, and what the panel makes of them."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

from agent import constants as C                          # noqa: E402
from api.workflow import Workflow, parse_alerts           # noqa: E402
from mocks.alert_mock import (                            # noqa: E402
    get_alert_and_ticket_details_from_archangel as alerts_of)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- the tool -------------------------------------------------------------
rows = alerts_of("APP-SRV-DC1-020")
check("a device with alerts returns a list", isinstance(rows, list) and rows,
      str(type(rows)))
check("each alert carries the ticket to open",
      all(r.get("ticket_id") for r in rows))
check("and the failing check, which is what an engineer scans for",
      all(r.get("check_name") for r in rows))
check("lookup is case-insensitive, as the CMDB spelling may differ",
      isinstance(alerts_of("app-srv-dc1-020"), list))
check("a device with none gets a sentence, not an empty list",
      isinstance(alerts_of("NO-SUCH-DEVICE"), str),
      str(alerts_of("NO-SUCH-DEVICE"))[:50])
check("the limit is honoured", len(alerts_of("APP-SRV-DC1-020", limit=2)) == 2)

# ---- parsing --------------------------------------------------------------
parsed, message = parse_alerts(json.dumps(rows))
check("the list parses into table rows", len(parsed) == len(rows), str(len(parsed)))
check("every column the table shows is present",
      all(all(k in r for k in ("device_name", "alert_title", "check_name",
                               "alert_type", "ticket_id")) for r in parsed))

# MCP returns a list as one content block per element, so the rows arrive
# run together rather than inside an array. Parsing only the last one is the
# bug this guards.
run_together = "\n".join(json.dumps(r, indent=2) for r in rows)
parsed, message = parse_alerts(run_together)
check("rows run together (MCP's list shape) all parse",
      len(parsed) == len(rows), f"{len(parsed)} of {len(rows)}")
check("and they keep their own devices, not just the last one",
      [r["alert_id"] for r in parsed] == [r["alert_id"] for r in rows])

parsed, message = parse_alerts(str(rows))          # a python repr, not JSON
check("a python repr parses too", len(parsed) == len(rows))

parsed, message = parse_alerts("No open alerts found for 'X'.")
check("'no alerts' yields no rows and keeps the sentence",
      parsed == [] and "No open alerts" in message, message[:40])

parsed, message = parse_alerts("Error querying Archangel: connection refused")
check("an error yields no rows and keeps the error",
      parsed == [] and "connection refused" in message, message[:40])

parsed, _ = parse_alerts(json.dumps([{"alert_id": "1", "ticket_id": "2",
                                      "severity": "major"}]))
check("a column the query grew is kept, not silently dropped",
      parsed and parsed[0].get("extra", {}).get("severity") == "major",
      str(parsed[:1]))

# ---- the stage ------------------------------------------------------------
check("alerts is a stage in the strip",
      "alerts" in [k for k, _ in __import__("api.workflow", fromlist=["x"]).STEP_DEFS])
check("it sits between the policy check and the conclusion",
      [k for k, _ in __import__("api.workflow", fromlist=["x"]).STEP_DEFS
       ].index("alerts") > [k for k, _ in __import__(
           "api.workflow", fromlist=["x"]).STEP_DEFS].index("policy"))

wf = Workflow()
wf.reset({"source": "APP-SRV-DC1-020", "dest": "PAY-API-DC2-010"})
wf.from_tool_call("get_alert_and_ticket_details_from_archangel",
                  {"device_name": "APP-SRV-DC1-020"}, "")
check("the alert tool marks the alerts stage, not the policy one",
      wf.state["alerts"]["status"] == "running"
      and wf.state["policy"]["status"] == "pending", str(wf.state["alerts"]))
wf.alerts.extend(parse_alerts(json.dumps(rows))[0])
check("the snapshot carries the rows for the table",
      len(wf.snapshot()["alerts"]) == len(rows))
check("a reset clears them", (wf.reset(), len(wf.snapshot()["alerts"]))[1] == 0)

# ---- wiring ---------------------------------------------------------------
check("the archangel server is registered", "archangel" in C.MCP_SERVERS)
check("its tool is named", C.ALERT_TOOL_NAMES ==
      {"get_alert_and_ticket_details_from_archangel"})
check("it is NOT approval-gated: a read-only SELECT runs on no device",
      "archangel" not in C.DEVICE_SERVERS)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
