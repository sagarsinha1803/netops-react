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
import ipaddress
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
# A rule is recognised by carrying an action plus SOMETHING that identifies it.
# Which of these SecureTrack uses varies by version and by device vendor: a
# Cisco ACL entry comes back with aclName/ruleNumber, while a Fortinet policy
# comes back with ruleIdentifier/ruleUid and a human name ("Deny-Example-Rule").
# Matching only the first pair meant a real deny rule was not recognised at all,
# so the summary reported BLOCKED with an empty blocking_rules list and the
# report could not name what dropped the traffic.
_RULE_IDS = ("aclName", "ruleNumber", "ruleIdentifier", "ruleUid", "name")


def _walk(node, found_rules, found_devices):
    if isinstance(node, dict):
        keys = set(node)
        if "action" in keys and any(k in keys for k in _RULE_IDS):
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
        # whichever of the identifiers this vendor filled in; the human name is
        # the one worth quoting in a report ("Deny-Example-Rule")
        "acl": (rule.get("aclName") or rule.get("name")
                or rule.get("ruleIdentifier") or ""),
        "rule_id": rule.get("ruleIdentifier") or rule.get("ruleNumber") or "",
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


def _hops(devices) -> list:
    """The device chain as STEPS, with parallel alternatives grouped.

    SecureTrack returns a graph: a router can have two next devices that are
    equal-cost alternatives, and both then lead to the same firewall. Listing
    the names flat makes those look sequential. This walks the graph from
    whichever device nothing else points at, one step at a time.
    """
    by_name, nexts = {}, {}
    for dev in devices:
        name = str(dev.get("name") or "")
        if not name:
            continue
        by_name[name] = dev
        nexts[name] = [str(n.get("name")) for n in _as_list(dev.get("nextDevices"))
                       if n.get("name")]

    pointed_at = {n for targets in nexts.values() for n in targets}
    start = [n for n in by_name if n not in pointed_at] or list(by_name)[:1]

    hops, current, seen = [], start, set()
    while current:
        step = [n for n in dict.fromkeys(current) if n not in seen]
        if not step:
            break
        hops.append(step)
        seen.update(step)
        current = [t for n in step for t in nexts.get(n, [])]
    return hops


def _reaches_destination(payload, results, devices):
    """False when SecureTrack says the pair is unrouted. None otherwise.

    Only unrouted_elements is conclusive. It is tempting to also read "the last
    device has no next hop" as non-delivery, but SecureTrack stops modelling at
    the last device it knows about -- a destination sitting directly on that
    device's segment looks identical. Guessing there would turn a delivered
    path into a reported failure, so this reports what it knows and leaves the
    rest unstated.
    """
    unrouted = (_as_list(payload.get("unrouted_elements"))
                or _as_list(results.get("unrouted_elements")))
    return False if unrouted else None


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
    # top level in some versions, inside path_calc_results in others
    for item in (_as_list(payload.get("unrouted_elements"))
                 or _as_list(results.get("unrouted_elements"))):
        unrouted.append({"destination": item.get("destination"),
                         "source": _as_list(item.get("source"))[:4]})

    # the ordered chain of device names, which is all the model reads
    chain, seen = [], set()
    for dev in devices:
        name = dev.get("name")
        if name and name not in seen:
            seen.add(name)
            chain.append(str(name))

    hops = _hops(devices)
    reaches = _reaches_destination(payload, results, devices)

    out = {
        "traffic_allowed": allowed,
        "verdict": ("ALLOWED" if allowed is True
                    else "BLOCKED" if allowed is False else "UNKNOWN"),
        "blocking_rules": blocking[:5],
        "rules_seen": len(rules),
        "device_path": chain[:20],
        # device_path is FLAT, and a topology is not: two routers that are
        # alternatives for the same step read as two consecutive hops, which
        # is how a report came to claim traffic transited both. hops keeps the
        # shape -- one entry per step, each listing the devices that are
        # alternatives at that step.
        "hops": hops[:20],
        "device_count": len(chain),
        "reaches_destination": reaches,
        "unrouted_elements": unrouted[:5],
    }
    if reaches is False:
        out["path_note"] = (
            "The modelled path does NOT reach the destination: the last "
            "devices have no next hop, or SecureTrack listed the pair in "
            "unrouted_elements. The path in the report must END where it "
            "stops, marked X -- do not append the destination to it.")

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


def _looks_like_ip(value) -> bool:
    """True for an IPv4/IPv6 address, with or without a prefix length."""
    text = str(value or "").strip().split("/")[0]
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


# ---- tool --------------------------------------------------------------------
@mcp.tool(
    name="get_firewall_path",
    description="Ask Tufin SecureTrack whether traffic from src to dst on a "
                "service is permitted end to end, and which rule blocks it if "
                "not. Read-only. service is like 'tcp:443', 'any' for all.")
def get_firewall_path(
    src: Annotated[str, Field(
        description="source IP ADDRESS (the managementIp from the CMDB "
                    "record) -- not a device name")],
    dst: Annotated[str, Field(
        description="destination IP ADDRESS (the managementIp from the CMDB "
                    "record) -- not a device name")],
    service: Annotated[Optional[str],
                       Field(description="e.g. tcp:443, udp:53, any")] = "any",
) -> dict | str:
    # SecureTrack matches its topology by address. A hostname matches nothing,
    # and the API answers about a pair that does not exist rather than
    # complaining -- which reads as a real verdict. Refuse it here instead, and
    # say where the address comes from, because the model asked by name has the
    # CMDB record in front of it.
    bad = [f"{label}={value!r}" for label, value in (("src", src), ("dst", dst))
           if not _looks_like_ip(value)]
    if bad:
        return ("get_firewall_path needs IP ADDRESSES, not device names: "
                + ", ".join(bad)
                + ". Use the managementIp from each device's CMDB record "
                  "(get_device_details). SecureTrack looks the pair up in its "
                  "topology by address, so a name silently matches nothing.")

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
