"""An API model must see stand-ins, and the graph must see reality.

The clipboard relay masked its own prompt; nothing masked an API model, so
LLM_MODE=api sent real addresses and hostnames to the endpoint while the UI
still reported masking as on. This pins the round trip in both directions.
"""
import os
import sys
from typing import Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MASK_IPS", "1")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (AIMessage, HumanMessage,  # noqa: E402
                                     SystemMessage, ToolMessage)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from agent import entities  # noqa: E402
from agent.llm import ip_mask  # noqa: E402
from agent.llm.masked_llm import MaskedChatModel  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class Recorder(BaseChatModel):
    """A stand-in endpoint: records what it was sent, replies with a tool call.

    bind_tools is deliberately NOT overridden. The real ChatOpenAI returns a
    RunnableBinding from it, whose _generate is reached through __getattr__ and
    silently drops the bound kwargs -- so a wrapper that binds the inner model
    offers the endpoint no tools at all, and the agent runs nothing. Inheriting
    the base implementation reproduces that, which is what the kwargs check
    below is for.
    """

    seen: list = []
    kwargs_seen: list = []
    reply: Any = None

    @property
    def _llm_type(self) -> str:
        return "recorder"

    def _generate(self, messages: List, stop: Optional[List[str]] = None,
                  run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(messages)
        self.kwargs_seen.append(kwargs)
        return ChatResult(generations=[ChatGeneration(message=self.reply)])


REAL_SRC, REAL_DST, REAL_HOP = "10.10.1.20", "172.20.5.10", "10.10.1.1"
CMDB = ('{"region":"INDIA","data":{"name":"APP-SRV-DC1-020",'
        '"managementIp":"10.10.1.20"}}')
CLI = ("APP-SRV-DC1-020#show route vrf PAYMENTS-PROD 172.20.5.10\n"
       "  10.10.1.1, from 10.10.1.1, via TenGigE0/0/0/1\n")

mask = ip_mask.session_mask()
entities.learn(mask, "get_device_details", CMDB)
entities.learn(mask, "execute_query_on_server", CLI)

# what the endpoint will be asked, and what it answers with -- in STAND-INS,
# the way a model that only ever saw stand-ins would answer
masked_dst = mask.mask(REAL_DST)
recorder = Recorder(reply=AIMessage(
    content=f"Pinging {masked_dst} from {mask.mask('APP-SRV-DC1-020')}.",
    tool_calls=[{"name": "execute_query_on_server", "id": "call_1",
                 "type": "tool_call",
                 "args": {"device_ip": mask.mask(REAL_SRC),
                          "commands": [f"ping {masked_dst} repeat 3"],
                          "region": "INDIA"}}]))

TOOL = {"type": "function",
        "function": {"name": "execute_query_on_server",
                     "description": "Run read-only CLI on a device",
                     "parameters": {"type": "object",
                                    "properties": {"device_ip": {"type": "string"}},
                                    "required": ["device_ip"]}}}

llm = MaskedChatModel(inner=recorder)
bound = llm.bind_tools([TOOL])             # binding must not unwrap the masking
check("bind_tools keeps the wrapper", isinstance(bound, MaskedChatModel))

conversation = [
    SystemMessage(content="You are a Network Operations troubleshooting agent."),
    HumanMessage(content=f"troubleshoot {REAL_SRC} to {REAL_DST}"),
    AIMessage(content="Looking up the source.",
              tool_calls=[{"name": "get_device_details", "id": "c0",
                           "type": "tool_call",
                           "args": {"device_name": REAL_SRC}}]),
    ToolMessage(content=CMDB, name="get_device_details", tool_call_id="c0"),
    ToolMessage(content=CLI, name="execute_query_on_server", tool_call_id="c1"),
]
result = bound.invoke(conversation)

# ---- outgoing: nothing real reached the endpoint --------------------------
sent = recorder.seen[-1]
blob = "\n".join(str(m.content) for m in sent)
for m in sent:                                    # tool call args travel too
    for call in getattr(m, "tool_calls", None) or []:
        blob += "\n" + str(call.get("args"))

secrets = [REAL_SRC, REAL_DST, REAL_HOP, "APP-SRV-DC1-020",
           "PAYMENTS-PROD", "TenGigE0/0/0/1"]
leaked = [s for s in secrets if s in blob]
check("no real value reaches the endpoint", not leaked, f"leaked={leaked}")
check("the whole history is masked, not just the last message",
      REAL_SRC not in blob and CMDB not in blob)
check("stand-ins did arrive", mask.mask(REAL_DST) in blob)

# The tools must survive the wrapper. Losing them is silent: the endpoint is
# simply never offered any, the model answers in prose, and the agent runs
# nothing while looking like it is working.
sent_kwargs = recorder.kwargs_seen[-1]
check("bound tools reach the endpoint",
      [t["function"]["name"] for t in sent_kwargs.get("tools") or []]
      == ["execute_query_on_server"], str(sent_kwargs.get("tools")))

# ---- incoming: the graph gets reality back --------------------------------
check("reply content is unmasked", REAL_DST in str(result.content),
      str(result.content))
args = result.tool_calls[0]["args"]
check("tool call device_ip is unmasked", args["device_ip"] == REAL_SRC,
      str(args["device_ip"]))
check("the command inside the args is unmasked",
      args["commands"][0] == f"ping {REAL_DST} repeat 3", str(args["commands"]))
check("an argument that was never masked survives", args["region"] == "INDIA")

# ---- the guard must see the real command ----------------------------------
from agent.guards import check_command  # noqa: E402
check("the unmasked command passes the read-only guard",
      check_command(args["commands"][0]) is None)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
