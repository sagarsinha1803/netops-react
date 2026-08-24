# Demo script

Two runs, about fifteen minutes, on a laptop with no access to anything. Every
address, hostname, ticket and alert below is invented — RFC 1918 / RFC 5737
ranges, no site, customer or employee names — so the screen can be shared
anywhere.

Both scenarios accept the **device name or the IP address** in either box. The
CMDB mock indexes both, so `DC1-APP-SW-07` and `10.20.30.7` are the same
device to it.

---

## Starting it

Two terminals. The first is the model, the second the app.

**With the VS Code Copilot bridge** (the LM Bridge extension running, serving
on :11434):

```powershell
$env:LLM_MODE="api"; $env:LLM_BASE_URL="http://localhost:11434/v1"; $env:LLM_MODEL="gpt-4o-mini"; .\run.ps1 -Mock
```

**With the clipboard relay** (M365 Copilot in a browser, you paste by hand):

```powershell
$env:LLM_MODE="clipboard"; $env:CLIP_MODE="delta"; .\run.ps1 -Mock
```

`-Mock` is what makes this safe: the CMDB, the devices, Tufin and Archangel are
all local processes answering from `tests/mocks/scenarios.py`. Nothing leaves
the laptop, and there is nothing to leave.

Then open <http://localhost:8000>.

---

## Scenario 1 — the healthy path (≈4 min)

| field | value |
|---|---|
| Source | `DC1-EDGE-RTR-01` |
| Destination | `DC2-WEB-LB-01` |
| Service | TCP **443** |

Approve each device command when the card appears — that is the point of the
card, so do not rush past it.

**What the audience sees**

```
✓ CMDB lookup     2 device record(s) fetched
✓ Ping            reachable
✓ Traceroute      3 hops
✓ Policy          Traffic allowed
✓ Alerts          1 alert(s), 1 ticket(s)
✓ Conclusion      report ready
```

Path tab: `DC1-EDGE-RTR-01 → DC1-CORE-SW-01 → DC2-CORE-SW-01 → DC2-WEB-LB-01`,
arriving.

Alerts tab: one row — `DC2-WEB-LB-01 · PowerSupplyRedundancyLost · PSU 2 ·
ticket 570000114`.

**What to say**

- The agent decided the ping syntax itself from the CMDB record: brand Cisco,
  OS IOS XR. Nobody typed a command.
- It ran Tufin **even though the ping succeeded**. ICMP getting through says
  nothing about tcp/443; those are different questions and the workflow asks
  both.
- The one open alert is a power supply on the destination. The agent reports it
  as context and does **not** claim it caused anything, because nothing is
  broken here. An agent that blames the nearest red thing is worse than no
  agent — this is the behaviour to point at.
- No "Run deeper checks" button: the result is confirmed, so there is nothing
  to dig into.

---

## Scenario 2 — the broken path, and the reasoning (≈8 min)

| field | value |
|---|---|
| Source | `10.20.30.7`  *(type the IP this time — same lookup)* |
| Destination | `10.60.40.12` |
| Service | TCP **3306** |

**What the audience sees**

```
✓ CMDB lookup     2 device record(s) fetched
✕ Ping            failed
✓ Traceroute      dies after the first hop
✓ Policy          Traffic allowed        <-- the interesting part
✓ Alerts          3 alert(s), 2 ticket(s)
✓ Conclusion      report ready
```

Path tab: `DC1-APP-SW-07 → DC1-CORE-SW-01 (10.20.30.1) → X` — it stops, and the
destination is deliberately not drawn as if it were reached.

Alerts tab:

| Device | Alert | Check | Ticket |
|---|---|---|---|
| DC1-APP-SW-07 | LinkStatusOperDown | Interface TenGigE0/0/0/3 | 570000231 |
| DC1-APP-SW-07 | BGPNeighborDown | Neighbor 10.20.30.129 | 570000231 |
| DC1-APP-SW-07 | InterfaceErrorRateHigh | Interface TenGigE0/0/0/3 | 570000232 |

**What to say**

