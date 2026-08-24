"""Mock of the DEVICE (ssh) MCP server: runs commands, touches no device.

It must expose only what the real one does. It used to carry a calculator, a
word_length and a get_device -- and a model, offered a get_device sitting
beside get_device_details, picked it: a CMDB lookup that lives on the device
server is device-touching by the gate's rule, so a plain inventory lookup
started asking for human approval. A mock that offers tools the real server
does not is not a mock, it is a different system.

Every answer comes from tests/mocks/scenarios.py, so the ping, the traceroute
and the deep checks agree with the CMDB, the firewall verdict and the alerts.

    python device_mock.py            # stdio
"""
import os
import re
import sys

from fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scenarios as S                                    # noqa: E402

mcp = FastMCP("tools")


def _target(command: str) -> str:
    """The address a ping/traceroute command is aimed at.

    Taking "the word after ping" pings the flag in `ping -c 4 10.0.0.1`, and
    the vrf name in `ping vrf RED 10.0.0.1`. Take the last thing in the line
    that is actually an address or a device this world knows.
    """
    words = [w.strip(",;") for w in str(command).split()]
    for word in reversed(words):
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", word) or S.resolve(word):
            return word
    return "unknown"


def _ping(dest: str, count: int = 3) -> str:
    ip = S.ip_of(dest)
    head = (f"Sending {count}, 100-byte ICMP Echos to {ip}, "
            f"timeout is 2 seconds:\n")
    if ip in S.PING_FAILS:
        return head + "." * count + f"\nSuccess rate is 0 percent (0/{count})"
    return (head + "!" * count
            + f"\nSuccess rate is 100 percent ({count}/{count}), "
              "round-trip min/avg/max = 1/1/2 ms")


def _traceroute(dest: str) -> str:
    ip = S.ip_of(dest)
    trace = S.TRACE.get(ip)
    if not trace:
        return f"Tracing the route to {ip}\n  1 * * *\n  2 * * *"
    lines = [f"  {i} {name} ({hop_ip}) {rtt}"
             for i, (name, hop_ip, rtt) in enumerate(trace["hops"], start=1)]
    if not trace["arrives"]:
        # the probes that never came back: this is what tells the agent WHERE
        # it stops, rather than merely that it failed
        nxt = len(lines) + 1
        lines += [f"  {n} * * *" for n in range(nxt, nxt + 3)]
    return f"Tracing the route to {ip}\n" + "\n".join(lines)


@mcp.tool()
def ping_device(source: str, dest: str, count: int = 3) -> str:
    """Ping dest from source (mock). Returns Cisco-style ping output."""
    return _ping(dest, count)


@mcp.tool()
def traceroute_device(source: str, dest: str) -> str:
    """Traceroute from source to dest (mock). Returns Cisco-style traceroute output."""
    return _traceroute(dest)


@mcp.tool(
    name="execute_query_on_server",
    description="Run read-only CLI commands on a device over SSH (mock). "
                "Same signature as the real ssh MCP: device_ip, commands (a "
                "LIST), region, port.")
def execute_query_on_server(device_ip: str, commands: list, region: str = "",
                            port: int = 22) -> str:
    """Mock of the office ssh tool, so a local run exercises the real code path.

    WHICH device is being asked matters: the same `show interface` is healthy
    on one source and down on another, and that difference is the whole of the
    broken scenario.
    """
    if isinstance(commands, str):
        commands = [commands]

    who = S.resolve(device_ip)
    deep = S.DEEP.get(who, S.DEFAULT_DEEP)
    prompt = who or str(device_ip)

    out = []
    for raw in commands:
        cmd = str(raw).strip()
        low = cmd.lower()
        body = None

        if re.match(r"^(execute\s+)?ping", low):
            # Honour what the model actually typed. A reply that says
            # "Sending 3" to a "repeat 5", or one that pings the flag in
            # "ping -c 4 <ip>", is the kind of small lie that makes an
            # audience stop believing the rest of the screen.
            n = re.search(r"(?:repeat|count)\s+(\d+)|-c\s*(\d+)", low)
            count = int(next(g for g in n.groups() if g)) if n else 3
            body = _ping(_target(low), max(1, min(count, 10)))
        elif re.match(r"^(traceroute|tracert|tracepath)", low):
            body = _traceroute(_target(low))
        else:
            for pattern, reply in deep:
                if re.match(pattern, low):
                    body = reply
                    break

        if body is None:
            # A real device rejects what it does not understand, and the agent
            # is built to notice that and try another syntax. Silence here
            # would teach it the command worked.
            body = f"% Invalid input detected at '^' marker.\n  {cmd}"
        out.append(f"{prompt}# {cmd}\n{body}")

    return "\n\n".join(out)


if __name__ == "__main__":
    # stdio transport: stdout must be pure JSON-RPC -> silence the banner
    mcp.run(transport="stdio", show_banner=False)
