"""Nothing secret may leave the CMDB, whatever the field is called.

The real CMDB sometimes returns login and password fields, and the key names
vary by region. So this feeds _slim a deliberately hostile record -- credentials
nested inside the objects the old code copied wholesale, under names nobody
would have thought to deny -- and asserts none of it survives.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "mcp_tools"))

import redact                      # noqa: E402
from unicorn_mcp import _slim      # noqa: E402

SECRETS = [
    # credentials
    "hunter2", "s3cr3t-pass", "AKIAIOSFODNN7EXAMPLE", "svc-account", "admin",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9aaaaaaaaaaaaaaaaaaaa",
    # people
    "Ada", "LOVELACE", "ada.lovelace@example.com", "TURING",
    "list.net-core@example.com", "NET/CORE",
    # physical location, down to the rack
    "EX01234.B90-DC10", "AZ25", "10A1", "Exampleville", "FRANCE", "RDC",
    # identifiers and posture
    "00000000-1111-2222-3333-444444444444", "1000001", "1000002", "9001",
    "EXA-MAN", "Production", "2027-12-09", "group-critical",
    # the console entry -- the agent connects over ssh
    "203.0.113.98", "7013",
]

# The real record's shape: everything nested, remoteManagement a LIST carrying
# credentials and database ids next to the address, owners with names and
# emails, and the physical location down to the rack. Values invented.
RECORD = {
    "brand": {"name": "Cisco", "slug": "cisco"},
    "teamInCharge": [{"name": "NET/CORE", "email": "list.net-core@example.com"}],
    "accountable": [{"firstName": "Ada", "lastName": "LOVELACE",
                     "email": "ada.lovelace@example.com"},
                    {"firstName": "Alan", "lastName": "TURING",
                     "email": "alan.turing-ext@example.com"}],
    "operatingSystemVersion": {
        "version": "7.11.2",
        "operatingSystem": {"name": "IOS XR", "slug": "ios-xr"}},
    "infrastructure": [{"name": "EXA-MAN", "type": "group-critical",
                        "kearId": "00000000-1111-2222-3333-444444444444"}],
    "environment": {"name": "Production", "slug": "production"},
    "brandModel": {"name": "ASR 9910", "endOfSupport": None,
                   "brand": {"name": "Cisco"}},
    "remoteManagement": [
        {"id": 1000001, "protocol": {"name": "console"}, "ip": "203.0.113.98",
         "port": 7013, "login": "svc-account", "password": "hunter2",
         "device": 9001},
        {"id": 1000002, "protocol": {"name": "ssh"}, "ip": "203.0.113.99",
         "port": "22", "login": "svc-account", "password": "hunter2",
         "device": 9001},
    ],
    "location": {"rack_position": "1", "cabinet": "AZ25", "room": "10A1",
                 "floor": "RDC", "building": "EX01234.B90-DC10",
                 "city": "Exampleville", "country": "FRANCE"},
    "operatingSystemVersionEndOfSupport": {"endOfSupport": "2027-12-09"},
    "tag": [],
    # credentials under names a denylist would not have covered
    "accessProfile": {"acct": "admin", "pw": "s3cr3t-pass"},
    "provisioning": [{"apiKey": "AKIAIOSFODNN7EXAMPLE"},
                     {"bearer": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9aaaaaaaaaaaaaaaaaaaa"}],
    "sshKeyMaterial": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    "modules": [{"serial": f"FOC{i}", "slot": i} for i in range(50)],
}

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


out = _slim(RECORD)
blob = json.dumps(out)
print(f"\nrecord out: {blob}\n")

leaked = [s for s in SECRETS if s in blob]
check("no credential survives", not leaked, f"leaked={leaked}")

for field in ("teamInCharge", "accountable", "location", "tag",
              "environment", "infrastructure",
              "operatingSystemVersionEndOfSupport"):
    check(f"{field} dropped", field not in out)

check("no nested object copied",
      all(not isinstance(v, (dict, list)) for v in out.values()), blob)
check("chassis inventory gone", "FOC1" not in blob)

# what the agent still needs
check("vendor kept", out.get("brand") == "Cisco", str(out.get("brand")))
check("model kept", out.get("brandModel") == "ASR 9910", str(out.get("brandModel")))
check("OS name kept", out.get("operatingSystem") == "IOS XR", str(out.get("operatingSystem")))
check("OS version kept", out.get("osVersion") == "7.11.2", str(out.get("osVersion")))
check("SSH management IP chosen over console",
      out.get("managementIp") == "203.0.113.99", str(out.get("managementIp")))
check("management port kept", str(out.get("managementPort")) == "22")
check("record is small", len(blob) < 400, f"{len(blob)} chars")

# unit level
check("secret key detected", redact.is_secret_key("loginId")
      and redact.is_secret_key("pw") and redact.is_secret_key("login"))
check("id key detected", redact.is_id_key("id") and redact.is_id_key("kearId")
      and redact.is_id_key("device_id") and redact.is_id_key("serial"))
check("id detector does not fire on ordinary words",
      not any(redact.is_id_key(w) for w in ("valid", "invalid", "width", "video")))
check("private key detected by value",
      redact.is_secret_value("-----BEGIN RSA PRIVATE KEY-----"))
check("ordinary value not flagged", not redact.is_secret_value("IOS-XR 7.5.2"))
check("dict never returned as a scalar",
      redact.safe_scalar("anything", {"a": 1}) is None)
check("empty record handled", _slim(None) is None and _slim({}) is None)

sys.exit(1 if fails else 0)
