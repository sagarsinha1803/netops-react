"""The next hop has to come from the route to the DESTINATION.

A real deeper-checks run drew "source -> mgmt0 -> <the source's own management
address> -> ? unconfirmed". Two faults stacked:

  1. The commands the model ran dump the WHOLE table ("show ip route vrf
     default"), and the next hop was read out of whichever prefix printed
     first. In that table the first entry was the source's own /32.
  2. That entry was a LOCAL route -- the device saying "this address is mine",
     not "this is the way onward" -- and nothing checked for that.

So the panel showed the source forwarding to itself, and then a question mark,
which is not a thing that can happen.

    .venv/Scripts/python.exe tests/test_route_scope.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api.workflow import (path_from_checks, pick_next_hop,   # noqa: E402
                          route_for)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC_ADDR, DST_ADDR = "198.51.100.27", "198.51.100.28"
SRC, DST = "EDGE-A1", "EDGE-B2"

# NX-OS, the shape that caused it: the device's own address first, the real
# entry further down.
TABLE = "\n".join([
    'IP Route Table for VRF "default"',
    "",
    "203.0.113.81/32, ubest/mbest: 1/0, attached",
    "    *via 203.0.113.81, Vlan231, [0/0], 4w0d, local",
    "203.0.113.0/24, ubest/mbest: 1/0, attached",
    "    *via 203.0.113.81, Vlan231, [0/0], 4w0d, direct",
    f"{SRC_ADDR}/32, ubest/mbest: 1/0, attached",
    f"    *via {SRC_ADDR}, mgmt0, [0/0], 4w0d, local",
    "198.51.100.0/24, ubest/mbest: 1/0, attached",
    "    *via 192.0.2.9, Ethernet1/54, [110/41], 2w1d, ospf-1, intra",
])

# ---- the reading, on its own ---------------------------------------------
scoped = route_for(TABLE, DST_ADDR)
check("the destination's own prefix is what gets read",
      bool(scoped) and "198.51.100.0/24" in scoped, str(scoped)[:70])
check("and not the source's /32 that printed first",
      bool(scoped) and f"{SRC_ADDR}/32" not in scoped, str(scoped)[:70])

hop, egress = pick_next_hop(scoped, SRC_ADDR)
check("the next hop is the one on that route", hop == "192.0.2.9", hop)
check("with the interface it leaves by", egress == "Ethernet1/54", egress)

own, _ = pick_next_hop("    *via 203.0.113.81, Vlan231, [0/0], 4w0d, local", "")
check("a local entry is never a next hop", own == "", own)
mine, _ = pick_next_hop(f"    *via {SRC_ADDR}, mgmt0, [0/0], 2d, static", SRC_ADDR)
check("nor is the source's own address", mine == "", mine)

# ---- and end to end -------------------------------------------------------
ROW = {"cmd": "show ip route vrf default", "kind": "device",
       "device": SRC_ADDR, "output": TABLE}
path = path_from_checks([ROW], SRC, DST, DST_ADDR)
labels = [n["label"] for n in (path or {}).get("nodes", [])]
print()
print("  path:", (path or {}).get("line"))

check("the drawn path leaves by the right interface",
      "Ethernet1/54" in labels, str(labels))
check("towards the right next hop", "192.0.2.9" in labels, str(labels))
check("and never draws the source as its own next hop",
      SRC_ADDR not in labels and "mgmt0" not in labels, str(labels))

# ---- no route at all is an answer, not a shrug ---------------------------
ELSEWHERE = "\n".join([
    'IP Route Table for VRF "default"',
    "10.0.0.0/8, ubest/mbest: 1/0",
    "    *via 10.9.9.1, Ethernet1/1, [110/41], 2w1d, ospf-1, intra",
])
gone = path_from_checks(
    [{"cmd": "show ip route vrf default", "kind": "device",
      "device": SRC_ADDR, "output": ELSEWHERE}], SRC, DST, DST_ADDR)
check("a table with nothing covering the destination says so",
      bool(gone) and gone.get("reached") is False, str(gone)[:70])
check("naming that as the blockage",
      bool(gone) and "no route" in str(gone["nodes"][-1].get("why", "")),
      str((gone or {}).get("nodes", [])[-1:]))

# a default route DOES cover it, and must not be read as "no route"
WITH_DEFAULT = ELSEWHERE + "\n0.0.0.0/0, ubest/mbest: 1/0\n" \
    "    *via 192.0.2.1, Ethernet1/2, [1/0], 4w0d, static"
viadef = path_from_checks(
    [{"cmd": "show ip route vrf default", "kind": "device",
      "device": SRC_ADDR, "output": WITH_DEFAULT}], SRC, DST, DST_ADDR)
check("a default route is a route",
      "192.0.2.1" in [n["label"] for n in (viadef or {}).get("nodes", [])],
      str(viadef)[:80])

# ---- output nobody can parse is left alone -------------------------------
VAGUE = {"cmd": "show route", "kind": "device", "device": SRC_ADDR,
         "output": f"Gateway of last resort is 192.0.2.1\n"
                   f"  via 192.0.2.1, Ethernet1/2\n"}
loose = path_from_checks([VAGUE], SRC, DST, DST_ADDR)
check("with no prefixes to scope by, the old reading still stands",
      bool(loose) and "192.0.2.1" in [n["label"] for n in loose["nodes"]],
      str(loose)[:70])

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
