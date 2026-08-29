"""The parsers, against output shaped the way real platforms actually print it.

Every other test in this suite reads output that a mock in this repository
wrote, which proves the parsers can read themselves. These samples are the
formats the platforms in the CMDB really use -- IOS, IOS-XR, NX-OS, EOS,
Junos, FortiOS, Linux -- with their real spacing, their real punctuation and
their real spelling of "nexthop".

A parser that only understands the mock is a parser that fails on the first
real device, and it fails QUIETLY: a stage goes green off output it did not
understand.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.vendors import ping_ok, parse_hops                    # noqa: E402
from api.workflow import path_from_checks                        # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# =========================================================== ping ===========
PINGS = {
    "IOS / IOS-XE success": ("""Type escape sequence to abort.
Sending 5, 100-byte ICMP Echos to 10.40.20.50, timeout is 2 seconds:
!!!!!
Success rate is 100 percent (5/5), round-trip min/avg/max = 1/2/4 ms""", True),

    "IOS partial (3 of 5)": ("""Sending 5, 100-byte ICMP Echos to 10.40.20.50:
!!...
Success rate is 60 percent (3/5), round-trip min/avg/max = 1/2/4 ms""", True),

    "IOS total loss": ("""Sending 5, 100-byte ICMP Echos to 10.60.40.12:
.....
Success rate is 0 percent (0/5)""", False),

    "NX-OS success": ("""PING 10.40.20.50 (10.40.20.50): 56 data bytes
64 bytes from 10.40.20.50: icmp_seq=0 ttl=254 time=1.02 ms
64 bytes from 10.40.20.50: icmp_seq=1 ttl=254 time=0.87 ms

--- 10.40.20.50 ping statistics ---
2 packets transmitted, 2 packets received, 0.00% packet loss""", True),

    "NX-OS total loss": ("""PING 10.60.40.12 (10.60.40.12): 56 data bytes
Request 0 timed out
Request 1 timed out

--- 10.60.40.12 ping statistics ---
2 packets transmitted, 0 packets received, 100.00% packet loss""", False),

    "IOS-XR success": ("""Sending 5, 100-byte ICMP Echos to 10.40.20.50, timeout is 2 seconds:
!!!!!
Success rate is 100 percent (5/5), round-trip min/avg/max = 1/1/2 ms""", True),

    "Junos success": ("""PING 10.40.20.50 (10.40.20.50): 56 data bytes
64 bytes from 10.40.20.50: icmp_seq=0 ttl=63 time=1.234 ms

--- 10.40.20.50 ping statistics ---
1 packets transmitted, 1 packets received, 0% packet loss""", True),

    "Junos total loss": ("""PING 10.60.40.12 (10.60.40.12): 56 data bytes

--- 10.60.40.12 ping statistics ---
3 packets transmitted, 0 packets received, 100% packet loss""", False),

    "FortiOS success": ("""PING 10.40.20.50 (10.40.20.50): 56 data bytes
64 bytes from 10.40.20.50: icmp_seq=0 ttl=255 time=0.4 ms

--- 10.40.20.50 ping statistics ---
5 packets transmitted, 5 packets received, 0% packet loss""", True),

    "Linux / Gaia success": ("""PING 10.40.20.50 (10.40.20.50) 56(84) bytes of data.
64 bytes from 10.40.20.50: icmp_seq=1 ttl=62 time=1.15 ms

--- 10.40.20.50 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms""", True),

    "Linux unreachable": ("""PING 10.60.40.12 (10.60.40.12) 56(84) bytes of data.
From 10.20.30.1 icmp_seq=1 Destination Host Unreachable

--- 10.60.40.12 ping statistics ---
3 packets transmitted, 0 received, +3 errors, 100% packet loss""", False),

    "Arista EOS success": ("""PING 10.40.20.50 (10.40.20.50) 72(100) bytes of data.
80 bytes from 10.40.20.50: icmp_seq=1 ttl=64 time=0.славно ms

--- 10.40.20.50 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4ms""", True),
}

print("---- ping ------------------------------------------------------------")
for name, (out, expected) in PINGS.items():
    got = ping_ok(out)
    check(f"ping: {name}", got == expected, f"read as {'up' if got else 'down'}")


# ====================================================== traceroute ==========
TRACES = {
    "IOS-XE, arrives": ("""Type escape sequence to abort.
Tracing the route to 10.40.20.50
VRF info: (vrf in name/id, vrf out name/id)
  1 DC1-CORE-SW-01 (10.20.10.254) 1 msec 0 msec 0 msec
  2 DC2-CORE-SW-01 (10.40.20.254) 2 msec 1 msec 2 msec
  3 DC2-WEB-LB-01 (10.40.20.50) 2 msec 2 msec *""", 3, 0),

    "IOS-XE, dies": ("""Tracing the route to 10.60.40.12
  1 10.20.30.1 1 msec 1 msec 2 msec
  2  *  *  *
  3  *  *  *""", 3, 2),

    "NX-OS": ("""traceroute to 10.40.20.50 (10.40.20.50), 30 hops max, 40 byte packets
 1  10.20.10.254 (10.20.10.254)  1.234 ms  0.987 ms  1.011 ms
 2  * * *
 3  10.40.20.50 (10.40.20.50)  2.001 ms  1.888 ms  1.900 ms""", 3, 1),

    "IOS-XR": ("""Tracing the route to 10.40.20.50

 1  10.20.10.254 1 msec  1 msec  1 msec
 2  10.40.20.254 2 msec  2 msec  2 msec""", 2, 0),

    "Junos": ("""traceroute to 10.40.20.50 (10.40.20.50), 30 hops max, 40 byte packets
 1  dc1-core-sw-01.example.net (10.20.10.254)  1.111 ms  0.999 ms  1.010 ms
 2  * * *""", 2, 1),

    "Linux numeric": ("""traceroute to 10.40.20.50 (10.40.20.50), 5 hops max, 60 byte packets
 1  10.20.10.254  0.512 ms  0.480 ms  0.470 ms
 2  10.40.20.50  1.220 ms  1.180 ms  1.170 ms""", 2, 0),

    "Windows tracert": ("""Tracing route to 10.40.20.50 over a maximum of 30 hops

  1     1 ms     1 ms     1 ms  10.20.10.254
  2     *        *        *     Request timed out.
  3     2 ms     2 ms     2 ms  10.40.20.50

