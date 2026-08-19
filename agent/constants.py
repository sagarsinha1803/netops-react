"""Everything you may need to change when moving between environments.

The office values live here and nowhere else: server addresses, which servers
touch real devices, and the loop budgets. Nothing in this module imports the
rest of the project, so it is safe to read from anywhere.
"""
import os
import sys

# project root -- the folder this package sits in
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# .env has to be read HERE, before the first os.environ lookup below. This is
# the first module the app imports, so loading it anywhere later (it used to be
# in graph.py) meant every setting on this page fell back to its default --
# CHAT_HISTORY in particular, which switches on Chainlit's auth callback and
# then stops the app booting for want of a JWT secret.
# Real environment variables win: `$env:MASK_STYLE="label"` still overrides.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

MCP_DIR = os.path.join(ROOT, "mcp_tools")
MOCK_DIR = os.path.join(ROOT, "tests", "mocks")
DATA_DIR = os.path.join(ROOT, "data")
CREDENTIALS = os.path.join(ROOT, "credentials.yml")

USE_MOCKS = os.environ.get("USE_MOCKS", "").lower() in ("1", "true", "yes")


def _stdio(folder: str, script: str) -> dict:
    """An MCP server run as a local subprocess over stdio."""
    return {"command": sys.executable,
            "args": [os.path.join(folder, script)],
            "transport": "stdio"}


# ---- MCP servers -------------------------------------------------------------
# The ssh server is mcp_tools/troubleshoot_agent_mcp.py -- execute_query_on_server,
# which SSHes to the device through the region's bastion. Its own docstring says
# not to run it from the base machine, so by default the agent talks to the copy
# deployed on the box that has bastion access, over SSE.
#
# SSH_MCP_TRANSPORT=stdio runs it as a local subprocess instead. Only do that ON
# the bastion-capable host, where credentials.yml holds SSH_JUMPHOST_DETAILS and
# DEVICE_DETAILS_SSH.
SSH_MCP_URL = os.environ.get("SSH_MCP_URL", "http://ssh-mcp-host:4021/sse")
SSH_MCP_TRANSPORT = os.environ.get("SSH_MCP_TRANSPORT", "sse").lower()

SSH_SERVER = (_stdio(MCP_DIR, "troubleshoot_agent_mcp.py")
              if SSH_MCP_TRANSPORT == "stdio"
              else {"url": SSH_MCP_URL, "transport": "sse"})

MCP_SERVERS = {
    "unicorn": _stdio(MCP_DIR, "unicorn_mcp.py"),          # get_device_details
    "tufin":   _stdio(MCP_DIR, "tufin_mcp.py"),            # get_firewall_path
    "ssh":     SSH_SERVER,                                 # execute_query_on_server
    "local":   _stdio(MCP_DIR, "local_probe_mcp.py"),      # local_ping / local_traceroute
}

if USE_MOCKS:
    MCP_SERVERS = {
        "unicorn": _stdio(MOCK_DIR, "unicorn_mock.py"),
        "tufin":   _stdio(MOCK_DIR, "tufin_mock.py"),
        "ssh":     _stdio(MOCK_DIR, "device_mock.py"),
        "local":   _stdio(MOCK_DIR, "local_probe_mock.py"),
    }

# Which SERVERS touch real devices. Every tool they expose -- including ones
# added later -- is automatically read-only checked and human approved. Tools
# whose origin cannot be determined are treated as device tools (deny by
# default), so a new server is never accidentally ungated.
#
# tufin is NOT here on purpose: it is a read-only GET against SecureTrack's
# topology API, it runs no command on a device, and gating it would put an
# approval prompt in front of every policy lookup.
# "local" is here too: its probes leave THIS machine's interfaces, and the
# reviewer asked that they be approved like any other command.
DEVICE_SERVERS = {"ssh", "local"}

# tools whose calls are worth printing as an explicit command trace
DEVICE_TOOL_NAMES = {"execute_query_on_server", "ping_device", "traceroute_device",
                     "local_ping", "local_traceroute"}
# tools that ask Tufin rather than a device
POLICY_TOOL_NAMES = {"get_firewall_path"}

# ---- behaviour ---------------------------------------------------------------
REQUIRE_APPROVAL = True

MAX_TOOL_LOOPS = 12       # agent<->tools round trips before we force an answer
                          # (2 CMDB + ping + traceroute + Tufin + escalation)
DEEP_MAX_LOOPS = 10       # budget for a deeper-checks turn: the model reasons
                          # its way through the escalation one check at a time,
                          # so it needs its own allowance. Per turn, via state.

# ---- masking -----------------------------------------------------------------
# Addresses (IPv4, IPv6, MAC) are handled by agent/llm/ip_mask.py, which reads
# MASK_IPS, MASK_STYLE and MASK_POOL itself.
#
# Names -- hostnames, ACLs, VRFs, interfaces, regions, route distinguishers --
# are the entity map in agent/entities.py. Default "auto" turns them on only for
# the clipboard relay: with an API model you have already accepted that
# endpoint's boundary, and stand-ins would only degrade its reasoning. Set 1 to
# mask them whatever the backend.
MASK_NAMES = os.environ.get("MASK_NAMES", "auto").strip().lower()

# CMDB credential redaction (mcp_tools/redact.py) has no switch on purpose:
# passwords have no business leaving the CMDB in any mode.


def mask_names_enabled(llm_mode: str) -> bool:
    if MASK_NAMES in ("0", "false", "no", "off"):
        return False
    if MASK_NAMES in ("1", "true", "yes", "on", "always"):
        return True
    return llm_mode == "clipboard"          # "auto"


# ---- LLM ---------------------------------------------------------------------
LLM_MODE = os.environ.get("LLM_MODE", "clipboard")
CLIP_MODE = os.environ.get("CLIP_MODE", "delta")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")

# Bearer token for the endpoint. A local vLLM usually ignores it, so the
# placeholder keeps that case working with nothing configured -- but GitHub
# Models and any gateway in front of vLLM will reject the placeholder, which
# surfaces as a 401 rather than anything about a missing setting.
#
# Keep it in .env or the environment, never in the code: it is a credential.
LLM_API_KEY = (os.environ.get("LLM_API_KEY")
               or os.environ.get("OPENAI_API_KEY")
               or os.environ.get("GITHUB_TOKEN")
               or "EMPTY")

# Seconds to wait for a completion. A reasoning model behind vLLM can be slow,
# and the default (600s) matches the patience the clipboard relay had.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "600"))

# ---- UI ----------------------------------------------------------------------
CHAT_HISTORY = os.environ.get("CHAT_HISTORY", "1").lower() not in ("0", "false", "no")
APP_USER = os.environ.get("APP_USER", "netops")
