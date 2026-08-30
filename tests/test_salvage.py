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

# ---- an ANSWER is not an argument bag -------------------------------------
# A second run produced "get_device_details()" with a red cross and a pydantic
# complaint that device_name was missing. The model had answered in the
# relay's envelope -- {"thought": ..., "final": {}} -- and mentioned the tool
# in passing. One tool named, an object sharing not one argument with it, and
# the old rule let it through because there was only one candidate.
ENVELOPE = ('{"thought": "I need to start the workflow and gather details '
            'before I can proceed.", "final": {}}\n\n'
            'Calling get_device_details for EDGE-A1.')
check("a final-answer envelope is not read as arguments",
      salvage_tool_call(ENVELOPE, TOOLS) is None,
      str(salvage_tool_call(ENVELOPE, TOOLS)))

check("nor is a report object that names a tool",
      salvage_tool_call('{"result": "INCONCLUSIVE", "cause": "the deeper '
                        'checks with execute_query_on_server found nothing"}',
                        TOOLS) is None)

# the rule that let it through: one candidate is not evidence of a fit
check("arguments that share nothing with the tool are not its arguments",
      salvage_tool_call('{"colour": "blue", "size": 4} -- get_device_details',
                        TOOLS) is None,
      str(salvage_tool_call('{"colour": "blue"} get_device_details', TOOLS)))
check("while arguments that DO fit are still salvaged",
      salvage_tool_call('calling get_device_details:\n'
                        '{"device_name": "EDGE-A1"}', TOOLS)
      == ("get_device_details", {"device_name": "EDGE-A1"}))


# ---- announced, and then not done ----------------------------------------
# The one that wasted a whole run: no JSON anywhere, just a model narrating
# the step it was about to take. Nothing to salvage -- but "there is nothing
# runnable here" and "this is the final answer" are not the same thing, and
# treating the second as the first left every stage grey.
from agent.salvage import looks_like_a_call                     # noqa: E402

ANNOUNCED = """Thought: I need to first gather device details for both the source (EDGE-A1) and destination (EDGE-B2) to understand their configurations and management IPs. I will start by calling the get_device_details function for EDGE-A1.

Now, I will proceed with the first step.

Calling get_device_details for EDGE-A1."""

check("an announced call carries nothing to salvage",
      salvage_tool_call(ANNOUNCED, TOOLS) is None,
      str(salvage_tool_call(ANNOUNCED, TOOLS)))
check("but it is recognised as a step that meant to happen",
      looks_like_a_call(ANNOUNCED, TOOLS), "so the graph asks once more")

# and the things that must NOT be re-asked, or a model gets talked out of an
# answer it already had
for finished in [
    "The link is down; policy permits the traffic.",
    '{"result": "NOT REACHABLE", "cause": "Ethernet1/2 is down"}',
    "I ran get_device_details and the result is INCONCLUSIVE.",
    "Next step is for a human: raise a ticket with the transport team.",
    "I will now summarise what the checks showed.",
]:
    check(f"a finished answer is left alone: {finished[:38]!r}",
          not looks_like_a_call(finished, TOOLS))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
