"""Scripted OpenAI-compatible endpoint, for driving the UI without the clipboard.

Replies with a fixed tool-call sequence, chosen by how many tool results the
request already carries: CMDB source -> CMDB destination -> ping -> traceroute
-> two deep checks -> final answer.

    python tests/fake_llm.py 11499
"""
import json
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
    _ssh("show route 172.20.5.10"),
    _ssh("show access-lists EDGE-OUT | include 172.20.5.10"),
]

# the manager scenario: two addresses the CMDB does not know. The script
# looks both up, gets nothing back, and switches to the local probes --
# no execute_query_on_server, no Tufin.
LOCAL_SCRIPT = [
    ("get_device_details", {"device_name": "198.51.100.5"}),
    ("get_device_details", {"device_name": "8.8.8.8"}),
    ("local_ping", {"dest": "8.8.8.8", "count": 3}),
    ("local_traceroute", {"dest": "8.8.8.8", "max_hops": 5}),
]

LOCAL_THOUGHTS = [
    "Looking up the source in the CMDB.",
    "Source is not in the CMDB. Checking the destination before deciding how "
    "to probe.",
    "Neither address is in the CMDB, so there is no device to SSH to and no "
    "topology for Tufin. Falling back to probing from the agent machine.",
    "Ping from the agent host succeeded. Tracing the path from here, bounded "
    "to 5 hops.",
]

LOCAL_FINAL = {
    "source": "198.51.100.5 (not in CMDB - probed from agent host)",
    "destination": "8.8.8.8 (not in CMDB)",
    "ping": "SUCCESS",
    "path": ["agent host", "192.168.1.1", "100.72.16.1", "8.8.8.8"],
    "result": "REACHABLE",
    "evidence": [
        "CMDB lookup for 198.51.100.5 -> no record in any region",
        "CMDB lookup for 8.8.8.8 -> no record in any region",
        "local ping 8.8.8.8 -> 3/3 replies (from the AGENT HOST)",
        "local tracert -> 3 hops, destination answered",
    ],
    "cause": "8.8.8.8 answers from the agent machine. The real source is not "
             "in the CMDB, so nothing was tested from it - policy between "
             "198.51.100.5 and 8.8.8.8 is unverified.",
    "next_step": "Add the source device to the CMDB (or name a device that "
                 "is) to test from the real source.",
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
    "Tufin says blocked by DENY-ALL. Confirming the route exists so I can rule "
    "out a routing fault.",
    "Route is valid, so the drop is policy. Reading the ACL for the detail.",
]


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
        msgs = body.get("messages") or []
        # count only this turn's tool results, so a second question in the same
        # conversation replays the script from the start
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"),
                        default=0)
        turn = msgs[last_user:]
        done = sum(1 for m in turn if m.get("role") == "tool")
        deep = "run either of them again" in json.dumps(turn)
        # the CMDB-miss scenario is keyed on its documentation-range source
        local = "198.51.100" in json.dumps(turn)

        if local and not deep:
            if done < len(LOCAL_SCRIPT):
                name, args = LOCAL_SCRIPT[done]
                msg = _msg(LOCAL_THOUGHTS[done], name, args)
            else:
                msg = _msg(json.dumps(LOCAL_FINAL))
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
            name, args = SCRIPT[done]
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
