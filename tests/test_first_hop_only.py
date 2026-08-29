"""A healthy first hop is not a destination that was reached.

A deeper-checks run proved everything the SOURCE can prove: a route, a
complete forwarding entry, an ARP adjacency, an egress interface up, and a
successful ping to the next hop. Its own report said the fault was somewhere
beyond that hop and called the run INCONCLUSIVE.

The Path tab drew it green, all the way through to the destination.

Every one of those checks is about the FIRST HOP. None of them says a packet
arrived -- and in that same run the probe to the destination went unanswered,
twice, including one sourced from the egress interface. A panel that draws an
arrival its own evidence denies is worse than one that draws nothing.

    .venv/Scripts/python.exe tests/test_first_hop_only.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from api.workflow import path_from_checks                        # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "EDGE-A1", "EDGE-B2"
SRC_ADDR, DST_ADDR = "198.51.100.9", "198.51.100.80"
HOP, LINK = "203.0.113.2", "Bundle-Ether1"


def row(cmd, output):
    return {"cmd": cmd, "kind": "device", "device": SRC_ADDR, "output": output}


# everything the source can prove about its own forwarding, and all of it good
HEALTHY = [
    row(f"show route {DST_ADDR}",
        f"Routing entry for 0.0.0.0/0\n"
        f"  Known via ospf, distance 110\n"
        f"  * via {HOP}, {LINK}\n"),
    row(f"show cef {DST_ADDR}",
        f"0.0.0.0/0, version 12, attached\n"
        f"   via {HOP}, {LINK}, 5 dependencies, weight 0\n"),
    row(f"ping {HOP} count 3",
        "!!!\nSuccess rate is 100 percent (3/3), round-trip min/avg/max = 1/2/3 ms\n"),
    row(f"show interfaces {LINK}",
        f"{LINK} is up, line protocol is up\n"),
    row(f"show arp {HOP}",
        f"{HOP}  00:04:54  0050.56be.1a2b  Dynamic ARPA  {LINK}\n"),
]

DEST_PROBE_FAILED = row(
    f"ping {DST_ADDR} source {LINK} count 3",
    "...\nSuccess rate is 0 percent (0/3)\n")

# ---- with the probe that failed, which is the run from the screenshot -----
path = path_from_checks(HEALTHY + [DEST_PROBE_FAILED], SRC, DST, DST_ADDR)
labels = [n["label"] for n in (path or {}).get("nodes", [])]
print()
print("  path:", (path or {}).get("line"))
print("  note:", (path or {}).get("note", "")[:110])

check("the path is drawn as far as the evidence goes",
      labels[:3] == [SRC, LINK, HOP], str(labels))
check("and stops at a question mark, not at the destination",
      labels[-1] == "?" and DST not in labels, str(labels))
check("the destination is never claimed as reached",
      (path or {}).get("reached") is None, str((path or {}).get("reached")))
check("the note says the first hop is all that was proved",
      "first hop" in str((path or {}).get("note", "")).lower(),
      str((path or {}).get("note"))[:90])
check("and that the probe to the destination went unanswered",
      "unanswered" in str((path or {}).get("note", "")).lower(),
      str((path or {}).get("note"))[:120])

# ---- and without it: still not an arrival, just an open question ---------
quiet = path_from_checks(HEALTHY, SRC, DST, DST_ADDR)
check("a healthy first hop alone still does not reach the destination",
      (quiet or {}).get("reached") is None, str((quiet or {}).get("reached")))
check("it ends at the question mark too",
      [n["label"] for n in (quiet or {}).get("nodes", [])][-1] == "?",
      str([n["label"] for n in (quiet or {}).get("nodes", [])]))

# ---- what DOES settle it, so the fix has not made the panel useless ------
ARRIVED = row(f"ping {DST_ADDR} count 3",
              "!!!\nSuccess rate is 100 percent (3/3), "
              "round-trip min/avg/max = 1/2/3 ms\n")
good = path_from_checks(HEALTHY + [ARRIVED], SRC, DST, DST_ADDR)
check("a probe that got replies still settles it",
      (good or {}).get("reached") is True, str((good or {}).get("reached")))
check("and draws the destination",
      DST in [n["label"] for n in (good or {}).get("nodes", [])],
      str([n["label"] for n in (good or {}).get("nodes", [])]))

# a directly attached destination whose probe failed is not reached either
ATTACHED = [
    row(f"show route {DST_ADDR}",
        f"{DST_ADDR}/32, attached\n    *via {DST_ADDR}, {LINK}, [0/0], 1w2d\n"),
    row(f"show arp {DST_ADDR}",
        f"{DST_ADDR}  00:01:02  0050.56be.9999  Dynamic ARPA  {LINK}\n"),
    row(f"ping {DST_ADDR} count 3", "...\nSuccess rate is 0 percent (0/3)\n"),
]
near = path_from_checks(ATTACHED, SRC, DST, DST_ADDR)
check("an attached neighbour that answers ARP but not ping is not reached",
      (near or {}).get("reached") is not True, str((near or {}).get("reached")))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
