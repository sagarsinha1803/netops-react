"""Nothing that changes a device may reach one. Enforced in code, not by prompt.

The model chooses the commands, so the prompt is guidance, not a control. Two
gates stand between a chosen command and a device, and a command has to pass
BOTH:

  1. this allowlist (agent/guards.py) -- ping / traceroute / show / display
     only, checked in code before anything runs;
  2. a human approval, collected for the whole batch BEFORE any command
     executes, so resuming a parked run can never run something twice.

This file pins the first. If a rule here is ever relaxed, that should be a
deliberate act with a failing test in front of it -- which is the point.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C                          # noqa: E402
from agent.guards import check_command                    # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- anything that writes, reboots, clears or reconfigures -----------------
DESTRUCTIVE = [
    # entering configuration
    "configure terminal", "conf t", "config", "configure", "edit",
    # saving / wiping
    "write memory", "write erase", "wr", "erase startup-config",
    "copy running-config startup-config", "copy tftp: flash:",
    "delete flash:image.bin", "format flash:", "install add file bootflash:x",
    # restarting
    "reload", "reload in 5", "reboot", "halt", "request system reboot",
    "restart routing", "shutdown", "no shutdown",
    # state changes that look harmless
    "clear counters", "clear ip bgp *", "clear line vty 0", "clear arp-cache",
    "commit", "rollback 1", "set interfaces ge-0/0/0 disable",
    "no router bgp 65001", "username admin password secret",
    "ip route 0.0.0.0 0.0.0.0 10.0.0.1",
    # smuggled in behind something allowed
    "ping 10.0.0.1 && configure terminal",
    "show version; reload",
    "show route 10.0.0.1 | utility sh -c 'reboot'",
]
allowed_through = [c for c in DESTRUCTIVE if check_command(c) is None]
check(f"all {len(DESTRUCTIVE)} state-changing commands are refused",
      not allowed_through, f"LET THROUGH: {allowed_through}")

# ---- the read-only work the agent actually does ----------------------------
READ_ONLY = [
    "ping 10.0.0.1", "ping 10.0.0.1 repeat 3", "ping 10.0.0.1 count 3",
    "ping vrf PROD 10.0.0.1", "ping 10.0.0.1 source Loopback0",
    "traceroute 10.0.0.1 maxttl 5 timeout 1 probe 1 numeric",
    "traceroute 10.0.0.1 ttl 1 5 timeout 1 probe 1",
    "traceroute -n -m 5 -w 1 -q 1 10.0.0.1",
    "tracert -m 5 10.0.0.1",
    "show route 10.0.0.1", "show ip route 10.0.0.1", "show cef 10.0.0.1",
    "show interface Gi0/0/0/1 brief", "display interface brief",
    "show access-lists EDGE-OUT | include 10.0.0.1",
    "show arp | include 10.0.0.1",
    "show bgp vrf PROD 10.0.0.1 | include Label",
]
wrongly_blocked = [(c, check_command(c)) for c in READ_ONLY
                   if check_command(c) is not None]
check(f"all {len(READ_ONLY)} read-only commands are allowed",
      not wrongly_blocked, str(wrongly_blocked[:2]))

# ---- output that would blow the context window -----------------------------
BULK = ["show running-config", "show tech-support", "show route", "show bgp",
        "show interfaces", "show arp", "show cef", "show logging",
        "show vrf all detail"]
let_through = [c for c in BULK if check_command(c) is None]
check("whole-table dumps are refused unless narrowed", not let_through,
      str(let_through))
check("...but the same command narrowed by a prefix is fine",
      check_command("show route 10.0.0.1") is None)
check("...or filtered through a pipe",
      check_command("show arp | include 10.0.0.1") is None)

check("an empty command is refused", check_command("") is not None)
check("whitespace only is refused", check_command("   ") is not None)

# ---- the second gate, and how tools are classified -------------------------
check("human approval is required for device commands", C.REQUIRE_APPROVAL is True)
check("the ssh server is classified as touching devices",
      "ssh" in C.DEVICE_SERVERS)
check("local probes, when enabled, are gated too", "local" in C.DEVICE_SERVERS)
check("tufin is NOT gated -- it is a read-only GET, not a device",
      "tufin" not in C.DEVICE_SERVERS and "unicorn" not in C.DEVICE_SERVERS)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
