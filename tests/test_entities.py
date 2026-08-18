"""Names must not reach the model, and must come back intact.

Covers the identifiers the CMDB and Tufin return, plus hostnames and interface
names appearing in CLI output. Every one has to survive the round trip, because
the reversed value is what ends up in a command sent to a real device.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import entities                    # noqa: E402
from agent.llm.ip_mask import IpMask          # noqa: E402

CMDB = json.dumps({
    "region": "PARIS", "lookup_by": "ip", "query": "10.10.1.20",
    "data": {"brand": "cisco", "brandModel": "ASR9010",
             "operatingSystemVersion": "IOS-XR 7.5.2",
             "managementIp": "10.10.1.20", "name": "APP-SRV-DC1-020"}})

TUFIN = json.dumps({
    "verdict": "BLOCKED", "traffic_allowed": False,
    "blocking_rules": [{"action": "Drop", "acl": "DENY-ALL",
                        "destinations": ["any"], "services": ["Any"]}],
    "device_path": [
        {"name": "RTR-EXAMPLE-02", "vendor": "Cisco",
         "interfaces": [{"name": "Loopback99", "ip": "203.0.113.1",
                         "vrf": "EXAMPLE-VRF-A"}],
         "next_hops": [{"device": "SW-EXAMPLE-B1", "next_hop": "203.0.113.45",
                        "interface": "TenGigE0/0/0/0", "mpls_label": "27084"}]}],
    "unrouted_elements": []})

TRACE = ("Tracing the route to 172.20.5.10\n"
         "  1 Leaf-101 (10.10.1.1) 1 msec\n"
         "  2 Border-Router-01 (10.10.0.1) 2 msec\n"
         "  3 FW-DC1-EDGE-01 (10.10.255.1) 3 msec\n  4 * * *")

ROUTE = ("Routing entry for 172.20.0.0/16\n"
         "  * 10.10.1.1, from 10.10.1.1, via TenGigE0/0/0/1")

SECRET_NAMES = ["APP-SRV-DC1-020", "DENY-ALL", "RTR-EXAMPLE-02", "SW-EXAMPLE-B1",
                "EXAMPLE-VRF-A", "Loopback99", "TenGigE0/0/0/0",
                "TenGigE0/0/0/1", "Leaf-101", "Border-Router-01",
                "FW-DC1-EDGE-01", "PARIS", "27084"]

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


m = IpMask()
for tool, text in (("get_device_details", CMDB), ("get_firewall_path", TUFIN),
                   ("execute_query_on_server", TRACE),
                   ("execute_query_on_server", ROUTE)):
    entities.learn(m, tool, text)

print(f"\nregistered {len(m.terms())} names")
for real, fake in sorted(m.terms().items()):
    print(f"  {real:<22} -> {fake}")

whole = CMDB + "\n" + TUFIN + "\n" + TRACE + "\n" + ROUTE
masked = m.mask(whole)

leaked = [n for n in SECRET_NAMES if n in masked]
check("no real name survives the mask", not leaked, f"leaked={leaked}")
check("no real address survives either",
      not any(ip in masked for ip in
              ["10.10.1.20", "172.20.5.10", "203.0.113.1", "203.0.113.45"]))
check("round trip is exact", m.unmask(masked) == whole)

# what the model still needs to choose syntax
check("vendor survives", "cisco" in masked)
check("OS survives", "IOS-XR" in masked)
check("verdict survives", "BLOCKED" in masked)
check("action survives", "Drop" in masked)

# a reply quoting stand-ins must come back real, because it becomes a command
host = m.register("FW-DC1-EDGE-01", "host")
acl = m.register("DENY-ALL", "acl")
intf = m.register("TenGigE0/0/0/1", "intf")
region = m.register("PARIS", "region")
reply = json.dumps({"tool": "execute_query_on_server",
                    "args": {"device_ip": m.mask_ip("10.10.1.20"),
                             "region": region,
                             "commands": [f"show interface {intf} brief",
                                          f"show access-lists {acl}"]},
                    "thought": f"{host} is the edge firewall."})
back = json.loads(m.unmask(reply))
check("region unmasks for the tool call", back["args"]["region"] == "PARIS",
      back["args"]["region"])
check("interface unmasks inside a command",
      back["args"]["commands"][0] == "show interface TenGigE0/0/0/1 brief",
      back["args"]["commands"][0])
check("acl unmasks inside a command",
      back["args"]["commands"][1] == "show access-lists DENY-ALL")
check("hostname unmasks in the reasoning",
      "FW-DC1-EDGE-01" in back["thought"])

# a model that retypes the case must still resolve
check("stand-in survives a case change",
      m.unmask(acl.upper()) == "DENY-ALL", m.unmask(acl.upper()))

# longest-first: DC1-EDGE must not be half-eaten by a shorter registered name
m2 = IpMask()
m2.register("DC1", "host")
m2.register("FW-DC1-EDGE-01", "host")
masked2 = m2.mask("FW-DC1-EDGE-01 sits behind DC1")
check("longest name replaced first", m2.unmask(masked2) == "FW-DC1-EDGE-01 sits behind DC1",
      masked2)

check("registering twice is stable", m.register("DENY-ALL", "acl") == acl)
check("an unknown stand-in is left alone", m.unmask("acl-99") == "acl-99")

# ---- hostnames that only ever appear in SSH output -------------------------
SSH = """APP-SRV-DC1-020#show cdp neighbors
Device ID: CORE-SW-DC1-01
  Interface: TenGigE0/0/0/1,  Port ID (outgoing port): Gi1/0/24
