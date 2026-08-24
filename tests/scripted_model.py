"""A scripted model, for tests only.

Not a backend. It has no port, no HTTP, and nothing can point at it: it is a
LangChain chat model that tests drop into the graph in place of the real one,
so the agent's own machinery -- the tool loop, the approval gate, the panel,
the alert step, the budget -- can be tested without a model at all.

The demo and every real run use the VS Code Copilot bridge or the clipboard
relay. This exists so that a regression in the agent is caught by a test rather
than in front of an audience.

Usage:

    from agent import graph as G
    G.llm = ScriptedModel([("get_device_details", {"device_name": "SW-1"})],
                          thoughts=["Looking it up."], final={...})
    app = await G.build_agent()

A step may be a (name, args) pair, or a callable taking the conversation so far
and returning one -- which is how a step depends on what an earlier tool
answered, the way a real model's would.
"""
import json
import re
from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

# the hostname a CMDB reply carries, and what it was looked up by
_NAME_IN_RESULT = re.compile(r'"name"\s*:\s*"([^"]+)"')
_QUERY_IN_RESULT = re.compile(r'"query"\s*:\s*"([^"]+)"')


class ScriptedModel(BaseChatModel):
    """Replays a fixed sequence of tool calls, then answers."""

    script: List[Any] = []
    thoughts: List[str] = []
    final: Any = "done"
    bound: List[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        # the graph binds tools; record them so a test can assert the agent
        # offered what it should, and keep returning self so the script runs
        self.bound = [getattr(t, "name", str(t)) for t in tools]
        return self

    # ---- what the conversation has already produced -----------------------
    @staticmethod
    def _turn(messages: List[BaseMessage]) -> List[BaseMessage]:
        """Only the current turn: a second question replays from the start."""
        last_human = max((i for i, m in enumerate(messages)
                          if getattr(m, "type", "") == "human"), default=0)
        return list(messages[last_human:])

    @staticmethod
    def cmdb_names(messages: List[BaseMessage]) -> List[str]:
        """The device names the CMDB has returned so far, in lookup order.

        A step that needs a device name takes it from here rather than from the
        request: Archangel is keyed by name, masking means the model never sees
        the real address anyway, and this is what the real model does.

        A lookup BY NAME comes back without a `name` field -- it would only
        echo the caller's own input -- so fall back to the `query` the record
        was found by, which is that same name.
        """
        out = []
        for m in messages:
            if getattr(m, "type", "") != "tool":
                continue
            body = str(getattr(m, "content", ""))
            found = _NAME_IN_RESULT.search(body) or _QUERY_IN_RESULT.search(body)
            if found and found.group(1) not in out:
                out.append(found.group(1))
        return out

    # ---- the model itself --------------------------------------------------
    def _generate(self, messages: List[BaseMessage],
                  stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        turn = self._turn(messages)
        done = sum(1 for m in turn if getattr(m, "type", "") == "tool")

        if done < len(self.script):
            step = self.script[done]
            name, args = step(messages) if callable(step) else step
            thought = (self.thoughts[done] if done < len(self.thoughts)
                       else f"Running {name}.")
            message = AIMessage(
                content=thought,
                tool_calls=[{"name": name, "args": dict(args),
                             "id": f"call_{done}"}])
        else:
            answer = self.final
            message = AIMessage(content=answer if isinstance(answer, str)
                                else json.dumps(answer))

        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages: List[BaseMessage],
                         stop: Optional[List[str]] = None,
                         run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return self._generate(messages, stop, None, **kwargs)


def ssh(device, command, region="INDIA"):
    """One read-only command on a device, as the ssh tool takes it."""
    return ("execute_query_on_server",
            {"device_ip": device, "region": region, "commands": [command]})


def alerts_for(index):
    """Ask Archangel about the index'th device the CMDB returned.

    A callable step, because the name is not known until the lookup has
    happened -- which is the whole reason the real model has to read it off
    the record.
    """
    def step(messages):
        names = ScriptedModel.cmdb_names(messages)
        who = names[index] if index < len(names) else "UNKNOWN"
        return ("get_alert_and_ticket_details_from_archangel",
                {"device_name": who})
    return step
