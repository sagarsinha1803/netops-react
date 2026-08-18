"""Test MCP server (FastMCP) -- small tools for exercising the UI/tool loop.

No devices, no credentials: calculator/word_length plus mock network tools that
mimic the shape of the real unicorn + ssh MCPs.

    python tools_mcp.py            # stdio

The agent connects via MultiServerMCPClient:
    "tools": {"command": "python", "args": ["tools_mcp.py"], "transport": "stdio"}
"""
import re

from fastmcp import FastMCP

mcp = FastMCP("tools")

# canned inventory, mirrors the real CMDB response shape
_DEVICES = {
    "10.10.1.20":  {"name": "APP-SRV-DC1-020", "dc": "DC1", "zone": "APP-DC1",
                    "vendor": "cisco", "os": "IOS-XE", "region": "INDIA"},
    "172.20.5.10": {"name": "PAY-API-DC2-010", "dc": "DC2", "zone": "PAYMENT-DC2",
                    "vendor": "cisco", "os": "NX-OS", "region": "INDIA"},
    "10.10.1.1":   {"name": "Leaf-101", "dc": "DC1", "zone": "APP-DC1",
                    "vendor": "cisco", "os": "NX-OS", "region": "INDIA"},
}
_UNREACHABLE = {"172.20.5.10"}     # ping fails for these -> exercises the fail path


@mcp.tool()
def calculator(expr: str) -> str:
    """Evaluate a math expression, e.g. '23*7+1'."""
    try:
        return str(eval(expr, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def word_length(word: str) -> str:
    """Count the characters in a word."""
    return str(len(word.strip().strip("'\"")))


@mcp.tool()
def get_device(device_name: str) -> dict:
    """Look up a device in the (mock) CMDB by name or IP. Returns vendor, os, region."""
    dev = _DEVICES.get(device_name.strip())
    if not dev:
        return {"found": False, "device_name": device_name}
    return {"found": True, "region": dev["region"], "data": dev}


@mcp.tool()
def ping_device(source: str, dest: str, count: int = 3) -> str:
    """Ping dest from source (mock). Returns Cisco-style ping output."""
    return _ping(dest, count)


@mcp.tool()
def traceroute_device(source: str, dest: str) -> str:
    """Traceroute from source to dest (mock). Returns Cisco-style traceroute output."""
    return _traceroute(dest)


def _traceroute(dest: str) -> str:
    body = ("  1 Leaf-101 (10.10.1.1) 1 msec\n"
            "  2 Border-Router-01 (10.10.0.1) 2 msec\n"
            "  3 FW-DC1-EDGE-01 (10.10.255.1) 3 msec\n")
    tail = ("  4 * * *\n  5 * * *" if dest in _UNREACHABLE
            else f"  4 {dest} 4 msec")
    return f"Tracing the route to {dest}\n{body}{tail}"


def _ping(dest: str, count: int = 3) -> str:
    if dest in _UNREACHABLE:
        return (f"Sending {count}, 100-byte ICMP Echos to {dest}, timeout is 2 seconds:\n"
                + "." * count + f"\nSuccess rate is 0 percent (0/{count})")
    return (f"Sending {count}, 100-byte ICMP Echos to {dest}, timeout is 2 seconds:\n"
            + "!" * count
            + f"\nSuccess rate is 100 percent ({count}/{count}), "
              "round-trip min/avg/max = 1/1/2 ms")


# Canned replies for the deeper checks, as IOS-XE would answer them. The story
# is consistent with the traceroute above: the route is present, the forwarding
# entry is programmed and the next hop is alive, so nothing local is wrong --
# the edge firewall's ACL is what drops it.
_DEEP = [
    (r"^show\s+(ip\s+)?route\s+(?P<ip>\S+)",
     "Routing entry for 172.20.0.0/16\n"
     "  Known via \"bgp 65001\", distance 20, metric 0\n"
     "  Routing Descriptor Blocks:\n"
     "  * 10.10.1.1, from 10.10.1.1, via TenGigE0/0/0/1\n"
     "      Route metric is 0"),
    (r"^show\s+vrf",
     "No VRFs configured. All interfaces are in the global routing table."),
    (r"^show\s+(ip\s+)?cef\s+",
     "172.20.0.0/16, version 84, cached adjacency 10.10.1.1\n"
     "  via 10.10.1.1, TenGigE0/0/0/1, 3 dependencies\n"
     "    next hop 10.10.1.1, TenGigE0/0/0/1"),
    (r"^show\s+arp",
     "Protocol  Address      Age (min)  Hardware Addr   Type  Interface\n"
     "Internet  10.10.1.1            4  0050.56be.1a2b  ARPA  TenGigE0/0/0/1"),
    (r"^show\s+interfaces?\s+",
     "TenGigE0/0/0/1 is up, line protocol is up\n"
     "  0 input errors, 0 CRC, 0 output drops"),
    (r"^show\s+access-lists?",
     "ipv4 access-list EDGE-OUT\n"
     " 30 permit tcp any 10.20.0.0/16\n"
     " 40 deny tcp any host 172.20.5.10 (1842 matches)\n"
     " 50 permit ipv4 any any"),
    (r"^show\s+bgp",
     "BGP routing table entry for 172.20.0.0/16\n"
     "  Not advertised to any peer\n"
     "  Local, from 10.10.1.1 (10.10.1.1)\n"
     "    Origin IGP, metric 0, localpref 100, valid, internal, best"),
    (r"^show\s+mpls",
     "No MPLS forwarding entry for that prefix (no label switching on this path)."),
]


@mcp.tool(
    name="execute_query_on_server",
    description="Run read-only CLI commands on a device over SSH (mock). "
                "Same signature as the real ssh MCP: device_ip, commands (a "
                "LIST), region, port.")
def execute_query_on_server(device_ip: str, commands: list, region: str = "",
                            port: int = 22) -> str:
    """Mock of the office ssh tool, so a local run exercises the real code path.

    Answers ping/traceroute plus the escalation `show` commands the agent
    reaches for when a ping fails. Anything else comes back as the platform's
    invalid-input error rather than silently succeeding.
    """
    if isinstance(commands, str):
        commands = [commands]

    out = []
    for raw in commands:
        cmd = str(raw).strip()
        low = cmd.lower()
        body = None

        m = re.match(r"^(execute\s+)?ping\s+(?:vrf\s+\S+\s+)?(\S+)", low)
        if m:
            body = _ping(m.group(2))
        elif re.match(r"^(traceroute|tracert|tracepath)\b", low):
            m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", low)
            body = _traceroute(m.group(1) if m else "unknown")
        else:
            for pattern, reply in _DEEP:
                if re.match(pattern, low):
                    body = reply
                    break

        if body is None:
            body = f"% Invalid input detected at '^' marker.\n  {cmd}"
        out.append(f"{device_ip}# {cmd}\n{body}")

    return "\n\n".join(out)


if __name__ == "__main__":
    # stdio transport: stdout must be pure JSON-RPC -> silence the banner
    mcp.run(transport="stdio", show_banner=False)
