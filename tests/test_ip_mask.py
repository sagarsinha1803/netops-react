"""Round-trip checks for ip_mask against realistic CLI output."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from agent.llm.ip_mask import IpMask, PoolCollision  # noqa: E402

SSH = """APP-SRV-DC1-020# show route 172.20.5.10
Routing entry for 172.20.0.0/16
  Known via "bgp 65001", distance 20, metric 0
  * 10.10.1.1, from 10.10.1.1, via TenGigE0/0/0/1
APP-SRV-DC1-020# traceroute 172.20.5.10 maxttl 5
  1 Leaf-101 (10.10.1.1) 1 msec
  2 Border-Router-01 (10.10.0.1) 2 msec
  3 FW-DC1-EDGE-01 (10.10.255.1) 3 msec
  4 * * *
APP-SRV-DC1-020# show arp | include 10.10.1.1
Internet  10.10.1.1   4  0050.56be.1a2b  ARPA  TenGigE0/0/0/1
APP-SRV-DC1-020# show ip interface brief
GigabitEthernet0/0  10.10.1.20  255.255.255.0  up  up
ip route 0.0.0.0 0.0.0.0 10.10.1.1
access-list 101 deny ip any host 172.20.5.10 0.0.0.255
"""

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


m = IpMask()
masked = m.mask(SSH)

# 1. no real address survives
reals = ["10.10.1.20", "10.10.1.1", "10.10.0.1", "10.10.255.1", "172.20.5.10",
         "172.20.0.0"]
leaked = [r for r in reals if r in masked]
check("no real address in the masked text", not leaked, f"leaked={leaked}")

# 2. round trip is exact
check("round trip restores the original byte for byte", m.unmask(masked) == SSH)

# 3. netmask, wildcard and default route untouched
check("subnet mask 255.255.255.0 preserved", "255.255.255.0" in masked)
check("default route 0.0.0.0 preserved", "ip route 0.0.0.0 0.0.0.0" in masked)
check("ACL wildcard 0.0.0.255 preserved", "0.0.0.255" in masked)

# 4. same real /24 -> same fake /24 (subnet reasoning survives)
a, b = m.mask_ip("10.10.1.20"), m.mask_ip("10.10.1.1")
check("same real /24 lands in the same fake /24",
      a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0], f"{a} {b}")
c = m.mask_ip("10.10.0.1")
check("different real /24 lands in a different fake /24",
      c.rsplit(".", 1)[0] != a.rsplit(".", 1)[0], f"{c} vs {a}")

# 5. stable across calls (the map must not drift between turns)
check("mapping is stable on re-mask", m.mask_ip("10.10.1.20") == a)

# 6. host octet preserved
check("host octet preserved", a.endswith(".20"), a)

# 7. a model-invented stand-in stays fake rather than resolving to a device
bogus = "198.19.240.77"
check("unknown stand-in is not resolved", m.unmask_ip(bogus) == bogus)

# 8. what a Copilot reply actually looks like coming back
reply = ('{"thought": "Route is present via ' + b + '. Pinging the next hop.", '
         '"tool": "execute_query_on_server", "args": {"device_ip": "' + a + '", '
         '"region": "INDIA", "commands": ["ping ' + b + ' repeat 3"]}}')
back = m.unmask(reply)
check("reply unmasks to the real device_ip", '"device_ip": "10.10.1.20"' in back)
check("reply unmasks the command", '"ping 10.10.1.1 repeat 3"' in back)
check("no stand-in survives into the reply", "198.18." not in back and "198.19." not in back)

# 9. a real address inside the pool must fail loudly, not silently mismap
try:
    IpMask(pool="10.10.0.0/15").mask("ping 10.10.1.20")
    check("real address inside the pool raises", False)
except PoolCollision:
    check("real address inside the pool raises", True)

# 10. volume: 400 addresses across 40 subnets
big = " ".join(f"10.{n // 10}.{n % 10}.{h}" for n in range(40) for h in (1, 2, 3))
bigm = m.mask(big)
check("bulk round trip", m.unmask(bigm) == big, f"{len(big.split())} addresses")

# 11. label style -- alphanumeric stand-ins, same guarantees
lab = IpMask(style="label")
lm = lab.mask(SSH)
lleaked = [r for r in reals if r in lm]
check("label: no real address survives", not lleaked, f"leaked={lleaked}")
check("label: round trip exact", lab.unmask(lm) == SSH)
check("label: netmask preserved", "255.255.255.0" in lm)
check("label: wildcard preserved", "0.0.0.255" in lm)
la, lb = lab.mask_ip("10.10.1.20"), lab.mask_ip("10.10.1.1")
check("label: same /24 shares a subnet index",
      la.split(".h")[0] == lb.split(".h")[0], f"{la} {lb}")
check("label: host octet preserved", la.endswith(".h20"), la)
check("label: unknown index left alone", lab.unmask("ip4.n999.h7") == "ip4.n999.h7")
check("label: survives the model changing case",
      lab.unmask(la.upper()) == "10.10.1.20", lab.unmask(la.upper()))
check("label: hostnames are not mistaken for labels",
      "Leaf-101" in lm and "FW-DC1-EDGE-01" in lm)
lreply = ('{"tool":"execute_query_on_server","args":{"device_ip":"' + la +
          '","commands":["ping ' + lb + ' repeat 3"]}}')
lback = lab.unmask(lreply)
check("label: reply unmasks to real",
      '"device_ip":"10.10.1.20"' in lback and '"ping 10.10.1.1 repeat 3"' in lback,
      lback)

print("\nLABEL STYLE SAMPLE\n" + "-" * 50)
print("\n".join(lm.splitlines()[4:8]))

# 12. IPv6 and MAC addresses
V6 = """APP-SRV#show ipv6 neighbors
2001:db8:acad:1::1     14  0050.56be.1a2b  REACH Gi0/0
fe80::21b:d4ff:fe1c:8a01  0  00:1b:d4:1c:8a:01  STALE
2001:db8:acad:1::99    22  00-1B-D4-1C-8A-02   REACH
::ffff:10.10.1.20      -   -                   -
Internet  10.10.1.1   4  0050.56be.aaaa  ARPA  Gi0/0
"""
m5 = IpMask()
v6m = m5.mask(V6)
v6_reals = ["2001:db8:acad:1::1", "fe80::21b:d4ff:fe1c:8a01", "2001:db8:acad:1::99",
            "0050.56be.1a2b", "00:1b:d4:1c:8a:01", "00-1B-D4-1C-8A-02",
            "0050.56be.aaaa", "10.10.1.1"]
check("no IPv6 or MAC survives", not [r for r in v6_reals if r in v6m],
      str([r for r in v6_reals if r in v6m]))
check("IPv6/MAC round trip exact", m5.unmask(v6m) == V6)
check("MAC keeps its original format",
      "0000.5e00.5300" in v6m and "00:00:5e:00:53:01" in v6m
      and "00-00-5E-00-53-02" in v6m, v6m)
check("same /64 shares an index",
      m5.mask_v6("2001:db8:acad:1::1").rsplit("::", 1)[0]
      == m5.mask_v6("2001:db8:acad:1::99").rsplit("::", 1)[0])
check("EUI-64 hardware address not carried into the stand-in",
      "1c8a01" not in m5.mask_v6("fe80::21b:d4ff:fe1c:8a01"))

# timestamps and counters are hex-and-colon shaped but are not addresses
TIMES = ("Uptime is 12:34:56, load 0.15\nLast input 00:00:02, output 00:00:01\n"
         "Up/Down 05:22:11\nCodes: L - local\n[MPLS: Label 27084 Exp 0]\n")
m6 = IpMask()
check("timestamps are not mistaken for IPv6", m6.mask(TIMES) == TIMES, m6.mask(TIMES))

print(f"\nsubnets mapped: {len(m)}")
for real, fake in m.pairs()[:5]:
    print(f"  {real:<16} -> {fake}")

print("\nMASKED SAMPLE\n" + "-" * 50)
print("\n".join(masked.splitlines()[:9]))

sys.exit(1 if fails else 0)
