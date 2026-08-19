"""Masking for an API model, so the backend choice does not change what leaks.

The clipboard relay masks its prompt on the way out and unmasks the reply on
the way back (see clipboard_llm._generate). Nothing did that for an API model,
so switching LLM_MODE from clipboard to api silently sent real addresses,
hostnames, ACLs and VRFs to whatever endpoint was configured -- with MASK_IPS=1
still set and the UI still reporting that masking was on.

This wraps any LangChain chat model with the same round trip:

    outgoing   real  ──mask──▶  stand-ins  ──▶ the endpoint
    incoming   stand-ins  ──unmask──▶  real  ──▶ guards, approval, the device

Both directions matter. The reply is unmasked before the graph sees it, so
check_command validates the real command, the approval prompt shows the real
device, and the SSH tool connects to the real address. The model only ever
handles stand-ins.
"""
from typing import Any, List, Optional, Sequence

from langchain_core.callbacks import (AsyncCallbackManagerForLLMRun,
                                      CallbackManagerForLLMRun)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import Field

from agent.llm import ip_mask


def _walk(value, fn):
    """Apply fn to every string inside a nested structure."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    return value


def _map_message(message: BaseMessage, fn) -> BaseMessage:
    """A copy of one message with every string in it mapped.

    The conversation is resent in full on every turn, so the history has to be
    mapped too -- not only the newest message. Tool call arguments carry
    addresses as often as the prose does (device_ip, the destination inside a
    command string), so they are mapped as well.
    """
    update: dict = {}
    if isinstance(message.content, (str, list, dict)):
        update["content"] = _walk(message.content, fn)
    calls = getattr(message, "tool_calls", None)
    if calls:
        update["tool_calls"] = [
            {**c, "name": c.get("name"), "args": _walk(c.get("args") or {}, fn)}
            for c in calls
        ]
    return message.model_copy(update=update) if update else message


class MaskedChatModel(BaseChatModel):
    """Delegates to `inner`, masking on the way out and unmasking on the way in."""

    inner: BaseChatModel = Field(...)

    @property
    def _llm_type(self) -> str:
        return f"masked-{self.inner._llm_type}"

    # ---- the round trip ---------------------------------------------------
    def _outgoing(self, messages: Sequence[BaseMessage]) -> List[BaseMessage]:
        mask = ip_mask.session_mask()
        return [_map_message(m, mask.mask) for m in messages]

    def _incoming(self, result: ChatResult) -> ChatResult:
        mask = ip_mask.session_mask()
        generations = []
        for gen in result.generations:
            message = gen.message
            if isinstance(message, AIMessage):
                message = _map_message(message, mask.unmask)
            generations.append(gen.model_copy(update={"message": message}))
        return result.model_copy(update={"generations": generations})

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = self.inner._generate(self._outgoing(messages), stop=stop,
                                      run_manager=run_manager, **kwargs)
        return self._incoming(result)

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = await self.inner._agenerate(self._outgoing(messages), stop=stop,
                                             run_manager=run_manager, **kwargs)
        return self._incoming(result)

    # ---- pass-through -----------------------------------------------------
    def bind_tools(self, tools, **kwargs):
        """Keep the wrapper on top: binding tools must not unwrap the masking.

        LangGraph calls bind_tools once per turn and uses what it returns, so a
        wrapper that handed back the bare inner model here would mask nothing.
        """
        return self.model_copy(update={"inner": self.inner.bind_tools(tools, **kwargs)})
