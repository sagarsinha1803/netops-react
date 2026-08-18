# unicorn_mcp.py -- CMDB lookup by device NAME or IP.
#
# One tool, auto-routing:
#   value looks like an IPv4 address -> searchEngine/?element=<ip> -> take the
#       'remote management' entry -> its name -> devices/?name=<name>
#   otherwise                        -> devices/?name=<NAME> directly
#
# Region may be omitted / 'AUTO' -> every configured region is tried in turn.
import os
import re
import sys
from typing import Annotated, Optional

import redact
import requests
import urllib3
import yaml
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

mcp = FastMCP("unicorn-server")

# Regions that require a proxy for Unicorn API calls
PROXY_REQUIRED_REGIONS = {"paris", "uk", "star_sdwan", "sgrf_sdwan", "afs"}


# credentials.yml sits at the project root, one level up from mcp_tools/,
# so the servers find it whatever directory they are launched from
_CREDENTIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "credentials.yml")


class Settings:
    try:
        with open(_CREDENTIALS, "r") as f:
            credentials = yaml.safe_load(f)
    except FileNotFoundError:
        credentials = {}

    # per-region base URL for the device API (must end in / -- see _base())
    UNICORN_URLS = credentials.get("UNICORN_URLS", {})
    UNICORN_TOKEN = credentials.get("UNICORN_TOKEN", {})
    PROXIES = credentials.get("PROXIES", [])


def _is_ip_address(value: str) -> bool:
    """True if value looks like an IPv4 address (e.g. 196.34.145.88)."""
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(value).strip()))


def _base(region: str) -> str:
    """Device API base for a region, always with a trailing slash."""
    url = str(Settings.UNICORN_URLS[region])
    return url if url.endswith("/") else url + "/"


def _proxy_info(proxy: dict) -> dict:
    """Build the authenticated proxy mapping from a credentials.yml entry."""
    begin, end = proxy.get("uri").split("//")
    return {proxy.get("protocol"):
            f"{begin}//{proxy.get('login')}:{proxy.get('password')}@{end}"}


