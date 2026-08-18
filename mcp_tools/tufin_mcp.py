# tufin_mcp.py -- Tufin SecureTrack path lookup: is the traffic permitted, and
# which rule blocks it?
#
#   GET https://<SecureTrack_IP>/securetrack/api/topology/path
#       ?src=<src>&dst=<dst>&service=<service>
#       &displayBlockedStatus=true&includeIncompletePaths=true&simulateNate=true
#
# The raw reply runs to hundreds of lines -- every device on the path, every
# interface, every route and every rule bound to it. Returning that verbatim
# blows the model's context in a single call, so the tool answers with the
# decision and the evidence for it: allowed or not, the rules that dropped it,
# the device chain, and anything the topology could not route.
#
# Credentials live in credentials.yml next to unicorn's, e.g.
#   TUFIN_URL: https://10.1.2.3
#   TUFIN_USER: apiuser
#   TUFIN_PASSWORD: ...
#   TUFIN_VERIFY_SSL: false
import os
import sys
from typing import Annotated, Optional

import requests
import urllib3
import yaml
from mcp.server.fastmcp import FastMCP
from pydantic import Field

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

mcp = FastMCP("tufin-server")

_HERE = os.path.dirname(os.path.abspath(__file__))


# credentials.yml sits at the project root, one level up from mcp_tools/,
# so the servers find it whatever directory they are launched from
_CREDENTIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "credentials.yml")


class Settings:
    # Catch EVERYTHING here, not just a missing file. This runs at import:
    # malformed YAML, a permissions problem or a stray tab in credentials.yml
    # kills the server before it ever speaks, and all the client sees is the
    # stdio pipe breaking -- "[Errno 9] Bad file descriptor" -- with no reason.
    load_error = ""
    try:
        with open(_CREDENTIALS, "r") as f:
            credentials = yaml.safe_load(f) or {}
    except FileNotFoundError:
        credentials = {}
    except Exception as ex:                          # noqa: BLE001
        credentials = {}
        load_error = f"{_CREDENTIALS} could not be read: {ex}"
        print(f"[tufin] {load_error}", file=sys.stderr)

    URL = str(credentials.get("TUFIN_URL", "")).rstrip("/")
    USER = credentials.get("TUFIN_USER", "")
    PASSWORD = credentials.get("TUFIN_PASSWORD", "")

    # A path to a CA bundle is valid here too; only a real false skips checking.
    VERIFY = credentials.get("TUFIN_VERIFY_SSL", False)
    if isinstance(VERIFY, str) and VERIFY.strip().lower() in ("false", "no", "0", ""):
        VERIFY = False

    # SecureTrack path calculation is slow. Keep this well inside the MCP
    # session's patience: if the client gives up first it tears the pipe down
    # while the request is still in flight, which is the other route to Errno 9.
    TIMEOUT = int(os.environ.get("TUFIN_TIMEOUT", "25"))


# ---- response slimming -------------------------------------------------------
# The exact nesting of the rule objects varies between SecureTrack versions, so
# rather than hard-coding a path into the document these walk it and recognise
# objects by the keys they carry. A dict with "action" and "aclName" is a rule;
# one with "incomingInterfaces" or "nextDevices" is a device on the path.
def _walk(node, found_rules, found_devices):
    if isinstance(node, dict):
        keys = set(node)
        if "action" in keys and ("aclName" in keys or "ruleNumber" in keys):
            found_rules.append(node)
        elif "incomingInterfaces" in keys or "nextDevices" in keys:
            found_devices.append(node)
        for value in node.values():
            _walk(value, found_rules, found_devices)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found_rules, found_devices)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _slim_rule(rule: dict) -> dict:
    return {
        "action": rule.get("action"),
        "acl": rule.get("aclName") or rule.get("name") or "",
        "sources": _as_list(rule.get("sources"))[:4],
        "destinations": _as_list(rule.get("destinations"))[:4],
        "services": _as_list(rule.get("services"))[:4],
        "enforced_on": _as_list(rule.get("enforcedOn"))[:4],
    }


