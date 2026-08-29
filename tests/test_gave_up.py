"""Two crosses that mean opposite things, and a run that stopped trying.

An operator read a red cross on the ping row as "the command did not work",
and reasonably so -- the same cross sits on rows where the device refused the
syntax. But that ping RAN. It came back "0 packets received, 100.00% packet
loss", which is a perfectly good answer and the whole reason for the
investigation. One glyph was carrying two meanings: the tool failed, and the
network failed.

And in the same run the deeper checks stopped with a third of the budget
unspent, handing back "check the forwarding table for the next hop" as the
next step -- a read-only command the agent was allowed to run itself.

    .venv/Scripts/python.exe tests/test_gave_up.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"
os.environ["REQUIRE_APPROVAL"] = "0"

from agent import constants as C                                # noqa: E402
from agent import graph as G                                    # noqa: E402
from api.workflow import Workflow, check_ok, usable_output      # noqa: E402
from scripted_model import ScriptedModel, ssh                   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"

# ---- 1. answered-and-bad is not the same as never-answered ----------------
LOST = (f"ping {DST} count 3\n"
        f"3 packets transmitted, 0 packets received, 100.00% packet loss\n")
REFUSED = (f"show ip route vrf all {DST}\n"
           f"Syntax error while parsing 'show ip route vrf all {DST}'\n")

check("a ping with no replies ANSWERED the question",
      usable_output(LOST, f"ping {DST} count 3"), LOST[:50])
check("and is still a failure, because nothing came back",
      not check_ok(LOST), LOST[:50])
check("a refused command answered nothing at all",
      not usable_output(REFUSED, f"show ip route vrf all {DST}"), REFUSED[:50])

wf = Workflow()
wf.reset({"source": SRC, "dest": DST})
i = wf.add_basic(f"ping {DST} count 3", kind="ping")
wf.finish_basic(i, False, "100.00% packet loss", answered=True)
j = wf.add_check(f"show ip route vrf all {DST}", SRC)
wf.finish_check(j, False, "Syntax error", answered=False)

rows = wf.snapshot()
ping_row = rows["basics"][0]
deep_row = rows["checks"][0]
check("the panel records that the ping answered",
      ping_row["status"] == "failed" and ping_row["answered"] is True,
      str(ping_row)[:80])
check("and that the refused command did not",
      deep_row["status"] == "failed" and deep_row["answered"] is False,
      str(deep_row)[:80])

# ---- 2. probing something ELSE is not a repeat ----------------------------
# The deeper checks are allowed to ping the next hop. Only the destination
# probes already run are off limits, and the dedupe keys on the command, so a
# different address is a different question.
from agent.graph import _call_key                                # noqa: E402

TOOL = "execute_query_on_server"
check("a probe of the next hop is not the probe of the destination",
      _call_key(TOOL, {"device_ip": SRC, "commands": [f"ping {DST} count 3"]})
      != _call_key(TOOL, {"device_ip": SRC,
                          "commands": ["ping 10.10.1.1 count 3"]}))
check("nor is the same destination from a different source interface",
      _call_key(TOOL, {"device_ip": SRC, "commands": [f"ping {DST} count 3"]})
      != _call_key(TOOL, {"device_ip": SRC,
                          "commands": [f"ping {DST} source Loopback0 count 3"]}))

from agent.prompts import DEEP_CHECK_PROMPT as LADDER            # noqa: E402

check("and the ladder says so, rather than banning probes outright",
      "Probing something ELSE is not a repeat" in LADDER,
      "a model told 'do not ping again' will not probe the next hop")
check("the ladder warns that a listing may truncate a name",
      "TRUNCATE" in LADDER, "a cut-off context name is not a searched context")


# ---- 3. a deeper-checks turn that gives up early gets one push ------------
import api.main as api                                           # noqa: E402
from starlette.testclient import TestClient                      # noqa: E402

C.MAX_TOOL_LOOPS = 16
C.DEEP_MAX_LOOPS = 14

BASIC = ScriptedModel(
    script=[("get_device_details", {"device_name": SRC}),
            ("get_device_details", {"device_name": DST}),
            ssh(SRC, f"ping {DST} repeat 3"),
            ssh(SRC, f"traceroute {DST} maxttl 5 timeout 1 probe 1 numeric"),
            ("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:22"})],
    thoughts=["", "", "", "", ""],
    final={"result": "INCONCLUSIVE", "cause": "the ping failed"})


class GivesUp(ScriptedModel):
    """Runs one check, then hands back a command instead of running it."""

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        pushed = any("stopped without isolating" in str(getattr(m, "content", ""))
                     for m in messages if getattr(m, "type", "") == "human")
        if not pushed:
            return super()._generate(messages, stop, run_manager, **kw)
        after = []
        for i, m in enumerate(messages):
            if ("stopped without isolating" in str(getattr(m, "content", ""))
                    and getattr(m, "type", "") == "human"):
                after = list(messages[i + 1:])
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        if any(getattr(m, "type", "") == "tool" for m in after):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content='{"result": "NOT REACHABLE", "cause": "the next hop '
                        'never answered", "next_step": "raise a ticket with '
                        'the transport team"}'))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="Probing the next hop, as asked.",
            tool_calls=[{"name": "execute_query_on_server",
                         "args": {"device_ip": SRC,
                                  "commands": ["ping 10.10.1.1 count 3"],
                                  "region": "INDIA"},
                         "id": "pushed_probe", "type": "tool_call"}]))])


DEEP = GivesUp(
    script=[ssh(SRC, f"show route {DST}")],
    thoughts=["Reading the forwarding table."],
    final={"result": "INCONCLUSIVE",
           "cause": "a routing or physical connectivity problem",
           "next_step": "Check the forwarding table for the next hop 10.10.1.1"})

wf2, notices = {}, []
with TestClient(api.app) as client:
    with client.websocket_connect("/ws") as ws:
        G.llm = BASIC
        ws.send_json({"type": "chat",
                      "text": f"Troubleshoot {SRC} to {DST} on tcp:22"})
        for _ in range(300):
            m = ws.receive_json()
            if m.get("type") == "approval_request":
                ws.send_json({"type": "approval", "id": m.get("id"),
                              "approved": True})
            elif m.get("type") == "final":
                break

        G.llm = DEEP
        ws.send_json({"type": "deep_check"})
        for _ in range(300):
            m = ws.receive_json()
            if m.get("type") == "approval_request":
                ws.send_json({"type": "approval", "id": m.get("id"),
                              "approved": True})
            elif m.get("type") == "workflow":
                wf2 = m.get("wf") or {}
            elif m.get("type") == "status" and m.get("state") == "degraded":
                notices.append(str(m.get("detail") or ""))
            elif m.get("type") == "final":
                break

checks = wf2.get("checks") or []
print()
for row in checks:
    print(f"  {str(row.get('cmd'))[:44]:46s} {row.get('status')}")
print(f"  notices: {notices}")

check("giving up early with budget left is not the end of it",
      any("stopped without isolating" in n for n in notices), str(notices)[:110])
check("the push produces another real check",
      any("ping 10.10.1.1" in str(r.get("cmd")) for r in checks),
      str([r.get("cmd") for r in checks])[:110])
check("and the run ends on a verdict instead of a shrug",
      str((wf2.get("deepReport") or {}).get("result")) == "NOT REACHABLE",
      str(wf2.get("deepReport"))[:80])
check("with a next step only a human can take",
      "ticket" in str((wf2.get("deepReport") or {}).get("next_step", "")).lower(),
      str((wf2.get("deepReport") or {}).get("next_step"))[:70])

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
