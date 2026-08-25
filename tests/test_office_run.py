"""The three faults a real office run turned up, in the shapes it produced.

The mocks return output the way a textbook prints it. The real ssh MCP does
not: it hands its list back already serialised, its ping says "100.00% packet
loss" rather than "100%", and the relay's chat window is finite so the earliest
results fall out of it. Every one of those was invisible here until the run
happened on real equipment.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from langchain_core.messages import (AIMessage, HumanMessage,     # noqa: E402
                                     SystemMessage, ToolMessage)

from agent.llm.clipboard_llm import _recap                        # noqa: E402
from agent.utils import tool_text                                 # noqa: E402
from api.workflow import (as_report, check_ok, cmdb_record,       # noqa: E402
                          failed_line, usable_output)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- 1. the ssh MCP returns its list ALREADY SERIALISED --------------------
LOSS = ("\r\n\r\nTue Aug 25 22:14:01\r\n"
        "PING 198.51.100.6: 56 data bytes\r\n"
        "--- 198.51.100.6 ping statistics ---\r\n"
        "3 packets transmitted, 0 packets received, 100.00% packet loss\r\n")
SERIALISED = json.dumps([{"cmd": "ping 198.51.100.6 count 3",
                          "stdout": LOSS, "stderr": "", "rc": 0}])

flat = tool_text(SERIALISED)
check("a serialised CLI result is flattened, not shown as JSON",
      not flat.lstrip().startswith("["), flat[:60])
check("the command heads it, as a session",
      flat.startswith("# ping 198.51.100.6 count 3"), flat[:40])
check("the output is real lines, not escapes",
      "\\r\\n" not in flat and "packet loss" in flat)
check("so the panel can read it",
      usable_output(flat, "ping 198.51.100.6 count 3"),
      "this is what put a JSON blob in the row and a cross beside it")
check("and the failing line is quotable",
      "packet loss" in str(failed_line(flat)), str(failed_line(flat))[:60])

MULTI = json.dumps([
    {"cmd": "show ip route 10.0.0.1", "stdout": "Routing entry for 10.0.0.0/8\r\n"},
    {"cmd": "show arp", "stdout": "10.0.0.1  0050.56be.1a2b  ARPA  Eth1/1\r\n"},
])
flat2 = tool_text(MULTI)
check("several commands in one call all come through",
      flat2.count("# show") == 2, repr(flat2[:60]))

# a CMDB record is JSON too, and must NOT be run through the CLI flattener
REC = json.dumps({"region": "INDIA", "lookup_by": "name",
                  "data": {"name": "EDGE-A1", "managementIp": "10.0.0.1"}})
check("a CMDB record is left alone", cmdb_record(tool_text(REC)) == (True, "EDGE-A1"),
      str(cmdb_record(tool_text(REC))))
check("and a plain sentence is still a plain sentence",
      tool_text("No data found for 'X' in any region.").startswith("No data"))


# ---- 2. total loss, however the platform prints it -------------------------
PINGS = {
    "3 packets transmitted, 0 packets received, 100.00% packet loss": False,
    "3 packets transmitted, 0 received, 100% packet loss": False,
    "5 packets transmitted, 5 packets received, 0.00% packet loss": True,
    "10 packets transmitted, 10 received, 0% packet loss": True,
    "4 packets transmitted, 2 received, 50% packet loss": True,
    "Success rate is 0 percent (0/5)": False,
    "Success rate is 100 percent (5/5)": True,
}
for text, want in PINGS.items():
    check(f"ping verdict: {text[:46]}", check_ok(text) == want,
          f"read as {'ok' if check_ok(text) else 'failed'}")


# ---- 3. the relay's window drops the early results ------------------------
def tool(name, body):
    return ToolMessage(content=body, name=name, tool_call_id=name)


THREAD = [
    SystemMessage(content="the prompt"),
    HumanMessage(content="Troubleshoot EDGE-A1 to EDGE-A2 on tcp:22"),
    tool("get_device_details", json.dumps({"data": {"name": "EDGE-A1"}})),
    tool("execute_query_on_server", "# ping 10.0.0.2\n" + LOSS.replace("\r\n", "\n")),
    tool("get_firewall_path", json.dumps({"verdict": "ALLOWED",
                                          "blocking_rules": []})),
    tool("get_alert_and_ticket_details_from_archangel", "5 open alerts"),
]

recap = _recap(THREAD)
check("the recap exists at all", bool(recap.strip()))
check("it carries the firewall verdict -- the thing the run lost",
      "ALLOWED" in recap, recap[:80])
check("and the ping result",
      "packet loss" in recap, recap[:120])
check("it names each tool, so the model knows what it already has",
      recap.count("- ") == 4, recap)
check("it tells the model not to run them again",
      "do not run them again" in recap.lower())
check("nothing to recap stays empty, rather than a heading over nothing",
      _recap([SystemMessage(content="x"), HumanMessage(content="y")]) == "")

many = [tool(f"t{i}", f"result {i}") for i in range(40)]
capped = _recap(many)
check("a long run keeps the most recent, and says it is doing so",
      capped.count("- ") == 14 and "most recent 14 of 40" in capped,
      capped.splitlines()[0][:90])


# ---- and the verdict the run never reached --------------------------------
NO_VERDICT = ("WORKFLOW COMPLETE. Do not run any more functions. Return the "
              "accumulated troubleshooting results; however, the firewall-path "
              "verdict is missing from the provided context, so a reachability "
              "verdict cannot be determined.")
report = as_report(NO_VERDICT) or {}
check("an answer with no verdict parses as text, not as a report",
      not report.get("result"), str(report)[:60])

print()
print("ALL PASSED" if not fails else f"FAILED ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
