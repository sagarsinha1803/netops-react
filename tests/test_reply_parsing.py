"""Copilot's reply must parse, including when it writes sloppy JSON.

A reply that fails to parse is not an error the user sees -- _to_message falls
back to treating the whole blob as a final answer with no tool calls, so the
graph simply ends. The run looks finished when it has actually stalled, which
is far worse than a visible failure.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llm.clipboard_llm import _extract_json, _repair_quotes  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# The one that stalled a real run: a CLI filter needs quotes, and the model
# wrote them straight into the JSON string without escaping.
LOOSE = ('{"thought":"checking the VPN route","tool":"execute_query_on_server",'
         '"args":{"device_ip":"ip4.n1.h245","commands":["show bgp vrf vrf-29 '
         'ip4.n5.h46 | include "Received Label|from|metric""],'
         '"region":"R-1","port":22}}')
obj = _extract_json(LOOSE)
check("a filter with unescaped quotes still parses", obj is not None)
check("it is read as a tool call, not a final answer",
      bool(obj) and obj.get("tool") == "execute_query_on_server")
check("the command survives intact",
      bool(obj) and "include" in obj["args"]["commands"][0]
      and "Received Label" in obj["args"]["commands"][0],
      str(obj["args"]["commands"]) if obj else "")

# and the shapes that already worked must keep working
GOOD = {
    "properly escaped quotes":
        '{"tool":"execute_query_on_server","args":{"commands":["show x | '
        'include \\"a|b\\""],"region":"R-1"}}',
    "no quotes at all":
        '{"tool":"execute_query_on_server","args":{"commands":["show route 1.2.3.4"]}}',
    "structured final answer":
        '{"thought":"done","final":{"result":"NOT REACHABLE","ping":"FAILED"}}',
    "final answer containing an apostrophe":
        '{"thought":"done","final":"the device\'s route is missing"}',
    "markdown-escaped tool name":
        r'{"thought":"x","tool":"get\_device\_details","args":{"device\_name":"a"}}',
    "curly quotes inside a code fence":
        "```json\n{\u201cthought\u201d:\u201chi\u201d,\u201cfinal\u201d:\u201cok\u201d}\n```",
    "several tool calls":
        '{"thought":"x","tools":[{"tool":"get_device_details","args":{"device_name":"a"}}]}',
}
for label, text in GOOD.items():
    check(f"still parses: {label}", _extract_json(text) is not None)

# the repair must not corrupt JSON that was already valid
VALID = '{"a":"plain","b":["x","y"],"c":{"d":"e"},"n":22}'
check("repair leaves valid JSON untouched", _repair_quotes(VALID) == VALID,
      _repair_quotes(VALID))

# ---- a brace too many, and the rest of what a paste picks up --------------
# The real one: a deeper-checks run stopped on step three and printed the
# model's next tool call where the report belongs. The object was correct
# except that it closed one brace too many -- and the old reader took the
# first "{" to the LAST "}" anywhere in the reply, so the extra brace came
# along and nothing parsed. Counting braces instead ends the candidate at the
# brace that closes the one it started on.
BASE = ('{"thought":"The interface check did not answer the requested command, '
        'so retry once with the plural form.",'
        '"tool":"execute_query_on_server",'
        '"args":{"device_ip":"203.0.113.9","region":"REGION-A",'
        '"commands":["show interfaces AGG-A brief"]}}')

DAMAGED = {
    "one brace too many": BASE + "}",
    "three braces too many": BASE + "}}}",
    "a stray paren where a brace belongs": BASE[:-1] + ")",
    "prose after the object": BASE + chr(10) * 2 + "Shall I run it?",
    "the tool name in bold": BASE.replace('"tool"', '**"tool"**'),
    "a paste that was cut short": BASE[:-3],
}
for label, text in DAMAGED.items():
    got = _extract_json(text)
    check("survives: " + label,
          bool(got) and got.get("tool") == "execute_query_on_server",
          str(got)[:70])

cut = _extract_json(BASE[:-3])
check("a cut-short paste keeps the arguments it did carry",
      bool(cut) and cut["args"]["commands"] == ["show interfaces AGG-A brief"],
      str(cut)[:80])

# a real final answer must still not be read as a call
REPORT = '{"result":"NOT REACHABLE","cause":"the link is down"}'
check("a report is still a report", _extract_json(REPORT) == {
    "result": "NOT REACHABLE", "cause": "the link is down"})

# ---- and the salvage path, which the graph uses when nothing was CALLED ----
from agent.salvage import looks_like_a_call, salvage_tool_call  # noqa: E402

TOOLS = {"execute_query_on_server": None, "get_device_details": None}
WANTED = ("execute_query_on_server",
          {"device_ip": "203.0.113.9", "region": "REGION-A",
           "commands": ["show interfaces AGG-A brief"]})
check("a damaged call is salvaged into a runnable one",
      salvage_tool_call(BASE + "}", TOOLS) == WANTED,
      str(salvage_tool_call(BASE + "}", TOOLS))[:80])

# what the graph asks before it accepts a reply as the end of the run
NOT_JSON = "{tool: execute_query_on_server, args: (device_ip 203.0.113.9)}"
check("an unreadable reply naming a tool is recognised as an attempt",
      looks_like_a_call(NOT_JSON, TOOLS), NOT_JSON)
check("a finished answer is not mistaken for one",
      not looks_like_a_call("The link is down; policy permits the traffic.",
                            TOOLS))
check("nor is a report that parses", not looks_like_a_call(REPORT, TOOLS))

print()
print("ALL PASSED" if not fails else "FAILED: " + str(fails))

sys.exit(1 if fails else 0)
