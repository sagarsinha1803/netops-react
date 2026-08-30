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

# Calling the tool directly (which the server does when the model skipped the
# step) hands back content blocks instead of text. Row count looked right
# while every column was blank -- worse than an outright failure.
blocks = [{"type": "text", "text": json.dumps(r)} for r in rows]
parsed, message = parse_alerts(json.dumps(blocks))
check("MCP content blocks are unwrapped, not treated as rows",
      len(parsed) == len(rows) and parsed[0]["device_name"] == rows[0]["device_name"],
      str(parsed[:1])[:80])

parsed, message = parse_alerts(json.dumps(
    [{"type": "text", "text": "No open alerts found for 'X'."}]))
check("a sentence inside a content block stays a sentence",
      parsed == [] and "No open alerts" in message, message[:40])

parsed, message = parse_alerts(str(rows))          # a python repr, not JSON
check("a python repr parses too", len(parsed) == len(rows))

parsed, message = parse_alerts("No open alerts found for 'X'.")
check("'no alerts' yields no rows and keeps the sentence",
      parsed == [] and "No open alerts" in message, message[:40])

parsed, message = parse_alerts("Error querying Archangel: connection refused")
check("an error yields no rows and keeps the error",
      parsed == [] and "connection refused" in message, message[:40])

# severity used to be the example here; it is a column of its own now, so the
# point is made with a field nobody has asked for yet
parsed, _ = parse_alerts(json.dumps([{"alert_id": "1", "ticket_id": "2",
                                      "assigned_group": "network-ops"}]))
check("a column the query grew is kept, not silently dropped",
      parsed and parsed[0].get("extra", {}).get("assigned_group")
      == "network-ops", str(parsed[:1]))
check("while severity is a column in its own right now",
      parsed and "severity" in parsed[0], str(parsed[:1])[:70])

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

# ---- severity, and how old the alert is -----------------------------------
# The table gained a Severity column and a filter for the last day / five /
# ten. Both come off the same reply: alert.status and alert.time.
import datetime as _dt                                            # noqa: E402

from api.workflow import alert_age_days                           # noqa: E402


def _ago(days, shape="%Y-%m-%d %H:%M:%S"):
    return (_dt.datetime.now() - _dt.timedelta(days=days, hours=1)).strftime(shape)


check("today reads as nought days old", alert_age_days(_ago(0)) == 0,
      str(alert_age_days(_ago(0))))
check("yesterday reads as one", alert_age_days(_ago(1)) == 1,
      str(alert_age_days(_ago(1))))
check("a week ago reads as seven", alert_age_days(_ago(7)) == 7,
      str(alert_age_days(_ago(7))))

# the shapes a database or a driver can hand back for the same instant
ISO_T = (_dt.datetime.now() - _dt.timedelta(days=3, hours=1)).isoformat()
check("an ISO timestamp with a T is read",
      alert_age_days(ISO_T) == 3, str(alert_age_days(ISO_T)))
check("and one with a Z on the end",
      alert_age_days(
          (_dt.datetime.now(_dt.timezone.utc)
           - _dt.timedelta(days=2, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")) == 2)
check("and a bare date", alert_age_days(_ago(4, "%Y-%m-%d")) in (4, 5),
      str(alert_age_days(_ago(4, "%Y-%m-%d"))))
check("and epoch seconds",
      alert_age_days(int((_dt.datetime.now(_dt.timezone.utc)
                          - _dt.timedelta(days=5, hours=1)).timestamp())) == 5)

# what must NOT happen: a filter hiding an alert because nobody could read its
# date. An old alert shown is a nuisance; a current one hidden is a fault.
for unreadable in (None, "", "   ", "not a date", "0000-00-00 00:00:00"):
    check(f"an unreadable time is None, never a number: {unreadable!r}",
          alert_age_days(unreadable) is None, str(alert_age_days(unreadable)))
check("a time in the future does not read as negative",
      alert_age_days(_ago(-5)) == 0, str(alert_age_days(_ago(-5))))

# ---- and end to end, through the reply the tool actually sends -------------
RAISED = _ago(6)
WITH_SEVERITY = json.dumps([{
    "alert_id": "1f0a-2b3c", "device_name": "EDGE-A1",
    "alert_type": "network", "alert_title": "LinkStatusOperDown",
    "check_name": "Interface Bundle-Ether7", "ticket_id": "560000009",
    "severity": "critical", "alert_time": RAISED,
}])
rows, message = parse_alerts(WITH_SEVERITY)
check("the severity reaches the table", rows and rows[0]["severity"] == "critical",
      str(rows[:1])[:80])
check("so does the time it was raised",
      rows and rows[0]["alert_time"] == RAISED, str(rows[:1])[:80])
check("and the age the filter works from",
      rows and rows[0]["age_days"] == 6, str(rows[0].get("age_days")))
check("a reply with neither still parses",
      parse_alerts(json.dumps([{"alert_id": "x", "device_name": "EDGE-A1"}]))[0][0]
      ["age_days"] is None)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
