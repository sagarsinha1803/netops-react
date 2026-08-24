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

from mcp_tools.tufin_mcp import summarise  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios as S  # noqa: E402

mcp = FastMCP("tufin-server")

_BLOCKED_DESTS = S.BLOCKED_DESTS


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
    return {
        "path_calc_results": {
            "traffic_allowed": not blocked,
            "device_info": [
                {
                    "id": 7349, "name": "RTR-EXAMPLE-02", "type": "router",
                    "vendor": "Cisco",
                    "incomingInterfaces": [
                        {"name": "Loopback99", "ip": f"{src}/32",
                         "incomingVrf": "EXAMPLE-VRF-A"},
                    ],
                    "nextDevices": [
                        {"name": "SW-EXAMPLE-B1", "routes": [
                            {"routeDestination": "0.0.0.0/0",
                             "nextHopIp": "203.0.113.45",
                             "outgoingInterfaceName": "TenGigE0/0/0/0",
                             "mplsOutputLabel": "27084"},
                        ]},
                        {"name": "RTR-EXAMPLE-01", "routes": []},
                    ],
                    "rules": [rule],
                    "enforcedOn": [],
                },
                {
                    "id": 7412, "name": "FW-DC1-EDGE-01", "type": "firewall",
                    "vendor": "Checkpoint",
                    "incomingInterfaces": [
                        {"name": "eth1", "ip": "10.10.255.1/24",
                         "incomingVrf": None},
                    ],
                    "nextDevices": [],
                    "rules": [rule] if blocked else [],
                    "enforcedOn": [],
                },
            ],
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
) -> dict:
    blocked = str(dst).strip() in _BLOCKED_DESTS
    return summarise(_payload(str(src).strip(), str(dst).strip(),
                              service or "any", blocked))


if __name__ == "__main__":
    mcp.run()   # stdio
