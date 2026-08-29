"""The traceroute is not optional, and a grey stage is not an answer.

A bridge run went CMDB, ping (failed), Tufin, alerts, conclusion -- and never
traced. The stage sat grey beside a green Conclusion, which reads as "there
was nothing to trace" when what happened is that nobody looked. A failed ping
is the reason to trace, not a reason to skip it.

Alerts already had a backstop. This is the same idea for the path, except that
the command belongs to the platform, so it goes back through the model rather
than being guessed in the server -- and lands on the same approval card as any
other device command.

    .venv/Scripts/python.exe tests/test_trace_backstop.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"

from langchain_core.messages import AIMessage                   # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult   # noqa: E402

from agent import graph as G                                    # noqa: E402
from scripted_model import ScriptedModel, ssh                   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


SRC, DST = "10.10.1.20", "172.20.5.10"
TRACE = f"traceroute {DST} maxttl 5 timeout 1 probe 1 numeric"
REPORT = ('{"result": "NOT REACHABLE", "cause": "the trace dies after the '
          'first hop", "source": "%s", "destination": "%s"}' % (SRC, DST))


class SkipsTheTrace(ScriptedModel):
    """Pings, asks Tufin, concludes -- and answers the nudge when it comes."""

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        after = []
        for i, m in enumerate(messages):
            if (getattr(m, "type", "") == "human"
                    and "not run the traceroute" in str(getattr(m, "content", ""))):
                after = list(messages[i + 1:])
        if not after and not any(
                "not run the traceroute" in str(getattr(m, "content", ""))
                for m in messages if getattr(m, "type", "") == "human"):
            return super()._generate(messages, stop, run_manager, **kw)

        # the nudge turn: trace once, then report
        traced = any(getattr(m, "type", "") == "tool" for m in after)
        if traced:
            return ChatResult(generations=[
                ChatGeneration(message=AIMessage(content=REPORT))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="Tracing the path, as asked.",
            tool_calls=[{"name": "execute_query_on_server",
                         "args": {"device_ip": SRC, "commands": [TRACE],
                                  "region": "INDIA"},
                         "id": "nudged_trace", "type": "tool_call"}]))])


G.llm = SkipsTheTrace(
    script=[("get_device_details", {"device_name": SRC}),
            ("get_device_details", {"device_name": DST}),
            ssh(SRC, f"ping {DST} repeat 3"),
            ("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:22"})],
    thoughts=["Source first.", "Then the destination.",
              "Pinging from the source.",
              "Ping failed; asking Tufin whether policy permits it."],
    final=REPORT)

import api.main as api                                          # noqa: E402
from starlette.testclient import TestClient                     # noqa: E402

wf, notices, approved = {}, [], []
with TestClient(api.app) as client:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat",
                      "text": f"Troubleshoot {SRC} to {DST} on tcp:22"})
        for _ in range(400):
            m = ws.receive_json()
            if m.get("type") == "approval_request":
                approved.append(str((m.get("payload") or {}).get("command", "")))
                ws.send_json({"type": "approval", "id": m.get("id"),
                              "approved": True})
            elif m.get("type") == "workflow":
                wf = m.get("wf") or {}
            elif m.get("type") == "status" and m.get("state") == "degraded":
                notices.append(str(m.get("detail") or ""))
            elif m.get("type") == "final":
                break

basics = wf.get("basics") or []
stages = {s["key"]: s for s in (wf.get("steps") or [])}
print()
for row in basics:
    print(f"  {str(row.get('cmd'))[:44]:46s} kind={row.get('kind')}")
print(f"  trace stage: {stages.get('trace')}")
print(f"  approvals:   {approved}")

check("the traceroute the model skipped is run",
      any(r.get("kind") == "trace" for r in basics),
      str([r.get("kind") for r in basics]))
check("the stage is no longer grey",
      (stages.get("trace") or {}).get("status") in ("done", "failed"),
      str(stages.get("trace")))
check("the operator was told the step had been skipped",
      any("traceroute" in n for n in notices), str(notices)[:120])
check("the command still went through the approval card",
      any("traceroute" in c for c in approved), str(approved))
check("the row says whose doing it was",
      any("workflow" in str(r.get("thought", "")).lower()
          or r.get("saidIt") is not None
          for r in basics if r.get("kind") == "trace"), "")
check("the path has hops to draw now",
      bool((wf.get("path") or {}).get("nodes"))
      or bool(((wf.get("paths") or {}).get("traceroute") or {}).get("nodes")),
      str(wf.get("path"))[:90])
check("and the report survives the extra turn",
      str((wf.get("report") or {}).get("result") or "") == "NOT REACHABLE",
      str(wf.get("report"))[:80])
check("nothing lands under DEEPER CHECKS before anyone asked for them",
      not (wf.get("checks") or []),
      str([c.get("cmd") for c in (wf.get("checks") or [])])[:90])


# ---- a rejected attempt has not used up the stage -------------------------
# The device refused the first traceroute -- wrong option for that platform --
# and the stage counted it anyway. The retry, which is the same step in a
# different dialect, was filed under DEEPER CHECKS: a different question, in a
# different part of the panel, before the operator had asked for any.
from api.workflow import Workflow                                # noqa: E402

wf2 = Workflow()
wf2.reset({"source": SRC, "dest": DST})

FIRST = f"traceroute {DST} ttl 1 5 timeout 1 probe 1 numeric"
RETRY = f"traceroute {DST} numeric"
LATER = f"traceroute {DST} source Loopback0 numeric"

check("the first attempt is the traceroute step",
      wf2.classify(FIRST) == "trace", wf2.classify(FIRST))

wf2.release_basic("trace")               # the device rejected it: no answer
check("so is the next syntax it tries",
      wf2.classify(RETRY) == "trace", wf2.classify(RETRY))
check("and the first attempt is still remembered as what it was",
      wf2.classify(FIRST) == "trace", wf2.classify(FIRST))

# that one ANSWERED, so the stage is settled and anything further is digging
check("once the stage has an answer, more tracing is escalation",
      wf2.classify(LATER) == "deep", wf2.classify(LATER))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
