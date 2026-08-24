"""A run that spends its tool budget must still produce an answer.

Ending on a tool call means no content, so no report -- the panel shows a run
that simply stopped, which an engineer cannot tell apart from a crash.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["REQUIRE_APPROVAL"] = "0"          # the gate is tested elsewhere

from agent import constants as C                                # noqa: E402
from agent import graph as G                                    # noqa: E402
from scripted_model import ScriptedModel, ssh                   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"

# a model that would keep going forever if the graph let it
G.llm = ScriptedModel(
    script=[("get_device_details", {"device_name": SRC})] * 20,
    thoughts=["Looking it up again."] * 20,
    final="never reached")


async def main():
    C.MAX_TOOL_LOOPS = 2                       # spend it almost immediately
    app = await G.build_agent()
    state = await app.ainvoke(
        {"messages": [("user", f"Troubleshoot {SRC} to {DST} on tcp:443")],
         "loops": 0},
        {"configurable": {"thread_id": "budget-1"}})

    answer = str(state.get("answer") or "")
    last = state["messages"][-1]
    print(f"\nloops used: {state.get('loops')}")
    print(f"answer: {answer[:200]}")

    check("the run still ends with an answer", bool(answer.strip()), answer[:60])
    check("the last message is not a bare tool call",
          not getattr(last, "tool_calls", None),
          str(getattr(last, "tool_calls", None))[:60])
    check("the loop really was cut short, not run to completion",
          (state.get("loops") or 0) <= 3, str(state.get("loops")))


asyncio.run(main())
print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
