"""The invented world the mock MCP servers answer from.

One file so that the CMDB record, the ping, the traceroute, the deep-check
output, the firewall verdict and the open alerts all tell the SAME story. Split
across five mocks they drift, and a demo where the traceroute dies at a hop the
route command says is healthy teaches the audience to distrust the tool.

Nothing here is real: RFC 5737 / RFC 1918 addresses, invented hostnames, no
customer, site or employee names. Safe to run and screen-share anywhere.

Three scenarios:

  1. GOOD   DC1-EDGE-RTR-01 (10.20.10.1)  ->  DC2-WEB-LB-01 (10.40.20.50)
            Everything works. Ping succeeds, the path completes, Tufin permits
            tcp:443, and the one open alert is on an unrelated interface -- so
            the agent has to say "context, not cause" rather than blaming the
            nearest red thing.

  2. BAD    DC1-APP-SW-07 (10.20.30.7)    ->  DC3-DB-CLU-02 (10.60.40.12)
            Ping fails and the trace dies after the first hop, but TUFIN
            PERMITS IT -- so policy is not the answer and the deeper checks
            have to find the real fault: the uplink TenGigE0/0/0/3 is down, so
            the next hop never resolves in ARP and the route points into a
            hole. Archangel independently has an open ticket for that exact
            interface, which is the moment the alerts step earns its place.

  3. POLICY APP-SRV-DC1-020 (10.10.1.20)  ->  PAY-API-DC2-010 (172.20.5.10)
            The original: reachable underneath, denied by an ACL. Kept because
            the tests script against it.

Every device is reachable by NAME or by IP: the CMDB mock indexes both.
"""
import datetime as _dt


# --------------------------------------------------------------- inventory --
# name -> the few facts the CMDB mock turns into a full record
DEVICES = {
    # ---- scenario 1: the healthy path
    "DC1-EDGE-RTR-01": dict(ip="10.20.10.1", brand="Cisco", model="ASR 9006",
                            os="IOS XR", version="7.11.2", region="INDIA"),
    "DC2-WEB-LB-01":   dict(ip="10.40.20.50", brand="Arista",
                            model="DCS-7050SX3", os="EOS", version="4.31.2F",
                            region="INDIA"),
    "DC1-CORE-SW-01":  dict(ip="10.20.10.254", brand="Cisco",
                            model="N9K-C93180", os="NX-OS", version="10.3",
                            region="INDIA"),

    # ---- scenario 2: the broken path
    "DC1-APP-SW-07":   dict(ip="10.20.30.7", brand="Cisco",
                            model="C9500-32C", os="IOS XE", version="17.9.4",
                            region="INDIA"),
    "DC3-DB-CLU-02":   dict(ip="10.60.40.12", brand="Cisco",
                            model="N9K-C93240", os="NX-OS", version="10.3",
                            region="PARIS"),

    # ---- scenario 3: the original policy block
    "APP-SRV-DC1-020": dict(ip="10.10.1.20", brand="Cisco", model="ASR 9010",
                            os="IOS XE", version="17.9.3", region="INDIA"),
    "PAY-API-DC2-010": dict(ip="172.20.5.10", brand="Cisco",
                            model="N9K-C93180", os="NX-OS", version="10.2",
                            region="INDIA"),
    "LEAF-101":        dict(ip="10.10.1.1", brand="Cisco", model="N9K-C93108",
                            os="NX-OS", version="10.2", region="INDIA"),
    "BORDER-ROUTER-01": dict(ip="10.10.0.1", brand="Cisco", model="ASR 9906",
                             os="IOS XR", version="7.11.2", region="INDIA"),
    "FW-DC1-EDGE-01":  dict(ip="10.10.255.1", brand="Checkpoint",
                            model="CP-6800", os="Gaia", version="R81.20",
                            region="INDIA"),
}

BY_IP = {d["ip"]: name for name, d in DEVICES.items()}


def resolve(value: str) -> str:
    """A name or an address -> the canonical device name, or ''."""
    v = str(value or "").strip()
    return BY_IP.get(v) or (v.upper() if v.upper() in DEVICES else "")


def ip_of(value: str) -> str:
    """A name or an address -> the address."""
    name = resolve(value)
    return DEVICES[name]["ip"] if name else str(value or "").strip()


