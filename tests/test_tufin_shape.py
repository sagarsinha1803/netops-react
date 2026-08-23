"""SecureTrack's reply must be understood whichever shape it arrives in.

Two things this pins, both found against a real SecureTrack:

  1. Rules are labelled differently per vendor. A Cisco ACL entry carries
     aclName/ruleNumber; a Fortinet policy carries ruleIdentifier/ruleUid and a
     human name ("Deny-Example-Rule"). Recognising only the first pair meant a real
     deny rule was invisible: the summary said BLOCKED with an empty
     blocking_rules list, and the report could not say what dropped the traffic.

  2. The tool may return the summary OR the raw payload. The panel read only
     the summary's "verdict" key, so a raw reply showed as UNKNOWN and the
     policy stage went grey -- for an answer that was perfectly good.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_tools"))

from api.workflow import policy_verdict                   # noqa: E402
from mcp_tools.tufin_mcp import summarise                 # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# The shape a real SecureTrack returned: a Fortinet policy in the middle of a
# Cisco path, unrouted_elements nested inside path_calc_results.
RAW = {
    "path_calc_results": {
        "traffic_allowed": False,
        "device_info": [
            {"id": 16849, "name": "RTR-EDGE-01", "type": "router",
             "vendor": "Cisco",
             "incomingInterfaces": [{"name": "Tunnel0", "ip": "198.51.100.194/30",
                                     "incomingVrf": "VRF-EXAMPLE"}],
             "nextDevices": [{"name": "RTR-CORE-02", "routes": [
                 {"routeDestination": "0.0.0.0/0", "nextHopIp": "10.9.1.1",
                  "outgoingInterfaceName": "Vlan634"}]}]},
            {"id": 18371, "name": "FW-DMZ-01", "type": "mgmt",
             "vendor": "Fortinet",
             "incomingInterfaces": [{"name": "port3", "ip": "10.9.2.1/24"}],
             "nextDevices": [{"name": "RTR-LAN-02", "routes": []}],
             "rules": [{"ruleIdentifier": "19",
                        "ruleUid": "{c9c80218-0000-0000-0000-000000000000}",
                        "sources": ["all"], "destinations": ["all"],
                        "services": ["ALL"], "action": "Deny",
                        "name": "Deny-Example-Rule"}]},
        ],
        "unrouted_elements": [{"destination": "192.0.2.9",
                               "source": ["198.51.100.5"]}],
    }
}

# ---- the summariser --------------------------------------------------------
out = summarise(RAW)
check("verdict is read from traffic_allowed", out["verdict"] == "BLOCKED",
      out["verdict"])
check("a Fortinet rule (ruleIdentifier/name) IS recognised",
      out["rules_seen"] >= 1 and out["blocking_rules"], str(out["blocking_rules"]))
check("the rule is named, so the report can quote it",
      out["blocking_rules"] and out["blocking_rules"][0]["acl"] == "Deny-Example-Rule",
      str(out["blocking_rules"][:1]))
check("the rule id is kept alongside the name",
      out["blocking_rules"] and out["blocking_rules"][0]["rule_id"] == "19")
check("the device chain survives", out["device_path"][:2] == ["RTR-EDGE-01", "FW-DMZ-01"],
      str(out["device_path"]))
check("unrouted_elements nested under path_calc_results are found",
      len(out["unrouted_elements"]) == 1, str(out["unrouted_elements"]))
check("no 'rule not identified' note now that the rule IS identified",
      "note" not in out, str(out.get("note"))[:60])

# ---- the shape of the path -------------------------------------------------
# device_path is flat, so two routers that are ALTERNATIVES for the same step
# read as two consecutive hops -- which is how a report came to draw traffic
# transiting both, and then to append a destination that was never reached.
BRANCHED = {"path_calc_results": {"traffic_allowed": False, "device_info": [
    {"name": "R-EDGE", "incomingInterfaces": [], "nextDevices": [
        {"name": "R-A", "routes": []}, {"name": "R-B", "routes": []}]},
    {"name": "R-A", "incomingInterfaces": [], "nextDevices": [
        {"name": "FW", "routes": []}]},
    {"name": "R-B", "incomingInterfaces": [], "nextDevices": [
        {"name": "FW", "routes": []}]},
    {"name": "FW", "incomingInterfaces": [], "nextDevices": [
        {"name": "R-LAN1", "routes": []}, {"name": "R-LAN2", "routes": []}],
     "rules": [{"action": "Deny", "name": "Deny-Example-Rule"}]},
    {"name": "R-LAN1", "incomingInterfaces": [], "nextDevices": []},
    {"name": "R-LAN2", "incomingInterfaces": [], "nextDevices": []}],
    "unrouted_elements": [{"destination": "192.0.2.9", "source": ["198.51.100.5"]}]}}

b = summarise(BRANCHED)
check("hops group alternatives instead of sequencing them",
      b["hops"] == [["R-EDGE"], ["R-A", "R-B"], ["FW"], ["R-LAN1", "R-LAN2"]],
      str(b["hops"]))
check("unrouted pair means the destination is NOT reached",
      b["reaches_destination"] is False, str(b["reaches_destination"]))
check("and the model is told not to append the destination",
      "must END where it stops" in b.get("path_note", ""), b.get("path_note", "")[:60])

REACHED = {"path_calc_results": {"traffic_allowed": True, "device_info": [
    {"name": "R-EDGE", "incomingInterfaces": [], "nextDevices": [
        {"name": "R-CORE", "routes": []}]},
    {"name": "R-CORE", "incomingInterfaces": [], "nextDevices": [
        {"name": "R-FAR", "routes": []}]}]}}
r = summarise(REACHED)
check("a path that continues past what was modelled is not called unreachable",
      r["reaches_destination"] is None, str(r["reaches_destination"]))
check("only unrouted_elements is treated as conclusive -- a tail with no next "
      "hop is NOT, since a directly attached destination looks the same",
      summarise({"path_calc_results": {"traffic_allowed": True, "device_info": [
          {"name": "R-ONLY", "incomingInterfaces": [], "nextDevices": []}]}})
      ["reaches_destination"] is None)
check("no path_note when there is nothing to warn about", "path_note" not in r)

# a Cisco-style rule must keep working
CISCO = {"path_calc_results": {"traffic_allowed": False, "device_info": [
    {"name": "RTR-A", "incomingInterfaces": [],
     "rules": [{"action": "Drop", "aclName": "DENY-ALL", "ruleNumber": 40,
                "sources": ["any"], "destinations": ["any"]}]}]}}
cisco = summarise(CISCO)
check("a Cisco rule (aclName/ruleNumber) still recognised",
      cisco["blocking_rules"] and cisco["blocking_rules"][0]["acl"] == "DENY-ALL",
      str(cisco["blocking_rules"][:1]))

# ---- what the panel makes of each shape ------------------------------------
verdict, acl = policy_verdict(json.dumps(summarise(RAW)))
check("panel reads the SUMMARY", (verdict, acl) == ("BLOCKED", "Deny-Example-Rule"),
      f"{verdict} / {acl}")

verdict, acl = policy_verdict(json.dumps(RAW))
check("panel reads the RAW payload too", verdict == "BLOCKED", verdict)
check("panel names the rule from the raw payload", acl == "Deny-Example-Rule", acl)

allowed = {"path_calc_results": {"traffic_allowed": True, "device_info": []}}
verdict, _ = policy_verdict(json.dumps(allowed))
check("a permitted path reads as ALLOWED from raw", verdict == "ALLOWED", verdict)

verdict, _ = policy_verdict("Error calling Tufin: connection refused")
check("a tool error is still UNKNOWN, not a verdict", verdict == "UNKNOWN", verdict)

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
