"""A tool call written as prose is still a tool call.

The VS Code Copilot bridge passes the tool schemas to vscode.lm, but whether
the model answers with a real tool call is its own decision. When it does not,
it writes the call out instead -- and the graph, seeing no tool calls, treats
the first sentence of the investigation as the final answer. Every stage grey,
Conclusion green, nothing run.

The exact text below is what came back from the bridge on a demo run.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.salvage import salvage_tool_call                      # noqa: E402

class _Tool:
    """Just enough of a LangChain tool: a name and its argument names."""

    def __init__(self, name, args):
        self.name = name
        self.args = {a: {} for a in args}


# the map the graph passes: which tool a set of arguments belongs to is
# answered by the schema, not by which name appeared last in the prose
TOOLS = {t.name: t for t in [
    _Tool("get_device_details", ["device_name", "region"]),
    _Tool("get_firewall_path", ["src", "dst", "service"]),
    _Tool("get_alert_and_ticket_details_from_archangel",
          ["device_name", "limit"]),
    _Tool("execute_query_on_server",
          ["device_ip", "commands", "region", "port"]),
]}

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- the real one ---------------------------------------------------------
FROM_THE_BRIDGE = """Thought: I need to start by looking up the device details \
for both source (DC1-EDGE-RTR-01) and destination (DC2-WEB-LB-01) to gather \
their management IPs, vendors, models, and operating systems. This information \
is necessary to proceed with the troubleshooting workflow.

I'll first call the `get_device_details` function for DC1-EDGE-RTR-01.

Now, I will proceed with the first tool call.

```json
{"device_name":"DC1-EDGE-RTR-01"}
```"""

got = salvage_tool_call(FROM_THE_BRIDGE, TOOLS)
check("the demo failure is salvaged", got is not None, str(got))
check("with the right tool", got and got[0] == "get_device_details", str(got))
check("and the right arguments",
      got and got[1] == {"device_name": "DC1-EDGE-RTR-01"}, str(got))

# ---- the clipboard protocol, sent to an API model -------------------------
got = salvage_tool_call(
    '{"thought": "looking it up", "tool": "get_device_details", '
    '"args": {"device_name": "SW-1"}}', TOOLS)
check("the relay's own {tool, args} shape works too",
      got == ("get_device_details", {"device_name": "SW-1"}), str(got))

got = salvage_tool_call(
    '{"tool": "get_firewall_path", "arguments": '
    '"{\\"src\\": \\"10.0.0.1\\", \\"dst\\": \\"10.0.0.2\\"}"}', TOOLS)
check("arguments as a JSON STRING, the way OpenAI sends them",
      got and got[0] == "get_firewall_path" and got[1].get("src") == "10.0.0.1",
      str(got))

# ---- what must NOT be salvaged -------------------------------------------
check("a plain answer is left alone",
      salvage_tool_call("The path is blocked by DENY-ALL on the edge.", TOOLS)
      is None)
check("a final report is not mistaken for a call",
      salvage_tool_call('{"result": "NOT REACHABLE", "cause": "ACL"}', TOOLS)
      is None,
      str(salvage_tool_call('{"result": "NOT REACHABLE"}', TOOLS)))
check("a tool nobody offered is refused, not invented",
      salvage_tool_call('{"tool": "reboot_device", "args": {"ip": "10.0.0.1"}}',
                        TOOLS) is None)
check("prose naming a tool but carrying no arguments is not a call",
      salvage_tool_call("I will use get_device_details next.", TOOLS) is None)
check("empty content is not a call", salvage_tool_call("", TOOLS) is None)

# a model that lists the whole workflow first must not be read as calling the
# last one it happened to mention in passing
listed = """I will run get_device_details, then execute_query_on_server for the
ping, then get_firewall_path for policy. Starting with the CMDB:

```json
{"device_name": "DC1-APP-SW-07"}
```"""
got = salvage_tool_call(listed, TOOLS)
check("the tool named closest to the arguments wins",
      got == ("get_device_details", {"device_name": "DC1-APP-SW-07"}), str(got))

# ---- it changes nothing about safety -------------------------------------
from agent.guards import check_command                           # noqa: E402

got = salvage_tool_call(
    'Running `execute_query_on_server`:\n'
    '```json\n{"device_ip": "10.0.0.1", "commands": ["configure terminal"]}\n```',
    TOOLS)
check("a write command can be salvaged as a CALL", got is not None, str(got))
check("...and is still refused by the allowlist",
      bool(check_command("configure terminal")),
      "the salvage path must not be a way around the guard")

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
