"""End-to-end: does the clipboard relay hand the graph REAL addresses back?

Stands in for Copilot: reads whatever lands on the clipboard, replies with a
tool call built from the addresses it sees there. If masking works, the prompt
it sees has no real address and the tool call the agent ends up with has
nothing but real ones.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import agent.llm.clipboard_llm as C  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402

SCHEMA = [{"type": "function", "function": {
    "name": "execute_query_on_server",
    "description": "Run read-only CLI commands on a device.",
    "parameters": {"type": "object", "properties": {
        "device_ip": {"type": "string"},
        "commands": {"type": "array"},
        "region": {"type": "string"}}}}}]

CMDB = ("{'region': 'INDIA', 'data': {'name': 'APP-SRV-DC1-020', "
        "'remoteManagement': '10.10.1.20', 'brand': 'cisco'}}")
TRACE = ("  1 Leaf-101 (10.10.1.1) 1 msec\n"
         "  2 Border-Router-01 (10.10.0.1) 2 msec\n"
         "  3 FW-DC1-EDGE-01 (10.10.255.1) 3 msec")

seen = {}


# whichever stand-in form MASK_STYLE produced
STANDIN = re.compile(r"\b198\.1[89]\.\d{1,3}\.\d{1,3}\b|\bip4\.n\d+\.h\d+\b")


def fake_copy(text):
    """The 'paste into Copilot' step, plus Copilot's answer."""
    seen["prompt"] = text
    found = STANDIN.findall(text)
    assert found, "nothing was masked - the relay sent real addresses"
    src, hop = found[0], found[-1]
    seen["reply"] = json.dumps({
        "thought": f"Route via {hop}. Checking the forwarding entry on {src}.",
        "tool": "execute_query_on_server",
        "args": {"device_ip": src, "region": "INDIA",
                 "commands": [f"show cef {hop}", f"ping {hop} repeat 3"]}})


C._copy = fake_copy
C._paste = lambda: seen.get("reply", "")

llm = C.ClipboardLLM(mode="full", beep=False, verbose_console=False,
                     prompt_file=None, tool_schemas=SCHEMA, poll_interval=0.01)

msgs = [HumanMessage("troubleshoot 10.10.1.20 to 172.20.5.10"),
        ToolMessage(content=CMDB, name="get_device_details", tool_call_id="a"),
        ToolMessage(content=TRACE, name="execute_query_on_server", tool_call_id="b")]

reply = llm.invoke(msgs)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


prompt = seen["prompt"]
REAL = ["10.10.1.20", "172.20.5.10", "10.10.1.1", "10.10.0.1", "10.10.255.1"]
leaked = [r for r in REAL if r in prompt]
check("nothing real on the clipboard", not leaked, f"leaked={leaked}")
check("stand-ins are on the clipboard", bool(STANDIN.search(prompt)))

call = reply.tool_calls[0]
args = call["args"]
blob = json.dumps(args)
check("tool call carries a real device_ip", args["device_ip"] in REAL, args["device_ip"])
check("no stand-in survives into the tool call", not STANDIN.search(blob), blob)
check("commands are real", all(any(r in c for r in REAL) for c in args["commands"]),
      str(args["commands"]))
check("thought shown to the user is real too",
      not STANDIN.search(reply.additional_kwargs.get("thought", "") + str(reply.content)))

print(f"\nWHAT COPILOT SAW  (MASK_STYLE={os.environ.get('MASK_STYLE', 'ip')})")
print("-" * 55)
for line in prompt.splitlines():
    if STANDIN.search(line) or "Leaf-101" in line:
        print("  " + line.strip()[:90])
print("\nWHAT THE AGENT WILL RUN")
print("-" * 55)
print(f"  device_ip: {args['device_ip']}")
for c in args["commands"]:
    print(f"  $ {c}")

sys.exit(1 if fails else 0)