# ------------------------------------------------------------ reachability --
# destination address -> does a ping from the source get through
PING_FAILS = {
    "10.60.40.12",       # scenario 2: the uplink is down
    "172.20.5.10",       # scenario 3: the ACL drops it
    "203.0.113.245",
}

# destination address -> the hops a traceroute reports, and whether it arrives
TRACE = {
    "10.40.20.50": {
        "hops": [("DC1-CORE-SW-01", "10.20.10.254", "1 msec"),
                 ("DC2-CORE-SW-01", "10.40.20.254", "2 msec"),
                 ("DC2-WEB-LB-01", "10.40.20.50", "2 msec")],
        "arrives": True,
    },
    "10.60.40.12": {
        # dies immediately after the local core: the uplink out of the source
        # is down, so nothing past the first hop ever answers
        "hops": [("DC1-CORE-SW-01", "10.20.30.1", "1 msec")],
        "arrives": False,
    },
    "172.20.5.10": {
        "hops": [("Leaf-101", "10.10.1.1", "1 msec"),
                 ("Border-Router-01", "10.10.0.1", "2 msec"),
                 ("FW-DC1-EDGE-01", "10.10.255.1", "3 msec")],
        "arrives": False,
    },
}

# --------------------------------------------------------- firewall policy --
# Scenario 2 is deliberately PERMITTED: if Tufin blamed the firewall there
# would be nothing for the deeper checks to find, and the demo would show a
# workflow that stops at the first plausible answer.
BLOCKED_DESTS = {"172.20.5.10", "203.0.113.245"}

POLICY_RULES = {
    "10.40.20.50": "PERMIT-WEB-TIER",
    "10.60.40.12": "PERMIT-DB-REPLICATION",
}

def _raised(days_ago, hour=9):
    """A created time that many days back, in the shape the database returns.

    EPOCH SECONDS, because that is what the real alert.time column holds --
    the mock is worth little if it hands back a shape production never sends.

    Relative rather than fixed, so the mock alerts do not silently age past
    every filter the day after they are written.
    """
    when = (_dt.datetime.now() - _dt.timedelta(days=days_ago)).replace(
        hour=hour, minute=17, second=4, microsecond=0)
    return int(when.timestamp())


# -------------------------------------------------------------- open alerts --
ALERTS = {
    # scenario 1: one alert, on an interface that is NOT in the path. The
    # agent should report it as context and not as the cause -- there is
    # nothing wrong with this path.
    "DC2-WEB-LB-01": [
        {"alert_id": "3f21a8c4-0000-1111-2222-444444444444",
         "device_name": "DC2-WEB-LB-01", "alert_type": "environment",
         "alert_title": "PowerSupplyRedundancyLost",
         "check_name": "PSU 2", "ticket_id": "570000114", "severity": "warning", "alert_time": _raised(6)},
    ],

    # scenario 2: the smoking gun. The interface named here is the same one
    # the deep checks find down, and it has an open ticket already.
    "DC1-APP-SW-07": [
        {"alert_id": "b81c07de-0000-1111-2222-555555555555",
         "device_name": "DC1-APP-SW-07", "alert_type": "network",
         "alert_title": "LinkStatusOperDown",
         "check_name": "Interface TenGigE0/0/0/3", "ticket_id": "570000231", "severity": "critical", "alert_time": _raised(0)},
        {"alert_id": "b81c1a55-0000-1111-2222-555555555555",
         "device_name": "DC1-APP-SW-07", "alert_type": "network",
         "alert_title": "BGPNeighborDown",
         "check_name": "Neighbor 10.20.30.129", "ticket_id": "570000231", "severity": "critical", "alert_time": _raised(0)},
        {"alert_id": "c40d99f1-0000-1111-2222-555555555555",
         "device_name": "DC1-APP-SW-07", "alert_type": "network",
         "alert_title": "InterfaceErrorRateHigh",
         "check_name": "Interface TenGigE0/0/0/3", "ticket_id": "570000232", "severity": "warning", "alert_time": _raised(3)},
    ],

    # scenario 3: unchanged, the tests script against these
    "APP-SRV-DC1-020": [
        {"alert_id": "9a7fc3aa-0000-1111-2222-333333333333",
         "device_name": "APP-SRV-DC1-020", "alert_type": "network",
         "alert_title": "LinkStatusOperDown",
         "check_name": "Interface Bundle-Ether90", "ticket_id": "560000001", "severity": "critical", "alert_time": _raised(2)},
        {"alert_id": "9a7ff104-0000-1111-2222-333333333333",
         "device_name": "APP-SRV-DC1-020", "alert_type": "network",
         "alert_title": "LinkStatusOperDown",
         "check_name": "Interface TenGigE0/0/0/14", "ticket_id": "560000001", "severity": "major", "alert_time": _raised(8)},
        {"alert_id": "fc23e54e-0000-1111-2222-333333333333",
         "device_name": "APP-SRV-DC1-020", "alert_type": "network",
         "alert_title": "InterfaceAlert",
         "check_name": "Interface Bundle-Ether20.3003", "ticket_id": "560000002", "severity": "warning", "alert_time": _raised(14)},
    ],
    "PAY-API-DC2-010": [
        {"alert_id": "a2f5d3a6-0000-1111-2222-333333333333",
         "device_name": "PAY-API-DC2-010", "alert_type": "network",
         "alert_title": "InterfaceAlert",
         "check_name": "Interface TenGigE0/0/0/5", "ticket_id": "560000003", "severity": "minor", "alert_time": _raised(1)},
    ],
}

