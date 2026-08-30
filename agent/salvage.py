"""Rescue a tool call a model wrote as prose instead of calling.

Not every endpoint that accepts an OpenAI `tools` array actually emits tool
calls. The VS Code Copilot bridge passes the schemas to `vscode.lm`, but
whether a call comes back as a `LanguageModelToolCallPart` is the model's
decision -- and a model that decides not to writes the call out instead:

    I'll first call the `get_device_details` function for DC1-EDGE-RTR-01.
    ```json
    {"device_name": "DC1-EDGE-RTR-01"}
    ```

The graph sees an assistant message with no tool calls, treats it as the final
answer, and the run ends on its first step with every stage grey and a
Conclusion tick. Nothing failed, so nothing is reported -- the worst shape a
failure can take.

So read it. A named tool plus a JSON object is a tool call written in prose,
and executing it changes nothing about safety: the salvaged call goes through
the same read-only allowlist and the same human approval as any other.
"""
import json
import re

from agent.llm.clipboard_llm import _extract_json

# ```json ... ``` first, then any bare {...}
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

# A model saying it is about to do the thing, instead of doing it. Present
# tense or future, first person -- the wording every model reaches for when it
# narrates a step it then forgets to take.
_ANNOUNCING = re.compile(
    r"\b(?:i(?:'| a)?m going to|i will|i'll|let me|let's|now[, ]|next[, ]|"
    r"start(?:ing)? (?:with|by)|proceed(?:ing)? with|"
    r"call(?:ing)?|invok(?:e|ing)|us(?:e|ing)|run(?:ning)?)\b", re.I)


def _args_from(text: str):
    """The first JSON object in the text, whatever it is wrapped in."""
    fenced = _FENCED.search(text)
    if fenced:
        try:
            got = json.loads(fenced.group(1))
            if isinstance(got, dict):
                return got
        except Exception:                       # noqa: BLE001
            pass
    got = _extract_json(text)                   # tolerates smart quotes, prose
    return got if isinstance(got, dict) else None


def _arg_keys(tools, name):
    """The argument names a tool takes, if the caller passed the tools.

    `tools` is usually the graph's {name: tool} map; a bare set of names is
    accepted too, and simply means there is no schema to match against.
    """
    tool = tools.get(name) if hasattr(tools, "get") else None
    if tool is None:
        return set()
    args = getattr(tool, "args", None)
    if isinstance(args, dict) and args:
        return set(args)
    schema = getattr(tool, "args_schema", None)
    try:
        return set(schema.model_json_schema().get("properties", {}))
    except Exception:                           # noqa: BLE001
        return set()


def salvage_tool_call(text: str, tool_names):
    """(name, args) for a tool call written as prose, or None.

    `tool_names` is what the model was actually offered: a name it invented is
    not a call, it is a hallucination, and running it is not this function's
    business.
    """
    body = str(text or "")
    if not body.strip():
        return None

    args = _args_from(body)
    if args is None:
        return None

    # the protocol the clipboard relay uses: {"tool": ..., "args": {...}}
    named = args.get("tool") or args.get("name") or args.get("function")
    if isinstance(named, str) and named in tool_names:
        inner = args.get("args") or args.get("arguments") or {}
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:                   # noqa: BLE001
                inner = {}
        return (named, inner if isinstance(inner, dict) else {})
    if isinstance(named, str) and named not in tool_names:
        return None                             # a tool nobody offered

    if not args or "tool" in args or "args" in args:
        return None                             # not an argument object

    # Otherwise the name is in the prose: "call the `get_device_details`
    # function". Which one it is comes from the ARGUMENTS, not from where the
    # words fell: a model that lists the workflow first -- "get_device_details,
    # then execute_query_on_server, then get_firewall_path" -- mentions three,
    # and only one of them takes a device_name.
    hits = [(body.rfind(name), name) for name in tool_names if name in body]
    hits = [h for h in hits if h[0] >= 0]
    if not hits:
        return None

    keys = set(args)
    scored = []
    for position, name in hits:
        wanted = _arg_keys(tool_names, name)
        # unknown schema scores 0 and loses to any tool that actually fits
        fit = len(keys & wanted) - len(keys - wanted) if wanted else 0
        scored.append((fit, position, name))
    best = max(scored)
    if best[0] <= 0 and len(scored) > 1:
        return None                             # nothing fits: do not guess
    return (best[2], args)


def looks_like_a_call(text, tool_names):
    """True when a reply reads like a tool call that would not parse.

    The difference that matters: a model that has finished says so in prose or
    in a report, and a model in the middle of an escalation writes an object
    naming a tool. When that object will not parse -- one brace too many, a
    paste cut short -- the graph sees no tool calls and reads the reply as the
    final answer, so the run stops mid-ladder and prints the braces where the
    report belongs. Worth one more ask before accepting that as the end.
    """
    body = str(text or "")
    if not body.strip():
        return False
    # A reply that reached a VERDICT is finished, whatever else it mentions.
    # Re-asking one of those would talk a model out of an answer it had.
    if re.search(r'"result"\s*:|\b(NOT )?REACHABLE\b|\bINCONCLUSIVE\b', body):
        return False

    named = [n for n in tool_names if n in body]

    # Shape one: an object naming a tool that will not parse -- a brace too
    # many, a paste cut short.
    if "{" in body and _args_from(body) is None:
        if '"tool"' in body or named:
            return True

    # Shape two, and the more embarrassing: no object at all. The model
    # ANNOUNCES the step in prose -- "Now, I will proceed with the first step.
    # Calling get_device_details for EDGE-A1." -- and calls nothing. Every
    # stage stays grey and the announcement becomes the final answer, which
    # reads as a run that decided a device lookup was the whole job.
    return bool(named and _ANNOUNCING.search(body))
