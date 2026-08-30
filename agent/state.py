"""The graph's state. One place, so the UI knows what it can read."""
from typing import Annotated, Optional, TypedDict

from langgraph.graph.message import add_messages


class NetState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    loops: int                 # agent<->tools round trips
    max_loops: int             # budget for THIS turn (deeper checks get more)
    devices: dict              # device_name -> CMDB result
    commands_run: list         # audit trail: {device_ip, command, approved}
    ping_ok: Optional[bool]    # parsed from the ping output
    hops: list                 # parsed from the traceroute output
    path: str                  # "src -> hop -> ... -> dst"
    platform: str              # what the SOURCE turned out to be, as the
                               # notebook keys it -- read from the CMDB
                               # record, corrected by the box itself
    answer: str                # final text from the agent
