"""One command that says which piece is broken.

    .venv\\Scripts\\python.exe tests\\diagnose.py

Checks, in the order a run depends on them:

  1. config      what the process actually thinks it is doing
  2. packages    the versions that have broken things before
  3. servers     starts the REAL MCP servers and lists their tools
  4. archangel   the alert query, if a device name is given
  5. model       sends the real system prompt AND tool schemas to the
                 configured endpoint, and reports what came back

Prints nothing secret: the DB URL is shown with the password blanked and no
credential is read out. Safe to paste.

    .venv\\Scripts\\python.exe tests\\diagnose.py --device SW-EDGE-01
    .venv\\Scripts\\python.exe tests\\diagnose.py --skip-model
"""
import asyncio
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

ARGS = sys.argv[1:]


def _arg(flag, default=None):
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS and ARGS.index(flag) + 1 < len(ARGS) else default


DEVICE = _arg("--device")
SKIP_MODEL = "--skip-model" in ARGS


def head(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---- 1. config --------------------------------------------------------------
head("1. CONFIG")
try:
    from agent import constants as C
    print(f"  python        {sys.version.split()[0]}")
    print(f"  USE_MOCKS     {os.environ.get('USE_MOCKS', '(unset)')}"
          f"   -> servers: {list(C.MCP_SERVERS)}")
    print(f"  LLM_MODE      {C.LLM_MODE}")
    print(f"  LLM_BASE_URL  {C.LLM_BASE_URL}")
    print(f"  LLM_MODEL     {C.LLM_MODEL}")
    print(f"  LLM_API_KEY   {'set' if C.LLM_API_KEY not in ('', 'EMPTY') else 'EMPTY / unset'}")
    print(f"  CLIP_MODE     {C.CLIP_MODE}")
    print(f"  MASK_NAMES    {C.MASK_NAMES}"
          f"  -> active: {C.mask_names_enabled(C.LLM_MODE)}")
    print(f"  LOCAL_PROBES  {C.LOCAL_PROBES}")
    print(f"  MAX_TOOL_LOOPS {C.MAX_TOOL_LOOPS}")
    creds = os.path.join(ROOT, "credentials.yml")
    print(f"  credentials   {'found' if os.path.exists(creds) else 'MISSING at ' + creds}")
except Exception:
    print("  CONFIG FAILED TO LOAD -- nothing else can work:")
    traceback.print_exc()
    raise SystemExit(1)

# ---- 2. packages ------------------------------------------------------------
head("2. PACKAGES")
for name in ("langchain_core", "langchain_openai", "langgraph", "openai",
             "langchain_mcp_adapters", "mcp", "fastmcp", "fastapi",
             "sqlalchemy", "psycopg2"):
    try:
        mod = __import__(name)
        print(f"  {name:24s} {getattr(mod, '__version__', '(no __version__)')}")
    except Exception as ex:
        note = ("  <- only the host running alert_mcp.py needs it"
                if name in ("sqlalchemy", "psycopg2") else "")
        print(f"  {name:24s} NOT INSTALLED: {ex}{note}")

# ---- 3. MCP servers ---------------------------------------------------------
head("3. MCP SERVERS (as configured above)")


async def _servers():
    from agent import graph as G
    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient(C.MCP_SERVERS)
    tools, owner, failed = await G._load_tools(client)
    for name in sorted(getattr(t, "name", "?") for t in tools):
        gated = " (needs approval)" if owner.get(name) in C.DEVICE_SERVERS else ""
        print(f"  OK    {owner.get(name, '?'):10s} {name}{gated}")
    for server, why in failed.items():
        print(f"  DEAD  {server:10s} {why[:120]}")
    if failed:
        print("\n  A dead server does not stop a run: the agent works with what")
        print("  it has and says which checks it could not do. It DOES mean")
        print("  that server's stage will be empty.")
    return tools


try:
    TOOLS = asyncio.run(_servers())
except Exception:
    TOOLS = []
    traceback.print_exc()

# ---- 4. archangel -----------------------------------------------------------
head("4. ARCHANGEL")
try:
    sys.path.insert(0, os.path.join(ROOT, "mcp_tools"))
    import alert_mcp
    print(f"  ARCHANGEL_DB_URL  {alert_mcp._redacted_url() or '(not set)'}")
    if not DEVICE:
        print("  no --device given, so the query was not run:")
        print("    .venv\\Scripts\\python.exe tests\\diagnose.py --device <NAME>")
    else:
        answer = alert_mcp.get_alert_and_ticket_details_from_archangel(DEVICE)
        if isinstance(answer, str):
            print(f"  {answer}")
        else:
            print(f"  {len(answer)} row(s), first: {answer[0] if answer else ''}")
except Exception:
    traceback.print_exc()

# ---- 5. the model -----------------------------------------------------------
head("5. MODEL")
if SKIP_MODEL:
    print("  skipped (--skip-model)")
elif C.LLM_MODE == "clipboard":
    print("  LLM_MODE=clipboard: the relay needs a human, so nothing to probe.")
    print("  Set LLM_MODE=api to test an endpoint from here.")
else:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from agent import prompts
        from agent.graph import build_llm
        llm = build_llm()
        if TOOLS:
            llm = llm.bind_tools(TOOLS)
        system = prompts.system_prompt(C.LLM_MODE)
        print(f"  system prompt {len(system)} chars, {len(TOOLS)} tool(s) bound")
        print(f"  asking {C.LLM_MODEL} at {C.LLM_BASE_URL} ...")
        reply = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content="Troubleshoot 10.10.1.20 to 172.20.5.10 on "
                                 "tcp:443"),
        ])
        calls = getattr(reply, "tool_calls", []) or []
        text = (reply.content or "")
        print(f"  replied: {len(calls)} tool call(s)"
              f"{', first: ' + calls[0]['name'] if calls else ''}")
        if text:
            print(f"  text: {str(text)[:300]}")
        if not calls and not text:
            print("  EMPTY REPLY -- the endpoint answered but said nothing. "
                  "Usually a model that cannot do tool calling, or a bridge "
                  "dropping the tool schemas.")
    except Exception:
        print("  THE MODEL CALL FAILED -- this alone breaks every run:")
        traceback.print_exc()

print()
print("=" * 70)
print("Paste this whole output. It names the layer that is broken.")
