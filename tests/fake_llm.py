"""Scripted OpenAI-compatible endpoint, for driving the UI without the clipboard.

Replies with a fixed tool-call sequence, chosen by how many tool results the
request already carries: CMDB source -> CMDB destination -> ping -> traceroute
-> two deep checks -> final answer.

    python tests/fake_llm.py 11499
"""
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

FINAL = {
    "source": "APP-SRV-DC1-020 / 10.10.1.20 (cisco IOS-XE)",
    "destination": "PAY-API-DC2-010 / 172.20.5.10 (cisco NX-OS)",
    "ping": "FAILED",
    "path": ["APP-SRV-DC1-020", "Leaf-101", "Border-Router-01",
             "FW-DC1-EDGE-01", "X"],
    "result": "NOT REACHABLE",
    "evidence": [
        "ping 172.20.5.10 repeat 3 -> success rate is 0 percent (0/3)",
        "traceroute stops after FW-DC1-EDGE-01 (10.10.255.1)",
        "Tufin get_firewall_path tcp:443 -> BLOCKED by ACL DENY-ALL",
    ],
    "cause": "Denied by policy: ACL DENY-ALL on the DC1 edge drops tcp:443 to "
             "172.20.5.10.",
    "next_step": "Raise a Tufin change request to permit tcp:443 from "
                 "APP-DC1 to PAYMENT-DC2, or amend DENY-ALL.",
}

# Mirrors the real agent: the ping fails, so it escalates IN THE SAME TURN --
# next-hop reachability, then a trace of the underlay. Those two must land in
# the Deep tab and must not overwrite the basic ping result or the path.
def _ssh(cmd):
    return ("execute_query_on_server",
            {"device_ip": "10.10.1.20", "region": "INDIA", "commands": [cmd]})


SCRIPT = [
    ("get_device_details", {"device_name": "10.10.1.20"}),
    ("get_device_details", {"device_name": "172.20.5.10"}),
    _ssh("ping 172.20.5.10 repeat 3"),
    _ssh("traceroute 172.20.5.10 maxttl 5 timeout 1 probe 1 numeric"),
    ("get_firewall_path", {"src": "10.10.1.20", "dst": "172.20.5.10",
                           "service": "tcp:443"}),
    ("get_alert_and_ticket_details_from_archangel",
     {"device_name": "APP-SRV-DC1-020"}),
    ("get_alert_and_ticket_details_from_archangel",
     {"device_name": "PAY-API-DC2-010"}),
    _ssh("show route 172.20.5.10"),
    _ssh("show access-lists EDGE-OUT | include 172.20.5.10"),
]

# the manager scenario: two addresses the CMDB does not know. The script
# looks both up, gets nothing back, and goes straight to Tufin -- no device
# command, because there is no device to SSH to, and no probe from elsewhere.
LOCAL_SCRIPT = [
    ("get_device_details", {"device_name": "198.51.100.5"}),
    ("get_device_details", {"device_name": "8.8.8.8"}),
    ("get_firewall_path", {"src": "198.51.100.5", "dst": "8.8.8.8",
                           "service": "any"}),
]

# no alert lookup here on purpose: with no CMDB record there is no device name
# for Archangel to be keyed by
LOCAL_THOUGHTS = [
    "Looking up the source in the CMDB.",
    "Source is not in the CMDB. Checking the destination too.",
    "No record for the source, so there is no management address or region to "
    "SSH to -- ping and traceroute only mean anything run ON the source, so "
    "they are skipped. Tufin needs only the two addresses, so the policy "
    "check is still available.",
]

LOCAL_FINAL = {
    "source": "198.51.100.5 (not in CMDB)",
    "destination": "8.8.8.8 (not in CMDB)",
    "ping": "NOT RUN",
    "path": ["198.51.100.5", "?", "8.8.8.8"],
    "result": "NOT REACHABLE",
    "evidence": [
        "CMDB lookup for 198.51.100.5 -> no record in any region",
        "CMDB lookup for 8.8.8.8 -> no record in any region",
        "no device to SSH to, so no ping and no traceroute were run",
        "Tufin get_firewall_path any -> BLOCKED by ACL DENY-ALL",
    ],
    "cause": "Denied by policy: ACL DENY-ALL blocks the pair. Nothing was "
             "tested from the source itself, since it is not in the CMDB.",
    "next_step": "Add the source device to the CMDB so the path can be tested "
                 "from it, and raise a change request for the ACL.",
}