Trace complete.""", 3, 1),
}

print()
print("---- traceroute ------------------------------------------------------")
for name, (out, want_hops, want_timeouts) in TRACES.items():
    hops = parse_hops(out)
    timeouts = sum(1 for h in hops if h["timeout"])
    check(f"trace: {name} -- hop count", len(hops) == want_hops,
          f"got {len(hops)}, wanted {want_hops}: {[h['host'] or '*' for h in hops]}")
    check(f"trace: {name} -- dead hops", timeouts == want_timeouts,
          f"got {timeouts}, wanted {want_timeouts}")

# a hop must never be read as a device called "*"
for name, (out, _h, _t) in TRACES.items():
    hosts = [str(h["host"]) for h in parse_hops(out) if h["host"]]
    check(f"trace: {name} -- silence is not a device",
          not any(set(h) <= {"*", " "} for h in hosts), str(hosts))


# ================================================== the deep path ===========
# What `show route` / `show ip cef` / `show arp` / `show interface` really
# print. The deep path is read out of these, and every one of them spells the
# next hop differently.
DEEP_CASES = {
    "IOS-XR route + XR interface": ("""RP/0/RSP0/CPU0:DC1-APP-SW-07#show route 10.60.40.12
Routing entry for 10.60.40.0/24
  Known via "bgp 65010", distance 200, metric 0
  Routing Descriptor Blocks
    10.20.30.129, from 10.20.30.129
      Route metric is 0
RP/0/RSP0/CPU0:DC1-APP-SW-07#show arp 10.20.30.129
Address         Age        Hardware Addr   State      Type  Interface
10.20.30.129    -          Incomplete      Incomplete  ARPA TenGigE0/0/0/3
RP/0/RSP0/CPU0:DC1-APP-SW-07#show interfaces TenGigE0/0/0/3 brief
TenGigE0/0/0/3 is down, line protocol is down""", "10.20.30.129", False),

    "IOS-XE cef nexthop wording": ("""DC1-APP-SW-07#show ip cef 10.60.40.12
10.60.40.0/24
  nexthop 10.20.30.129 TenGigabitEthernet0/0/3
DC1-APP-SW-07#show ip arp 10.20.30.129
Protocol  Address     Age (min)  Hardware Addr   Type   Interface
Internet  10.20.30.129        -  Incomplete      ARPA
DC1-APP-SW-07#show interfaces TenGigabitEthernet0/0/3
TenGigabitEthernet0/0/3 is down, line protocol is down (notconnect)""",
                                   "10.20.30.129", False),

    "NX-OS *via form": ("""DC1-APP-SW-07# show ip route 10.60.40.12
10.60.40.0/24, ubest/mbest: 1/0
    *via 10.20.30.129, Eth1/3, [200/0], 02:14:31, bgp-65010
DC1-APP-SW-07# show ip arp 10.20.30.129
Address         Age       MAC Address     Interface
10.20.30.129    -         INCOMPLETE      Ethernet1/3
DC1-APP-SW-07# show interface Ethernet1/3
Ethernet1/3 is down (Link not connected)""", "10.20.30.129", False),

    "healthy IOS-XE": ("""DC1-EDGE-RTR-01#show ip route 10.40.20.50
Routing entry for 10.40.20.0/24
  Known via "bgp 65010"
  * 10.20.10.254, from 10.20.10.254, via TenGigabitEthernet0/0/0
DC1-EDGE-RTR-01#show ip arp 10.20.10.254
Internet  10.20.10.254        3  0050.56be.7f01  ARPA  TenGigabitEthernet0/0/0
DC1-EDGE-RTR-01#show interfaces TenGigabitEthernet0/0/0
TenGigabitEthernet0/0/0 is up, line protocol is up""", "10.20.10.254", None),
    # None, not True: route + adjacency + interface prove the FIRST HOP. They
    # say nothing about the destination, and a panel that draws an arrival
    # nothing showed is worse than one that admits the gap.
}

print()
print("---- the deep path ---------------------------------------------------")
for name, (out, want_hop, want_reached) in DEEP_CASES.items():
    got = path_from_checks([{"output": out}], "SRC", "DST")
    check(f"deep: {name} -- a path is drawn at all", got is not None)
    if not got:
        continue
    check(f"deep: {name} -- names the right next hop",
          want_hop in got["line"], got["line"])
    check(f"deep: {name} -- gets the verdict right",
          got["reached"] == want_reached,
          f"reached={got['reached']}: {got['note'][:70]}")

print()
print("ALL PASSED" if not fails else f"FAILED ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