# ------------------------------------------------------------ deep checks ----
# What each SOURCE device answers to the escalation commands. Keyed by device
# so the two scenarios can disagree: the same `show interface` has to be
# healthy on one and down on the other, or there is only ever one story.
#
# Each entry is (regex matched against the lower-cased command, reply).

_HEALTHY_DEEP = [
    (r"^show\s+(ip\s+)?route\s+",
     "Routing entry for 10.40.20.0/24\n"
     "  Known via \"bgp 65010\", distance 20, metric 0, best\n"
     "  Routing Descriptor Blocks:\n"
     "  * 10.20.10.254, from 10.20.10.254, via TenGigE0/0/0/0\n"
     "      Route metric is 0, traffic share count is 1"),
    (r"^show\s+vrf",
     "No VRFs configured. All interfaces are in the global routing table."),
    (r"^show\s+(ip\s+)?cef\s+",
     "10.40.20.0/24, version 210, attached, cached adjacency 10.20.10.254\n"
     "  via 10.20.10.254, TenGigE0/0/0/0, 5 dependencies\n"
     "    next hop 10.20.10.254, TenGigE0/0/0/0  (complete)"),
    (r"^show\s+(ip\s+)?arp",
     "Protocol  Address        Age (min)  Hardware Addr   Type  Interface\n"
     "Internet  10.20.10.254           3  0050.56be.7f01  ARPA  TenGigE0/0/0/0"),
    (r"^show\s+interfaces?\s+",
     "TenGigE0/0/0/0 is up, line protocol is up\n"
     "  Description: uplink to DC1-CORE-SW-01\n"
     "  Last link flap 21 weeks, 3 days ago\n"
     "  0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored\n"
     "  0 output errors, 0 collisions, 0 output drops"),
    (r"^show\s+(ip\s+)?bgp\s+summary",
     "Neighbor        AS      Up/Down  State/PfxRcd\n"
     "10.20.10.254    65010   21w3d    1847"),
    (r"^show\s+access-lists?",
     "ipv4 access-list DC-EGRESS\n"
     " 10 permit tcp any 10.40.0.0/16 eq 443 (18422 matches)\n"
     " 90 permit ipv4 any any"),
    (r"^show\s+logging",
     "No matching entries in the last 24 hours."),
]

