# alert_mock.py -- Archangel alerts without a database.
#
# Shaped exactly like the real reply: a list of dicts, one per open alert, with
# the UUID-ish alert_id and numeric ticket_id the real table carries.
from typing import Annotated, Optional

import os
import sys

from mcp.server.fastmcp import FastMCP
from pydantic import Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scenarios as S  # noqa: E402

mcp = FastMCP("archangel-server")

_ALERTS = S.ALERTS


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
