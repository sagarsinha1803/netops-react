# local_probe_mcp.py -- ping / traceroute FROM THE AGENT MACHINE.
#
# The fallback for addresses the CMDB does not know: with no source device
# record there is nothing to SSH to and no region for Tufin's topology, so the
# only probe left is from where the agent itself runs. The report must say so --
# "reachable from the agent host" is a different claim from "reachable from the
# source device".
#
# Safety model: the model never writes a command string here. The tools take an
# IP ADDRESS as their only free argument, validated with ipaddress, and the
# command line is assembled in code from fixed flags -- so there is nothing to
# inject and nothing for the allowlist to reject. The graph still gates both
# tools behind human approval (the "local" server is in DEVICE_SERVERS).
import ipaddress
import platform
import subprocess
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("local-probe-server")

_WINDOWS = platform.system() == "Windows"

# bounded exactly like the device traceroute: 5 hops, 1s, no DNS
_MAX_HOPS = 5
_PING_COUNT = 3
_TIMEOUT_S = 30


def _valid_ip(value: str) -> str:
    """The address as a string, or raise -- the only user-controlled argument."""
    return str(ipaddress.ip_address(str(value).strip()))


def _run(argv: list) -> str:
    """Run one probe; answer as "# command\\noutput" text, the same shape the
    ssh server sends. A dict here arrives at the client as a JSON string with
    ESCAPED newlines, and every line-anchored parser (traceroute hops, the
    entity patterns) then silently matches nothing."""
    cmd = " ".join(argv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_TIMEOUT_S)
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return f"# {cmd}\n{out.strip()}"
    except subprocess.TimeoutExpired:
        return f"# {cmd}\n(timed out after {_TIMEOUT_S}s)"
    except Exception as ex:
        return f"# {cmd}\nerror: {ex}"


@mcp.tool(
    name="local_ping",
    description="Ping a destination FROM THE AGENT MACHINE. Use ONLY when "
                "the source is not in the CMDB. dest must be an IP address.")
def local_ping(
    dest: Annotated[str, Field(description="destination IPv4/IPv6 address")],
    count: Annotated[int, Field(description="echo requests to send (1-5)")] = _PING_COUNT,
) -> str:
    try:
        addr = _valid_ip(dest)
    except ValueError:
        return f"'{dest}' is not a valid IP address"
    n = str(max(1, min(int(count or _PING_COUNT), 5)))
    argv = (["ping", "-n", n, "-w", "1000", addr] if _WINDOWS
            else ["ping", "-c", n, "-W", "1", addr])
    return _run(argv)


@mcp.tool(
    name="local_traceroute",
    description="Traceroute FROM THE AGENT MACHINE, bounded to 5 hops. Use "
                "ONLY when the source is not in the CMDB. dest is an IP address.")
def local_traceroute(
    dest: Annotated[str, Field(description="destination IPv4/IPv6 address")],
    max_hops: Annotated[int, Field(description="hop limit (1-10)")] = _MAX_HOPS,
) -> str:
    try:
        addr = _valid_ip(dest)
    except ValueError:
        return f"'{dest}' is not a valid IP address"
    m = str(max(1, min(int(max_hops or _MAX_HOPS), 10)))
    argv = (["tracert", "-d", "-h", m, "-w", "1000", addr] if _WINDOWS
            else ["traceroute", "-n", "-m", m, "-w", "1", "-q", "1", addr])
    return _run(argv)


if __name__ == "__main__":
    mcp.run()   # stdio