# The broken source. Every answer here points at ONE fault, from a different
# angle: the route is fine, the forwarding entry is not, the next hop never
# resolved, and the interface it would leave by is down.
_BROKEN_DEEP = [
    (r"^show\s+(ip\s+)?route\s+",
     "Routing entry for 10.60.40.0/24\n"
     "  Known via \"bgp 65010\", distance 200, metric 0\n"
     "  Last update from 10.20.30.129 02:14:31 ago\n"
     "  Routing Descriptor Blocks:\n"
     "  * 10.20.30.129, from 10.20.30.129, via TenGigE0/0/0/3\n"
     "      Route metric is 0\n"
     "      NOTE: next hop is not resolved"),
    (r"^show\s+vrf",
     "No VRFs configured. All interfaces are in the global routing table."),
    (r"^show\s+(ip\s+)?cef\s+",
     "10.60.40.0/24, version 903, epoch 0\n"
     "  via 10.20.30.129, TenGigE0/0/0/3, 0 dependencies\n"
     "    next hop 10.20.30.129, TenGigE0/0/0/3  (INCOMPLETE - drop adjacency)\n"
     "  packets dropped: 41,882"),
    (r"^show\s+(ip\s+)?arp",
     "Protocol  Address        Age (min)  Hardware Addr   Type  Interface\n"
     "Internet  10.20.30.129           -  Incomplete      ARPA  TenGigE0/0/0/3\n"
     "Internet  10.20.30.1             2  0050.56be.4402  ARPA  TenGigE0/0/0/1"),
    (r"^show\s+interfaces?\s+ten\S*0/0/0/1\b",
     "TenGigE0/0/0/1 is up, line protocol is up\n"
     "  Description: uplink to DC1-CORE-SW-01 (primary)\n"
     "  0 input errors, 0 CRC, 0 output drops"),
    (r"^show\s+interfaces?\s+",
     "TenGigE0/0/0/3 is down, line protocol is down (notconnect)\n"
     "  Description: uplink to DC1-CORE-SW-02 (DB replication)\n"
     "  Last link flap 02:14:33 ago\n"
     "  Hardware is Ten Gigabit Ethernet, address is 0050.56be.9d31\n"
     "  1874 input errors, 1874 CRC, 0 frame, 0 overrun\n"
     "  Transceiver: Rx power -40.0 dBm (LOW-ALARM), Tx power -2.1 dBm"),
    (r"^show\s+(ip\s+)?bgp\s+summary",
     "Neighbor        AS      Up/Down   State/PfxRcd\n"
     "10.20.30.1      65010   14w2d     2104\n"
     "10.20.30.129    65010   02:14:33  Idle"),
    (r"^show\s+(ip\s+)?bgp\s+",
     "BGP routing table entry for 10.60.40.0/24\n"
     "  Paths: (1 available, no best path)\n"
     "  Local, from 10.20.30.129 -- peer is Idle, path not usable"),
    (r"^show\s+access-lists?",
     "ipv4 access-list DC-EGRESS\n"
     " 10 permit tcp any 10.60.0.0/16 eq 3306 (0 matches)\n"
     " 90 permit ipv4 any any\n"
     "  (no denies matched -- nothing here is dropping this traffic)"),
    (r"^show\s+logging",
     "Feb 18 04:12:44: %LINK-3-UPDOWN: Interface TenGigE0/0/0/3, changed "
     "state to down\n"
     "Feb 18 04:12:45: %LINEPROTO-5-UPDOWN: Line protocol on Interface "
     "TenGigE0/0/0/3, changed state to down\n"
     "Feb 18 04:12:46: %BGP-5-ADJCHANGE: neighbor 10.20.30.129 Down "
     "Interface flap\n"
     "Feb 18 04:12:51: %PLATFORM-4-XCVR_ALARM: TenGigE0/0/0/3 Rx power low"),
    (r"^show\s+(interfaces?\s+)?transceiver",
     "TenGigE0/0/0/3   Rx power  -40.0 dBm  (LOW-ALARM)   Tx power -2.1 dBm\n"
     "TenGigE0/0/0/1   Rx power   -2.4 dBm  (normal)      Tx power -2.2 dBm"),
]

DEEP = {
    "DC1-EDGE-RTR-01": _HEALTHY_DEEP,
    "DC1-APP-SW-07": _BROKEN_DEEP,
}

# scenario 3 keeps the original answers: route fine, forwarding fine, next hop
# alive -- so nothing local explains it and the ACL is the only candidate left
DEEP["APP-SRV-DC1-020"] = [
    (r"^show\s+(ip\s+)?route\s+",
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
    (r"^show\s+(ip\s+)?arp",
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
    (r"^show\s+(ip\s+)?bgp",
     "BGP routing table entry for 172.20.0.0/16\n"
     "  Not advertised to any peer\n"
     "  Local, from 10.10.1.1 (10.10.1.1)\n"
     "    Origin IGP, metric 0, localpref 100, valid, internal, best"),
    (r"^show\s+mpls",
     "No MPLS forwarding entry for that prefix (no label switching on this path)."),
    (r"^show\s+logging",
     "No matching entries in the last 24 hours."),
]

# used when the source is not one of the three above
DEFAULT_DEEP = _HEALTHY_DEEP
