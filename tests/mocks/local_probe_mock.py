# local_probe_mock.py -- deterministic local probes, in WINDOWS output format
# on purpose: that is what the real tool produces on the agent machine, so the
# parsers are exercised against the shape they will actually meet.
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("local-probe-server")

# addresses that answer; everything else times out
_ALIVE = {"8.8.8.8", "1.1.1.1", "192.0.2.10"}

_PING_OK = """Pinging {dest} with 32 bytes of data:
Reply from {dest}: bytes=32 time=12ms TTL=57
Reply from {dest}: bytes=32 time=11ms TTL=57
Reply from {dest}: bytes=32 time=12ms TTL=57

Ping statistics for {dest}:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 11ms, Maximum = 12ms, Average = 11ms"""

_PING_DEAD = """Pinging {dest} with 32 bytes of data:
Request timed out.
Request timed out.
Request timed out.

Ping statistics for {dest}:
    Packets: Sent = 3, Received = 0, Lost = 3 (100% loss),"""

_TRACE_OK = """Tracing route to {dest} over a maximum of 5 hops

  1    <1 ms    <1 ms    <1 ms  192.168.1.1
  2     3 ms     2 ms     3 ms  100.72.16.1
  3    11 ms    10 ms    11 ms  {dest}

Trace complete."""

_TRACE_DEAD = """Tracing route to {dest} over a maximum of 5 hops

  1    <1 ms    <1 ms    <1 ms  192.168.1.1
  2     *        *        *     Request timed out.
  3     *        *        *     Request timed out.
  4     *        *        *     Request timed out.
  5     *        *        *     Request timed out.

Trace complete."""


@mcp.tool(name="local_ping",
          description="Ping a destination FROM THE AGENT MACHINE (mock).")
def local_ping(
    dest: Annotated[str, Field(description="destination IP address")],
    count: Annotated[int, Field(description="echo requests")] = 3,
) -> str:
    body = (_PING_OK if dest in _ALIVE else _PING_DEAD).format(dest=dest)
    # a string, like the real server: a dict would arrive as escaped JSON
    return f"# ping -n {count} -w 1000 {dest}\n{body}"


@mcp.tool(name="local_traceroute",
          description="Traceroute FROM THE AGENT MACHINE (mock).")
def local_traceroute(
    dest: Annotated[str, Field(description="destination IP address")],
    max_hops: Annotated[int, Field(description="hop limit")] = 5,
) -> str:
    body = (_TRACE_OK if dest in _ALIVE else _TRACE_DEAD).format(dest=dest)
    return f"# tracert -d -h {max_hops} -w 1000 {dest}\n{body}"


if __name__ == "__main__":
    mcp.run()   # stdio