def _slim_device(dev: dict) -> dict:
    interfaces = [
        {"name": i.get("name"), "ip": i.get("ip"), "vrf": i.get("incomingVrf")}
        for i in _as_list(dev.get("incomingInterfaces"))[:3]
    ]
    next_hops = []
    for nxt in _as_list(dev.get("nextDevices"))[:4]:
        for route in _as_list(nxt.get("routes"))[:2]:
            next_hops.append({
                "device": nxt.get("name"),
                "route": route.get("routeDestination"),
                "next_hop": route.get("nextHopIp"),
                "interface": route.get("outgoingInterfaceName"),
                "mpls_label": route.get("mplsOutputLabel"),
            })
        if not _as_list(nxt.get("routes")):
            next_hops.append({"device": nxt.get("name")})
    return {
        "name": dev.get("name"),
        "type": dev.get("type"),
        "vendor": dev.get("vendor"),
        "interfaces": interfaces,
        "next_hops": next_hops,
    }


def summarise(payload: dict) -> dict:
    """Turn a full path response into the handful of facts the agent needs.

    A real reply is thousands of characters: a dozen devices, each with every
    interface, next hop, route and MPLS label. Returning that verbatim costs
    more context than the rest of the conversation put together, and the agent
    uses almost none of it -- the traceroute already established the path. What
    it needs is the verdict, the rule that caused it, and the device chain.
    """
    results = payload.get("path_calc_results") or payload
    rules, devices = [], []
    _walk(results, rules, devices)

    blocking = [_slim_rule(r) for r in rules
                if str(r.get("action", "")).lower() in ("drop", "deny", "reject")]
    allowed = results.get("traffic_allowed")

    unrouted = []
    for item in _as_list(payload.get("unrouted_elements")):
        unrouted.append({"destination": item.get("destination"),
                         "source": _as_list(item.get("source"))[:4]})

    # the ordered chain of device names, which is all the model reads
    chain, seen = [], set()
    for dev in devices:
        name = dev.get("name")
        if name and name not in seen:
            seen.add(name)
            chain.append(str(name))

    out = {
        "traffic_allowed": allowed,
        "verdict": ("ALLOWED" if allowed is True
                    else "BLOCKED" if allowed is False else "UNKNOWN"),
        "blocking_rules": blocking[:5],
        "rules_seen": len(rules),
        "device_path": chain[:20],
        "device_count": len(chain),
        "unrouted_elements": unrouted[:5],
    }

    if allowed is False and not blocking:
        out["note"] = (
            "SecureTrack says the traffic is blocked but returned no matching "
            "rule, so the ACL cannot be named from this result. Treat it as "
            "blocked by policy somewhere on the path and say the rule is not "
            "identified - do not guess one."
            + (" The topology also could not route it; see unrouted_elements."
               if unrouted else ""))
    return out


def summarise_verbose(payload: dict) -> dict:
    """The full per-device detail: interfaces, next hops, routes, MPLS labels.

    Not sent to the model -- kept for a human reading the raw reply, and for
    working out why a rule was not matched.
    """
    results = payload.get("path_calc_results") or payload
    rules, devices = [], []
    _walk(results, rules, devices)
    return {"rules": [_slim_rule(r) for r in rules],
            "devices": [_slim_device(d) for d in devices]}


# ---- tool --------------------------------------------------------------------
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
    if not Settings.URL:
        return ("Tufin is not configured: add TUFIN_URL, TUFIN_USER and "
                "TUFIN_PASSWORD to credentials.yml.")

    url = f"{Settings.URL}/securetrack/api/topology/path"
    params = {
        "src": src, "dst": dst, "service": service or "any",
        "displayBlockedStatus": "true",
        "includeIncompletePaths": "true",
        "simulateNate": "true",
    }
    try:
        response = requests.get(
            url,
            params=params,
            auth=(Settings.USER, Settings.PASSWORD),
            headers={"Accept": "application/json"},
            verify=Settings.VERIFY,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as ex:
        # stderr: stdout carries the MCP JSON-RPC stream
        print(f"[tufin] path lookup failed: {ex}", file=sys.stderr)
        return f"Error calling Tufin: {ex}"

    return summarise(payload)


if __name__ == "__main__":
    mcp.run()   # stdio
