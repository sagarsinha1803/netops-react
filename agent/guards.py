"""Read-only enforcement, in code. The model is never trusted.

Two independent gates protect the devices: this allowlist, and the human
approval collected in the graph. A command has to pass both.
"""
import re
from typing import Optional

_ALLOWED = re.compile(
    r"^\s*(ping|ping6|traceroute|traceroute6|tracert|tracepath|show|display|get|"
    r"execute\s+ping|execute\s+traceroute|run\s+/util|/ping|/tool\s+traceroute)\b",
    re.I)

_BLOCKED = re.compile(
    r"\b(conf(ig)?(ure)?|write|erase|reload|reboot|delete|remove|copy|clear|reset|"
    r"shutdown|set\s|commit|rollback|request|restart|halt|format|install)\b", re.I)

# Commands that dump whole tables. Harmless to the device, fatal to the context
# window (a full route table or config is hundreds of thousands of characters).
# Allowed only when narrowed by an argument or piped through a filter.
_BULK = re.compile(
    r"^\s*show\s+("
    r"run(ning-config)?|tech(-support)?|config(uration)?|logging|version\s*$|"
    r"route\s*$|route\s+(ipv4|ipv6|vrf\s+\S+)?\s*$|"
    r"bgp\s*$|bgp\s+(ipv4|vpnv4|vrf\s+\S+)?\s*(unicast)?\s*$|"
    r"cef\s*$|arp\s*$|mpls\s+forwarding\s*$|"
    r"interfaces?\s*$|ip(v4|v6)?\s+interface\s*$|"
    r"access-lists?\s*$|vrf\s+all\s+detail"
    r")\b", re.I)


def check_command(cmd: str) -> Optional[str]:
    """Return an error string if this is not a safe, appropriately scoped
    read-only command, or None if it may run."""
    c = (cmd or "").strip()
    if not c:
        return "empty command"
    if not _ALLOWED.match(c):
        return f"'{c}' is not in the read-only allowlist (ping/traceroute/show only)"
    if _BLOCKED.search(c):
        return f"'{c}' contains a state-changing keyword"
    if _BULK.match(c) and "|" not in c:
        return (f"'{c}' would dump a whole table. Narrow it with a specific "
                "prefix/interface, or filter it, e.g. "
                "'show route 10.1.1.1' or 'show arp | include 10.1.1.1'")
    return None
