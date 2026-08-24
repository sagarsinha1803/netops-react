"""Small shared helpers: flattening MCP results and describing a tool call.

Used by both the graph and the UI, so the approval prompt, the audit trail and
the panel can never describe the same command differently.
"""
import json


def _cli_result(item: dict) -> str:
    """Render one {cmd, stdout, stderr, rc} entry as the CLI session it was.

    execute_query_on_server answers with a list of these. Falling through to
    str() gave a PYTHON REPR, so every newline arrived as a literal \\r\\n --
    the model had to read escaped output, and every line-anchored pattern in
    entities.py (VRF rows, the prompt echo, CDP neighbours) silently matched
    nothing, because as far as a regex was concerned the whole reply was one
    line. That is how VRF names reached the paste unmasked.
    """
    parts = []
    cmd = item.get("cmd") or item.get("command")
    if cmd:
        parts.append(f"# {cmd}")
    for key in ("stdout", "output", "result"):
        body = item.get(key)
        if body:
            parts.append(str(body))
            break
    err = item.get("stderr")
    if err:
        parts.append(str(err))
    if not parts:
        return str(item)
    text = "\n".join(parts)
    # devices send CRLF; normalise so "^" anchors behave
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _one(item) -> str:
    if isinstance(item, dict):
        if "text" in item:
            return str(item["text"])
        if any(k in item for k in ("cmd", "command", "stdout", "output")):
            return _cli_result(item)
        return str(item)
    if hasattr(item, "text"):
        return str(item.text)
    return str(item)


def tool_text(result) -> str:
    """Flatten an MCP tool result into plain text.

    MCP returns content blocks -- [{'type': 'text', 'text': '...'}] -- and the
    ssh tool returns a list of per-command dicts. A raw str() of either keeps
    the repr escaping, which breaks the output parsers, the maskers and what the
    model gets to read.
    """
    if isinstance(result, str):
        return result.replace("\r\n", "\n")
    if isinstance(result, (list, tuple)):
        return "\n".join(_one(item) for item in result)
    return _one(result)


def commands_of(args: dict) -> list:
    """Pull the command strings out of a device tool's arguments."""
    cmds = args.get("commands") or args.get("command") or []
    if isinstance(cmds, str):
        cmds = [cmds]
    return [str(c) for c in cmds]


def display_command(name: str, args: dict) -> str:
    """The command as it will run, for the approval prompt and the UI.

    Tools that take a command string give it directly. Tools that take
    source/dest instead (ping_device, traceroute_device) get the equivalent CLI
    written out, so a reviewer always sees a command rather than a dict of
    addresses.
    """
    cmds = commands_of(args)
    if cmds:
        return "; ".join(cmds)
    dest = args.get("dest") or args.get("destination")
    if dest:
        low = name.lower()
        if "trace" in low:
            return f"traceroute {dest}"
        if "ping" in low:
            count = args.get("count")
            return f"ping {dest}" + (f" repeat {count}" if count else "")
        return f"{args.get('source')} -> {dest}"
    # Anything else: name the TOOL, not just its arguments. A card reading
    # {"device_name": "172.20.5.10"} on ? asks a reviewer to approve something
    # it never identifies -- and the honest answer to that is always "reject".
    inner = ", ".join(f"{k}={v}" for k, v in args.items()) if args else ""
    return f"{name}({inner})"


def describe_call(name: str, args: dict) -> str:
    """A plain description of a step, for when the model attached no words.

    Most models answer a tool call with tool_calls and NOTHING else -- content
    is empty, because everything they had to say is in the arguments. The
    activity feed then has nothing to show under "show reasoning", so a run
    that explains itself perfectly well in the transcript looks silent.

    This is a DESCRIPTION, not reasoning, and the panel labels it as such: it
    says what the step is, never why it was chosen, because only the model
    knows that and this one did not say.
    """
    args = args or {}
    who = args.get("device_name") or args.get("device_ip") or args.get("source")
    if name == "get_device_details":
        return f"CMDB lookup for {who}." if who else "CMDB lookup."
    if name == "get_firewall_path":
        service = args.get("service") or "any"
        return (f"Policy check with Tufin: {args.get('src', '?')} to "
                f"{args.get('dst', '?')} on {service}.")
    if name.startswith("get_alert"):
        return f"Open alerts for {who}." if who else "Open alerts."
    if name.startswith("local_"):
        dest = args.get("dest") or args.get("destination") or "?"
        return f"Probe to {dest} from the agent host, not from the source."
    cmds = commands_of(args)
    if cmds:
        return f"Read-only command on {who or 'the device'}: {'; '.join(cmds)}"
    return f"{name} on {who}." if who else f"{name}."
