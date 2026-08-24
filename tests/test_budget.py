"""A run that spends its tool budget must still produce an answer.

Ending on a tool call means no content, so no report -- the panel shows a run
that simply stopped, which an engineer cannot tell apart from a crash.
"""
import asyncio
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:11502/v1"
os.environ["LLM_MODEL"] = "gpt-4o"
os.environ["LLM_API_KEY"] = "EMPTY"
os.environ["REQUIRE_APPROVAL"] = "0"          # the gate is tested elsewhere

llm = subprocess.Popen([sys.executable, "tests/fake_llm.py", "11502"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


async def main():
    from agent import constants as C
    C.MAX_TOOL_LOOPS = 2                       # spend it almost immediately
    from agent.graph import build_agent
    app = await build_agent()
    state = await app.ainvoke(
        {"messages": [("user", "Troubleshoot 10.10.1.20 to 172.20.5.10 on tcp:443")],
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
    check("it does not claim a verdict it never reached",
          "not reachable" not in answer.lower() or "inconclusive" in answer.lower()
          or "budget" in answer.lower(), answer[:80])

asyncio.run(main())
llm.terminate()
print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