def _make_request(session, url, api_key, **kwargs):
    """Authenticated GET against the Unicorn API."""
    response = session.get(
        url,
        headers={"Accept": "application/json",
                 "Authorization": f"Token {api_key}"},
        verify=False,
        timeout=30,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def _resolve_name_from_ip(session, region, ip, api_key, **kwargs) -> Optional[str]:
    """IP -> device name, via the search engine's 'remote management' entry."""
    search_url = f"{_base(region)}searchEngine/?element={ip}"
    search_result = _make_request(session, search_url, api_key, **kwargs)

    if not isinstance(search_result, list) or len(search_result) == 0:
        return None

    rm_entry = None
    for item in search_result:
        if item.get("type") == "remote management":
            rm_entry = item
            break
    if not rm_entry:
        return None
    return rm_entry.get("name")


# What the agent is allowed to see. The raw record carries the whole chassis
# inventory -- and in some regions login and password fields whose names are not
# known in advance. Only these scalars are copied; anything else is absent by
# construction rather than needing to be anticipated.
#
# Deliberately NOT here: teamInCharge and accountable (people's names),
# location, tag, and operatingSystemVersionEndOfSupport (which boxes are past
# support). The agent never reads them, so they have no reason to leave the CMDB.
# Exactly what the agent needs, and where it lives in the real record. Anything
# not on this list never leaves the CMDB: employee names and emails, the
# physical location down to the rack, infrastructure group, environment,
# end-of-support dates, tags, database ids and every credential field.
#
# Paths, not a key filter: the record nests almost everything. brand is
# {'name': 'Cisco'}, the OS sits two levels down, and remoteManagement is a list
# of dicts carrying login/password/id alongside the address.
_PATHS = {
    "brand":            ("brand", "name"),                  # Cisco
    "brandModel":       ("brandModel", "name"),             # ASR 9910
    "operatingSystem":  ("operatingSystemVersion", "operatingSystem", "name"),
    "osVersion":        ("operatingSystemVersion", "version"),
}

# SSH is what the agent actually connects over; console is a fallback.
_MGMT_PREFERRED = ("ssh", "telnet")


def _management(data: dict) -> dict:
    """The management address, preferring the SSH entry over the console one.

    Only the address and port are taken. Each entry also carries login,
    password, a database id and a device id -- none of which are copied.
    """
    entries = data.get("remoteManagement")
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return {}

    def rank(entry):
        proto = str(redact.path(entry, "protocol", "name", default="")).lower()
        return _MGMT_PREFERRED.index(proto) if proto in _MGMT_PREFERRED else 9

    for entry in sorted((e for e in entries if isinstance(e, dict)), key=rank):
        ip = redact.safe_scalar("ip", entry.get("ip"))
        if ip and redact.looks_like_ip(ip):
            out = {"managementIp": ip}
            port = redact.safe_scalar("port", entry.get("port"))
            if port:
                out["managementPort"] = port
            proto = redact.path(entry, "protocol", "name")
            if proto:
                out["managementProtocol"] = proto
            return out
    return {}


def _slim(data: Optional[dict]) -> Optional[dict]:
    """Build the record the agent sees. Nothing is copied that is not listed.

    Built rather than filtered, so a field appearing in a future API version is
    absent by default instead of leaking by default.
    """
    if not data:
        return None
    out = {}
    for label, keys in _PATHS.items():
        value = redact.path(data, *keys)
        if value not in (None, ""):
            out[label] = value
    out.update(_management(data))
    return out


def _fetch(session, region, value, api_key, **kwargs) -> Optional[dict]:
    """Look up one device in one region. value may be a name or an IP."""
    resolved = None
    if _is_ip_address(value):
        # the search engine knows the real hostname for an address
        resolved = _resolve_name_from_ip(session, region, value, api_key, **kwargs)
        if not resolved:
            return None
        device_name = resolved
    else:
        device_name = str(value).upper()

    device_url = f"{_base(region)}devices/?name={device_name}"
    data = _make_request(session, device_url, api_key, **kwargs)
    results = (data or {}).get("results", [])
    if not results:
        return None
    slim = _slim(results[0])
    # The device record carries no hostname of its own. Only report a "name"
    # when the search engine actually resolved one from an address -- for a
    # lookup BY name it would just be the caller's own input, upper-cased, and
    # the model reads that back as a CMDB field disagreeing with its query.
    if resolved:
        slim["name"] = resolved
    return slim


@mcp.tool(
    name="get_device_details",
    description="""
    Look up device details in unicorn (CMDB).
    Accepts either a device NAME or a device IP address -- an IP is resolved to
    its device name first, then the device record is fetched.
    Region is optional: omit it or pass 'AUTO' to search every region.
    """)
def get_device_details(
    device_name: Annotated[str, Field(
        description="device name OR IPv4 address of the device")],
    region: Annotated[Optional[str], Field(
        description="Region (PARIS, ASIA, AMER, UK, INDIA, IBFS). "
                    "Omit or set 'AUTO' to auto-detect")] = None,
) -> dict | str:
    try:
        value = str(device_name).strip()
        lookup_by = "ip" if _is_ip_address(value) else "name"

        requested = (region or "").strip().upper()
        candidates = (list(Settings.UNICORN_URLS.keys())
                      if not requested or requested == "AUTO" else [requested])

        if requested and requested != "AUTO" and requested not in Settings.UNICORN_URLS:
            return (f"Invalid region '{requested}'. "
                    f"Valid: {', '.join(Settings.UNICORN_URLS.keys())}")

        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=2))

            for candidate in candidates:
                api_key = Settings.UNICORN_TOKEN[candidate]
                needs_proxy = candidate.lower() in PROXY_REQUIRED_REGIONS
                proxies = Settings.PROXIES if needs_proxy else [None]

                for index_proxy, proxy in enumerate(proxies):
                    kwargs = {"proxies": _proxy_info(proxy)} if proxy else {}
                    try:
                        data = _fetch(session, candidate, value, api_key, **kwargs)
                        if data:
                            return {"region": candidate, "lookup_by": lookup_by,
                                    "query": value, "data": data}
                        break                      # reached the API, no such device
                    except Exception as ex:
                        if index_proxy == len(proxies) - 1:
                            # stderr: stdout carries the MCP JSON-RPC stream
                            print(f"[{candidate}] lookup failed: {ex}",
                                  file=sys.stderr)
                        continue                   # try the next proxy

        return (f"No data found for {lookup_by} '{value}'"
                + ("." if len(candidates) == 1 else " in any region."))
    except Exception as ex:
        return f"Error: {str(ex)}"


if __name__ == "__main__":
    mcp.run()   # stdio