LEAF-102-DC1     Gi0/1     142    R S I
APP-SRV-DC1-020#show run | include hostname
hostname APP-SRV-DC1-020
APP-SRV-DC1-020#show lldp neighbors detail
System Name: EDGE-RTR-PAR-07
APP-SRV-DC1-020#ping 172.20.5.10
Success rate is 0 percent (0/5)
"""
m3 = IpMask()
entities.learn(m3, "execute_query_on_server", SSH)
ssh_masked = m3.mask(SSH)
cli_only = ["APP-SRV-DC1-020", "CORE-SW-DC1-01", "LEAF-102-DC1",
            "EDGE-RTR-PAR-07", "TenGigE0/0/0/1", "Gi1/0/24", "Gi0/1"]
still_there = [n for n in cli_only if n in ssh_masked]
check("SSH-only hostnames and interfaces masked", not still_there,
      f"leaked={still_there}")
check("prompt echo masked", "node-" in ssh_masked.splitlines()[0])
check("SSH round trip exact", m3.unmask(ssh_masked) == SSH)

# ---- and the opposite risk: ordinary CLI text must not be mistaken for names
NOISE = """Routing entry for 172.20.0.0/16
  Known via "bgp 65001", distance 20, metric 0
Codes: L - local, C - connected, S - static
Type escape sequence to abort.
Success rate is 100 percent (5/5), round-trip min/avg/max = 1/1/2 ms
% Invalid input detected at '^' marker.
Total number of prefixes 42
Warning: something happened
BGP table version is 84, local router ID is 10.10.1.20
"""
m4 = IpMask()
entities.learn(m4, "execute_query_on_server", NOISE)
check("no false-positive hostnames from plain CLI output",
      len(m4.terms()) == 0, str(m4.terms()))
noise_masked = m4.mask(NOISE)
check("CLI keywords survive untouched",
      all(k in noise_masked for k in
          ["Routing entry", "Codes:", "Success rate", "Invalid input", "BGP table"]))
check("noise round trip exact", m4.unmask(noise_masked) == NOISE)

# ---- interface names across every vendor the agent talks to ----------------
from agent.entities import _IFACE                     # noqa: E402

VENDOR_IFACES = {
    "Cisco IOS": ["GigabitEthernet0/1", "FastEthernet0/0",
                  "TenGigabitEthernet1/0/1", "Serial0/0/0:0", "Loopback0",
                  "Vlan100", "Port-channel10", "Tunnel0", "Null0", "BVI1",
                  "Dialer1", "ATM0/0", "POS1/0", "Gi0/0", "Fa0/1", "Te1/1/1"],
    "IOS-XR": ["TenGigE0/0/0/0", "HundredGigE0/0/0/1", "Bundle-Ether100", "Lo0",
               "BE100", "MgmtEth0/RP0/CPU0/0", "Mg0/0/CPU0/0", "tunnel-te1",
               "tunnel-ip1", "PW-Ether1"],
    "NX-OS": ["Ethernet1/1", "port-channel10", "Vlan10", "nve1", "Vethernet1",
              "Po10", "Eth1/1"],
    "Arista": ["Ethernet3/1", "Management1", "Port-Channel10", "Vxlan1",
               "Loopback0"],
    "Junos": ["ge-0/0/0", "xe-0/0/0.100", "et-1/0/0:2", "ae0", "ae0.100", "lo0",
              "lo0.0", "fxp0", "em0", "reth1", "irb.100", "vlan.100", "st0.0",
              "gr-0/0/0"],
    "Huawei VRP": ["GigabitEthernet0/0/1", "Eth-Trunk1", "Vlanif100",
                   "LoopBack0", "10GE1/0/1", "100GE1/0/1", "MEth0/0/1",
                   "GE1/0/1"],
    "H3C Comware": ["Ten-GigabitEthernet1/0/1", "Bridge-Aggregation1",
                    "Vlan-interface100", "M-GigabitEthernet0/0/0", "BAGG1",
                    "XGE1/0/1"],
    "PAN-OS": ["ethernet1/1", "tunnel.1", "loopback.1"],
    "Cumulus": ["swp1", "swp1s0"],
    "MikroTik": ["ether1", "sfp-sfpplus1", "bridge1"],
}
for vendor, names in VENDOR_IFACES.items():
    unmatched = [n for n in names if _IFACE.fullmatch(n) is None]
    check(f"{vendor} interfaces recognised", not unmatched, f"missed {unmatched}")

IFACE_FALSE_POSITIVES = [
    "Ethernet", "the tunnel", "internal", "Success rate", "12:34:56", "1/1/2",
    "IOS-XR 7.5.2", "AS 65001", "Codes: L - local", "min/avg/max",
    "BGP table version is 84", "administratively down", "Link not connected",
    "Last input 00:00:02", "load 0.15", "router ID", "WiFi 5",
]
hits = [t for t in IFACE_FALSE_POSITIVES if _IFACE.search(t)]
check("interface pattern does not fire on ordinary text", not hits, str(hits))

# ---- the prompt form differs per vendor, and each one carries the hostname --
PROMPTS = """user@EDGE-RTR-PAR-07> show interfaces terse
<CORE-SW-BJ-01>display interface brief
[CORE-SW-BJ-02]display vlan
RP/0/RSP0/CPU0:XR-PE-LON-03#show route
APP-SRV-DC1-020#show ip int brief
admin@FW-GAIA-01:~$ netstat -rn
root@LNX-JUMP-01:/var/log# tail syslog
[edit]
"""
m5 = IpMask()
entities.learn(m5, "execute_query_on_server", PROMPTS)
pm = m5.mask(PROMPTS)
prompt_hosts = ["EDGE-RTR-PAR-07", "CORE-SW-BJ-01", "CORE-SW-BJ-02",
                "XR-PE-LON-03", "APP-SRV-DC1-020", "FW-GAIA-01", "LNX-JUMP-01"]
still = [h for h in prompt_hosts if h in pm]
check("hostname masked in every vendor prompt form", not still, f"leaked={still}")
check("Junos '[edit]' is not treated as a hostname", "[edit]" in pm)
check("prompt round trip exact", m5.unmask(pm) == PROMPTS)

# ---- VRF output: names come from tables, not JSON --------------------------
VRF = """APP-SRV-DC1-020#show vrf
  Name                             Default RD            Protocols   Interfaces
  PAYMENTS-PROD                    65001:100             ipv4        Gi0/0.100
  EXAMPLE-VRF-A                    10.10.1.1:200         ipv4        Gi0/1
