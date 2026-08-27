"""The three faults a real office run turned up, in the shapes it produced.

The mocks return output the way a textbook prints it. The real ssh MCP does
not: it hands its list back already serialised, its ping says "100.00% packet
loss" rather than "100%", and the relay's chat window is finite so the earliest
results fall out of it. Every one of those was invisible here until the run
happened on real equipment.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from langchain_core.messages import (AIMessage, HumanMessage,     # noqa: E402
                                     SystemMessage, ToolMessage)

from agent.llm.clipboard_llm import _recap                        # noqa: E402
from agent.utils import tool_text                                 # noqa: E402
from api.workflow import (as_report, check_ok, cmdb_record,       # noqa: E402
                          failed_line, usable_output)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- 1. the ssh MCP returns its list ALREADY SERIALISED --------------------
LOSS = ("\r\n\r\nTue Aug 25 22:14:01\r\n"
        "PING 198.51.100.6: 56 data bytes\r\n"
        "--- 198.51.100.6 ping statistics ---\r\n"
        "3 packets transmitted, 0 packets received, 100.00% packet loss\r\n")
SERIALISED = json.dumps([{"cmd": "ping 198.51.100.6 count 3",
                          "stdout": LOSS, "stderr": "", "rc": 0}])

flat = tool_text(SERIALISED)
check("a serialised CLI result is flattened, not shown as JSON",
      not flat.lstrip().startswith("["), flat[:60])
check("the command heads it, as a session",
      flat.startswith("# ping 198.51.100.6 count 3"), flat[:40])
check("the output is real lines, not escapes",
      "\\r\\n" not in flat and "packet loss" in flat)
check("so the panel can read it",
      usable_output(flat, "ping 198.51.100.6 count 3"),
      "this is what put a JSON blob in the row and a cross beside it")
check("and the failing line is quotable",
      "packet loss" in str(failed_line(flat)), str(failed_line(flat))[:60])

MULTI = json.dumps([
    {"cmd": "show ip route 10.0.0.1", "stdout": "Routing entry for 10.0.0.0/8\r\n"},
    {"cmd": "show arp", "stdout": "10.0.0.1  0050.56be.1a2b  ARPA  Eth1/1\r\n"},
])
flat2 = tool_text(MULTI)
check("several commands in one call all come through",
      flat2.count("# show") == 2, repr(flat2[:60]))

# a CMDB record is JSON too, and must NOT be run through the CLI flattener
REC = json.dumps({"region": "INDIA", "lookup_by": "name",
                  "data": {"name": "EDGE-A1", "managementIp": "10.0.0.1"}})
check("a CMDB record is left alone", cmdb_record(tool_text(REC)) == (True, "EDGE-A1"),
      str(cmdb_record(tool_text(REC))))
check("and a plain sentence is still a plain sentence",
      tool_text("No data found for 'X' in any region.").startswith("No data"))


# ---- 2. total loss, however the platform prints it -------------------------
PINGS = {
    "3 packets transmitted, 0 packets received, 100.00% packet loss": False,
    "3 packets transmitted, 0 received, 100% packet loss": False,
    "5 packets transmitted, 5 packets received, 0.00% packet loss": True,
    "10 packets transmitted, 10 received, 0% packet loss": True,
    "4 packets transmitted, 2 received, 50% packet loss": True,
    "Success rate is 0 percent (0/5)": False,
    "Success rate is 100 percent (5/5)": True,
}
for text, want in PINGS.items():
    check(f"ping verdict: {text[:46]}", check_ok(text) == want,
          f"read as {'ok' if check_ok(text) else 'failed'}")


# ---- 3. the relay's window drops the early results ------------------------
def tool(name, body):
    return ToolMessage(content=body, name=name, tool_call_id=name)


THREAD = [
    SystemMessage(content="the prompt"),
    HumanMessage(content="Troubleshoot EDGE-A1 to EDGE-A2 on tcp:22"),
    tool("get_device_details", json.dumps({"data": {"name": "EDGE-A1"}})),
    tool("execute_query_on_server", "# ping 10.0.0.2\n" + LOSS.replace("\r\n", "\n")),
    tool("get_firewall_path", json.dumps({"verdict": "ALLOWED",
                                          "blocking_rules": []})),
    tool("get_alert_and_ticket_details_from_archangel", "5 open alerts"),
]

recap = _recap(THREAD)
check("the recap exists at all", bool(recap.strip()))
check("it carries the firewall verdict -- the thing the run lost",
      "ALLOWED" in recap, recap[:80])
check("and the ping result",
      "packet loss" in recap, recap[:120])
check("it names each tool, so the model knows what it already has",
      recap.count("- ") == 4, recap)
check("it tells the model not to run them again",
      "do not run them again" in recap.lower())
check("nothing to recap stays empty, rather than a heading over nothing",
      _recap([SystemMessage(content="x"), HumanMessage(content="y")]) == "")

many = [tool(f"t{i}", f"result {i}") for i in range(40)]
capped = _recap(many)
check("a long run keeps the most recent, and says it is doing so",
      capped.count("- ") == 14 and "most recent 14 of 40" in capped,
      capped.splitlines()[0][:90])


# ---- and the verdict the run never reached --------------------------------
NO_VERDICT = ("WORKFLOW COMPLETE. Do not run any more functions. Return the "
              "accumulated troubleshooting results; however, the firewall-path "
              "verdict is missing from the provided context, so a reachability "
              "verdict cannot be determined.")
report = as_report(NO_VERDICT) or {}
check("an answer with no verdict parses as text, not as a report",
      not report.get("result"), str(report)[:60])

# ---- 4. what the paths make of a real run ---------------------------------
from agent.vendors import parse_hops                               # noqa: E402
from api.workflow import path_from_checks, path_from_policy        # noqa: E402

TRACE = "\r\n".join([
    "", "", "Tue Aug 25 22:41:13.931 MEST", "",
    "Type escape sequence to abort.",
    "Tracing the route to 198.51.100.6", "",
    " 1  203.0.113.2 2 msec ",
    " 2  203.0.113.78 2 msec ",
    " 3  203.0.113.190 2 msec ",
    " 4  * ",
    " 5  * ",
    "",
])
hops = parse_hops(tool_text(json.dumps(
    [{"cmd": "traceroute 198.51.100.6 maxttl 5 timeout 1 probe 1 numeric",
      "stdout": TRACE, "rc": 0}])))
check("the real traceroute yields hops, so the Path tab has one to draw",
      len(hops) == 5, str(len(hops)))
check("three answered and two did not",
      sum(1 for h in hops if not h["timeout"]) == 3
      and sum(1 for h in hops if h["timeout"]) == 2,
      str([h["host"] or "*" for h in hops]))

# an IOS-XR routing block names the next hop and NO interface
XR = "\n".join([
    "Routing entry for 198.51.100.6/32",
    '  Known via "bgp 65000", distance 200, metric 0',
    "  Routing Descriptor Blocks",
    "    203.0.113.246, from 203.0.113.246",
    "      Route metric is 0",
])
deep = path_from_checks([{"output": XR}], "edge-a1", "edge-a2")
check("the deep path is drawn from IOS-XR output", deep is not None)
check("it names the next hop the source is trying",
      deep and "203.0.113.246" in deep["line"], deep and deep["line"])
check("a route ALONE proves nothing about what happens after it",
      deep and deep["reached"] is None,
      "a route is an intention, not a delivery")
check("so the path ends in a question, not at the destination",
      deep and deep["nodes"][-1]["label"] == "?",
      deep and deep["line"])

confirmed = path_from_checks(
    [{"output": XR + "\nInternet 203.0.113.246 3 0050.56be.7f01 ARPA Te0/0/0/0"}],
    "edge-a1", "edge-a2")
check("an ARP entry with a hardware address DOES settle it",
      confirmed and confirmed["reached"] is True, confirmed and confirmed["line"])

# SecureTrack lists the two ends and its own markers among the "devices"
POLICY = json.dumps({"verdict": "ALLOWED",
                     "hops": [["EDGE-A1"], ["EDGE-A2"],
                              ["DIRECTLY_CONNECTED"]],
                     "reaches_destination": True})
pol = path_from_policy(POLICY, "EDGE-A1", "EDGE-A2")
labels = [n["label"] for n in pol["nodes"]]
check("the source is not drawn twice", labels.count("EDGE-A1") == 1, str(labels))
check("nor the destination", labels.count("EDGE-A2") == 1, str(labels))
check("and a routing marker is not drawn as equipment",
      "DIRECTLY_CONNECTED" not in labels, str(labels))


# ---- 5. a BLOCKED path must not be drawn as arriving -----------------------
# SecureTrack can model a complete chain AND deny the traffic on it -- that is
# what a firewall is. Reading only the routing drew a green line through to the
# destination while the verdict beside it said BLOCKED.
BLOCKED = json.dumps({
    "traffic_allowed": False,
    "verdict": "BLOCKED",
    "blocking_rules": [{"action": "Deny", "acl": "Clean Up Rule",
                        "rule_id": "40"}],
    "rules_seen": 3,
    "device_path": ["EDGE-SW-01", "FW-EDGE-01", "CORE-01"],
    "hops": [["EDGE-SW-01"], ["FW-EDGE-01"], ["CORE-01"],
             ["Cloud 203.0.113.49"]],
    "device_count": 4,
    "reaches_destination": None,       # SecureTrack often says nothing here
    "unrouted_elements": [],
})
blocked = path_from_policy(BLOCKED, "198.51.100.10", "198.51.100.20")
check("a BLOCKED path does not reach the destination",
      blocked["reached"] is False, str(blocked["reached"]))
check("it ends in a wall, not in the destination",
      blocked["nodes"][-1]["label"] == "X", blocked["line"][-40:])
check("the destination is not drawn as if traffic arrived",
      "198.51.100.20" not in blocked["line"], blocked["line"])
check("and the note says the traffic is denied ON the chain, not lost",
      "denied" in blocked["note"] and "Clean Up Rule" in blocked["note"],
      blocked["note"][:100])
check("the modelled devices are still shown -- they are where it stops",
      "FW-EDGE-01" in blocked["line"], blocked["line"])

ALLOWED = json.dumps({
    "traffic_allowed": True, "verdict": "ALLOWED", "blocking_rules": [],
    "hops": [["EDGE-SW-01"], ["FW-EDGE-01"]],
    "reaches_destination": None, "unrouted_elements": [],
})
allowed = path_from_policy(ALLOWED, "198.51.100.10", "198.51.100.20")
check("an ALLOWED and routed path DOES reach the destination",
      allowed["reached"] is True, str(allowed["reached"]))
check("and names it at the end", allowed["nodes"][-1]["label"] == "198.51.100.20",
      allowed["line"][-30:])

UNROUTED = json.dumps({
    "verdict": "UNKNOWN", "hops": [["EDGE-SW-01"]],
    "unrouted_elements": [{"destination": "198.51.100.20"}],
})
unrouted = path_from_policy(UNROUTED, "198.51.100.10", "198.51.100.20")
check("no route at all is still a dead end",
      unrouted["reached"] is False, str(unrouted["reached"]))
check("and says the traffic is delivered nowhere",
      "not delivered anywhere" in unrouted["note"], unrouted["note"][:60])

NO_VERDICT_POLICY = json.dumps({
    "hops": [["EDGE-SW-01"]], "reaches_destination": None,
    "unrouted_elements": [],
})
unsettled = path_from_policy(NO_VERDICT_POLICY, "198.51.100.10", "198.51.100.20")
check("a routed chain with NO verdict is unsettled, not a success",
      unsettled["reached"] is None, str(unsettled["reached"]))
check("so it ends in a question", unsettled["nodes"][-1]["label"] == "?",
      unsettled["line"][-20:])


# ---- 6. the deeper checks' own probes settle the deep path -----------------
# A run found the destination in a VRF, pinged it there, got 3/3 replies -- and
# the panel still drew "? unconfirmed" and called the result INCONCLUSIVE,
# because the deep path was built only from routing output. A probe that comes
# back IS the answer.
DEST = "198.51.100.31"

vrf_ping = {
    "cmd": f"ping vrf EXAMPLE-VRF {DEST} count 3",
    "output": (f"# ping vrf EXAMPLE-VRF {DEST} count 3\n"
               f"Type escape sequence to abort.\n"
               f"Sending 3, 100-byte ICMP Echos to {DEST}, timeout is 2 seconds:\n"
               "!!!\n"
               "Success rate is 100 percent (3/3), round-trip min/avg/max = 2/2/3 ms"),
}
route_only = {
    "cmd": "show route " + DEST,
    "output": ("Routing entry for 0.0.0.0/0\n"
               "  Known via \"ospf 1\"\n"
               "  Routing Descriptor Blocks\n"
               "    203.0.113.2, from 203.0.113.2, via Bundle-Ether9\n"),
}

settled = path_from_checks([route_only, vrf_ping], "EDGE-A1", "EDGE-B2", DEST)
check("a successful VRF ping settles the deep path",
      settled["reached"] is True, str(settled["reached"]))
check("and draws through to the destination",
      settled["nodes"][-1]["label"] == "EDGE-B2", settled["line"])
check("the note names the VRF it took, because that IS the finding",
      "EXAMPLE-VRF" in settled["note"], settled["note"][:90])
check("and says the earlier failure was the wrong context",
      "wrong routing context" in settled["note"], settled["note"][-60:])

# a ping to the NEXT HOP is not a ping to the destination
next_hop_ping = {
    "cmd": "ping 203.0.113.2 count 3",
    "output": ("# ping 203.0.113.2 count 3\n!!!\n"
               "Success rate is 100 percent (3/3)"),
}
not_settled = path_from_checks([route_only, next_hop_ping],
                               "EDGE-A1", "EDGE-B2", DEST)
check("a next-hop ping does NOT claim the destination was reached",
      not_settled["reached"] is not True, str(not_settled["reached"]))

# a FAILED ping to the destination must not be read as success either
failed_ping = {
    "cmd": f"ping {DEST} count 3",
    "output": (f"# ping {DEST} count 3\n...\n"
               "Success rate is 0 percent (0/3)"),
}
still_open = path_from_checks([route_only, failed_ping],
                              "EDGE-A1", "EDGE-B2", DEST)
check("a failed ping settles nothing on its own",
      still_open["reached"] is not True, str(still_open["reached"]))

# ---- a silent hop between answering hops is not a break --------------------
trace = {
    "cmd": f"traceroute vrf EXAMPLE-VRF {DEST} maxttl 5 timeout 1 probe 1",
    "output": (f"# traceroute vrf EXAMPLE-VRF {DEST}\n"
               f"Tracing the route to {DEST}\n"
               " 1  203.0.113.2 [MPLS: Label 90001 Exp 0] 2 msec\n"
               " 2  203.0.113.78 [MPLS: Label 90002 Exp 0] 2 msec\n"
               " 3  * \n"
               " 4  203.0.113.202 3 msec\n"),
}
traced = path_from_checks([route_only, trace], "EDGE-A1", "EDGE-B2", DEST)
check("a traceroute the deeper checks ran is used for the path",
      "203.0.113.78" in traced["line"], traced["line"])
check("a hop that answered nothing between hops that did is a HIDDEN hop",
      "hidden hop" in traced["line"], traced["line"])
check("it is not drawn as a wall",
      "blocked" not in traced["line"] and "X" not in traced["nodes"][-1]["label"],
      traced["line"])
check("and the note says why silence there is not a drop",
      "declining to reply" in traced["note"], traced["note"][:110])
check("the trace alone still does not claim arrival",
      traced["reached"] is None, str(traced["reached"]))

# a probe beats a trace: both present, the probe wins
both = path_from_checks([route_only, trace, vrf_ping], "EDGE-A1", "EDGE-B2", DEST)
check("a probe that answered outranks a trace that did not",
      both["reached"] is True, str(both["reached"]))


# ---- 7. a proved path shows its hops, from the right context ---------------
# "Reachable" drawn as two nodes with nothing between them asks the reader to
# take it on faith. The hops are usually right there in the run.
vrf_trace = {
    "cmd": f"traceroute vrf EXAMPLE-VRF {DEST} maxttl 5 timeout 1 probe 1",
    "output": (f"Tracing the route to {DEST}\n"
               " 1  203.0.113.2 2 msec\n"
               " 2  203.0.113.78 2 msec\n"
               " 3  * \n"
               " 4  203.0.113.202 3 msec\n"),
}
global_trace = {
    "cmd": f"traceroute {DEST} maxttl 5 timeout 1 probe 1",
    "output": (f"Tracing the route to {DEST}\n"
               " 1  198.18.0.9 1 msec\n 2  * \n 3  * \n"),
}

shown = path_from_checks([route_only, vrf_ping, vrf_trace],
                         "EDGE-A1", "EDGE-B2", DEST)
check("a proved path draws the hops behind it",
      "203.0.113.78" in shown["line"], shown["line"])
check("it still ends at the destination, since the probe arrived",
      shown["nodes"][-1]["label"] == "EDGE-B2" and shown["reached"] is True,
      shown["line"])
check("a silent hop among answering ones is drawn as hidden, not as a wall",
      "hidden hop" in shown["line"], shown["line"])
check("and the note says where the hops came from",
      "same context" in shown["note"], shown["note"][-120:])

# a traceroute from ANOTHER table proves nothing about this path
mixed = path_from_checks([route_only, global_trace, vrf_ping],
                         "EDGE-A1", "EDGE-B2", DEST)
check("a global-table trace is NOT drawn under a VRF verdict",
      "198.18.0.9" not in mixed["line"], mixed["line"])
check("the next hop from the source's own routing is used instead",
      "203.0.113.2" in mixed["line"], mixed["line"])
check("and the note admits the hops between are not known",
      "no traceroute was run in that context" in mixed["note"].lower(),
      mixed["note"][-120:])

# nothing to draw with at all: still honest, still arrives
bare = path_from_checks([vrf_ping], "EDGE-A1", "EDGE-B2", DEST)
check("with no routing evidence it is still REACHED",
      bare["reached"] is True, str(bare["reached"]))
check("and says the hops are not shown rather than inventing them",
      "not shown" in bare["note"], bare["note"][-90:])


# ---- 8. the shape of a path ------------------------------------------------
# Equal-cost devices were crammed into one "A / B" chip: right about the
# topology, unreadable as a path. And the interface the traffic leaves by is a
# step in the route, not a footnote on the next hop -- it is usually the thing
# that turns out to be down.
SEQ = json.dumps({
    "verdict": "ALLOWED", "reaches_destination": True, "unrouted_elements": [],
    "hops": [["SW-A1", "SW-A2"], ["FW-EDGE-01"], ["CORE-01"]],
})
seq = path_from_policy(SEQ, "EDGE-A1", "EDGE-B2")
labels = [n["label"] for n in seq["nodes"]]
check("equal-cost devices are laid out in sequence, not run together",
      "SW-A1 / SW-A2" not in seq["line"] and "SW-A1" in labels
      and "SW-A2" in labels, seq["line"])
check("every device gets its own step",
      labels == ["EDGE-A1", "SW-A1", "SW-A2", "FW-EDGE-01", "CORE-01",
                 "EDGE-B2"], str(labels))
check("alternatives are still MARKED as alternatives, not passed off as a chain",
      [n.get("alt") for n in seq["nodes"] if n["label"] == "SW-A1"]
      == ["1 of 2 at this step"], str([n.get("alt") for n in seq["nodes"]]))
check("a device that has no alternative carries no mark",
      not any(n.get("alt") for n in seq["nodes"] if n["label"] == "FW-EDGE-01"))

WITH_INTF = "\n".join([
    "Routing entry for 198.51.100.31/32",
    "  Routing Descriptor Blocks",
    "    203.0.113.2, from 203.0.113.2, via Ethernet1/54",
])
via = path_from_checks([{"cmd": "show ip route", "output": WITH_INTF}],
                       "EDGE-A1", "EDGE-B2", "198.51.100.31")
kinds = [n["kind"] for n in via["nodes"]]
check("the egress interface is a step in the deep path",
      "intf" in kinds, str(kinds))
check("and it sits between the source and the next hop",
      [n["label"] for n in via["nodes"]][:3]
      == ["EDGE-A1", "Ethernet1/54", "203.0.113.2"],
      str([n["label"] for n in via["nodes"]]))

DOWN = WITH_INTF + "\nEthernet1/54 is down, line protocol is down\n"
broke = path_from_checks([{"cmd": "show ip route", "output": DOWN}],
                         "EDGE-A1", "EDGE-B2", "198.51.100.31")
check("with the interface down the path still shows it, then stops",
      [n["label"] for n in broke["nodes"]]
      == ["EDGE-A1", "Ethernet1/54", "203.0.113.2", "X"],
      str([n["label"] for n in broke["nodes"]]))


# ---- 9. what the ladder tells the model about real platforms ---------------
from agent.prompts import DEEP_CHECK_PROMPT as LADDER               # noqa: E402

for rule, why in [
    ("YOUR JOB IS THE PATH", "the job is the route, not just the fault"),
    ("RESOLVE RECURSIVELY", "a next hop is itself reached through a route"),
    ("EQUAL COST", "several next hops must all be named, not one picked"),
    ("show mpls forwarding-table", "the label stack IS the path in an L3VPN"),
    ("show lldp neighbors", "the neighbour names the hop when a trace cannot"),
    ("source <intf>", "the source address changes which route is tested"),
    ("REPORT THE PATH", "the hop list must be what was actually proved"),
    ("VRF-Name", "a column header was once read as the name of a VRF"),
    ("show ip route vrf all", "listing VRFs and stopping answers nothing"),
    ("management VRF", "a management address is not reachable from the "
                       "default table, and that is not a fault"),
    ("show forwarding ipv4 route", "the hardware table is the authority"),
    ("show cdp neighbors", "who the neighbour is, rather than assuming"),
    ("mac address-table", "empty is NORMAL on a routed interface"),
    ("HOP 1", "one hop is expected when the two ends are directly connected"),
    ("ARP resolving", "a first probe that fails while the rest succeed"),
    ("traceroute takes few options", "NX-OS rejects the bounded form"),
]:
    check(f"the ladder covers: {why}", rule in LADDER, f"missing {rule!r}")


# ---- 10. two ends on one wire, and where a path stops ----------------------
# A directly connected pair has no next hop to chase: the path IS the
# interface, and that is what should be drawn.
CONNECTED = [
    {"cmd": f"show ip route {DEST}",
     "output": ("198.51.100.28/30, ubest/mbest: 1/0\n"
                "    *via 198.51.100.30, Ethernet1/54, [0/0], "
                "directly connected, Ethernet1/54\n")},
    {"cmd": "show ip arp",
     "output": f"{DEST}  00:04:54  0050.56be.1111  Ethernet1/54\n"},
    {"cmd": "show interface Ethernet1/54",
     "output": "Ethernet1/54 is up, line protocol is up\n"},
]
link = path_from_checks(CONNECTED, "EDGE-A1", "EDGE-B2", DEST)
check("a directly connected pair is drawn through its interface",
      [n["label"] for n in link["nodes"]]
      == ["EDGE-A1", "Ethernet1/54", "EDGE-B2"], link["line"])
check("and counts as reached, since ARP resolved and the link is up",
      link["reached"] is True, str(link["reached"]))
check("the note says one hop, no router between",
      "no router in between" in link["note"], link["note"][:80])

DOWN_LINK = [
    {"cmd": f"show ip route {DEST}",
     "output": "198.51.100.28/30 is directly connected, Ethernet1/54\n"},
    {"cmd": "show interface Ethernet1/54",
     "output": "Ethernet1/54 is down, line protocol is down\n"},
]
broken_link = path_from_checks(DOWN_LINK, "EDGE-A1", "EDGE-B2", DEST)
check("a dead link still shows the interface, then stops there",
      [n["label"] for n in broken_link["nodes"]]
      == ["EDGE-A1", "Ethernet1/54", "X"], broken_link["line"])
check("and the stopping point carries the reason",
      broken_link["nodes"][-1].get("why") == "Ethernet1/54 is down",
      str(broken_link["nodes"][-1]))

# ---- the blockage is named, in the words that identify it ------------------
from api.workflow import blockage                                  # noqa: E402

ROUTE = ("Routing entry for 198.51.100.31/32\n"
         "    203.0.113.2, from 203.0.113.2, via Bundle-Ether9\n")
cases = [
    ("an interface down beats every other reason",
     ROUTE + "Bundle-Ether9 is down, line protocol is down\n",
     "Bundle-Ether9 is down"),
    ("an unresolved next hop is named with its address",
     ROUTE + "203.0.113.2  -  Incomplete  ARPA  Bundle-Ether9\n",
     "203.0.113.2 never answered ARP"),
    ("a drop adjacency is called what it is",
     ROUTE + "  via 203.0.113.2, Bundle-Ether9 (INCOMPLETE - drop adjacency)\n",
     "drop adjacency"),
]
for name, text, want in cases:
    got = blockage(text, "203.0.113.2", "Bundle-Ether9")
    check(name, want in got, f"got {got!r}")

check("no route anywhere is only claimed when there is no next hop either",
      "no route" in blockage("% Network not in table\nRoute not found\n"),
      blockage("% Network not in table"))
check("a healthy run names no blockage at all",
      blockage(ROUTE + "Bundle-Ether9 is up, line protocol is up\n",
               "203.0.113.2", "Bundle-Ether9") == "",
      "a reason invented for a working path is worse than none")

print()
print("ALL PASSED" if not fails else f"FAILED ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
