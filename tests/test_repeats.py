"""Asking the same question twice is not investigating.

A real run on the bridge spent four of its deeper-checks steps re-running two
commands it had already run -- the same lines scrolling past the operator a
second time, the same approval card, and no new information, because a device
asked the same question gives the same answer.

A model does this when it cannot see its own progress. The fix is not a better
prompt: the graph knows exactly what it has already asked, so it answers a
repeat itself, out of what came back the first time, and says so plainly
enough that the model moves on.

    .venv/Scripts/python.exe tests/test_repeats.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["REQUIRE_APPROVAL"] = "1"       # the repeat must not ask for one

from langchain_core.messages import ToolMessage                 # noqa: E402
from langgraph.types import Command                             # noqa: E402

from agent import graph as G                                    # noqa: E402
from scripted_model import ScriptedModel, ssh                   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"
SAME = f"show route {DST}"

G.llm = ScriptedModel(
    script=[ssh(SRC, SAME),
            ssh(SRC, SAME),                    # word for word again
            ssh(SRC, f"show route  {DST}  "),  # and again, spaced differently
            ssh(SRC, f"show arp {DST}")],      # a real next step, must still run
    thoughts=["Reading the source's forwarding table.",
              "Reading the source's forwarding table.",
              "Reading the source's forwarding table.",
              "Now the address resolution entry."],
    final={"result": "INCONCLUSIVE", "cause": "the next hop never resolved"})


async def run():
    app = await G.build_agent()
    config = {"configurable": {"thread_id": "repeats-1"}}
    state = await app.ainvoke(
        {"messages": [("user", f"Troubleshoot {SRC} to {DST} on tcp:22")],
         "loops": 0}, config)
    asked = 0
    while "__interrupt__" in state:
        asked += 1
        if asked > 6:                          # a loop would hang the test
            break
        state = await app.ainvoke(Command(resume=True), config)
    return state, asked


state, approvals = asyncio.run(run())

results = [m for m in state["messages"] if isinstance(m, ToolMessage)]
repeats = [m for m in results if str(m.content).startswith("ALREADY RUN")]
audit = state.get("commands_run") or []

print()
for m in results:
    print(f"  {str(m.content)[:64].splitlines()[0]}")
print(f"\n  approvals asked: {approvals}   audited commands: {len(audit)}")

check("the command really was asked for four times",
      len(results) == 4, str(len(results)))
check("but only the first of the three identical ones ran",
      len(repeats) == 2, str(len(repeats)))
check("a repeat is answered with what the first one returned",
      all("ALREADY RUN" in str(m.content) for m in repeats)
      and all(len(str(m.content)) > 60 for m in repeats),
      str([len(str(m.content)) for m in repeats]))
check("spacing does not make it a different question",
      G._call_key("execute_query_on_server",
                  {"device_ip": SRC, "commands": [SAME]})
      == G._call_key("execute_query_on_server",
                     {"device_ip": SRC, "commands": [f" show  route   {DST} "]}),
      G._call_key("execute_query_on_server",
                  {"device_ip": SRC, "commands": [SAME]}))
check("but a different device is a different question",
      G._call_key("execute_query_on_server",
                  {"device_ip": SRC, "commands": [SAME]})
      != G._call_key("execute_query_on_server",
                     {"device_ip": "10.10.1.21", "commands": [SAME]}))
check("the operator is not asked to approve the same command twice",
      approvals == 2, f"{approvals} approvals for 2 distinct commands")
check("and the audit records what ran, not what was asked",
      len(audit) == 2, str([a.get("command") for a in audit]))
check("a genuinely different check still runs",
      any("arp" in str(a.get("command", "")) for a in audit),
      str([a.get("command") for a in audit]))
check("the run still reaches a report",
      "INCONCLUSIVE" in str(state.get("answer") or ""),
      str(state.get("answer"))[:70])

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
