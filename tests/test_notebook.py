"""The agent writes down what worked, and reads it back next time.

A real run on an NX-OS box spent five of its steps permuting the same wrong
shape -- the context keyword in the wrong place -- while the device refused
each one. The working form was already on screen at step one; nothing carried
it forward, and nothing would have carried it to tomorrow's run either.

So the agent keeps a notebook: when a command RUNS and answers, its shape is
written down against the platform it ran on; when a platform refuses one, that
is written down too. It is not the model learning -- the model is identical
tomorrow. The knowledge lives outside it, which is why it survives changing
the model.

    .venv/Scripts/python.exe tests/test_notebook.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.notebook import (Notebook, platform_of,          # noqa: E402
                            question_of, shape)

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- 1. what a command was FOR -------------------------------------------
for command, want in [
    ("show ip route 198.51.100.28 vrf management", "the route for an address"),
    ("show ip route vrf management 198.51.100.28", "the route for an address"),
    ("show vrf", "the routing contexts"),
    ("show vrf all", "the routing contexts"),
    ("show cef 198.51.100.28", "the forwarding entry"),
    ("show arp 192.0.2.9", "address resolution for a next hop"),
    ("show interfaces Bundle-Ether1", "interface state"),
    ("show bgp ipv4 unicast 198.51.100.28", "the control plane for a prefix"),
    ("ping 198.51.100.28 count 3", "a probe to an address"),
    ("show version | include Cisco", "the platform and version"),
]:
    check(f"{command!r} asks about {want}", question_of(command) == want,
          question_of(command))

check("a route lookup that names a context is still a route lookup",
      question_of("show ip route 198.51.100.28 vrf management")
      != question_of("show vrf"),
      "otherwise both entries poison each other")


# ---- 2. the shape, never the command --------------------------------------
for command, want in [
    ("show ip route 198.51.100.28 vrf management", "show ip route <addr> vrf <name>"),
    ("show ip route vrf management 198.51.100.28", "show ip route vrf <name> <addr>"),
    ("show interfaces Bundle-Ether7", "show interfaces <intf>"),
    ("show access-list CORP-DENY-IN", "show access-list <name>"),
    ("show arp 0050.56be.1a2b", "show arp <mac>"),
    ("ping 198.51.100.28 source Loopback0 count 3",
     "ping <addr> source <intf> count 3"),
]:
    check(f"{command!r} keeps its shape", shape(command) == want, shape(command))

check("two runs against different addresses are ONE piece of knowledge",
      shape("show ip route 10.1.1.1 vrf a") == shape("show ip route 10.9.9.9 vrf b"))
check("and nothing identifying survives into the file",
      "198.51" not in shape("show ip route 198.51.100.28 vrf management")
      and "CORP" not in shape("show access-list CORP-DENY-IN"))


# ---- 3. the platform key --------------------------------------------------
NXOS = ("Cisco Nexus Operating System (NX-OS) Software\n"
        "  system:    version 10.2(5)\n")
XR = "Cisco IOS XR Software, Version 7.11.2\n"
RECORD = json.dumps({"data": {"brand": "Cisco", "brandModel": "ASR 9910",
                              "operatingSystem": "IOS XR",
                              "osVersion": "7.11.2"}})

check("the box's own answer names the platform",
      platform_of(version_output=NXOS).startswith("cisco-nxos"),
      platform_of(version_output=NXOS))
check("a CMDB record will do when there is nothing better",
      platform_of(record=RECORD).startswith("cisco-iosxr"),
      platform_of(record=RECORD))
check("and the box OUTRANKS the record when they disagree",
      platform_of(record=RECORD, version_output=NXOS).startswith("cisco-nxos"),
      platform_of(record=RECORD, version_output=NXOS))
check("a vendor nobody recognises is not guessed at",
      platform_of(record=json.dumps({"data": {"brand": "Acme"}})) == "",
      "an entry filed under the wrong platform is worse than no entry")
check("the key is a family and a major version, not an exact build",
      platform_of(version_output=XR) == "cisco-iosxr 7.11",
      platform_of(version_output=XR))


# ---- 4. writing it down, and reading it back ------------------------------
with tempfile.TemporaryDirectory() as tmp:
    book = Notebook(os.path.join(tmp, "notes.json"))
    PLAT = "cisco-nxos 10.2"

    # the run from the screenshot: the wrong shape refused, the right one works
    book.record(PLAT, "show ip route vrf management 198.51.100.28", worked=False)
    book.record(PLAT, "show ip route 198.51.100.28 vrf management", worked=True)

    hints = book.hints(PLAT)
    print()
    print(hints)
    check("what worked comes back", "show ip route <addr> vrf <name>" in hints,
          hints[:80])
    check("and what the platform refused is named as refused",
          "show ip route vrf <name> <addr>" in hints.split("Refused")[-1], hints)
    check("a platform nobody has run against says nothing",
          book.hints("juniper-junos 21.4") == "")

    # it survives the process: that is the entire point
    again = Notebook(os.path.join(tmp, "notes.json"))
    check("the notebook is still there next time",
          "show ip route <addr> vrf <name>" in again.hints(PLAT))

    # an OS upgrade changes a syntax: the newest verdict wins
    again.record(PLAT, "show ip route 198.51.100.28 vrf management", worked=False)
    after = again.hints(PLAT)
    check("a shape that starts failing is let go of",
          "show ip route <addr> vrf <name>" not in after.split("Refused")[0],
          after)
    check("and moves to the refused list",
          "show ip route <addr> vrf <name>" in after.split("Refused")[-1], after)

    # nothing is written when there is nothing to key on
    fresh = Notebook(os.path.join(tmp, "empty.json"))
    check("no platform, no entry",
          not fresh.record("", "show ip route 10.1.1.1", worked=True))
    check("no recognisable question, no entry",
          not fresh.record(PLAT, "show something-odd", worked=True))
    check("so the file is never written for nothing",
          not os.path.exists(os.path.join(tmp, "empty.json")))

    # a file somebody corrupted must not take the run down with it
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    check("a corrupt notebook is ignored, not fatal", Notebook(bad).hints(PLAT) == "")


# ---- 5. it is not a way around the guards ---------------------------------
from agent.guards import check_command                            # noqa: E402

check("a remembered command is still checked by the allowlist",
      bool(check_command("configure terminal")),
      "the notebook changes what is SUGGESTED, never what may run")
check("and the version command it needs is allowed",
      check_command("show version") is None, str(check_command("show version")))
check("while a real table dump is still refused",
      bool(check_command("show running-config")))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