# a second turn ("run deeper checks") continues from step 4
DEEP = [
    _ssh("show route 172.20.5.10"),
    _ssh("show vrf all"),
    _ssh("show cef 172.20.5.10"),
    _ssh("show arp | include 10.10.1.1"),
    _ssh("show interface TenGigE0/0/0/1 brief"),
    _ssh("show access-lists EDGE-OUT | include 172.20.5.10"),
]

DEEP_THOUGHTS = [
    "Is there a route at all? No route settles it locally.",
    "Route exists. Checking whether the destination sits in a VRF.",
    "Global table. Is the prefix actually programmed for forwarding?",
    "Forwarding is fine. Is the next hop resolving in ARP?",
    "Next hop is up. Checking the egress interface for errors or drops.",
    "Everything local is healthy, so something filters it. Checking the edge ACL.",
]

THOUGHTS = [
    "Looking up the source device in the CMDB.",
    "Now the destination, so I know its platform and region.",
    "Source is Cisco IOS-XE, so: ping <dest> repeat 3.",
    "Ping failed. Running a bounded traceroute to find where it dies.",
    "Trace stops at the edge firewall. Asking Tufin whether policy permits "
    "tcp:443 end to end.",
    "Tufin says blocked by DENY-ALL. Checking Archangel for open alerts on the "
    "source before I conclude.",
    "And the destination's open alerts.",
    "Confirming the route exists so I can rule out a routing fault.",
    "Route is valid, so the drop is policy. Reading the ACL for the detail.",
]


_REQUEST_RE = re.compile(r"troubleshoot\s+([\w.\-]+)\s+to\s+([\w.\-]+)", re.I)


def _retarget(step, src, dst):
    """Point a scripted step at the pair actually being asked about."""
    name, args = step
    out = json.loads(json.dumps(args)
                     .replace("10.10.1.20", src)
                     .replace("172.20.5.10", dst)
                     .replace("198.51.100.5", src)
                     .replace("8.8.8.8", dst))
    return name, out


def _answer_from_context(asked: str, msgs) -> dict:
    """A plain answer built from what the run already established.

    The real model has the whole conversation and answers from it; this
    imitates that closely enough to exercise the chat path: it looks at the
    TOOL RESULTS in the thread -- not the whole request, whose system prompt
    mentions the CMDB and the fallback and so matches every rule below.
    """
    seen = json.dumps([m.get("content") for m in msgs if m.get("role") == "tool"])
    q = asked.lower()
    if "DENY-ALL" in seen and ("block" in q or "why" in q or "cause" in q):
        answer = ("Tufin returned BLOCKED for tcp:443: rule **DENY-ALL** on the "
                  "DC1 edge drops it. The route and the path are fine up to "
                  "FW-DC1-EDGE-01 - the traffic is denied by policy, not lost.")
    elif "not in CMDB" in seen or "No data found" in seen:
        answer = ("Neither address is in the CMDB, so there was no device to "
                  "SSH to and no ping or traceroute was run. Only the firewall "
                  "policy could be checked, and that is what the verdict rests "
                  "on.")
    elif "next" in q or "check" in q:
        answer = ("Next: confirm the ACL hit counter is rising "
                  "(show access-lists EDGE-OUT | include 172.20.5.10), then "
                  "raise the Tufin change request.")
    elif "hop" in q or "path" in q:
        answer = ("The path is APP-SRV-DC1-020 -> Leaf-101 -> Border-Router-01 "
                  "-> FW-DC1-EDGE-01, where it stops. The firewall is the last "
                  "device that answered.")
    elif not seen.strip("[]"):
        answer = ("Nothing has been run in this conversation yet, so I have no "
                  "context to answer from. Fill in a source and a destination "
                  "and press Run, then ask me about the result.")
    else:
        answer = ("Ask about the run: the cause, the path, the ACL, or what to "
                  "check next.")
    return {"thought": "Answering from the conversation, no tools needed.",
            "final": answer}


