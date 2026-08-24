"""A model that never emits a tool call must still drive the run.

This is the bridge failure end to end: the model answers with prose and a JSON
block every time, never once with a real tool call. Before the salvage path,
the run ended on the first reply -- every stage grey, Conclusion green, nothing
executed, and a "report" that was the model clearing its throat.
"""
import asyncio
import os
import sys
from typing import Any, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["REQUIRE_APPROVAL"] = "0"          # the gate is tested elsewhere

from langchain_core.messages import AIMessage, BaseMessage       # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult    # noqa: E402

from agent import graph as G                                     # noqa: E402
from scripted_model import ScriptedModel                         # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"


class ProseOnlyModel(ScriptedModel):
    """Writes every tool call out as text, exactly as the bridge did."""

    def _generate(self, messages: List[BaseMessage],
                  stop: Optional[List[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        real = super()._generate(messages, stop, run_manager, **kwargs)
        message = real.generations[0].message
        calls = getattr(message, "tool_calls", None)
        if not calls:
            return real
        call = calls[0]
        import json
        prose = (f"Thought: {message.content}\n\n"
                 f"I'll call the `{call['name']}` function now.\n\n"
                 f"```json\n{json.dumps(call['args'])}\n```")
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=prose))])


G.llm = ProseOnlyModel(
    script=[
        ("get_device_details", {"device_name": SRC}),
        ("get_device_details", {"device_name": DST}),
        ("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:443"}),
    ],
    thoughts=["Looking up the source.", "Now the destination.",
              "Asking Tufin about tcp:443."],
    final={"result": "NOT REACHABLE", "cause": "DENY-ALL"})


async def main():
    app = await G.build_agent()
    state = await app.ainvoke(
        {"messages": [("user", f"Troubleshoot {SRC} to {DST} on tcp:443")],
         "loops": 0},
        {"configurable": {"thread_id": "salvage-1"}})

    tool_messages = [m for m in state["messages"]
                     if getattr(m, "type", "") == "tool"]
    names = [getattr(m, "name", "") for m in tool_messages]
    print(f"\ntools actually executed: {names}")
    print(f"answer: {str(state.get('answer'))[:120]}")

    check("the run did not stop on the first prose reply",
          len(tool_messages) >= 3, str(len(tool_messages)))
    check("the CMDB was looked up for both devices",
          names.count("get_device_details") == 2, str(names))
    check("and the policy check ran", "get_firewall_path" in names, str(names))
    check("the run still reaches an answer",
          bool(str(state.get("answer") or "").strip()))
    check("the devices were recorded, so the panel has something to show",
          len(state.get("devices") or {}) >= 1,
          str(list((state.get("devices") or {}).keys())))


asyncio.run(main())
print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
