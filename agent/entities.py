"""Learn the names that must not reach the model, from the tool results.

Addresses can be recognised by shape, so ip_mask finds them on its own. Names
cannot -- FW-DC1-EDGE-01 and TenGigE0/0/0/1 look like ordinary words. So they
are registered as they arrive, from the two tools whose replies are structured
and therefore unambiguous (the CMDB and Tufin), plus hostnames appearing in
traceroute output.

Registration is deliberately narrow. Guessing at names by pattern would risk
mangling a command the model then has to run, and a stand-in that leaks into a
command is worse than a name that leaks into a paste.

Everything registered here is REVERSIBLE: Copilot sees node-1 and acl-2, and the
reply is turned back before the command is validated or the report is rendered,
so the approval prompt and the panel still show FW-DC1-EDGE-01 and DENY-ALL.
Credentials are a different problem and are dropped at the MCP boundary --
see mcp_tools/redact.py -- never registered here.
"""
import ast
import json
import re

# "  1 FW-DC1-EDGE-01 (10.10.255.1) 3 msec" -- a named hop in traceroute output
_HOP_NAME = re.compile(r"^\s*\d+\s+([A-Za-z][\w.\-]{2,})\s*\(", re.M)

# Hostnames that only ever appear in CLI output, never in the CMDB. Each is a
# narrow, anchored pattern rather than a general "looks like a hostname" guess:
# over-matching here would put a stand-in inside a command the model then runs.
_CLI_HOSTNAME = (
    # the device echoing its own prompt. Every vendor writes it differently:
    #   Cisco IOS / NX-OS   APP-SRV-DC1-020#show route ...
    #   Cisco IOS-XR        RP/0/RSP0/CPU0:CORE-RTR-01#
    #   Junos, Linux, Gaia  user@EDGE-RTR-PAR-07> show interfaces
    #   Huawei user-view    <CORE-SW-BJ-01>display interface brief
    #   Huawei/H3C sysview  [CORE-SW-BJ-01]
    re.compile(r"^\s*(?:\S+:)?([A-Za-z][\w.\-]{2,})\s*[#>]", re.M),
    # user@host> (Junos) and user@host:~$ / user@host:/var/log# (Gaia, Linux)
    re.compile(r"^\s*\S+@([A-Za-z][\w.\-]{2,})(?::\S*)?\s*[#>$]", re.M),
    re.compile(r"^\s*<([A-Za-z][\w.\-]{2,})>", re.M),
    re.compile(r"^\s*\[([A-Za-z][\w.\-]{2,})\]", re.M),
    # "hostname FW-DC1-EDGE-01" in configuration output
    re.compile(r"^\s*hostname\s+(\S{3,})", re.I | re.M),
    # CDP and LLDP neighbours -- devices the CMDB lookup never touched
    re.compile(r"^\s*Device ID:\s*(\S{3,})", re.I | re.M),
    re.compile(r"^\s*System Name:\s*(\S{3,})", re.I | re.M),
    # the two-column form of "show cdp neighbors"
    re.compile(r"^([A-Za-z][\w.\-]{3,})\s+(?:Gi|Te|Fa|Eth|Ten)\S*\s+\d+", re.M),
)

# Words that show up in prompt position but are not hostnames. Registering one
# would replace it everywhere, including inside commands.
_NOT_A_HOSTNAME = {
    "success", "sending", "type", "tracing", "routing", "protocol", "internet",
    "gateway", "destination", "codes", "warning", "error", "invalid", "percent",
    "router", "switch", "press", "translating", "unknown", "total", "usage",
    # bracketed things that are not prompts: Junos "[edit]", route attributes
    "edit", "ok", "down", "up", "none", "any", "local", "static", "connected",
    "mpls", "label", "inet", "inet6", "admin", "link", "proto", "interface",
}

