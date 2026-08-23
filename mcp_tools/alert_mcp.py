# alert_mcp.py -- open alerts and their incident tickets, from Archangel.
#
#   get_alert_and_ticket_details_from_archangel(device_name)
#     -> [{alert_id, alert_type, alert_title, check_name, device_name,
#          ticket_id}, ...]
#
# Read-only by construction: one SELECT, no session.commit() anywhere, and the
# connection is opened without autocommit. Nothing here can change a row.
#
# The device name is BOUND, never interpolated. It arrives from the model, and
# a name like  X' OR '1'='1  in an f-string is a live SQL injection -- one that
# would run with whatever rights the connection has.
#
# Credentials live in credentials.yml beside the others:
#   ARCHANGEL_DB_URL: postgresql+psycopg2://user:password@host:5432/archangel
import os
import sys
from typing import Annotated, Optional

import yaml
from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("archangel-server")

_CREDENTIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "credentials.yml")


class Settings:
    load_error = ""
    try:
        with open(_CREDENTIALS, "r") as f:
            credentials = yaml.safe_load(f) or {}
    except FileNotFoundError:
        credentials = {}
    except Exception as ex:                              # noqa: BLE001
        credentials = {}
        load_error = f"{_CREDENTIALS} could not be read: {ex}"
        print(f"[archangel] {load_error}", file=sys.stderr)

    DB_URL = str(credentials.get("ARCHANGEL_DB_URL", "") or "")
    # seconds; a stuck query must not hold the whole run
    TIMEOUT = int(os.environ.get("ARCHANGEL_TIMEOUT", "20"))


# Only open alerts, and only the columns the agent reads. The join is on the
# ticket the alert points at, so an alert with no ticket does not appear -- an
# alert nobody has raised a ticket for is not what this question is about.
_SQL = """
    select alert.id         as alert_id,
           alert.name       as device_name,
           alert.alert_type as alert_type,
           alert.title      as alert_title,
           alert.check_name as check_name,
           ticket.ticket_id as ticket_id
      from public.alert alert
      join public.ticket ticket on ticket.id = alert.ticket_table_id
     where ticket.ticket_status = 'open_state'
       and upper(alert.name) = :device
     order by alert.id
     limit :limit
"""

_ENGINE = None


def _engine():
    """One engine for the process, built on first use.

    Built lazily so the server still starts -- and can say what is wrong --
    when the database is unreachable or the driver is missing.
    """
    global _ENGINE
    if _ENGINE is None:
        from sqlalchemy import create_engine
        _ENGINE = create_engine(
            Settings.DB_URL,
            pool_pre_ping=True,                 # a stale pooled connection is
                                                # otherwise a random failure
            connect_args={"connect_timeout": Settings.TIMEOUT}
            if Settings.DB_URL.startswith("postgresql") else {},
        )
    return _ENGINE


@mcp.tool(
    name="get_alert_and_ticket_details_from_archangel",
    description="Open alerts and their incident ticket IDs for one device, "
                "from Archangel. Read-only. Takes the device NAME as the CMDB "
                "spells it. Returns one entry per open alert, or a message if "
                "the device has none.")
def get_alert_and_ticket_details_from_archangel(
    device_name: Annotated[str, Field(
        description="device name, as the CMDB record spells it")],
    limit: Annotated[Optional[int], Field(
        description="most alerts to return (1-50)")] = 25,
) -> list | dict | str:
    name = str(device_name or "").strip()
    if not name:
        return "device_name is required."
    if not Settings.DB_URL:
        return ("Archangel is not configured: add ARCHANGEL_DB_URL to "
                "credentials.yml.")

    try:
        from sqlalchemy import text
    except ImportError:
        return ("SQLAlchemy is not installed on this host: "
                "uv pip install -r requirements.txt")

    rows_wanted = max(1, min(int(limit or 25), 50))
    try:
        # a connection, not a session: nothing here writes, and this way there
        # is no transaction left open to commit or roll back
        with _engine().connect() as conn:
            result = conn.execute(text(_SQL),
                                  {"device": name.upper(), "limit": rows_wanted})
            columns = list(result.keys())
            rows = result.fetchall()
    except Exception as ex:                              # noqa: BLE001
        # stderr: stdout carries the MCP JSON-RPC stream
        print(f"[archangel] query failed: {ex}", file=sys.stderr)
        return f"Error querying Archangel: {ex}"

    if not rows:
        return (f"No open alerts found for '{name}'. The device may have none, "
                f"or may be spelled differently in Archangel than in the CMDB.")

    return [dict(zip(columns, row)) for row in rows]


if __name__ == "__main__":
    mcp.run()   # stdio
