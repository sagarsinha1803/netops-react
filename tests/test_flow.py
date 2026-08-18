"""End-to-end test of the net_agent graph with a scripted model.

No Copilot, no devices: a fake LLM replays canned replies that carry the exact
quirks the real Copilot output has (markdown-escaped names, curly quotes, code
fences, newlines inside strings). Proves the graph, the MCP tool execution, the
guards and the state capture all work.

    uv run python tests/test_flow.py
"""
import asyncio
import os
import sys
from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
_MOCKS = os.path.join(_HERE, "mocks")

from agent import constants as C                    # noqa: E402
from agent import graph as net_agent                # noqa: E402
from agent.llm.clipboard_llm import ClipboardLLM    # noqa: E402

# --- scripted replies, deliberately mangled the way Copilot mangles them ------
SCRIPT = [
    # 1. markdown-escaped tool + arg names
    r'{"thought":"Need the source device platform first.",'
    r'"tool":"get\_device\_details","args":{"device\_name":"10.10.1.20","region":"AUTO"}}',

    # 2. curly quotes + code fence
    "```json\n"
    "{\u201cthought\u201d:\u201cNow the destination.\u201d,"
    "\u201ctool\u201d:\u201cget_device_details\u201d,"
    "\u201cargs\u201d:{\u201cdevice_name\u201d:\u201c172.20.5.10\u201d}}\n"
    "```",

    # 3. ping, IOS-XE syntax, commands as a list
    '{"thought":"Source is Cisco IOS-XE, so ping <dest> repeat 3.",'
    '"tool":"ping_device","args":{"source":"10.10.1.20","dest":"172.20.5.10","count":3}}',

    # 4. a BLOCKED command -> must be rejected by check_command, never executed
    '{"thought":"Trying a config command on purpose.",'
    '"tool":"execute_query_on_server","args":{"device_ip":"10.10.1.20",'
    '"commands":["configure terminal"],"region":"INDIA"}}',

    # 5. traceroute, with a newline inside the thought string
    '{"thought":"Ping failed.\nRunning traceroute to find the break.",'
    '"tool":"traceroute_device","args":{"source":"10.10.1.20","dest":"172.20.5.10"}}',

    # 6. final answer
    '{"thought":"Path stops after the firewall.","final":"Source: APP-SRV-DC1-020 '
    '(cisco IOS-XE)\\nPing: FAILED\\nPath: 10.10.1.20 -> Leaf-101 -> Border-Router-01 '
    '-> FW-DC1-EDGE-01 -> X\\nResult: not reachable"}',
]


_STEP = {"i": 0}          # module-level: pydantic would turn a class attr private


class ScriptedLLM(ClipboardLLM):
    """ClipboardLLM with the clipboard replaced by a canned script."""

    def _generate(self, messages: List[BaseMessage],
                  stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        prompt = self._build(messages)
        idx = _STEP["i"]
        _STEP["i"] += 1
        reply = SCRIPT[idx] if idx < len(SCRIPT) else '{"final":"done"}'
        print(f"\n--- step {idx + 1} ---")
        print(f"paste would be {len(prompt)} chars")
        return ChatResult(generations=[
            ChatGeneration(message=self._to_message(reply))])


async def main():
    # mocks only: no creds, no devices, no approval prompts
    net_agent.C.MCP_SERVERS = {
        "unicorn": {"command": sys.executable,
                    "args": [os.path.join(_MOCKS, "unicorn_mock.py")],
                    "transport": "stdio"},
        # must be keyed "ssh": DEVICE_SERVERS gates by SERVER name, so calling
        # it anything else leaves every device tool ungated and the guard
        # assertion below passes for the wrong reason
        "ssh": {"command": sys.executable,
                "args": [os.path.join(_MOCKS, "device_mock.py")],
                "transport": "stdio"},
    }
    net_agent.C.REQUIRE_APPROVAL = False
    net_agent.llm = ScriptedLLM(mode="agent", beep=False, verbose_console=False,
                                prompt_file=None)

    app = await net_agent.build_agent()
    state = await app.ainvoke({"messages": [
        ("user", "troubleshoot 10.10.1.20 to 172.20.5.10")], "loops": 0},
        {"configurable": {"thread_id": "test-1"}})

    print("\n" + "=" * 60)
    print("TOOL CALLS AND RESULTS")
    for m in state["messages"]:
        if getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                print(f"  call  {tc['name']}({tc['args']})")
        elif getattr(m, "type", "") == "tool":
            first = str(m.content).splitlines()[0] if m.content else ""
            print(f"  ->    {first[:80]}")

    print("\n" + "=" * 60)
    print("GRAPH STATE")
    print(f"  ping_ok      : {state.get('ping_ok')}")
    print(f"  hops         : {len(state.get('hops') or [])}")
    print(f"  path         : {state.get('path')}")
    print(f"  loops        : {state.get('loops')}")
    for c in state.get("commands_run") or []:
        print(f"  command      : {c}")
    print(f"\n  answer:\n{state.get('answer')}")

    # --- assertions ---------------------------------------------------------
    problems = []
    names = [tc["name"] for m in state["messages"]
             for tc in (getattr(m, "tool_calls", None) or [])]
    if "get_device_details" not in names:
        problems.append("markdown-escaped tool name did not resolve")
    if state.get("ping_ok") is not False:
        problems.append("ping_ok should be False for 172.20.5.10")
    if not state.get("path"):
        problems.append("path not parsed from traceroute")
    rejected = [m for m in state["messages"]
                if getattr(m, "type", "") == "tool"
                and str(m.content).startswith("REJECTED")]
    if not rejected:
        problems.append("'configure terminal' was NOT rejected by the guard")

    print("\n" + "=" * 60)
    if problems:
        for p in problems:
            print(f"  FAIL: {p}")
    else:
        print("  ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