# interface names as vendors write them: TenGigE0/0/0/1, Gi0/0, eth1, Vlan100
# ---------------------------------------------------------------- interfaces
# Interface names across the vendors this agent talks to. Every alternative
# REQUIRES a digit immediately after the type, which is what keeps "Ethernet
# frame" or "the tunnel" in prose from being mistaken for an interface.
#
# Longest first: TenGigabitEthernet has to win over Te, TwentyFiveGigE over Tw,
# Bridge-Aggregation over BE. Python's alternation takes the first that matches,
# not the longest, so the order below is load-bearing.
#
# Deliberately NOT matched:
#   port1 / wan1 / internal / dmz (FortiOS), eth0 / lo / bond0 / mgmt (Linux,
#   Gaia), 1/1/1 (Nokia SR OS), 1.1 (F5). Either they are ordinary English words
#   whose masking would mangle prose -- "internal error" -- or they are bare
#   numbers indistinguishable from versions, ratios and dates. They also reveal
#   almost nothing: "port1" says less about a network than its own hostname does.
_IFACE_TYPES = (
    # --- Cisco long forms, IOS / IOS-XE / IOS-XR / NX-OS
    r"TwoHundredGigabitEthernet|HundredGigabitEthernet|FortyGigabitEthernet",
    r"TwentyFiveGigabitEthernet|TenGigabitEthernet|FiveGigabitEthernet",
    r"TwoGigabitEthernet|AppGigabitEthernet|GigabitEthernet|FastEthernet",
    r"HundredGigE|FortyGigE|TwentyFiveGigE|TenGigE|GigE",
    r"Bundle-Ether|Port-channel|PW-Ether|tunnel-te|tunnel-ip|MgmtEth",
    r"Vethernet|Loopback|Ethernet|Serial|Tunnel|Dialer|Vlan|Null|BVI|ATM|POS|nve",
    # --- Huawei VRP and H3C Comware
    r"Ten-GigabitEthernet|M-GigabitEthernet|Bridge-Aggregation|Vlan-interface",
    r"Eth-Trunk|Route-Aggregation|Vlan-int|Vlanif|LoopBack|MEth|BAGG|RAGG|XGE",
    r"400GE|100GE|40GE|25GE|10GE|GE",
    # --- Arista
    r"Port-Channel|Management|Vxlan",
    # --- Cumulus, MikroTik
    r"swp\d+s|swp|sfp-sfpplus|sfpplus|ether|bridge",
    # --- Cisco abbreviations, longest first
    r"Twe|Hu|Fo|Te|Fi|Tw|Gi|Fa|Po|Lo|BE|Mg|Vl|Se|Tu|Nu|Ap|Eth|Et",
)

# Junos is its own shape: a two-letter media type, a hyphen, then fpc/pic/port,
# optionally a channel (:0) and a unit (.100). Plus the ones with no hyphen --
# ae0, lo0, fxp0, em0, reth1 -- and the dotted logicals, irb.100 and vlan.100.
_JUNOS = (
    r"\b(?:ge|xe|et|fe|so|at|gr|ip|lt|vt|pd|pe|st|sp|mt|es|umd|vcp|dsc)"
    r"-\d+(?:/\d+){0,2}(?::\d+)?(?:\.\d+)?\b"
    r"|\b(?:ae|lo|st|fxp|em|me|reth|irb|lsi|pimd|pime)\d+(?:\.\d+)?\b"
    r"|\b(?:irb|vlan|demux)\.\d+\b"
)

# PAN-OS: ethernet1/1, ae1, tunnel.1, vlan.1, loopback.1
_PANOS = r"\b(?:ethernet|ae|tunnel|vlan|loopback)\.?\d+(?:/\d+)?(?:\.\d+)?\b"

_IFACE = re.compile(
    r"\b(?:" + "|".join(_IFACE_TYPES) + r")\d[\w/.:\-]*"
    r"|" + _JUNOS +
    r"|" + _PANOS,
    re.I)


def _parse(text):
    """The tool result as a dict, whether it arrived as JSON or a Python repr."""
    body = str(text or "").strip()
    if not body.startswith("{"):
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            got = loader(body)
            if isinstance(got, dict):
                return got
        except Exception:
            continue
    return None