- **Tufin permits this traffic.** The easy answer is gone. A tool that stops at
  the first plausible explanation would have blamed the firewall and been
  wrong, and somebody would have spent an afternoon on a change request for a
  rule that was never the problem.
- Archangel already has an **open ticket** for `TenGigE0/0/0/3` on the source.
  The agent found that on its own, and it is about to matter.

Now press **Run deeper checks**.

### The escalation

The agent works down one check at a time, and each answer decides the next:

| # | Command | What comes back | What it rules out |
|---|---|---|---|
| 1 | `show route 10.60.40.12` | route present via BGP, next hop `10.20.30.129` out `TenGigE0/0/0/3` | not a missing route |
| 2 | `show vrf` | global table | not a VRF leak |
| 3 | `show cef 10.60.40.12` | **INCOMPLETE — drop adjacency**, 41,882 packets dropped | the route exists but nothing can be forwarded down it |
| 4 | `show arp \| include 10.20.30.129` | **Incomplete** — the next hop never answered ARP | the next hop is not there |
| 5 | `show interface TenGigE0/0/0/3` | **down/down (notconnect)**, last flap 02:14:33, Rx power −40 dBm LOW-ALARM | the interface is dead, and the optic says why |
| 6 | `show ip bgp summary` | neighbor `10.20.30.129` **Idle**, down 02:14:33 | the peer went with it |
| 7 | `show logging` | `%LINK-3-UPDOWN` then `%BGP-5-ADJCHANGE`, same timestamps | the order of events |

**The conclusion the agent reaches**

> The route to 10.60.40.0/24 is in the table but the forwarding entry is an
> incomplete adjacency: the next hop 10.20.30.129 never resolved in ARP,
> because TenGigE0/0/0/3 — the interface it would be reached through — has been
> down for 02:14:33 with an optical Rx alarm. Policy permits the traffic; this
> is a dead link, not a firewall. Archangel ticket **570000231** is already
> open against that interface.

**The line worth landing**

Three independent sources — the forwarding table, the interface counters, and
the alert database — agree on one interface, and the timestamps line up to the
second. That is the difference between "the ping failed" and something an
engineer can act on. The ticket number means the next step is a phone call, not
a fresh investigation.

---

## If someone asks

**"Does it change anything on my devices?"**
It cannot. Every command is checked against a read-only allowlist in code
before it is offered, and a human approves each one — you saw the cards. A
config command is rejected by the allowlist even if the model asks for it and
even if you approve it. `tests/test_guards.py` runs 35 destructive commands
against it and every one is refused.

**"What does the model see?"**
Addresses and hostnames are replaced with stand-ins before anything is sent,
and mapped back on the way in. That runs for both the clipboard relay and API
models. Show `MASK_STYLE=label` if they want to see it.

**"What if a system is down?"**
It runs with what it has and says which checks it could not do. Kill one MCP
server and re-run if they want to see it — the run continues.

**"Why does it ask permission for some things and not others?"**
Approval is for anything that touches a device. A CMDB lookup, a Tufin path
calculation and an alert query all read from systems, not devices, so they run
unattended.

**"How do I know the demo is not just a recording?"**
Type a different pair. `DC1-EDGE-RTR-01` → `10.60.40.12` works, and so does any
pair of devices in the inventory; nothing is scripted to one input. Or run
`.venv\Scripts\python.exe tests\test_demo_scenarios.py`, which drives both
scenarios through the same socket the browser uses.

---

## Rehearsing

```powershell
.venv\Scripts\python.exe tests\test_demo_scenarios.py
```

Runs both scenarios end to end and checks the panel says what this page
promises. Under a minute, no credentials. Run it before you present: the thing
that breaks on stage is never the part you rehearsed.

## Changing the world

Everything invented lives in `tests/mocks/scenarios.py` — the inventory, which
destinations answer a ping, the hops each traceroute reports, the firewall
verdicts, the alerts, and what each device says to each `show` command. One
file, so the story stays consistent: change a device's fate there and the CMDB,
the commands, the policy check and the alerts all follow.
