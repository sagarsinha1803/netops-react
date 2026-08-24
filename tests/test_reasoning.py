"""Every row in the activity feed has to explain itself.

Most models answer a tool call with tool_calls and NOTHING else: content is
empty, because everything they had to say is in the arguments. "Show
reasoning" then revealed nothing at all, and a run that was working perfectly
looked like it was hiding something.

The panel now describes the step itself when the model says nothing -- and
marks those lines, because the feed is evidence and must not put words in the
model's mouth.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.chdir(ROOT)
os.environ["USE_MOCKS"] = "1"

from langchain_core.messages import AIMessage                     # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult     # noqa: E402

from agent import graph as G                                      # noqa: E402
from agent.utils import describe_call                             # noqa: E402
from scripted_model import ScriptedModel, ssh                     # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- the descriptions themselves -----------------------------------------
check("a CMDB lookup names the device",
      "10.1.1.1" in describe_call("get_device_details", {"device_name": "10.1.1.1"}))
check("a policy check names both ends and the service",
      all(x in describe_call("get_firewall_path",
                             {"src": "10.1.1.1", "dst": "10.2.2.2",
                              "service": "tcp:443"})
          for x in ("10.1.1.1", "10.2.2.2", "tcp:443")))
check("an alert lookup names the device",
      "SW-1" in describe_call("get_alert_and_ticket_details_from_archangel",
                              {"device_name": "SW-1"}))
check("a device command quotes the command",
      "show ip route" in describe_call("execute_query_on_server",
                                       {"device_ip": "10.1.1.1",
                                        "commands": ["show ip route 10.2.2.2"]}))
check("a probe from this host says so, since that is a different question",
      "agent host" in describe_call("local_ping", {"dest": "10.2.2.2"}))


# ---- and end to end, with a model that says nothing ----------------------
SRC, DST = "10.10.1.20", "172.20.5.10"


class Silent(ScriptedModel):
    """Emits tool calls with no content, the way most models do."""

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        real = super()._generate(messages, stop, run_manager, **kw)
        m = real.generations[0].message
        if getattr(m, "tool_calls", None):
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content="", tool_calls=m.tool_calls))])
        return real


G.llm = Silent(
    script=[("get_device_details", {"device_name": SRC}),
            ("get_device_details", {"device_name": DST}),
            ssh(SRC, f"ping {DST} repeat 3"),
            ("get_firewall_path", {"src": SRC, "dst": DST, "service": "tcp:443"})],
    # no alert call: the server runs that itself, and those rows have to say
    # whose doing they were
    thoughts=["", "", "", ""],
    final={"result": "NOT REACHABLE", "cause": "DENY-ALL"})

import api.main as api                                             # noqa: E402
from starlette.testclient import TestClient                        # noqa: E402

wf = {}
with TestClient(api.app) as client:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat",
                      "text": f"Troubleshoot {SRC} to {DST} on tcp:443"})
        for _ in range(300):
            msg = ws.receive_json()
            if msg.get("type") == "approval_request":
                ws.send_json({"type": "approval", "id": msg.get("id"),
                              "approved": True})
            elif msg.get("type") == "workflow":
                wf = msg.get("wf") or {}
            elif msg.get("type") == "final":
                break

rows = wf.get("basics") or []
print()
for r in rows:
    print(f"  {str(r.get('cmd'))[:42]:44s} said={r.get('saidIt')!s:5s} "
          f"{str(r.get('thought'))[:52]}")

check("every row explains itself", rows and all(r.get("thought") for r in rows),
      str([r.get("cmd") for r in rows if not r.get("thought")]))
check("and none of them claims the model said it",
      all(r.get("saidIt") is False for r in rows),
      str([r.get("cmd") for r in rows if r.get("saidIt")]))
check("the alert rows the SERVER ran say that they were the server's doing",
      any("not by the model" in str(r.get("thought"))
          for r in rows if r.get("kind") == "alerts"),
      str([r.get("thought") for r in rows if r.get("kind") == "alerts"])[:80])

# and when the model DOES speak, its words are kept and marked as its own
G.llm = ScriptedModel(
    script=[("get_device_details", {"device_name": SRC})],
    thoughts=["Looking up the source, to get its platform and region."],
    final={"result": "INCONCLUSIVE"})

wf2 = {}
with TestClient(api.app) as client:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat",
                      "text": f"Troubleshoot {SRC} to {DST} on tcp:443"})
        for _ in range(300):
            msg = ws.receive_json()
            if msg.get("type") == "approval_request":
                ws.send_json({"type": "approval", "id": msg.get("id"),
                              "approved": True})
            elif msg.get("type") == "workflow":
                wf2 = msg.get("wf") or {}
            elif msg.get("type") == "final":
                break

spoke = [r for r in (wf2.get("basics") or []) if r.get("kind") == "cmdb"]
check("the model's own words survive", spoke and "platform and region"
      in str(spoke[0].get("thought")), str(spoke[:1])[:80])
check("and are marked as the model's, not ours",
      spoke and spoke[0].get("saidIt") is True, str(spoke[:1])[:80])

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