def _walk_strings(node, key_filter, out, depth=0):
    """Collect string values whose key is in key_filter, at any depth."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and str(key) in key_filter and value.strip():
                out.append(value.strip())
            else:
                _walk_strings(value, key_filter, out, depth + 1)
    elif isinstance(node, list):
        for item in node[:50]:
            _walk_strings(item, key_filter, out, depth + 1)


# VRF names in CLI output. Tufin returns them as JSON, but "show vrf" prints a
# table, so they have to be recovered from the text as well -- and a VRF name is
# often the most telling string on the box (PAYMENTS-PROD, EXAMPLE-VRF-A).
_VRF_PATTERNS = (
    re.compile(r"\bVRF\s+([A-Za-z][\w.\-]+)", re.I),          # "VRF X;" and "vrf X"
    # "VRF: X", 'Nexthop in Vrf: "X"', and the IOS-XR form that decorates the
    # name -- "VRF: **nVSatellite" -- so skip any punctuation after the colon
    re.compile(r"\bVrf:\s*[*\"'\\]*([A-Za-z][\w.\-]+)", re.I),
    # Huawei writes "VPN-Instance Name and ID : PAYMENTS-BJ, 1", so allow any
    # wording between the keyword and the colon
    re.compile(r"\bVPN-Instance[^:\n]*:\s*([A-Za-z][\w.\-]+)", re.I),
    re.compile(r"\brouting-instance[s]?\s+([A-Za-z][\w.\-]+)", re.I),  # Junos
    re.compile(r"\bvpn-instance\s+([A-Za-z][\w.\-]+)", re.I),
)

# The "show vrf" table has no keyword to anchor on -- but every row carries a
# route distinguisher, so a line with an RD on it starts with a VRF name.
# Captures both halves: the row carries the RD with no keyword in front of it,
# so this is the only place that one can be recognised.
_VRF_ROW = re.compile(
    r"^\s*([A-Za-z][\w.\-]+)\s+((?:\d+|\d{1,3}(?:\.\d{1,3}){3}):\d+)\b", re.M)

# Route distinguishers and route targets in ASN form. Anchored on RD/RT so a
# timestamp like 10:22 is never mistaken for one. The address form
# (10.10.1.1:200) needs no rule here -- the IPv4 pass masks its address part.
# "show vrf all detail" lists route targets as bare "import 64911:114000000" /
# "export ...", and the VRF row carries the RD with no keyword at all, so the
# row rule below picks that one up.
_RD = re.compile(
    r"(?:\bRD\s+|\bRT:\s*|\bRoute[- ]Distinguisher\s*:\s*|"
    r"\bRoute[- ]Target\s*:\s*|\b(?:import|export)\s+)(\d+:\d+)", re.I)

_NOT_A_VRF = {"all", "default", "detail", "name", "ipv4", "ipv6", "unicast",
              "interface", "interfaces", "brief", "table", "id", "not", "set",
              # route-target lines start with a word and an RD too, so without
              # these "import" and "export" get registered as VRF names and
              # then substituted everywhere they appear
              "import", "export", "rd", "rt", "vrf", "vpn", "route", "target",
              "address", "family", "description", "vpn-instance"}

_KEYS = {
    "host": {"name", "hostname", "deviceName", "device", "device_name"},
    "acl": {"acl", "aclName", "ruleName", "policyName"},
    # Archangel names the failing interface inside check_name ("Interface
    # TenGigE0/0/0/14"); the interface pattern picks that out on its own, but
    # the alert title is free text worth leaving alone.
    "vrf": {"vrf", "incomingVrf", "outgoingVrf"},
    "intf": {"interface", "outgoingInterfaceName", "incomingInterfaceName"},
    "label": {"mpls_label", "mplsOutputLabel"},
    "region": {"region"},
}


def learn(mask, tool_name: str, text: str) -> int:
    """Register every identifier in one tool result. Returns how many are new."""
    before = len(mask.terms())
    data = _parse(text)

    # Interface names first. Tufin nests them under a "name" key, which the
    # hostname rule below would otherwise claim -- harmless for the round trip,
    # but it makes the audit list read wrong.
    for match in _IFACE.finditer(str(text or "")):
        mask.register(match.group(0), "intf")

    if data:
        for kind, keys in _KEYS.items():
            found = []
            _walk_strings(data, keys, found)
            for value in found:
                # a region is an opaque argument the model passes back to the
                # SSH tool; the rest are names it only ever quotes
                mask.register(value, kind)

    body = str(text or "")
    # Belt and braces: if a tool result still arrives with escaped newlines --
    # a Python repr, or JSON that was not decoded -- every "^" anchored rule
    # below would match nothing and names would sail through. utils.tool_text
    # normalises this at the source; this catches anything that slips past.
    if "\\n" in body and "\n" not in body:
        body = body.replace("\\r\\n", "\n").replace("\\n", "\n")

    # VRF names and route distinguishers, which "show vrf" prints as a table
    # rather than returning as structured data
    vrfs = []
    for pattern in _VRF_PATTERNS:
        vrfs.extend(pattern.findall(body))
    rds = list(_RD.findall(body))
    for name, rd in _VRF_ROW.findall(body):
        vrfs.append(name)
        rds.append(rd)          # the row's own RD has no keyword to anchor on
    for vrf in vrfs:
        name = str(vrf).strip().strip('";,')
        if len(name) > 2 and name.lower() not in _NOT_A_VRF:
            mask.register(name, "vrf")
    for rd in rds:
        mask.register(rd, "rd")

    # hostnames arriving as free text: traceroute hops, the device's own prompt,
    # config lines, CDP/LLDP neighbours
    names = list(_HOP_NAME.findall(body))
    for pattern in _CLI_HOSTNAME:
        names.extend(pattern.findall(body))
    for name in names:
        if _plausible_hostname(name):
            mask.register(name, "host")

    return len(mask.terms()) - before


def _plausible_hostname(name: str) -> bool:
    """Filter the false positives an anchored pattern still lets through."""
    text = str(name).strip().strip(".,:;")
    if len(text) < 3 or text.lower() in _NOT_A_HOSTNAME:
        return False
    if not re.match(r"^[A-Za-z][\w.\-]*$", text):
        return False
    # an interface name in prompt position is an interface, not a host
    return not _IFACE.fullmatch(text)