def _msg(content=None, tool=None, args=None):
    m = {"role": "assistant", "content": content}
    if tool:
        m["tool_calls"] = [{
            "id": f"call_{int(time.time() * 1000) % 100000}",
            "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args)},
        }]
    return m


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        print("[fake-llm]", a[0] % a[1:], flush=True)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
        # NETOPS_SPY=<path> records every request, so a test can assert that
        # nothing real ever reached the endpoint
        spy = os.environ.get("NETOPS_SPY")
        if spy:
            with open(spy, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(body) + "\n")
        msgs = body.get("messages") or []
        # count only this turn's tool results, so a second question in the same
        # conversation replays the script from the start
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"),
                        default=0)
        turn = msgs[last_user:]
        done = sum(1 for m in turn if m.get("role") == "tool")
        deep = "run either of them again" in json.dumps(turn)
        # The CMDB-miss scenario is recognised the way the real model would:
        # from the CMDB's own reply. Keying on the literal address stopped
        # working once masking reached API models too -- the endpoint sees a
        # stand-in, never 198.51.100.5.
        tool_results = json.dumps([m.get("content") for m in turn
                                   if m.get("role") == "tool"])
        local = "No data found" in tool_results
        # A chat question -- route D in the prompt: answer from the conversation
        # so far, no tools. Recognised the way the real model would: the request
        # names no source AND destination.
        asked = next((str(m.get("content") or "") for m in reversed(turn)
                      if m.get("role") == "user"), "")
        question = (not deep and not _REQUEST_RE.search(asked)
                    and asked.strip().endswith("?"))

        # Whatever pair the caller asked about. The agent unmasks arguments on
        # the way to the tools, so echoing back the stand-ins it sent is
        # correct -- and it means this endpoint drives ANY addresses, not the
        # two that happen to be written into the script.
        pair = _REQUEST_RE.search(asked or json.dumps(turn))
        src, dst = (pair.group(1), pair.group(2)) if pair else ("SOURCE", "DEST")

        if question:
            msg = _msg(json.dumps(_answer_from_context(asked, msgs)))
        elif local and not deep:
            if done < len(LOCAL_SCRIPT):
                name, args = _retarget(LOCAL_SCRIPT[done], src, dst)
                msg = _msg(LOCAL_THOUGHTS[done], name, args)
            else:
                msg = _msg(json.dumps(_retarget(("final", LOCAL_FINAL),
                                                src, dst)[1]))
        elif deep:
            step = done
            if 0 <= step < len(DEEP):
                name, args = DEEP[step]
                msg = _msg(DEEP_THOUGHTS[step], name, args)
            else:
                deep_final = dict(FINAL)
                deep_final.update({
                    "result": "INCONCLUSIVE",
                    "evidence": [
                        "show route 172.20.5.10 -> valid BGP route via 10.10.1.1",
                        "show vrf all -> global table, no VRF involved",
                        "show cef 172.20.5.10 -> prefix programmed, adjacency 10.10.1.1",
                        "show arp -> next hop 10.10.1.1 resolves",
                        "show interface TenGigE0/0/0/1 brief -> up/up, no drops",
                        "show access-lists EDGE-OUT -> deny tcp any host "
                        "172.20.5.10 (1842 matches)",
                    ],
                    "cause": "Routing and forwarding are healthy. An ACL on the "
                             "DC1 edge is dropping the traffic: EDGE-OUT line 40.",
                    "next_step": "Amend or except EDGE-OUT line 40 on "
                                 "FW-DC1-EDGE-01 for the PAYMENT-DC2 zone.",
                })
                msg = _msg(json.dumps(deep_final))
        elif done < len(SCRIPT):
            name, args = _retarget(SCRIPT[done], src, dst)
            msg = _msg(THOUGHTS[done], name, args)
        else:
            msg = _msg(json.dumps(FINAL))

        out = {
            "id": "chatcmpl-fake", "object": "chat.completion",
            "created": int(time.time()), "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    print(f"fake LLM on http://localhost:{port}/v1", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
