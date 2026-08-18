"""Delta mode must stay a delta across turns.

build_agent() runs on every message, so bind_tools() is called once per turn.
When that reset the "already sent" bookkeeping, every turn re-pasted the whole
conversation -- and by the deeper-checks turn the paste was long enough that
Copilot filed it as a Context_.txt attachment instead of reading it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import (AIMessage, HumanMessage,  # noqa: E402
                                     SystemMessage, ToolMessage)

import agent.llm.clipboard_llm as relay  # noqa: E402

SCHEMA = [{"type": "function", "function": {
    "name": "execute_query_on_server",
    "description": "Run read-only CLI commands on a device.",
    "parameters": {"type": "object", "properties": {
        "device_ip": {"type": "string"}, "commands": {"type": "array"}}}}}]

BIG = "\n".join(f"  {i} hop-{i}.example.net (10.10.{i}.1) {i} msec"
                for i in range(40))

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def tool_msg(i):
    return ToolMessage(content=f"result {i}\n{BIG}", name="execute_query_on_server",
                       tool_call_id=str(i))


relay._RELAY.update({"system": "", "count": 0})

llm = relay.ClipboardLLM(mode="delta", beep=False, verbose_console=False,
                         prompt_file=None)

msgs = [SystemMessage("SYSTEM PROMPT " * 50),
        HumanMessage("troubleshoot 10.10.1.20 to 172.20.5.10")]

# turn 1: a fresh binding, so the whole context goes across
first = llm.bind_tools([]).model_copy(update={"tool_schemas": SCHEMA})._build(msgs)
check("first paste carries the system prompt", "SYSTEM PROMPT" in first)

# turns 2..5: the conversation grows, each turn rebinds like build_agent does
sizes, resent = [], []
for turn in range(4):
    msgs = msgs + [AIMessage(content=f"thinking {turn}"), tool_msg(turn)]
    bound = llm.bind_tools([]).model_copy(update={"tool_schemas": SCHEMA})
    paste = bound._build(msgs)
    sizes.append(len(paste))
    resent.append("SYSTEM PROMPT" in paste)

print(f"\nfirst paste: {len(first)} chars")
print(f"later pastes: {sizes}")

# size alone proves nothing here -- each later turn carries a fresh 40-line
# traceroute, which is bigger than this test's short system prompt. What matters
# is that the context is not sent again.
check("later pastes do not resend the system prompt", not any(resent), str(resent))
check("later pastes stay small (delta, not the whole thread)",
      all(s < 4000 for s in sizes), str(sizes))
check("paste size does not grow with the conversation",
      max(sizes) - min(sizes) < 1500, f"spread {max(sizes) - min(sizes)}")

# a new Copilot chat must force a full resend again
llm.reset_conversation()
after = llm.bind_tools([]).model_copy(update={"tool_schemas": SCHEMA})._build(msgs)
check("reset_conversation resends the context", "SYSTEM PROMPT" in after)

sys.exit(1 if fails else 0)