RP/0/RSP0/CPU0:CORE-RTR-01#show vrf all detail
VRF PAYMENTS-PROD; RD 65001:100; VPN ID not set
    TenGigE0/0/0/1.100
      RT:65001:100
RP/0/RSP0/CPU0:CORE-RTR-01#show route vrf PAYMENTS-PROD 172.20.5.10
  Installed Aug  6 10:22:31
    203.0.113.9, from 203.0.113.9
      Nexthop in Vrf: "default", Table id 0xe0000000
<CORE-SW-BJ-01>display ip vpn-instance verbose
 VPN-Instance Name and ID : PAYMENTS-BJ, 1
 Route Distinguisher : 65002:300
 Interfaces : Vlanif100
user@EDGE-RTR-PAR-07> show route instance PAYMENTS-VPN
routing-instance PAYMENTS-VPN
"""
m6 = IpMask()
entities.learn(m6, "execute_query_on_server", VRF)
vm = m6.mask(VRF)
vrf_secrets = ["PAYMENTS-PROD", "EXAMPLE-VRF-A", "PAYMENTS-BJ", "PAYMENTS-VPN",
               "65001:100", "65002:300", "Gi0/0.100", "TenGigE0/0/0/1.100",
               "Vlanif100", "10.10.1.1", "172.20.5.10", "203.0.113.9",
               "APP-SRV-DC1-020", "CORE-RTR-01", "CORE-SW-BJ-01",
               "EDGE-RTR-PAR-07"]
vrf_leaked = [s for s in vrf_secrets if s in vm]
check("VRF output leaks nothing", not vrf_leaked, f"leaked={vrf_leaked}")
check("route distinguisher masked", "rd-" in vm)
check("a timestamp is not mistaken for a route distinguisher", "10:22:31" in vm)
check("the 'default' VRF is left alone", '"default"' in vm)
check("VRF round trip exact", m6.unmask(vm) == VRF)

# ---- "show vrf all" as the ssh tool actually returns it ---------------------
# A list of {cmd, stdout, ...} dicts with CRLF inside. Flattening that with a
# bare str() produced a Python repr whose newlines were escaped, so every
# line-anchored rule matched nothing and VRF names reached the paste in clear.
from agent.utils import tool_text                       # noqa: E402

SSH_VRF = [{"cmd": "show vrf all",
            "stdout": ("VRF                              RD\r\n"
                       "L1_EXAMPLE_DEV                   64911:114026245\r\n"
                       "  import  64911:114000000    IPV4 Unicast\r\n"
                       "  export  64911:114000000    IPV4 Unicast\r\n"
                       "L1_EXAMPLE_PRD                   64911:313822245\r\n"
                       "  import  64911:313800000    IPV4 Unicast\r\n"),
            "stderr": "", "rc": 0}]
flat = tool_text(SSH_VRF)
check("ssh result flattens to real newlines, not an escaped repr",
      "\n" in flat and "\\n" not in flat, repr(flat[:60]))
check("the command is shown, not the dict", flat.lstrip().startswith("# show vrf all"))

m7 = IpMask()
entities.learn(m7, "execute_query_on_server", flat)
fm = m7.mask(flat)
vrf_all_secrets = ["L1_EXAMPLE_DEV", "L1_EXAMPLE_PRD", "64911:114026245",
                   "64911:114000000", "64911:313822245", "64911:313800000"]
gone = [s for s in vrf_all_secrets if s in fm]
check("show vrf all leaks no VRF name or route target", not gone, f"leaked={gone}")
check("'import' and 'export' are not registered as VRF names",
      "import" in fm and "export" in fm, fm)
check("show vrf all round trip exact", m7.unmask(fm) == flat)

# "show route vrf all" heads each block with "VRF: name", and IOS-XR decorates
# some of them -- "VRF: **nVSatellite" -- which slipped past a pattern that
# expected a letter straight after the colon.
ROUTE_VRF = ("Fri Aug  7 10:24:06.691 MEST\n\n"
             "VRF: **nVSatellite\n\n% Network not in table\n\n"
             "VRF: L1_PAYMENTS_PRD\n\nRouting entry for 0.0.0.0/0\n"
             '  Known via "bgp 64911", distance 200, metric 0\n'
             "    10.4.1.130, from 10.4.1.190, BGP backup path\n"
             '      Nexthop in Vrf: "default", Table: "default"\n\n'
             "VRF: L1_LUX_DEV\n")
m8 = IpMask()
entities.learn(m8, "execute_query_on_server", ROUTE_VRF)
rm = m8.mask(ROUTE_VRF)
route_vrf_names = ["nVSatellite", "L1_PAYMENTS_PRD", "L1_LUX_DEV",
                   "10.4.1.130", "10.4.1.190"]
rleaked = [n for n in route_vrf_names if n in rm]
check("show route vrf all masks a decorated VRF name", not rleaked,
      f"leaked={rleaked}")
check("the 'default' VRF is still left alone", '"default"' in rm)
check("show route vrf all round trip exact", m8.unmask(rm) == ROUTE_VRF)

sys.exit(1 if fails else 0)
