"""MOCK Tufin SecureTrack path MCP -- local testing only (no creds, no HTTP).

Same tool name and reply shape as tufin_mcp.py, and the canned payload mirrors a
real /securetrack/api/topology/path response: traffic_allowed false, a DENY-ALL
rule, the device chain with MPLS-labelled next hops, and an unrouted element.
Reused through tufin_mcp.summarise, so the mock exercises the real slimming code
rather than a parallel copy that can drift.
"""
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from mcp_tools.tufin_mcp import _looks_like_ip, summarise  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios as S  # noqa: E402

mcp = FastMCP("tufin-server")

_BLOCKED_DESTS = S.BLOCKED_DESTS


def _chain(dst: str):
    """The device chain SecureTrack would model for this destination.

    Built from the same scenario the traceroute answers from, so the two
    drawings in the Path tab are about the same network. They still disagree
    where they should: SecureTrack models the topology and the rules, and a
    link that went down five minutes ago is not in either.
    """
    trace = S.TRACE.get(dst) or {}
    names = [name for name, _ip, _rtt in trace.get("hops", [])]
    # SecureTrack sees the firewall in the middle; the traceroute only sees it
    # if it answers probes
    if not names:
        names = ["RTR-EXAMPLE-02"]
    edge = "FW-DC1-EDGE-01"
    return [n for n in names if n != S.resolve(dst)] + [edge]


def _payload(src: str, dst: str, service: str, blocked: bool) -> dict:
    rule = {
        "sources": ["any"], "sourceNegated": False,
        "destinations": ["any"], "destNegated": False,
        "services": ["Any"], "serviceNegated": False,
        "applications": [], "users": [],
        "action": "Drop" if blocked else "Accept",
        "aclName": ("DENY-ALL" if blocked else
                    S.POLICY_RULES.get(dst, "PERMIT-INTRA-DC")),
        "name": "",
    }

    chain = _chain(dst)
    devices = []
    for i, name in enumerate(chain):
        last = i == len(chain) - 1
        devices.append({
            "id": 7000 + i,
            "name": name,
            "type": "firewall" if last else "router",
            "vendor": "Checkpoint" if last else "Cisco",
            "incomingInterfaces": [
                {"name": "Loopback99" if i == 0 else f"TenGigE0/0/0/{i}",
                 "ip": f"{src}/32" if i == 0 else None,
                 "incomingVrf": None},
            ],
            "nextDevices": ([] if last else
                            [{"name": chain[i + 1], "routes": [
                                {"routeDestination": f"{dst}/32",
                                 "nextHopIp": "203.0.113.45",
                                 "outgoingInterfaceName": f"TenGigE0/0/0/{i}",
                                 "mplsOutputLabel": "27084"}]}]),
            "rules": [rule] if (last or i == 0) else [],
            "enforcedOn": [],
        })

    return {
        "path_calc_results": {
            "traffic_allowed": not blocked,
            "device_info": devices,
        },
        "unrouted_elements": ([{"destination": dst, "source": [src]}]
                              if blocked else []),
    }


@mcp.tool(
    name="get_firewall_path",
    description="Ask Tufin SecureTrack whether traffic from src to dst on a "
                "service is permitted end to end, and which rule blocks it if "
                "not. Read-only. service is like 'tcp:443', 'any' for all.")
def get_firewall_path(
    src: Annotated[str, Field(description="source IPv4 address")],
    dst: Annotated[str, Field(description="destination IPv4 address")],
    service: Annotated[Optional[str],
                       Field(description="e.g. tcp:443, udp:53, any")] = "any",
) -> dict | str:
    # the REAL server refuses a device name here, because SecureTrack matches
    # its topology by address and answers about a pair that does not exist
    # rather than complaining. A mock that quietly accepts names teaches the
    # model a habit that fails in production, and hides the refusal the demo
    # should be able to show.
    bad = [f"{label}={value!r}" for label, value in (("src", src), ("dst", dst))
           if not _looks_like_ip(value)]
    if bad:
        return ("get_firewall_path needs IP ADDRESSES, not device names: "
                + ", ".join(bad)
                + ". Use the managementIp from each device's CMDB record "
                  "(get_device_details).")

    blocked = str(dst).strip() in _BLOCKED_DESTS
    return summarise(_payload(str(src).strip(), str(dst).strip(),
                              service or "any", blocked))


if __name__ == "__main__":
    mcp.run()   # stdio
