# alert_mock.py -- Archangel alerts without a database.
#
# Shaped exactly like the real reply: a list of dicts, one per open alert, with
# the UUID-ish alert_id and numeric ticket_id the real table carries.
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("archangel-server")

_ALERTS = {
    "APP-SRV-DC1-020": [
        {"alert_id": "9a7fc3aa-0000-1111-2222-333333333333",
         "device_name": "APP-SRV-DC1-020", "alert_type": "network",
         "alert_title": "LinkStatusOperDown",
         "check_name": "Interface Bundle-Ether90", "ticket_id": "560000001"},
        {"alert_id": "9a7ff104-0000-1111-2222-333333333333",
         "device_name": "APP-SRV-DC1-020", "alert_type": "network",
         "alert_title": "LinkStatusOperDown",
         "check_name": "Interface TenGigE0/0/0/14", "ticket_id": "560000001"},
        {"alert_id": "fc23e54e-0000-1111-2222-333333333333",
         "device_name": "APP-SRV-DC1-020", "alert_type": "network",
         "alert_title": "InterfaceAlert",
         "check_name": "Interface Bundle-Ether20.3003", "ticket_id": "560000002"},
    ],
    "PAY-API-DC2-010": [
        {"alert_id": "a2f5d3a6-0000-1111-2222-333333333333",
         "device_name": "PAY-API-DC2-010", "alert_type": "network",
         "alert_title": "InterfaceAlert",
         "check_name": "Interface TenGigE0/0/0/5", "ticket_id": "560000003"},
    ],
}


@mcp.tool(
    name="get_alert_and_ticket_details_from_archangel",
    description="Open alerts and their incident ticket IDs for one device, "
                "from Archangel (mock). Read-only.")
def get_alert_and_ticket_details_from_archangel(
    device_name: Annotated[str, Field(description="device name")],
    limit: Annotated[Optional[int], Field(description="most alerts")] = 25,
) -> list | str:
    name = str(device_name or "").strip().upper()
    rows = _ALERTS.get(name)
    if not rows:
        return (f"No open alerts found for '{device_name}'. The device may "
                f"have none, or may be spelled differently in Archangel than "
                f"in the CMDB.")
    return rows[: max(1, min(int(limit or 25), 50))]


if __name__ == "__main__":
    mcp.run()   # stdio
