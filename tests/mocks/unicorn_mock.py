"""MOCK unicorn CMDB MCP -- local testing only (no creds, no HTTP).

Same tool name/signature as unicorn_mcp.py, including name-OR-ip lookup, so the
agents behave identically off-network.

The canned records are deliberately RAW: nested remoteManagement objects with
login and password inside, owner names, a chassis inventory -- the shape the
real CMDB returns. They are passed through the real unicorn_mcp._slim, so a mock
run exercises the actual redaction rather than a hand-cleaned copy that cannot
prove anything.
"""
import os
import re
import sys
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "mcp_tools"))

from unicorn_mcp import _slim  # noqa: E402

mcp = FastMCP("unicorn-server")

def _raw(name, ip, brand, model, os_name, os_version):
    """A record shaped like the real unicorn API's.

    Mirrors the production structure exactly -- nested brand/model/OS objects, a
    remoteManagement LIST with console and ssh entries carrying login, password
    and database ids, owner records with names and emails, and the physical
    location down to the rack. Values are invented; the shape is the point, and
    real employee names and building codes have no business in a repository.
    """
    return {
        "brand": {"name": brand, "slug": brand.lower()},
        "teamInCharge": [{"name": "NET/CORE",
                          "email": "list.net-core@example.com",
                          "slug": "netcore"}],
        "accountable": [{"firstName": "Ada", "lastName": "LOVELACE",
                         "email": "ada.lovelace@example.com",
                         "slug": "ada-lovelace"},
                        {"firstName": "Alan", "lastName": "TURING",
                         "email": "alan.turing-ext@example.com",
                         "slug": "alan-turing"}],
        "operatingSystemVersion": {
            "version": os_version,
            "operatingSystem": {"name": os_name, "slug": os_name.lower()},
            "slug": f"{os_name.lower()}-{os_version}"},
        "infrastructure": [{"name": "EXA-MAN", "slug": "exa-man",
                            "type": "group-critical", "decommissioned": False,
                            "description": "",
                            "kearId": "00000000-1111-2222-3333-444444444444"}],
        "environment": {"name": "Production", "slug": "production"},
        "brandModel": {"name": model, "endOfSupport": None, "eosm": None,
                       "brand": {"name": brand, "slug": brand.lower()},
                       "slug": f"{brand.lower()}-{model.lower().replace(' ', '-')}"},
        "remoteManagement": [
            {"id": 1000001, "protocol": {"name": "console"},
             "ip": "203.0.113.98", "port": 7013, "additionalInformation": None,
             "plainTextURL": None, "login": "svc-account",
             "password": "hunter2", "device": 9001},
            {"id": 1000002, "protocol": {"name": "ssh"}, "ip": ip, "port": "22",
             "additionalInformation": None, "plainTextURL": None,
             "login": "svc-account", "password": "hunter2", "device": 9001},
        ],
        "location": {"last_physical_inventory_date": "2026-02-18",
                     "rack_position": "1", "cabinet": "AZ25", "room": "10A1",
                     "floor": "RDC", "building": "EX01234.B90-DC10",
                     "city": "Exampleville", "country": "FRANCE",
                     "geographical_zone": "Europe"},
        "operatingSystemVersionEndOfSupport": {"endOfSupport": "2027-12-09"},
        "tag": [],
    }


_RAW = {
    "APP-SRV-DC1-020": _raw("APP-SRV-DC1-020", "10.10.1.20", "Cisco",
                            "ASR 9010", "IOS XE", "17.9.3"),
    "PAY-API-DC2-010": _raw("PAY-API-DC2-010", "172.20.5.10", "Cisco",
                            "N9K-C93180", "NX-OS", "10.2"),
    "LEAF-101": _raw("Leaf-101", "10.10.1.1", "Cisco", "N9K-C93108",
                     "NX-OS", "10.2"),
    "BORDER-ROUTER-01": _raw("Border-Router-01", "10.10.0.1", "Cisco",
                             "ASR 9906", "IOS XR", "7.11.2"),
    "FW-DC1-EDGE-01": _raw("FW-DC1-EDGE-01", "10.10.255.1", "Checkpoint",
                           "CP-6800", "Gaia", "R81.20"),
}
_REGION = {name: "INDIA" for name in _RAW}
# keyed on the ssh entry, which is what the agent connects over
_BY_IP = {d["remoteManagement"][1]["ip"]: key for key, d in _RAW.items()}


def _is_ip_address(value: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(value).strip()))


@mcp.tool(
    name="get_device_details",
    description="Look up device details in unicorn (CMDB) by device NAME or IP address. "
                "Region optional: omit or 'AUTO' to search every region.")
def get_device_details(
    device_name: Annotated[str, Field(description="device name OR IPv4 address")],
    region: Annotated[Optional[str], Field(description="region or AUTO")] = None,
) -> dict | str:
    value = str(device_name).strip()
    by = "ip" if _is_ip_address(value) else "name"
    key = _BY_IP.get(value) if by == "ip" else value.upper()
    raw = _RAW.get(key or "")
    if not raw:
        return f"No data found for {by} '{value}' in any region."
    # through the REAL slimmer, so the mock proves the redaction works
    slim = _slim(raw)
    # Only an IP lookup resolves a real hostname. By name it would just echo the
    # caller's own input back as if it were a CMDB field, which the model then
    # reports as the record disagreeing with its own query.
    if by == "ip":
        slim["name"] = key
    return {"region": _REGION[key], "lookup_by": by, "query": value,
            "data": slim}


if __name__ == "__main__":
    mcp.run()   # stdio
