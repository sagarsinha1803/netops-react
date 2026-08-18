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

sys.exit(1 if fails else 0)
