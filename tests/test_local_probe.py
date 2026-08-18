"""Windows-format probe output must parse, and the local server must be gated.

The local fallback runs ping/tracert on the agent machine, which on this
estate is Windows -- output the parsers never saw when only devices answered.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C                       # noqa: E402
from agent import vendors                              # noqa: E402
from agent.utils import display_command, tool_text     # noqa: E402
from mocks.local_probe_mock import (_PING_DEAD, _PING_OK,  # noqa: E402
                                    _TRACE_DEAD, _TRACE_OK)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- ping_ok on Windows output ----------------------------------------------
ok = _PING_OK.format(dest="8.8.8.8")
dead = _PING_DEAD.format(dest="203.0.113.9")
check("windows ping success parses as reachable", vendors.ping_ok(ok))
check("windows ping timeout parses as failed", not vendors.ping_ok(dead))
check("windows 'destination host unreachable' is failed even when 'received'",
      not vendors.ping_ok(
          "Reply from 192.168.1.1: Destination host unreachable.\n"
          "    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss)"))
# the shapes that already worked must keep working
check("cisco success still parses", vendors.ping_ok("Success rate is 100 percent"))
check("cisco failure still parses", not vendors.ping_ok("Success rate is 0 percent"))
check("linux success still parses",
      vendors.ping_ok("3 packets transmitted, 3 received, 0% packet loss"))

# ---- parse_hops on tracert output -------------------------------------------
hops = vendors.parse_hops(_TRACE_OK.format(dest="8.8.8.8"))
check("tracert full path: 3 hops", len(hops) == 3, str(hops))
check("tracert hop ip read from the ms columns",
      hops and hops[0]["ip"] == "192.168.1.1" and not hops[0]["timeout"],
      str(hops[:1]))

hops = vendors.parse_hops(_TRACE_DEAD.format(dest="203.0.113.9"))
check("tracert 'Request timed out.' rows are timeouts",
      len(hops) == 5 and hops[1]["timeout"] and not hops[0]["timeout"], str(hops))
check("dead trace: first failed hop is the gateway",
      (vendors.first_failed_hop(hops) or {}).get("ip") == "192.168.1.1")

# windows can also name a hop: "name [ip]"
named = vendors.parse_hops("  1     2 ms    1 ms    2 ms  gw.example.net [10.0.0.1]")
check("tracert 'name [ip]' form keeps the name",
      named and named[0]["host"] == "gw.example.net" and named[0]["ip"] == "10.0.0.1",
      str(named))

# ---- the mock answers in the ssh tool's shape --------------------------------
flat = tool_text([{"cmd": "ping -n 3 -w 1000 8.8.8.8",
                   "stdout": ok, "rc": 0}])
check("probe result flattens like an ssh result",
      flat.startswith("# ping -n 3") and "Received = 3" in flat, flat[:40])

# ---- wiring ------------------------------------------------------------------
check("local server is registered", "local" in C.MCP_SERVERS)
check("local probes are approval-gated", "local" in C.DEVICE_SERVERS)
check("local tools are traced as device tools",
      {"local_ping", "local_traceroute"} <= C.DEVICE_TOOL_NAMES)
check("approval prompt shows a command, not a dict",
      display_command("local_ping", {"dest": "8.8.8.8", "count": 3})
      .startswith("ping 8.8.8.8"))

# the guard must not reject the local tools for having no command strings
from agent.guards import check_command                 # noqa: E402
from agent.utils import commands_of                    # noqa: E402
check("guard passes an argument-only tool call",
      next((e for e in (check_command(c)
                        for c in commands_of({"dest": "8.8.8.8"})) if e), None)
      is None)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
