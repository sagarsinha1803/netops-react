"""The final answer has to reach the Report tab as a report.

Models rarely return the bare object the prompt asks for. They summarise in
prose first, fence the JSON, wrap it in {"thought":.., "final":..}, or all
three -- and each of those used to fall through to "render the whole blob as
text", so a run that worked perfectly still looked like a chatbot transcript.

The long string below is what the Copilot bridge actually returned on a demo
run of the healthy scenario.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api.workflow import as_report                              # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


FROM_THE_BRIDGE = """Final Thought: I have completed the troubleshooting process. \
Here are the results:

- **Ping from DC1-EDGE-RTR-01 to DC2-WEB-LB-01**: SUCCESS (100% success rate)
- **Traceroute**: 1st hop DC1-CORE-SW-01 (10.20.10.254)
- **Firewall Path Check**: Verdict ALLOWED
- **Open Alerts**: DC2-WEB-LB-01 has a power supply alert (570000114).

Based on this information, the final result is as follows:

```json
{
  "thought": "Ping was successful, traceroute shows the path, and the firewall \
allows traffic on TCP 443.",
  "final": {
    "source": "DC1-EDGE-RTR-01 / 10.20.10.1 (Cisco IOS XR)",
    "destination": "DC2-WEB-LB-01 / 10.40.20.50 (Arista EOS)",
    "ping": "SUCCESS",
    "path": ["DC1-EDGE-RTR-01", "DC1-CORE-SW-01", "DC2-CORE-SW-01",
             "DC2-WEB-LB-01"],
    "result": "REACHABLE",
    "evidence": ["Ping to 10.40.20.50: SUCCESS (100% success rate)"],
    "cause": "Traffic is allowed by the firewall, and the path is clear.",
    "next_step": "Monitor the power supply alert on DC2-WEB-LB-01."
  }
}
```"""

got = as_report(FROM_THE_BRIDGE)
check("the demo's answer parses into a report", isinstance(got, dict))
check("it is the report, not the envelope",
      got and "thought" not in got and got.get("result") == "REACHABLE",
      str(sorted(got or {})))
check("the fields the tab lays out are all there",
      got and all(k in got for k in ("source", "destination", "ping", "path",
                                     "result", "evidence", "cause",
                                     "next_step")),
      str(sorted(got or {})))
check("the path stayed a list, so the Path tab can draw it",
      isinstance((got or {}).get("path"), list), str((got or {}).get("path")))
check("the prose around it is dropped, not shown as the answer",
      "Final Thought" not in json.dumps(got or {}))

# ---- the other shapes models use -----------------------------------------
check("a bare object still works",
      (as_report('{"result": "NOT REACHABLE", "cause": "ACL DENY-ALL"}') or {})
      .get("result") == "NOT REACHABLE")
check("a bare envelope still works",
      (as_report('{"thought": "done", "final": {"result": "REACHABLE"}}') or {})
      .get("result") == "REACHABLE")
check("an envelope nested twice is still unwrapped",
      (as_report('{"final": {"final": {"result": "REACHABLE"}}}') or {})
      .get("result") == "REACHABLE")
check("a fenced object with no envelope works",
      (as_report('Here you go:\n```json\n{"result": "REACHABLE"}\n```') or {})
      .get("result") == "REACHABLE")
check("a final that is a sentence, not an object, becomes text",
      (as_report('{"thought": "x", "final": "It is reachable."}') or {})
      .get("text") == "It is reachable.")

# ---- what must stay prose ------------------------------------------------
plain = "The path is blocked by DENY-ALL on the DC1 edge."
check("a plain answer stays plain", as_report(plain) == {"text": plain})
check("a JSON object that is NOT a report stays prose",
      (as_report('I will call it now:\n```json\n{"device_name": "SW-1"}\n```')
       or {}).get("text", "").startswith("I will call it"),
      str(as_report('```json\n{"device_name": "SW-1"}\n```')))
check("nothing at all is no report", as_report("") is None)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
