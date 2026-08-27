"""Vendor command registry + output parsers. Cisco first; add families as needed.

One entry per OS FAMILY (not per model) -- ~8000 models collapse to ~25 CLI
dialects. CMDB vendor/os string -> family key -> command template + parser.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_JSON = os.path.join(os.path.dirname(_HERE), "data",
                     "vendor_commands.json")

DEFAULT_FAMILY = "cisco_ios"


# ---------------------------------------------------------------- registry --
def load_registry():
    """Command templates per OS family. Only needed by the deterministic agent
    (path_agent); the agentic one (net_agent) uses just the parsers below, so a
    missing file is not fatal."""
    try:
        with open(_JSON, "r", encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except FileNotFoundError:
        return {}


REGISTRY = load_registry()

# substrings found in CMDB vendor/os/model text -> family key.
# order matters: first match wins, so put specific before generic.
_MATCH = [
    ("nx-os", "cisco_nxos"), ("nxos", "cisco_nxos"), ("nexus", "cisco_nxos"),
    ("ios-xr", "cisco_xr"), ("iosxr", "cisco_xr"),
    ("asa", "cisco_asa"),
    ("ios-xe", "cisco_ios"), ("ios", "cisco_ios"), ("cisco", "cisco_ios"),
    ("arista", "arista_eos"), ("eos", "arista_eos"),
    ("junos", "juniper_junos"), ("juniper", "juniper_junos"),
    ("fortios", "fortinet_fortios"), ("fortinet", "fortinet_fortios"),
    ("pan-os", "paloalto_panos"), ("palo", "paloalto_panos"),
    ("gaia", "checkpoint_gaia"), ("checkpoint", "checkpoint_gaia"),
    ("netscaler", "citrix_netscaler"), ("citrix", "citrix_netscaler"),
    ("big-ip", "f5_tmos"), ("f5", "f5_tmos"),
    ("gigamon", "gigamon_gigavue"), ("gigavue", "gigamon_gigavue"),
]


def detect_family(*cmdb_fields):
    """Map free-text CMDB vendor/os/model fields to a family key."""
    blob = " ".join(str(f or "") for f in cmdb_fields).lower()
    for needle, family in _MATCH:
        if needle in blob:
            return family
    return DEFAULT_FAMILY


def command_for(family, action, **kw):
    """Build a command string, e.g. command_for('cisco_ios','ping',dest=x,count=3)."""
    entry = REGISTRY.get(family) or REGISTRY.get(DEFAULT_FAMILY)
    if not entry:
        raise RuntimeError(
            f"{_JSON} not found -- needed for command lookup (path_agent only)")
    tmpl = entry.get(action)
    if not tmpl:
        return None
    return tmpl.format(**kw)


# ----------------------------------------------------------------- parsers --
def ping_ok(out: str) -> bool:
    """True if the ping succeeded. Covers Cisco + Linux-style output."""
    o = (out or "").lower()
    # Windows counts an ICMP "destination unreachable" REPLY as a received
    # packet, so this check has to come before the received-count ones
    if "destination host unreachable" in o or "destination net unreachable" in o:
        return False
    if re.search(r"success rate is (?:100|[1-9]\d?) percent", o):   # cisco, any >0%
        return True
    if re.search(r"\b0% packet loss", o):                            # linux/bsd
        return True
    if re.search(r"(\d+) (?:packets )?received", o):                 # linux partial
        m = re.search(r"(\d+) (?:packets )?received", o)
        return int(m.group(1)) > 0
    m = re.search(r"received = (\d+)", o)                            # windows
    if m:
        return int(m.group(1)) > 0
    return False


# A hop and its answer are on ONE line. This used \s+, and \s matches a
# NEWLINE: a bare "  1" followed by the banner "traceroute to <dst> (<dst>),
# 30 hops max" was read as hop 1 whose host is the destination -- so a
# traceroute that went nowhere drew the destination as a hop it had reached.
_HOP_RE = re.compile(
    r"^[ \t]*(\d+)[ \t]*([^\n]*)$",  # "  3  FW-01 (10.0.0.1) 4 msec" | "  4  *  *  *"
    re.M)
# what a traceroute prints ABOUT itself. These are not hops, and one of them
# carries the destination address, which is exactly the wrong thing to believe.
_TRACE_BANNER = re.compile(
    r"^(traceroute to|tracing the route|type escape sequence|no route to host)",
    re.I)
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_NAME_RE = re.compile(r"^([A-Za-z][\w.\-]*)\s*\(")   # "NAME (1.2.3.4)"
_NAME_BRACKET = re.compile(r"\b([A-Za-z][\w.\-]*)\s*\[")  # windows "name [1.2.3.4]"


def parse_hops(out: str):
    """Parse traceroute output -> list of {'n','host','ip','timeout'} in order.

    Handles Cisco ('1 name (ip) 4 msec' or '1 ip 4 msec' or '2 * * *'), the
    Linux form, and Windows tracert ('1 <1 ms <1 ms <1 ms 10.0.0.1' or
    '2 * * * Request timed out.'). Unresponsive hops come back as timeout=True
    with host None.
    """
    hops = []
    for m in _HOP_RE.finditer(out or ""):
        n, rest = int(m.group(1)), m.group(2).strip()
        if _TRACE_BANNER.match(rest):
            continue                 # the trace talking about itself, not a hop
        if (not rest or set(rest.replace(" ", "")) <= {"*"}
                or "request timed out" in rest.lower()
                or rest.split()[0] == "*"):
            hops.append({"n": n, "host": None, "ip": None, "timeout": True})
            continue
        ip = _IP_RE.search(rest)
        nm = _NAME_RE.match(rest) or _NAME_BRACKET.search(rest)
        host = nm.group(1) if nm else (ip.group(1) if ip else rest.split()[0])
        hops.append({"n": n, "host": host,
                     "ip": ip.group(1) if ip else None, "timeout": False})
    return hops


def first_failed_hop(hops):
    """Last responding hop before the first timeout run (where the path dies)."""
    for i, h in enumerate(hops):
        if h["timeout"]:
            return hops[i - 1] if i else None
    return None


def path_line(source, hops, dest, reached=True):
    """'source -> hop1 -> hop2 -> destination' (marks where it stops if not reached)."""
    parts = [source]
    for h in hops:
        if h["timeout"]:
            break
        label = h["host"] or h["ip"] or f"hop{h['n']}"
        if label not in (dest,):
            parts.append(label)
    parts.append(dest if reached else "X (unreachable)")
    return " -> ".join(parts)
