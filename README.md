# NetOps troubleshooting agent — React UI

The same agent as `netops`, with the Chainlit UI replaced by **React + FastAPI
over one WebSocket**, and one new capability: when the **source is not in the
CMDB**, the agent probes with ping/traceroute **from the machine it runs on**
instead of SSHing anywhere — and skips the Tufin lookup, since there is no
topology to ask about. The report says plainly that reachability was tested
from the agent host, not the real source.

The agent itself — the LangGraph graph, the guards, the three privacy layers,
the clipboard relay — is unchanged from `netops`.

## Layout

```
netops-react/
├── run.ps1                     start it            .\run.ps1 -Mock
├── requirements.txt            backend deps (no chainlit)
│
├── agent/                      the agent — same as netops, plus:
│   ├── prompts.py              LOCAL FALLBACK routing when the CMDB misses
│   └── vendors.py              parses Windows ping/tracert output too
│
├── mcp_tools/
│   ├── unicorn_mcp.py          get_device_details        (CMDB)
│   ├── tufin_mcp.py            get_firewall_path         (SecureTrack)
│   ├── troubleshoot_agent_mcp.py  execute_query_on_server (SSH via bastion)
│   └── local_probe_mcp.py      local_ping / local_traceroute  (NEW —
│                               subprocess on the agent host; args are a
│                               validated IP only, nothing to inject)
│
├── api/
│   ├── main.py                 FastAPI + the /ws WebSocket protocol
│   └── workflow.py             panel state, ported from the Chainlit UI
│
├── frontend/                   Vite + React (no UI framework)
│   └── src/App.jsx · Console.jsx · styles.css
│                               agent console, not a chat: command bar,
│                               stage strip, compact activity feed (reasoning
│                               and raw output behind toggles), verdict card.
│                               Light/dark theme button, persisted.
│
└── tests/                      same suite as netops, plus test_local_probe.py
```

## Setup

```powershell
cd netops-react
uv venv --python 3.12
uv pip install -r requirements.txt
cd frontend; npm install; npm run build; cd ..
Copy-Item ..\netops\credentials.yml .    # or fill credentials.example.yml
```

`.env` carries the same switches as netops (`LLM_MODE`, `MASK_IPS`,
`MASK_NAMES`, `SSH_MCP_URL`, …). `CHAT_HISTORY` is gone — this UI is
deliberately single-session.

## Run

```powershell
.\run.ps1 -Mock            # mock CMDB, devices, Tufin AND local probes
.\run.ps1                  # the real office MCPs + real local ping/tracert
.\run.ps1 -Dev             # + Vite dev server on :5173 with hot reload
```

Open http://localhost:8000. The frontend is served by FastAPI from
`frontend/dist`, so in normal use only one process runs.

## The WebSocket protocol

One socket per tab: `/ws`. The client sends
`{"type":"chat","text":…}`, `{"type":"deep_check"}` and
`{"type":"approval","id":…,"approved":…}`; the server streams `thought`,
`tool_result`, `approval_request`, `workflow` (full panel snapshot), `step`,
`final`, `status` and `error` frames. No polling anywhere — the flicker and
lag of the Chainlit sidebar (each update re-mounted the component) is gone
by construction.

## The local fallback

CMDB knows neither address → the model is told (prompt routing rule) to use
`local_ping` / `local_traceroute` and to **skip SSH and Tufin**:

- The tools take an **IP address argument only**, validated with `ipaddress`;
  the command line is assembled in code (`ping -n 3 -w 1000 <ip>` /
  `tracert -d -h 5 -w 1000 <ip>` on Windows). The model never writes a
  command string, so the read-only guard has nothing to reject and there is
  no injection surface.
- They are still **approval-gated** like every other probe (the `local`
  server is in `DEVICE_SERVERS`), and audited as `agent host`.
- The report and the panel carry a visible warning that the probe ran from
  the agent machine, so a clean ping does **not** claim the real source can
  reach the destination.
- Source in CMDB but destination missing → normal workflow: the source
  device can ping whatever address it is given.

## Watching the prompts

Local prompt tracing -- LangSmith without the cloud, nothing leaving the box:

```powershell
$env:TRACE="file"; .un.ps1 -Mock          # data/prompt_trace.jsonl, no extra deps
$env:TRACE="phoenix"; .un.ps1 -Mock       # the above + a UI on localhost:6006
```

Each entry is one model call: the messages that went out, the reply, any tool
calls, and how long it took. `TRACE_MASKED=1` records BOTH the real prompt and
the masked one the endpoint actually received, side by side -- which is how you
check the masking rather than trusting it.

`TRACE=phoenix` needs `uv pip install -r requirements-trace.txt`. Watch the
`openai` version afterwards: phoenix pulls a newer one than langchain-openai
supports, and the only symptom is "Connection error" on every call.

**The trace holds UNMASKED prompts** -- that is the point of it, and the reason
`data/prompt_trace.jsonl` is gitignored. Delete it when you are done.

## Safety — unchanged from netops

Read-only allowlist in code, human approval on every device command, Tufin
ungated (read-only GET), credential redaction at the MCP boundary, reversible
IP/name masking for the clipboard relay.

## Tests

```powershell
.venv\Scripts\python.exe tests\test_local_probe.py    # NEW: windows parsing + gating
.venv\Scripts\python.exe tests\test_flow.py           # whole graph (set USE_MOCKS=1)
.venv\Scripts\python.exe tests\test_ip_mask.py
.venv\Scripts\python.exe tests\test_entities.py
.venv\Scripts\python.exe tests\test_redact.py
.venv\Scripts\python.exe tests\test_relay_mask.py
.venv\Scripts\python.exe tests\test_relay_delta.py
.venv\Scripts\python.exe tests\test_reply_parsing.py
```

UI run with no Copilot and no devices:

```powershell
.venv\Scripts\python.exe tests\fake_llm.py 11499
$env:LLM_MODE="api"; $env:LLM_BASE_URL="http://127.0.0.1:11499/v1"; .\run.ps1 -Mock
```

## Copilot agent instructions

The tool list grew (`local_ping`, `local_traceroute`), so if you use
`CLIP_MODE=agent`, regenerate and re-paste the instructions:

```powershell
$env:USE_MOCKS="1"; .venv\Scripts\python.exe -m agent.llm.clipboard_llm
```
