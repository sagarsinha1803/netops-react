"""Find out why get_firewall_path fails, by watching the server's own stderr.

"[Errno 9] Bad file descriptor" is the CLIENT's view of the stdio pipe dying.
It says nothing about the cause, which is always on the server side. This runs
the server three ways -- imported, spawned raw, and through the MCP client --
so the step that breaks is obvious.

    uv run python tests/diagnose_tufin.py [src] [dst]
"""
import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import constants as C  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "10.10.1.20"
DST = sys.argv[2] if len(sys.argv) > 2 else "10.10.1.21"
SERVER = os.path.join(C.MCP_DIR, "tufin_mcp.py")


def rule(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# ---- 1. does the module import, and is it configured? ----------------------
rule("1. import the server module directly")
sys.path.insert(0, C.MCP_DIR)
try:
    import tufin_mcp
    print("   import OK")
    print(f"   credentials file : {tufin_mcp._CREDENTIALS}")
    print(f"   exists           : {os.path.isfile(tufin_mcp._CREDENTIALS)}")
    print(f"   load_error       : {getattr(tufin_mcp.Settings, 'load_error', '') or 'none'}")
    print(f"   TUFIN_URL        : {tufin_mcp.Settings.URL or '(not set)'}")
    print(f"   TUFIN_USER       : {'set' if tufin_mcp.Settings.USER else '(not set)'}")
    print(f"   TUFIN_PASSWORD   : {'set' if tufin_mcp.Settings.PASSWORD else '(not set)'}")
    print(f"   VERIFY           : {tufin_mcp.Settings.VERIFY!r}")
    print(f"   TIMEOUT          : {getattr(tufin_mcp.Settings, 'TIMEOUT', '(old copy)')}")
except Exception as e:
    print(f"   IMPORT FAILED: {type(e).__name__}: {e}")
    print("   -> this alone causes Errno 9: the process dies before it speaks.")
    raise SystemExit(1)

# ---- 2. call the function in-process, no MCP in the way --------------------
rule("2. call get_firewall_path directly (no MCP)")
try:
    out = tufin_mcp.get_firewall_path(SRC, DST, "any")
    text = str(out)
    print(f"   returned {len(text)} chars")
    print(f"   {text[:400]}")
except Exception as e:
    print(f"   RAISED: {type(e).__name__}: {e}")
    print("   -> the HTTP call itself is the problem, not MCP.")

# ---- 3. spawn it exactly as the agent does, and read stdout/stderr ---------
rule("3. spawn as a subprocess and watch the streams")
proc = subprocess.Popen([sys.executable, SERVER],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
try:
    out, err = proc.communicate(input="", timeout=15)
except subprocess.TimeoutExpired:
    proc.kill()
    out, err = proc.communicate()
    print("   still running after 15s (normal for a server)")

if out.strip():
    print("   !! WROTE TO STDOUT -- stdout is the JSON-RPC channel, and any")
    print("      banner or debug dump there corrupts it:")
    for line in out.strip().splitlines()[:5]:
        print(f"        {line[:100]}")
else:
    print("   stdout clean (correct: only JSON-RPC belongs there)")
if err.strip():
    print("   stderr:")
    for line in err.strip().splitlines()[:10]:
        print(f"        {line[:120]}")

# ---- 4. the real thing, through the MCP client -----------------------------
rule("4. call it through the MCP client, as the agent does")


async def via_mcp():
    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient({"tufin": C._stdio(C.MCP_DIR, "tufin_mcp.py")})
    try:
        tools = await client.get_tools(server_name="tufin")
        print(f"   tools loaded: {[t.name for t in tools]}")
    except Exception as e:
        print(f"   get_tools FAILED: {type(e).__name__}: {e}")
        return
    tool = {t.name: t for t in tools}.get("get_firewall_path")
    try:
        result = await tool.ainvoke({"src": SRC, "dst": DST, "service": "any"})
        print(f"   call OK: {str(result)[:300]}")
    except Exception as e:
        print(f"   call FAILED: {type(e).__name__}: {e}")
        print("   -> compare with step 2: if that worked and this did not,")
        print("      the fault is in the stdio transport, not the Tufin call.")


asyncio.run(via_mcp())
print()
