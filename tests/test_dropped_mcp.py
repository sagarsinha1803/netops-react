"""An MCP server that drops the connection has not answered anything.

The SSH MCP server is reached over SSE, which is two HTTP requests: a
long-lived stream and the posts that ride alongside it. Anything in between --
the server recycling a worker, a proxy's idle timeout, a keep-alive expiring --
can close them, and the SDK surfaces that as a traceback ending in

    httpx.RemoteProtocolError: Server disconnected without sending a response.

The next attempt opens a new connection and usually just works. Treating the
first one as "that server is down" throws away a run over a hiccup -- and worse,
reads to the operator as though the device had refused the command.

    .venv/Scripts/python.exe tests/test_dropped_mcp.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["USE_MOCKS"] = "1"

import httpx                                                    # noqa: E402

from agent.graph import _retrying, _was_dropped                 # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class Group(Exception):
    """An exception group, the shape anyio wraps a task-group failure in."""

    def __init__(self, subs):
        self.exceptions = subs


DROPPED = httpx.RemoteProtocolError("Server disconnected without sending a "
                                    "response.")

# ---- telling a dropped connection from a refusal --------------------------
check("the one from the screenshot", _was_dropped(DROPPED))
check("wrapped in a task group", _was_dropped(Group([DROPPED])))
check("nested two deep", _was_dropped(Group([Group([DROPPED])])))
check("a connect failure counts too",
      _was_dropped(httpx.ConnectError("connection refused")))
check("and a read timeout", _was_dropped(httpx.ReadTimeout("timed out")))

check("a tool that answered NO is not a dropped connection",
      not _was_dropped(ValueError("No data found for device X")))
check("nor is a bad argument",
      not _was_dropped(TypeError("get_firewall_path() needs src and dst")))
check("nor is a missing script",
      not _was_dropped(FileNotFoundError("unicorn_mcp.py")))


# ---- and what the retry does with each ------------------------------------
async def main():
    tries = {"n": 0}

    async def flaky():
        tries["n"] += 1
        if tries["n"] < 3:
            raise DROPPED
        return "the route table"

    got = await _retrying(flaky, delay=0)
    check("a connection that drops twice still gets its answer",
          got == "the route table", str(got))
    check("and it took three attempts to get it", tries["n"] == 3, str(tries))

    tries["n"] = 0

    async def always():
        tries["n"] += 1
        raise DROPPED

    try:
        await _retrying(always, delay=0)
        check("a server that never answers gives up", False, "no exception")
    except httpx.RemoteProtocolError:
        check("a server that never answers gives up", True)
    check("after a bounded number of attempts, not forever",
          tries["n"] == 3, str(tries))

    tries["n"] = 0

    async def refused():
        tries["n"] += 1
        raise ValueError("No data found for device X")

    try:
        await _retrying(refused, delay=0)
    except ValueError:
        pass
    check("a real refusal is not retried -- it would only be refused again",
          tries["n"] == 1, str(tries))


asyncio.run(main())

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
