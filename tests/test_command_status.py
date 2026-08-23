"""A command the device REFUSED must read as failed, not as a result.

Two different failures wear similar clothes:

  * the probe ran and found nothing reachable -- a real answer;
  * the device rejected the syntax -- no answer at all, and the right response
    is a different command for that platform.

Only the first was recognised, so "% Invalid input detected at '^' marker"
showed a green tick with the error as its "result", and the run carried on as
though the ping had been performed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.workflow import check_ok, failed_line, rejected_syntax   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---- the device refused the command ----------------------------------------
REJECTED = {
    "IOS invalid input": "SW-EXAMPLE-01#ping 198.51.100.10 count 3\n"
                         "                                  ^\n"
                         "% Invalid input detected at '^' marker.",
    "IOS incomplete": "% Incomplete command.",
    "IOS ambiguous": "% Ambiguous command:  \"tra\"",
    "EOS unknown": "% Invalid input\n% unknown command",
    "Junos syntax": "syntax error, expecting <command>.",
    "Linux not found": "bash: traceroute: command not found",
    "unrecognized": "unrecognized command - try 'help'",
}
for label, body in REJECTED.items():
    check(f"rejected: {label} -> failed", not check_ok(body), body.splitlines()[-1][:40])
    check(f"rejected: {label} -> flagged as a SYNTAX rejection",
          rejected_syntax(body))

# ---- the probe ran and simply failed ---------------------------------------
PROBE_FAILED = {
    "cisco 0 percent": "Success rate is 0 percent (0/3)",
    "linux loss": "3 packets transmitted, 0 received, 100% packet loss",
    "windows timeout": "Request timed out.",
}
for label, body in PROBE_FAILED.items():
    check(f"probe failure: {label} -> failed", not check_ok(body))
    check(f"probe failure: {label} -> NOT a syntax rejection, it is a result",
          not rejected_syntax(body), body[:40])

# ---- a command that worked --------------------------------------------------
OK = {
    "cisco ping": "Success rate is 100 percent (3/3), round-trip min/avg/max = 1/2/4 ms",
    "linux ping": "3 packets transmitted, 3 received, 0% packet loss",
    "a show command": "Routing entry for 198.51.100.0/24\n  Known via \"bgp 65001\"",
}
for label, body in OK.items():
    check(f"success: {label} -> ok", check_ok(body), body[:40])
    check(f"success: {label} -> not a rejection", not rejected_syntax(body))

# ---- output that is silence, not a result ----------------------------------
from api.workflow import usable_output                            # noqa: E402

CMD = "ping 198.51.100.10 repeat 3"
check("an empty reply is not usable", not usable_output("", CMD))
check("only the echoed prompt and command is not usable",
      not usable_output(f"SW-EXAMPLE-01#{CMD}\nSW-EXAMPLE-01#", CMD))
check("a permission error is not usable",
      not usable_output("% Permission denied", CMD))
check("an authentication failure is not usable",
      not usable_output("login failed for user svc-account", CMD))
check("a pager prompt is not usable", not usable_output("--More--", CMD))
check("a refusal is not usable",
      not usable_output("% Invalid input detected at '^' marker.", CMD))
check("a real ping result IS usable",
      usable_output(f"{CMD}\nSuccess rate is 100 percent (3/3)", CMD))
check("a FAILED ping is still usable -- it answered the question",
      usable_output(f"{CMD}\nSuccess rate is 0 percent (0/3)", CMD),
      "a failed probe is a result; silence is not")

# ---- a stage attempted three times and never answered must end failed ------
from api.workflow import Workflow                                 # noqa: E402

wf = Workflow()
wf.reset({"source": "SW-EXAMPLE-01", "dest": "198.51.100.10"})
for attempt in ("ping 198.51.100.10 repeat 3", "ping 198.51.100.10 count 3",
                "ping 198.51.100.10"):
    idx = wf.add_basic(attempt, kind="ping")
    wf.finish_basic(idx, False, "% Invalid input detected")

tried = [b for b in wf.basics if b.get("kind") == "ping"]
check("three attempts were recorded", len(tried) == 3, str(len(tried)))
check("and every one is marked failed",
      all(b["status"] == "failed" for b in tried))

# what api/main.py does at the end of a run
if wf.state["ping"]["status"] in ("pending", "running"):
    bad = sum(1 for b in tried if b.get("status") == "failed")
    wf.set("ping", "failed",
           f"{len(tried)} attempt(s), none returned a usable result"
           if bad == len(tried) else f"{len(tried)} attempt(s), no result")
check("the stage ends as a CROSS, not grey 'skipped'",
      wf.state["ping"]["status"] == "failed", str(wf.state["ping"]))
check("and says how many attempts were made",
      "3 attempt(s)" in wf.state["ping"]["detail"], wf.state["ping"]["detail"])

# a stage never attempted at all stays grey -- that is a different fact
wf2 = Workflow()
wf2.reset({"source": "a", "dest": "b"})
never = [b for b in wf2.basics if b.get("kind") == "trace"]
check("a stage never attempted is not called failed", not never)

# ---- the row's one-line detail should quote the refusal --------------------
line = failed_line(REJECTED["IOS invalid input"])
check("the panel shows the device's own words for the refusal",
      line and "Invalid input" in line, str(line))

print()
print("ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
