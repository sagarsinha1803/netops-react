"""A CMDB reply must be judged on whether a RECORD came back.

Judging on whether it carries a hostname marked a real record as "not found in
CMDB": a lookup BY NAME deliberately omits `name`, since it would only echo the
caller's own input back. The row went red while the stage above it, reading the
same data, stayed green.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.workflow import Workflow, cmdb_record            # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# a real record from a lookup BY NAME -- no "name" key, by design
BY_NAME = json.dumps({
    "region": "ASIA", "lookup_by": "name", "query": "SW-EXAMPLE-01",
    "data": {"brand": "Arista", "brandModel": "DCS-7010T-48-R",
             "operatingSystem": "EOS", "osVersion": "4.19.5M",
             "managementIp": "198.51.100.10", "managementPort": "22",
             "managementProtocol": "ssh"}})

BY_IP = json.dumps({
    "region": "ASIA", "lookup_by": "ip", "query": "198.51.100.10",
    "data": {"brand": "Arista", "name": "SW-EXAMPLE-01",
             "managementIp": "198.51.100.10"}})

MISS = "No data found for name 'NOPE' in any region."
ERROR = "Error: connection refused"
REPR = str({"region": "ASIA", "query": "SW-EXAMPLE-01",
            "data": {"brand": "Arista"}})          # python repr, not JSON

found, label = cmdb_record(BY_NAME)
check("a record without a hostname field IS found", found is True)
check("and is labelled by what was asked for", label == "SW-EXAMPLE-01", label)

found, label = cmdb_record(BY_IP)
check("a record WITH a hostname is found and uses it",
      found is True and label == "SW-EXAMPLE-01", label)

check("a genuine miss is not found", cmdb_record(MISS) == (False, ""))
check("an error string is not found", cmdb_record(ERROR) == (False, ""))
check("an empty reply is not found", cmdb_record("") == (False, ""))
check("a python repr is understood too, not only JSON",
      cmdb_record(REPR)[0] is True)
check("a record whose data is empty is not found",
      cmdb_record(json.dumps({"query": "x", "data": {}})) == (False, ""))

# ---- the stage strip must agree with the rows ------------------------------
wf = Workflow()
wf.reset({"source": "SW-EXAMPLE-01", "dest": "SW-EXAMPLE-02"})
wf.from_state({"devices": {"SW-EXAMPLE-01": BY_NAME,
                           "SW-EXAMPLE-02": BY_NAME},
               "ping_ok": None, "hops": [], "commands_run": []})
stage = wf.state["cmdb"]
check("both records found -> the stage is done, not failed",
      stage["status"] == "done", str(stage))
check("and it does not claim a CMDB miss", wf.cmdb_miss is False)

wf2 = Workflow()
wf2.reset({"source": "NOPE", "dest": "ALSO-NOPE"})
wf2.from_state({"devices": {"NOPE": MISS, "ALSO-NOPE": MISS},
                "ping_ok": None, "hops": [], "commands_run": []})
check("two real misses still fail the stage",
      wf2.state["cmdb"]["status"] == "failed", str(wf2.state["cmdb"]))
check("and still set the CMDB-miss flag", wf2.cmdb_miss is True)

wf3 = Workflow()
wf3.reset({"source": "SW-EXAMPLE-01", "dest": "NOPE"})
wf3.from_state({"devices": {"SW-EXAMPLE-01": BY_NAME, "NOPE": MISS},
                "ping_ok": None, "hops": [], "commands_run": []})
check("one found, one missing -> done, and NOT a CMDB miss",
      wf3.state["cmdb"]["status"] == "done" and wf3.cmdb_miss is False,
      str(wf3.state["cmdb"]))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
